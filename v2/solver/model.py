"""
v2.solver.model — clean OR-Tools formulation.

Single objective: minimize fleet cost
    = travel_cost + labor_cost + ot_cost + dispatch_cost + drop_penalty
      − terminal_reward

Hard constraints:
    • Truck total capacity (10K lbs)
    • Per-compartment per-product capacity (5K lbs)
    • Daily shift cap (12h hard)
    • Tank can't overflow (encoded in refill cap)
    • No DNS / excluded clients in pool
    • Saturday: only Truck2 dispatchable

Soft signals (in arc cost):
    • Per-truck-day dispatch fixed cost ($50)
    • Per-vehicle "wrong-day" penalty proportional to (max_refill − day_refill)
      → solver prefers visiting on the day with biggest refill (= fuller tanks at horizon end)
    • Per-vehicle territory penalty (out-of-territory visits cost more)

What's deliberately NOT here:
    • Tier ladders / magic multipliers
    • Earliness penalty (replaced by wrong-day cost above)
    • Per-arc fill bonuses with negative values (broke disjunctions in v1)

Vehicle indexing: v = truck_idx * n_days + day_idx
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Tuple

import numpy as np
from ortools.constraint_solver import pywrapcp, routing_enums_pb2

from v2.domain.problem import ProblemInstance


# ─────────────────────────────────────────────────────────────────────────────
# Cost scaling — OR-Tools needs integer arc costs.
# Multiply all dollar amounts by COST_SCALE to keep cent-level precision.
# ─────────────────────────────────────────────────────────────────────────────
COST_SCALE = 1000   # $1 → 1000 cost units (mills), keeps OT premium precise


@dataclass
class ModelArtifacts:
    """Returned by build_routing_model — everything needed to solve and extract."""
    manager: pywrapcp.RoutingIndexManager
    routing: pywrapcp.RoutingModel

    # Vehicle indexing helpers
    n_days: int
    n_trucks: int
    n_clients: int

    # Pre-computed per-day per-client refill (lbs)
    # refills_by_day[d][i+1] = lbs delivered if client_i visited on day d
    # (Index 0 = depot, refill = 0)
    refills_by_day: List[List[int]]

    # Per-vehicle dispatch cost (for cost calc)
    dispatch_cost_units: int

    # Vehicle → (truck_idx, day_idx) decoder
    v2td: callable

    # Client list (matches node indices 1..n_clients)
    clients: tuple

    # Node index map (client_id → matrix row index in the OSRM matrix)
    node_index: Dict[str, int]

    # Map: pool index (0..n_clients-1) → OSRM matrix index
    pool_to_osrm: List[int]


def build_routing_model(problem: ProblemInstance) -> ModelArtifacts:
    """
    Build the complete OR-Tools RoutingModel from a ProblemInstance.

    Returns ModelArtifacts containing the manager, routing model, and all
    metadata needed to extract a Plan from a solution.
    """
    # ── Filter to routable clients (skip DNS and excluded) ───────────────
    pool: List = []
    pool_to_osrm: List[int] = []
    for c in problem.clients:
        if c.do_not_schedule or c.excluded:
            continue
        if c.id not in problem.node_index:
            continue   # not in OSRM matrix
        if c.id not in problem.initial_tanks:
            continue   # no inventory state
        pool.append(c)
        pool_to_osrm.append(problem.node_index[c.id])

    n_clients = len(pool)
    n_days = len(problem.horizon_dates)
    n_trucks = len(problem.trucks)
    n_vehicles = n_trucks * n_days
    n_nodes = n_clients + 1   # depot + clients

    def v2td(v: int) -> Tuple[int, int]:
        """Vehicle index → (truck_idx, day_idx)."""
        return v // n_days, v % n_days

    # ── Build sub-matrices (depot + pool clients) ────────────────────────
    osrm_indices = [0] + pool_to_osrm   # depot is node 0 in OSRM
    sub_dist_m = problem.distance_matrix_m[np.ix_(osrm_indices, osrm_indices)]
    sub_time_min = problem.time_matrix_min[np.ix_(osrm_indices, osrm_indices)]

    # ── Compute per-day refills ──────────────────────────────────────────
    # refills_by_day[d][i] = lbs delivered to client at pool index (i-1) on day d
    # Node 0 = depot, always 0
    refills_by_day: List[List[int]] = []
    rate_per_client: List[float] = [0.0]    # depot has rate 0
    tank_per_client: List[int] = [0]
    product_per_client: List[str] = ['']

    for c in pool:
        ts = problem.initial_tanks[c.id]
        rate_per_client.append(float(ts.rate_lbs_per_day or 0.0))
        tank_per_client.append(int(c.tank_capacity_lbs))
        product_per_client.append(c.product)

    # Compute refill at day d using REAL projected tank level — no clamp.
    # Refill = tank − max(0, current − d × rate).
    # For a client with 1000 lbs today and 50 lbs/day consumption:
    #   Day 0 visit → refill 0      (tank already full)
    #   Day 5 visit → refill 250    (5 × 50 lbs consumed)
    #   Day 10 visit → refill 500
    #   Day 20 visit → refill 1000  (tank empty by then)
    # The solver compares these refills against the day-arc cost (mileage +
    # dispatch) and picks the day that minimizes total cost while respecting
    # the stockout penalty.
    #
    # Previously we clamped raw_level at "target_arrival_fraction × tank"
    # (30%), which forced every visited client to "arrive at 30%" regardless
    # of reality — producing the artificial "Fill % = 70 everywhere" output.
    # That clamp lied about arrival state and under-ordered refills for
    # clients whose real arrival was above 30%.
    # Enforce min_stop_lbs in the refill computation: if a day's refill
    # would be below the economic threshold (e.g., 200 lbs), set to 0 for
    # non-urgent clients. This prevents the solver from scheduling
    # uneconomic tiny top-offs. Urgent clients (DTE ≤ 3) can still be
    # visited with small refills if needed for stockout prevention.
    min_stop_lbs = int(getattr(problem, 'min_stop_lbs', 0))
    for d in range(n_days):
        day_refills = [0]  # depot
        for i, c in enumerate(pool):
            ts = problem.initial_tanks[c.id]
            rate = float(ts.rate_lbs_per_day or 0.0)
            tank = float(c.tank_capacity_lbs)
            raw_level = ts.current_lbs - d * rate
            level_at_day = max(0.0, raw_level)
            refill = max(0, int(round(tank - level_at_day)))
            # If refill is below min_stop_lbs AND client isn't urgent on this day,
            # zero it out → solver can't visit this client on this day.
            if refill < min_stop_lbs:
                # Urgency check: today's DTE (= current_lbs / rate) gives the
                # most relevant signal. If client is or will soon be at low
                # supply, allow tiny stop.
                dte_today = (ts.current_lbs / rate) if rate > 0 else 999.0
                if dte_today > 3.0:
                    refill = 0
            day_refills.append(refill)
        refills_by_day.append(day_refills)

    # Pre-compute max refill per client across the horizon (for day-badness signal)
    max_refill_per_client = [0]  # depot
    for i in range(1, n_nodes):
        max_refill_per_client.append(
            max(refills_by_day[d][i] for d in range(n_days))
        )

    # ── OR-Tools setup ───────────────────────────────────────────────────
    manager = pywrapcp.RoutingIndexManager(
        n_nodes, n_vehicles,
        [0] * n_vehicles,  # all start at depot
        [0] * n_vehicles,  # all end at depot
    )
    routing = pywrapcp.RoutingModel(manager)

    # ─── Arc cost: distance × cost_per_mile + day-badness wrong-day penalty ──
    # Day-badness encourages the solver to put each client on the day where
    # its refill is biggest (= fullest tank at horizon end). All non-negative.
    cost_per_mile_units = int(round(problem.cost_per_mile * COST_SCALE))

    # Wrong-day penalty rate (cost units per lb of "missed refill")
    # Calibrated so a 1000-lb missed refill (visiting a small-fill day instead
    # of full-fill day) costs ~ terminal_value_per_lb × 1000 in cost units.
    wrong_day_cost_per_lb = int(round(problem.terminal_value_per_lb * COST_SCALE))

    def _make_cost_cb(_mgr, _sd, _refills_day, _max_refills, _wrong_day_cost,
                       _mi_cost):
        def _cb(from_idx, to_idx):
            fn = _mgr.IndexToNode(from_idx)
            tn = _mgr.IndexToNode(to_idx)
            # Distance cost in miles × cost_per_mile (sd is meters → convert)
            dist_m = int(_sd[fn, tn])
            mile_cost = int((dist_m / 1609.34) * _mi_cost)
            # NOTE: removed the "wrong-day penalty" that previously penalized
            # arcs into clients on any day other than their max-refill day.
            # That penalty pushed every visit to the latest possible day,
            # leaving the committed window empty even when clients with
            # DTE ≤ 3 days needed service. The day-choice signal now comes
            # entirely from:
            #   (a) Travel cost (smaller mileage = preferred)
            #   (b) Stockout penalty on unserved-before-empty (disjunction)
            #   (c) Dispatch cost (one less truck-day = $X saved)
            # If geographic/temporal urgency don't pull a client forward,
            # they'll be scheduled later — but no artificial push delays
            # everyone past day 0.
            return mile_cost
        return _cb

    for v in range(n_vehicles):
        truck_idx, day_idx = v2td(v)
        cb = _make_cost_cb(
            manager, sub_dist_m, refills_by_day[day_idx],
            max_refill_per_client, wrong_day_cost_per_lb,
            cost_per_mile_units,
        )
        cb_idx = routing.RegisterTransitCallback(cb)
        routing.SetArcCostEvaluatorOfVehicle(cb_idx, v)

    # ─── Time dimension (shift cap, per-vehicle pump + setup) ────────────
    _st = sub_time_min
    truck_specs = list(problem.trucks)
    time_cb_indices = []
    for v in range(n_vehicles):
        truck_idx, day_idx = v2td(v)
        truck = truck_specs[truck_idx]
        setup = truck.fixed_setup_min
        rate_pump = truck.pump_rate_lbs_per_min
        rf = refills_by_day[day_idx]

        def _make_time_cb(_mgr, _st, _rf, _setup, _rate):
            def _cb(from_idx, to_idx):
                fn = _mgr.IndexToNode(from_idx)
                tn = _mgr.IndexToNode(to_idx)
                travel = int(_st[fn, tn])
                if fn == 0:
                    return travel
                # Service time at from_node = setup + pump_time
                pump_min = int(_rf[fn] / _rate) + (1 if _rf[fn] > 0 else 0)
                return travel + _setup + pump_min
            return _cb

        cb = _make_time_cb(manager, _st, rf, setup, rate_pump)
        time_cb_indices.append(routing.RegisterTransitCallback(cb))

    routing.AddDimensionWithVehicleTransits(
        time_cb_indices,
        0,                                  # no slack
        problem.shift_hard_max_min,         # hard 12h cap
        True,                                # start at zero
        'Time',
    )

    # Overtime: soft upper bound at shift_target_min, coef = ot_premium × labor_cost
    ot_coef = int(round(
        (problem.overtime_multiplier - 1.0)
        * problem.cost_per_minute_labor * COST_SCALE
    ))
    base_labor_coef = int(round(problem.cost_per_minute_labor * COST_SCALE))
    if ot_coef > 0:
        time_dim = routing.GetDimensionOrDie('Time')
        for v in range(n_vehicles):
            end_idx = routing.End(v)
            # Soft upper bound: cost per minute over target
            time_dim.SetCumulVarSoftUpperBound(
                end_idx, problem.shift_target_min, ot_coef
            )
            # Linear cost on total minutes (regular labor)
            # Use SetSpanCostCoefficientForVehicle for total time × labor_cost
            time_dim.SetSpanCostCoefficientForVehicle(base_labor_coef, v)

    # ─── Capacity dimension (truck total cap, shared callback) ───────────
    # Audit finding: per-vehicle demand callbacks SILENTLY BROKE DISJUNCTIONS.
    # Use shared callback with conservative (high) per-client demand to keep
    # capacity meaningful. Pick the worst-case (max) refill across horizon.
    # This may be slightly conservative for early-day vehicles but is the only
    # way to keep OR-Tools' disjunction-aware insertion working correctly.
    def _demand_cb(from_idx):
        n = manager.IndexToNode(from_idx)
        return max_refill_per_client[n]

    dcb = routing.RegisterUnaryTransitCallback(_demand_cb)
    truck_caps = [truck_specs[v2td(v)[0]].capacity_lbs for v in range(n_vehicles)]
    routing.AddDimensionWithVehicleCapacity(dcb, 0, truck_caps, True, 'Capacity')

    # ─── Per-product compartment capacity ────────────────────────────────
    for product in problem.products:
        def _make_product_cb(_mgr, _mr, _np, _product):
            def _cb(from_idx):
                n = _mgr.IndexToNode(from_idx)
                return _mr[n] if _np[n] == _product else 0
            return _cb

        pcb = routing.RegisterUnaryTransitCallback(
            _make_product_cb(manager, max_refill_per_client,
                              product_per_client, product)
        )
        # Each truck's compartment for this product = 5000 lbs (1 compartment)
        prod_caps: List[int] = []
        for v in range(n_vehicles):
            truck = truck_specs[v2td(v)[0]]
            # Assume two compartments, each can hold one product
            # Conservative: assume only 1 compartment of this product → 5000 lbs
            # The solver picks SPLIT config (one compartment each product) by default
            # If a truck-day ends up needing all canola, both compartments work — but
            # the routing model doesn't decide config; we fix at SPLIT for safety.
            cap = truck.compartments[0].capacity_lbs   # 5000 typically
            prod_caps.append(cap)
        routing.AddDimensionWithVehicleCapacity(
            pcb, 0, prod_caps, True, f'Cap_{product.replace(" ", "_")}'
        )

    # ─── Saturday: only allowed trucks dispatchable ──────────────────────
    # Implement by setting effectively-infinite fixed cost on Saturday Truck9
    # vehicles. The solver will leave them empty (depot-to-depot).
    sat_disabled = set()
    saturday_truck_ids = set(getattr(problem, 'saturday_truck_ids', ['Truck2']))
    for v in range(n_vehicles):
        truck_idx, day_idx = v2td(v)
        dt = problem.horizon_dates[day_idx]
        if dt.strftime('%a') == 'Sat':
            if truck_specs[truck_idx].id not in saturday_truck_ids:
                sat_disabled.add(v)
                # Set huge fixed cost → solver leaves this vehicle empty
                routing.SetFixedCostOfVehicle(10**9, v)

    # ─── Truck-day dispatch cost (the "use this truck today?" decision) ──
    dispatch_cost_units = int(round(problem.truck_dispatch_cost * COST_SCALE))
    for v in range(n_vehicles):
        if v in sat_disabled:
            continue   # already has huge fixed cost
        routing.SetFixedCostOfVehicle(dispatch_cost_units, v)

    # ─── Disjunctions: drop penalty per client ───────────────────────────
    # Drop penalty = "cost if this client is NOT served this horizon"
    # = days the client would be dry × consumption rate × stockout_cost_per_lb_day
    # If horizon is long enough that they wouldn't be dry, drop penalty = 0
    # (they'll be picked up next horizon naturally).
    # Plus an operator-pin override (huge penalty if pinned).
    pins_by_client: Dict[str, int] = {}
    for pin in problem.overrides.pins:
        pins_by_client[pin.client_id] = 1   # mark as pinned
    forbidden_dates_by_client: Dict[str, set] = {}
    for fb in problem.overrides.forbids:
        forbidden_dates_by_client.setdefault(fb.client_id, set()).update(fb.dates)

    horizon_days_count = n_days
    stockout_cost_units = int(round(problem.stockout_cost_per_lb_day * COST_SCALE))

    for i, c in enumerate(pool):
        node = manager.NodeToIndex(i + 1)   # +1 because depot is 0
        ts = problem.initial_tanks[c.id]
        rate = float(ts.rate_lbs_per_day or 0.0)
        current = float(ts.current_lbs)
        # Days of supply at start
        if rate > 0:
            days_supply = current / rate
        else:
            days_supply = 999.0
        # Days dry if not served = max(0, horizon - days_supply)
        days_dry = max(0.0, horizon_days_count - days_supply)
        # Drop penalty in cost units
        drop_penalty = int(days_dry * rate * stockout_cost_units)
        # Add a small base so even safe clients are slightly preferred over dropping
        # (matches "we'd rather serve than not, if it's cheap")
        drop_penalty = max(drop_penalty, 100)   # 100 cost units = $0.10 floor

        # Pin override: force-include
        if c.id in pins_by_client:
            drop_penalty = 10**9   # effectively mandatory

        routing.AddDisjunction([node], drop_penalty)

    # ─── Forbid: block client from forbidden days ────────────────────────
    for client_id, forbidden_dates in forbidden_dates_by_client.items():
        # Find pool index
        try:
            i = next(idx for idx, c in enumerate(pool) if c.id == client_id)
        except StopIteration:
            continue
        node_idx = manager.NodeToIndex(i + 1)
        for v in range(n_vehicles):
            _, day_idx = v2td(v)
            if problem.horizon_dates[day_idx] in forbidden_dates:
                routing.VehicleVar(node_idx).RemoveValue(v)

    # ─── Forbid days where refill would be 0 (uneconomic) ────────────────
    # The shared capacity callback uses max_refill_per_client (conservative
    # upper bound), so the solver could otherwise visit a client on a day
    # where their REAL refill is 0 (tank already full) — wasting a stop.
    # Block these (client, day) combinations explicitly.
    for i, c in enumerate(pool):
        node_idx = manager.NodeToIndex(i + 1)
        for v in range(n_vehicles):
            _, day_idx = v2td(v)
            if refills_by_day[day_idx][i + 1] <= 0:
                # Don't drop forbidden values blindly — check it's still a valid value
                try:
                    routing.VehicleVar(node_idx).RemoveValue(v)
                except Exception:
                    pass

    return ModelArtifacts(
        manager=manager,
        routing=routing,
        n_days=n_days,
        n_trucks=n_trucks,
        n_clients=n_clients,
        refills_by_day=refills_by_day,
        dispatch_cost_units=dispatch_cost_units,
        v2td=v2td,
        clients=tuple(pool),
        node_index=problem.node_index,
        pool_to_osrm=pool_to_osrm,
    )
