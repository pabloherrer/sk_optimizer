"""
v2.solver.extract — convert OR-Tools solution → Plan dataclass.

Pure function: given the model + a solution, walk each vehicle's route and
build immutable Plan / Route / Stop objects.
"""
from __future__ import annotations
import math
from datetime import date, datetime
from typing import Dict, List, Tuple

import numpy as np
from ortools.constraint_solver import pywrapcp

from v2.domain.plan import Plan, Route, Stop
from v2.domain.problem import ProblemInstance
from v2.solver.model import ModelArtifacts, COST_SCALE


def extract_plan(
    problem: ProblemInstance,
    artifacts: ModelArtifacts,
    solution: pywrapcp.Assignment,
    solve_seconds: float,
) -> Plan:
    """
    Walk the OR-Tools solution and produce a Plan.

    Computes:
      • Per-stop: arrival/depart times, refill, tank state
      • Per-route: total minutes, total miles, costs, cap utilization
      • Plan-level: deferred clients, aggregate KPIs, capacity warnings
    """
    manager = artifacts.manager
    routing = artifacts.routing
    pool = artifacts.clients
    n_clients = artifacts.n_clients
    refills_by_day = artifacts.refills_by_day
    sub_dist_m = problem.distance_matrix_m[
        np.ix_([0] + artifacts.pool_to_osrm, [0] + artifacts.pool_to_osrm)
    ]
    sub_time_min = problem.time_matrix_min[
        np.ix_([0] + artifacts.pool_to_osrm, [0] + artifacts.pool_to_osrm)
    ]

    # Track which clients were served (for deferred report)
    served_ids = set()

    routes: Dict[Tuple[date, str], Route] = {}
    truck_specs = list(problem.trucks)
    n_days = artifacts.n_days
    n_trucks = artifacts.n_trucks

    # The Time dimension exists if the model added it (always true for
    # FINAL). Reading CumulVar(node) gives the truck's ACTUAL arrival time
    # at that node, including any waits inserted to satisfy time windows.
    # Without this, ETAs would ignore "wait until 9 AM" insertions.
    try:
        time_dim = routing.GetDimensionOrDie('Time')
    except Exception:
        time_dim = None

    # Per-day per-truck route extraction
    for v in range(n_trucks * n_days):
        truck_idx, day_idx = artifacts.v2td(v)
        truck = truck_specs[truck_idx]
        route_date = problem.horizon_dates[day_idx]

        # Walk the route
        index = routing.Start(v)
        if routing.IsEnd(solution.Value(routing.NextVar(index))):
            # Empty route — truck not dispatched
            continue

        stops: List[Stop] = []
        cum_miles = 0.0
        cum_min = 0
        seq = 0
        prev_node = 0
        load_canola = 0
        load_fryers = 0

        while not routing.IsEnd(index):
            next_idx = solution.Value(routing.NextVar(index))
            node = manager.IndexToNode(index)
            next_node = manager.IndexToNode(next_idx)

            travel_m = float(sub_dist_m[node, next_node])
            travel_min = int(sub_time_min[node, next_node])
            travel_mi = travel_m / 1609.34

            if next_node == 0:
                # Last leg back to depot
                cum_miles += travel_mi
                cum_min += travel_min
                index = next_idx
                continue

            # Visit to client at next_node
            client = pool[next_node - 1]
            refill = int(refills_by_day[day_idx][next_node])

            # Service time
            pump_min = math.ceil(refill / truck.pump_rate_lbs_per_min) if refill > 0 else 0
            setup_min = truck.fixed_setup_min
            service_min = setup_min + pump_min

            # ARRIVAL: pull from the Time dimension if available — this is
            # the truck's *actual* arrival, after any "wait until window
            # opens" slack OR-Tools inserted. Falling back to the
            # cum_min+travel computation makes pre-time-windows tests still
            # pass, but ETA for time-windowed clients will be off in that
            # mode.
            if time_dim is not None:
                arrival = int(solution.Value(time_dim.CumulVar(next_idx)))
            else:
                arrival = cum_min + travel_min
            depart = arrival + service_min

            cum_miles += travel_mi
            cum_min = depart

            ts = problem.initial_tanks[client.id]
            rate = float(ts.rate_lbs_per_day or 0.0)
            tank_cap = float(client.tank_capacity_lbs)
            safety_floor = tank_cap * float(problem.min_reserve_fraction or 0.0)
            # Calendar-days from today to scheduled arrival day. Customers
            # consume on weekends too, so we project using calendar days
            # (NOT the working-day horizon index). Without this the level
            # is over-estimated for any stop past Tue (Sun/Mon skipped).
            cal_days_to_arrival = max(0, (route_date - problem.today).days)
            raw_level = ts.current_lbs - cal_days_to_arrival * rate
            level_at_arrival = max(0.0, raw_level)
            level_after = min(tank_cap, level_at_arrival + refill)
            # DTE_at_arrival = days of usable supply ABOVE the safety floor.
            usable_lbs = max(0.0, level_at_arrival - safety_floor)
            days_until_so = (usable_lbs / rate) if rate > 0 else 999.0
            urgency = _urgency_tier(days_until_so)

            seq += 1
            stops.append(Stop(
                sequence=seq,
                client_id=client.id,
                customer=client.customer,
                address=client.address,
                lat=client.lat,
                lon=client.lon,
                product=client.product,
                tank_capacity_lbs=client.tank_capacity_lbs,
                # TODAY's actual tank level (from Anova or estimate) — what
                # the dispatcher would see if they checked right now.
                # NOT the projected-at-arrival value.
                current_lbs_today=round(float(ts.current_lbs), 1),
<<<<<<< HEAD
                # CALENDAR days from today to arrival — NOT the working-day
                # horizon index. The driver wants "deliver in 4 days" not
                # "the 4th truck-day in the plan."
                days_to_arrival=int(cal_days_to_arrival),
=======
                days_to_arrival=int(day_idx),
>>>>>>> caeda64909d41943ae38b06ce9168d27c26fc964
                level_at_arrival_lbs=round(level_at_arrival, 1),
                delivery_lbs=refill,
                level_after_lbs=round(level_after, 1),
                # Stop times are OFFSETS from shift start (NOT absolute minutes
                # since midnight). Reporting code adds shift_start_min when
                # formatting clock times. Previously we added it here too,
                # producing 6 AM stops shown as 12 PM (double-counted).
                arrival_min=arrival,
                setup_min=setup_min,
                pump_min=pump_min,
                depart_min=depart,
                travel_miles=round(travel_mi, 2),
                cumulative_miles=round(cum_miles, 2),
                days_until_stockout_at_arrival=round(days_until_so, 1),
                urgency_tier=urgency,
                pinned=any(p.client_id == client.id for p in problem.overrides.pins),
                notes=client.notes,
            ))
            served_ids.add(client.id)
            if 'CANOLA' in client.product.upper():
                load_canola += refill
            else:
                load_fryers += refill

            index = next_idx
            prev_node = next_node

        if not stops:
            continue

        # Compute route-level summary. Stop.depart_min is an OFFSET from shift
        # start (post arrival_min fix), so total_min = last_depart + return_leg.
        total_min = stops[-1].depart_min if stops else 0
        # Add return-to-depot leg from last stop
        last_node_pool_idx = next(
            (i + 1 for i, c in enumerate(pool) if c.id == stops[-1].client_id), 0
        )
        return_travel_min = int(sub_time_min[last_node_pool_idx, 0])
        return_travel_mi = float(sub_dist_m[last_node_pool_idx, 0]) / 1609.34
        total_min += return_travel_min
        cum_miles += return_travel_mi

        ot_min = max(0, total_min - problem.shift_target_min)
        reg_min = total_min - ot_min

        cost_mi = cum_miles * problem.cost_per_mile
        cost_lab = reg_min * problem.cost_per_minute_labor
        cost_ot = ot_min * problem.cost_per_minute_labor * (problem.overtime_multiplier - 1.0)
        cost_disp = problem.truck_dispatch_cost
        cost_total = cost_mi + cost_lab + cost_ot + cost_disp

        # Compartment assignment: always SPLIT for now (canola in A, fryers in B)
        routes[(route_date, truck.id)] = Route(
            date=route_date,
            truck_id=truck.id,
            territory_label='',     # Filled in later if territory pre-pass ran
            stops=tuple(stops),
            compartment_a_product='CANOLA',
            compartment_a_lbs=float(load_canola),
            compartment_b_product='FRYERS CHOICE',
            compartment_b_lbs=float(load_fryers),
            # depart/return depot are OFFSETS from shift start (consistent
            # with stop.arrival_min/depart_min). Reporting code adds the
            # absolute shift_start_min when formatting clock times.
            depart_depot_min=0,
            return_depot_min=int(total_min),
            total_minutes=int(total_min),
            overtime_minutes=int(ot_min),
            total_miles=round(cum_miles, 2),
            cost_miles_dollars=round(cost_mi, 2),
            cost_labor_dollars=round(cost_lab, 2),
            cost_overtime_dollars=round(cost_ot, 2),
            cost_dispatch_dollars=round(cost_disp, 2),
            cost_total_dollars=round(cost_total, 2),
            total_load_lbs=float(load_canola + load_fryers),
            cap_pct=round(100.0 * (load_canola + load_fryers) / truck.capacity_lbs, 1),
        )

    # ── Deferred clients ─────────────────────────────────────────────────
    deferred: Dict[str, str] = {}
    for c in problem.clients:
        if c.id in served_ids:
            continue
        if c.do_not_schedule:
            deferred[c.id] = 'DO_NOT_SCHEDULE'
        elif c.excluded:
            deferred[c.id] = 'EXCLUDED (far-cluster run)'
        elif c.id not in problem.initial_tanks:
            deferred[c.id] = 'NO_INVENTORY_STATE'
        elif c.id not in problem.node_index:
            deferred[c.id] = 'NOT_IN_MATRIX'
        else:
            ts = problem.initial_tanks[c.id]
            if not ts.rate_lbs_per_day or ts.rate_lbs_per_day <= 0:
                deferred[c.id] = 'INSUFFICIENT_CONSUMPTION_DATA'
            else:
                deferred[c.id] = 'NOT_NEEDED_THIS_HORIZON'

    # ── Aggregate KPIs ───────────────────────────────────────────────────
    all_stops = [s for r in routes.values() for s in r.stops]
    total_stops = len(all_stops)
    total_lbs = sum(s.delivery_lbs for s in all_stops)
    total_miles = sum(r.total_miles for r in routes.values())
    total_minutes = sum(r.total_minutes for r in routes.values())
    fill_pcts = [
        100.0 * s.delivery_lbs / max(s.tank_capacity_lbs, 1) for s in all_stops
    ]
    avg_fill = (sum(fill_pcts) / len(fill_pcts)) if fill_pcts else 0.0
    target_fill = 0.70 * 100   # 70%+ is "good"
    pct_under_target = (
        100.0 * sum(1 for fp in fill_pcts if fp < target_fill) / len(fill_pcts)
        if fill_pcts else 0.0
    )

    # ── Capacity warnings (proactive "Tuesday is tight") ────────────────
    warnings = []
    by_date: Dict[date, List[Route]] = {}
    for (dt, _), r in routes.items():
        by_date.setdefault(dt, []).append(r)
    for dt, rts in sorted(by_date.items()):
        total_load = sum(r.total_load_lbs for r in rts)
        total_cap = sum(truck_specs[i].capacity_lbs for i in range(n_trucks))
        # Find this date's truck-availability (Saturday rule)
        # If only Truck2 active, total_cap halves
        # Simple heuristic: if utilization > 85% and there's no overtime allowed
        max_ot = max(r.overtime_minutes for r in rts) if rts else 0
        util = 100.0 * total_load / max(total_cap, 1)
        if util > 85 or max_ot > 60:
            warnings.append(
                f"{dt.strftime('%a %b %d')}: "
                f"{util:.0f}% capacity, {max_ot} min OT — consider extra truck"
            )

    # ── Plan-level cost ──────────────────────────────────────────────────
    plan_cost = (
        solution.ObjectiveValue() / COST_SCALE
        if solution and hasattr(solution, 'ObjectiveValue') else
        sum(r.cost_total_dollars for r in routes.values())
    )

    return Plan(
        run_id=problem.run_id,
        generated_at=datetime.now(),
        today=problem.today,
        horizon_dates=problem.horizon_dates,
        commit_days=problem.commit_days,
        routes=routes,
        deferred=deferred,
        solve_seconds=round(solve_seconds, 2),
        objective_cost_dollars=round(plan_cost, 2),
        solver_status='FEASIBLE' if solution else 'INFEASIBLE',
        total_stops=total_stops,
        total_lbs_delivered=round(total_lbs, 1),
        total_miles=round(total_miles, 1),
        total_minutes=int(total_minutes),
        avg_fill_pct=round(avg_fill, 1),
        pct_stops_under_target_fill=round(pct_under_target, 1),
        capacity_warnings=tuple(warnings),
        shift_start_min=problem.shift_start_min,
        shift_target_min=problem.shift_target_min,
    )


def _urgency_tier(days_until_stockout: float) -> str:
    if days_until_stockout <= 0:
        return 'stockout'
    if days_until_stockout <= 1.5:
        return 'critical'
    if days_until_stockout <= 3.0:
        return 'urgent'
    return 'normal'
