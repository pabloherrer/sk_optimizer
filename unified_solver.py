"""
unified_solver.py — Single-Call Unified Weekly Solver
=====================================================
One OR-Tools CVRP call schedules the entire week:
  10 virtual vehicles = 2 trucks × 5 days (Tue–Sat)

Design — following the proven notebook pattern
-----------------------------------------------
  • ONE node per eligible client (no day-copies).
  • DISTANCE is the objective → geographic coherence emerges naturally.
  • TIME is a constraint (shift cap), not the objective.
  • Each client is visited by at most one vehicle (disjunction).
  • Any vehicle can serve any client — the optimizer decides geography.
  • Urgency drives disjunction penalties (critical = essentially mandatory).

Why this beats the two-phase approach
--------------------------------------
  • The optimizer sees the entire week and balances everything at once.
  • No heuristic day assignment, no k-means, no territory splits.
  • Compact routes are a CONSEQUENCE of minimizing distance, not a hack.
  • The old approach used 591 day-copy nodes with 999,999-cost penalties
    that poisoned the local search and produced scattered routes.
    This model uses ~130 nodes — clean, fast, and correct.

Vehicle mapping
---------------
  V0–V4 = Truck2/{Tue,Wed,Thu,Fri,Sat}
  V5–V9 = Truck9/{Tue,Wed,Thu,Fri,Sat}
  (Truck2 gets first 5 slots, Truck9 gets next 5.)
"""

import math
import numpy as np
import pandas as pd
from datetime import timedelta
from typing import Dict, List, Optional, Tuple, Set
from ortools.constraint_solver import routing_enums_pb2, pywrapcp

from config import (
    DAYS, NUM_DAYS, TRUCKS, TRUCK_NAMES, NUM_TRUCKS,
    SHIFT_MIN, MAX_SHIFT_MIN, OT_PENALTY_PER_MIN, OT_MULTIPLIER,
    LABOR_COST_PER_MIN, SOLVE_SEC_WEEK,
    OPPORTUNISTIC_FILL_PCT, CRITICAL_DAYS, URGENT_DAYS,
    METERS_PER_MILE, PRODUCTS, COMPARTMENT_CAPACITY_LBS,
    DEPOT_LAT, DEPOT_LON, EXCLUDED_CLIENT_IDS,
    MAX_SERVICE_INTERVAL_DAYS, EFFICIENCY_WEIGHT, ENFORCE_TIME_WINDOWS,
    USE_FORWARD_REFILLS,
)
import config as _cfg
from inventory import (
    compute_refill, fill_efficiency, days_until_stockout,
    project_level, urgency_tier,
)
from schema_loaders import is_client_open, is_client_closed_on


# ── Constants ────────────────────────────────────────────────────────────────
#
# Each physical truck-day has 3 possible load configurations (compartment plans):
#   CFG_SPLIT   : 1 compartment of product A (5,000 lbs) + 1 of product B (5,000 lbs)
#   CFG_A_ONLY  : 2 compartments of product A (10,000 lbs), 0 of B
#   CFG_B_ONLY  : 0 of A, 2 compartments of product B (10,000 lbs)
#
# We materialize all three configs as distinct virtual vehicles, and use a
# "at most one config active per truck-day" constraint to prevent double-dispatch.
#
#   PRODUCTS[0] = CANOLA  (= product A)
#   PRODUCTS[1] = FRYERS CHOICE  (= product B)

LOAD_CONFIGS = ['SPLIT', 'A_ONLY', 'B_ONLY']   # config index: 0, 1, 2
NUM_CONFIGS  = len(LOAD_CONFIGS)

# Per-config per-product capacity (lbs)
#   (config_idx, product_idx) -> capacity
CONFIG_CAP = {
    ('SPLIT',  0): COMPARTMENT_CAPACITY_LBS,          # 5,000 lbs product A
    ('SPLIT',  1): COMPARTMENT_CAPACITY_LBS,          # 5,000 lbs product B
    ('A_ONLY', 0): 2 * COMPARTMENT_CAPACITY_LBS,      # 10,000 lbs product A
    ('A_ONLY', 1): 0,                                  # no B
    ('B_ONLY', 0): 0,                                  # no A
    ('B_ONLY', 1): 2 * COMPARTMENT_CAPACITY_LBS,      # 10,000 lbs product B
}

NUM_VEHICLES = NUM_TRUCKS * NUM_DAYS * NUM_CONFIGS   # 2 × 5 × 3 = 30


# ── Geographic clustering thresholds ─────────────────────────────────────────
# These REPLACE the legacy `Zone` column, which proved unreliable (e.g.,
# Flagstaff clients labeled "Zone 4" alongside SW Phoenix clients).
# Clusters are computed from actual lat/lon distance from depot.
FAR_CLUSTER_MI         = 40     # Clients beyond this radius are "far clusters"
FAR_SPATIAL_BUCKET_DEG = 0.25   # ~15 mi buckets for grouping far clients
                                # (when a City value is missing / unreliable)


def _haversine_mi(lat1, lon1, lat2, lon2):
    """Great-circle distance in miles."""
    R = 3958.8
    lat1r, lon1r, lat2r, lon2r = map(math.radians, [lat1, lon1, lat2, lon2])
    dlat = lat2r - lat1r
    dlon = lon2r - lon1r
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1r) * math.cos(lat2r) * math.sin(dlon / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


# Max extra miles a corridor client can add vs driving straight to the far cluster.
# 10 mi ≈ 15 min — trivial when you're already making a 2-hour trip.
CORRIDOR_DETOUR_MI = 10


def _compute_geo_clusters(pool, depot_lat, depot_lon):
    """
    Build geography-based cluster keys from lat/lon.

    Returns:
        clusters : list[str]      — cluster key per client (len = len(pool))
        is_far   : list[bool]     — True if client is > FAR_CLUSTER_MI from depot

    Metro clients (≤ FAR_CLUSTER_MI) are subdivided into 4 quadrants around the
    depot (NE/NW/SE/SW) so intra-metro cohesion is still nudged.  Far clients
    are grouped by City when available; otherwise by coarse lat/lon bucket.

    Second pass — corridor detection: metro clients that sit "on the way" to a
    far cluster are reassigned to that cluster. This prevents a truck driving
    past Dillon Western on Thursday to reach Wickenburg, then Truck 9 making a
    separate partial trip Saturday just for Dillon Western.

    The legacy `Zone` / `Zone_Code` columns are intentionally ignored here.
    """
    clusters: List[str] = []
    is_far:   List[bool] = []
    lats:     List[float] = []
    lons:     List[float] = []

    for i in range(len(pool)):
        row = pool.iloc[i]
        lat = float(row['Lat'])
        lon = float(row['Lon'])
        lats.append(lat)
        lons.append(lon)
        dist_mi = _haversine_mi(depot_lat, depot_lon, lat, lon)

        if dist_mi <= FAR_CLUSTER_MI:
            ns = 'N' if lat >= depot_lat else 'S'
            ew = 'E' if lon >= depot_lon else 'W'
            clusters.append(f'METRO_{ns}{ew}')
            is_far.append(False)
        else:
            city = str(row.get('City', '') or '').upper().strip()
            if city:
                clusters.append(f'FAR_{city}')
            else:
                lat_b = round(lat / FAR_SPATIAL_BUCKET_DEG) * FAR_SPATIAL_BUCKET_DEG
                lon_b = round(lon / FAR_SPATIAL_BUCKET_DEG) * FAR_SPATIAL_BUCKET_DEG
                clusters.append(f'FAR_{lat_b:.2f}_{lon_b:.2f}')
            is_far.append(True)

    # ── Second pass: corridor detection ──────────────────────────────────
    # Compute centroid of each far cluster, then check if any metro client
    # is "on the way" (depot→client + client→centroid < depot→centroid + detour).
    far_centroids: Dict[str, Tuple[float, float]] = {}
    far_counts:    Dict[str, int] = {}
    for i, (c, f) in enumerate(zip(clusters, is_far)):
        if f:
            if c not in far_centroids:
                far_centroids[c] = (0.0, 0.0)
                far_counts[c] = 0
            olat, olon = far_centroids[c]
            far_centroids[c] = (olat + lats[i], olon + lons[i])
            far_counts[c] = far_counts.get(c, 0) + 1
    for c in far_centroids:
        olat, olon = far_centroids[c]
        far_centroids[c] = (olat / far_counts[c], olon / far_counts[c])

    n_corridor = 0
    for i in range(len(clusters)):
        if is_far[i]:
            continue  # already a far client
        clat, clon = lats[i], lons[i]
        depot_to_client = _haversine_mi(depot_lat, depot_lon, clat, clon)
        # Only consider clients that are at least 20 mi from depot (true outliers)
        if depot_to_client < 20:
            continue
        # Check each far cluster
        best_cluster = None
        best_extra   = CORRIDOR_DETOUR_MI + 1  # must beat threshold
        for fc, (fc_lat, fc_lon) in far_centroids.items():
            depot_to_far    = _haversine_mi(depot_lat, depot_lon, fc_lat, fc_lon)
            client_to_far   = _haversine_mi(clat, clon, fc_lat, fc_lon)
            via_client       = depot_to_client + client_to_far
            extra            = via_client - depot_to_far
            if extra < best_extra:
                best_extra   = extra
                best_cluster = fc
        if best_cluster is not None and best_extra <= CORRIDOR_DETOUR_MI:
            clusters[i] = best_cluster
            is_far[i]   = True
            n_corridor  += 1

    if n_corridor:
        print(f"  Corridor detection: reassigned {n_corridor} metro client(s) "
              f"to far clusters (on-the-way)")

    return clusters, is_far


def _compute_geo_clusters_single(row, depot_lat, depot_lon) -> Tuple[str, bool]:
    """Single-row version of _compute_geo_clusters for pool-builder far-sweep.
    Note: this does NOT include corridor detection (that requires the full pool).
    Far-sweep only needs to know if a client is in a far cluster, which this handles."""
    lat = float(row['Lat'])
    lon = float(row['Lon'])
    dist_mi = _haversine_mi(depot_lat, depot_lon, lat, lon)
    if dist_mi <= FAR_CLUSTER_MI:
        ns = 'N' if lat >= depot_lat else 'S'
        ew = 'E' if lon >= depot_lon else 'W'
        return f'METRO_{ns}{ew}', False
    city = str(row.get('City', '') or '').upper().strip()
    if city:
        return f'FAR_{city}', True
    lat_b = round(lat / FAR_SPATIAL_BUCKET_DEG) * FAR_SPATIAL_BUCKET_DEG
    lon_b = round(lon / FAR_SPATIAL_BUCKET_DEG) * FAR_SPATIAL_BUCKET_DEG
    return f'FAR_{lat_b:.2f}_{lon_b:.2f}', True


# ── Vehicle mapping ──────────────────────────────────────────────────────────
# V ordering: grouped by truck, then day, then config.
#   Truck2 / Tue / {SPLIT, A_ONLY, B_ONLY}  = v 0,1,2
#   Truck2 / Wed / {SPLIT, A_ONLY, B_ONLY}  = v 3,4,5
#   ...

def vehicle_to_truck_day_config(v: int) -> Tuple[str, int, str]:
    """Virtual vehicle index → (truck_name, day_index, config_name)."""
    truck_idx = v // (NUM_DAYS * NUM_CONFIGS)
    rem       = v %  (NUM_DAYS * NUM_CONFIGS)
    day       = rem // NUM_CONFIGS
    cfg_idx   = rem %  NUM_CONFIGS
    return TRUCK_NAMES[truck_idx], day, LOAD_CONFIGS[cfg_idx]


def vehicle_to_truck_day(v: int) -> Tuple[str, int]:
    """Backward-compat: (truck, day) without config."""
    t, d, _ = vehicle_to_truck_day_config(v)
    return t, d


def truck_day_config_to_vehicle(truck_name: str, day: int, cfg: str) -> int:
    return (
        TRUCK_NAMES.index(truck_name) * NUM_DAYS * NUM_CONFIGS
        + day * NUM_CONFIGS
        + LOAD_CONFIGS.index(cfg)
    )


def truck_day_to_vehicles(truck_name: str, day: int) -> List[int]:
    """All 3 config-vehicles for a given (truck, day)."""
    base = TRUCK_NAMES.index(truck_name) * NUM_DAYS * NUM_CONFIGS + day * NUM_CONFIGS
    return [base + c for c in range(NUM_CONFIGS)]


# ── Public API ───────────────────────────────────────────────────────────────

def solve_week(
    clients_df:        pd.DataFrame,
    dist_matrix:       np.ndarray,
    time_matrix_min:   np.ndarray,
    node_index_map:    dict,
    start_day:         int = 0,
    solve_seconds:     int = None,
    time_windows_df:   pd.DataFrame = None,
    closures_df:       pd.DataFrame = None,
    today:             pd.Timestamp = None,
    depot_config:      dict = None,
    plan_dates:        Optional[List[pd.Timestamp]] = None,
) -> Tuple[Dict[int, pd.DataFrame], pd.DataFrame]:
    """
    Solve the full week in one OR-Tools call.

    Parameters
    ----------
    clients_df        : Enriched client table (with Current_lbs, Avg_LbsPerDay, etc.)
    dist_matrix       : (N×N) integer metres from OSRM
    time_matrix_min   : (N×N) integer minutes from OSRM
    node_index_map    : {client_id: matrix_row_index}
    start_day         : 0=Tue, 1=Wed, etc.  Days before this are skipped.
    solve_seconds     : Solver time limit.  Defaults to config.SOLVE_SEC_WEEK.
    time_windows_df   : DataFrame with Client_ID, Day_of_Week, Open_Min, Close_Min
    closures_df       : DataFrame with Client_ID, Start_Date, End_Date, Reason
    today             : Reference date for closure checking (default: today)
    depot_config      : Dict with shift_start_min, shift_end_min (optional)
    plan_dates        : List of pd.Timestamp, one per planning slot (length = NUM_DAYS).
                        If provided, the output rows get a `Date` column and the `Day`
                        column uses the actual weekday short-name of that date.
                        If None, falls back to legacy Tue-Sat weekday labeling.

    Returns
    -------
    routes   : {day_index: DataFrame} with one row per stop
    deferred : DataFrame of clients not scheduled this week with Reason and Reason_Detail
    """
    if solve_seconds is None:
        solve_seconds = SOLVE_SEC_WEEK

    # ── Set defaults ─────────────────────────────────────────────────────
    if time_windows_df is None:
        time_windows_df = pd.DataFrame(columns=['Client_ID', 'Day_of_Week', 'Open_Min', 'Close_Min'])
    if closures_df is None:
        closures_df = pd.DataFrame(columns=['Client_ID', 'Start_Date', 'End_Date', 'Reason'])
    if today is None:
        today = pd.Timestamp.today()
    else:
        today = pd.Timestamp(today)
    if depot_config is None:
        depot_config = {'shift_start_min': 6 * 60, 'shift_end_min': 16 * 60}

    # ── Step 0: Pre-filter closed clients and mark deferral reasons ──────
    deferred_reasons = {}  # {client_id: (reason_code, reason_detail)}

    # Exclude Tucson/Flagstaff — separate bi-weekly Saturday run
    if EXCLUDED_CLIENT_IDS:
        n_excl = 0
        for idx, row in clients_df.iterrows():
            cid = row['ID']
            if cid in EXCLUDED_CLIENT_IDS:
                deferred_reasons[cid] = ('SEPARATE_RUN',
                    'Tucson/Flagstaff — scheduled on separate bi-weekly Saturday run')
                n_excl += 1
        if n_excl:
            print(f"  Excluded {n_excl} Tucson/Flagstaff client(s) (separate run)")

    # Check for missing GPS, tank, consumption before building pool
    for idx, row in clients_df.iterrows():
        cid = row['ID']
        if cid in deferred_reasons:
            continue

        # NO_GPS
        if pd.isna(row.get('Lat')) or pd.isna(row.get('Lon')):
            deferred_reasons[cid] = ('NO_GPS', 'Missing latitude/longitude')
            continue

        # NO_TANK_SIZE
        if pd.isna(row.get('Tank_lbs')) or row.get('Tank_lbs', 0) <= 0:
            deferred_reasons[cid] = ('NO_TANK_SIZE', 'Missing or zero tank size')
            continue

        # NO_CONSUMPTION_DATA
        if pd.isna(row.get('Avg_LbsPerDay')) or row.get('Avg_LbsPerDay', 0) <= 0:
            deferred_reasons[cid] = ('NO_CONSUMPTION_DATA', 'No delivery history and no fallback rate')
            continue

    # Check for full-week closures
    for idx, row in clients_df.iterrows():
        cid = row['ID']
        if cid in deferred_reasons:
            continue

        # Check if closed on all work days (Tue-Sat)
        is_closed_all_days = all(
            is_client_closed_on(cid, today + timedelta(days=(DAYS.index(day_name))), closures_df)
            for day_name in DAYS
        )

        if is_closed_all_days:
            closure_rows = closures_df[closures_df['Client_ID'] == cid]
            if not closure_rows.empty:
                first_closure = closure_rows.iloc[0]
                reason_detail = f"Closed {first_closure['Start_Date'].date()} to {first_closure['End_Date'].date()}"
                if first_closure.get('Reason'):
                    reason_detail += f" ({first_closure['Reason']})"
            else:
                reason_detail = 'Closed all week (closure details not found)'
            deferred_reasons[cid] = ('CLOSED_ALL_WEEK', reason_detail)

    # ── Step 1: Build eligible client pool ───────────────────────────────
    pool, pool_meta = _build_pool(clients_df, node_index_map, deferred_reasons)

    if pool.empty:
        print("  No eligible clients.")
        empty = {d: pd.DataFrame() for d in range(start_day, NUM_DAYS)}
        return empty, clients_df.copy()

    n_clients = len(pool)
    n_nodes   = n_clients + 1   # +1 for depot at index 0
    n_vehicles = NUM_VEHICLES

    # ── Per-day projected refills & urgency ──────────────────────────────
    # Each planning day is N days into the future.  Project each client's
    # tank level forward so the solver sees realistic demand and urgency.
    # plan_dates[0] = tomorrow → days_ahead=1, plan_dates[4] → days_ahead=5-ish.
    # If plan_dates is None, assume days_ahead = day_index + 1 (tomorrow-based).
    refills_by_day: List[List[int]] = []     # [day][node] → lbs
    dte_by_day:     List[List[float]] = []   # [day][node] → days-to-empty at visit
    urgency_by_day: List[List[str]] = []     # [day][node] → urgency tier at visit

    for d in range(NUM_DAYS):
        if plan_dates and d < len(plan_dates):
            # Exact days from today to this delivery date
            days_ahead = (plan_dates[d] - (today or pd.Timestamp.today().normalize())).days
        else:
            days_ahead = d + 1  # fallback: day 0 = tomorrow

        day_refills = [0]   # depot
        day_dte     = [999.0]
        day_urg     = ['normal']
        for i in range(n_clients):
            row = pool.iloc[i]
            cur  = float(row['Current_lbs'])
            rate = float(row['Avg_LbsPerDay'])
            tank = float(row['Tank_lbs'])

            projected_level = project_level(cur, rate, days_ahead, tank)
            refill = max(int(round(tank - projected_level)), 0)
            dte    = days_until_stockout(projected_level, rate, tank)
            urg    = urgency_tier(dte)

            day_refills.append(refill)
            day_dte.append(dte)
            day_urg.append(urg)

        refills_by_day.append(day_refills)
        dte_by_day.append(day_dte)
        urgency_by_day.append(day_urg)

    # Print demand summary by day
    print(f"\n  Model: {n_clients} clients, {n_vehicles} virtual vehicles "
          f"({NUM_TRUCKS} trucks × {NUM_DAYS} days)")
    for d in range(NUM_DAYS):
        day_total = sum(refills_by_day[d])
        day_label = plan_dates[d].strftime('%a %b %d') if plan_dates and d < len(plan_dates) else f'Day {d}'
        n_crit = sum(1 for u in urgency_by_day[d][1:] if u in ('stockout', 'critical'))
        n_urg  = sum(1 for u in urgency_by_day[d][1:] if u == 'urgent')
        print(f"    {day_label}: {day_total:>8,} lbs demand  |  "
              f"{n_crit} critical/stockout, {n_urg} urgent")
    print(f"  Weekly capacity: "
          f"{sum(TRUCKS[t]['capacity_lbs'] for t in TRUCK_NAMES) * NUM_DAYS:,} lbs")

    # ── Step 2: Build sub-matrices ───────────────────────────────────────
    depot_midx = node_index_map.get('DEPOT', 0)
    midx = np.array(
        [depot_midx] + pool_meta['matrix_idx'].tolist(), dtype=int
    )

    sub_dist = dist_matrix[np.ix_(midx, midx)].astype(np.int64)
    sub_time = time_matrix_min[np.ix_(midx, midx)].astype(np.int64)

    # Per-node refill used by solver capacity/time callbacks.
    # Uses end-of-week projected refill (most depleted across the planning
    # horizon) as a conservative upper bound. This follows the inventory-routing
    # tradition (Coelho-Cordeau-Laporte 2014, "Thirty Years of Inventory
    # Routing") of sizing capacity against projected demand, not today's
    # snapshot. Rationale: if a client is visited on Day 4 of the plan, the
    # truck must physically carry the Day-4 refill amount, not today's — and a
    # snapshot-sized solver will under-provision capacity for late-week stops.
    # Using the end-of-week value keeps the feasibility space conservative
    # (any earlier day needs less) and service time realistic.
    # When USE_FORWARD_REFILLS=False (legacy/benchmark A/B), size from Day 0
    # snapshot instead — risks late-week under-provisioning.
    if getattr(_cfg, 'USE_FORWARD_REFILLS', True):
        refills = list(refills_by_day[NUM_DAYS - 1])
    else:
        refills = list(refills_by_day[0])

    # ── Step 3: OR-Tools model ───────────────────────────────────────────
    manager = pywrapcp.RoutingIndexManager(
        n_nodes, n_vehicles,
        [0] * n_vehicles,
        [0] * n_vehicles,
    )
    routing = pywrapcp.RoutingModel(manager)

    _sd = sub_dist

    # ── Objective: minimise DISTANCE ─────────────────────────────────────
    # This is the key to geographic coherence — nearby stops are cheaper.
    def _dist_cb(from_idx, to_idx):
        return int(_sd[manager.IndexToNode(from_idx),
                       manager.IndexToNode(to_idx)])

    dist_cb_idx = routing.RegisterTransitCallback(_dist_cb)
    routing.SetArcCostEvaluatorOfAllVehicles(dist_cb_idx)

    # ── Distance dimension (for reporting route totals) ──────────────────
    routing.AddDimension(dist_cb_idx, 0, 500_000_000, True, 'Distance')

    # ── Time dimension (constraint: shift cap) ───────────────────────────
    # Use vehicle-specific callbacks for truck-specific service times.
    # Support per-client Service_Min overrides.
    _st  = sub_time
    _rf  = refills

    # Build service time overrides: node_index -> service_min
    service_overrides = {}
    if 'Service_Min' in pool.columns:
        for i, cid in enumerate(pool['ID']):
            override = pool.iloc[i].get('Service_Min')
            if pd.notna(override) and override > 0:
                service_overrides[i + 1] = int(override)  # +1 because depot is index 0

    time_cb_indices = []
    for v in range(n_vehicles):
        truck_name, _ = vehicle_to_truck_day(v)
        setup = TRUCKS[truck_name]['fixed_setup_min']
        rate  = TRUCKS[truck_name]['pump_rate_lbs_per_min']

        def _make_time_cb(_mgr, _st, _rf, _setup, _rate, _overrides):
            def _cb(from_idx, to_idx):
                fn = _mgr.IndexToNode(from_idx)
                tn = _mgr.IndexToNode(to_idx)
                travel = int(_st[fn, tn])
                # Service time at from_node (depot has no service)
                if fn == 0:
                    return travel
                # Use override if available, else compute from refill
                if fn in _overrides:
                    svc = _overrides[fn]
                else:
                    svc = _setup + math.ceil(_rf[fn] / _rate)
                return travel + svc
            return _cb

        cb = _make_time_cb(manager, _st, _rf, setup, rate, service_overrides)
        cb_idx = routing.RegisterTransitCallback(cb)
        time_cb_indices.append(cb_idx)

    routing.AddDimensionWithVehicleTransits(
        time_cb_indices,
        0,               # no slack
        MAX_SHIFT_MIN,   # HARD driver-hours ceiling (12 hr)
        True,            # start cumul at zero
        'Time',
    )

    # ── Overtime model ──────────────────────────────────────────────────────
    # Minutes beyond SHIFT_MIN are legal (up to MAX_SHIFT_MIN) but cost extra.
    # SetCumulVarSoftUpperBound adds `coef × max(0, CumulVar - soft)` to the
    # objective, exactly matching "pay 1.5× for minutes over shift".
    # Read OT penalty dynamically from config so A/B benchmarks can override.
    ot_penalty_dynamic = int(
        getattr(_cfg, 'LABOR_COST_PER_MIN', LABOR_COST_PER_MIN)
        * (getattr(_cfg, 'OT_MULTIPLIER', OT_MULTIPLIER) - 1.0)
    )
    if ot_penalty_dynamic > 0:
        time_dim_for_ot = routing.GetDimensionOrDie('Time')
        for v in range(n_vehicles):
            end_idx = routing.End(v)
            time_dim_for_ot.SetCumulVarSoftUpperBound(end_idx, SHIFT_MIN, ot_penalty_dynamic)

    # ── Per-client time windows (Cornillier, Boctor, Laporte & Renaud 2009 PSRPTW) ──
    # Apply CumulVar bounds on the Time dimension for any client with hours
    # listed in time_windows_df. Minutes-since-midnight in the sheet are
    # rebased to minutes-since-shift-start, matching the Time dimension which
    # starts at cumul 0 when the truck leaves the depot.
    #
    # Clients with windows only on specific week-days also get vehicle-var
    # restrictions: vehicles on days NOT listed in the client's window set are
    # forbidden. This is how we express "Client X is morning-only on Tue/Thu,
    # don't even try scheduling them on Wed".
    _enforce_tw = bool(getattr(_cfg, 'ENFORCE_TIME_WINDOWS', ENFORCE_TIME_WINDOWS))
    if _enforce_tw and time_windows_df is not None and not time_windows_df.empty:
        time_dim = routing.GetDimensionOrDie('Time')
        shift_start = int(depot_config.get('shift_start_min', 6 * 60))

        # Build a fast lookup: client_id -> list of (day_index, open_min_rel, close_min_rel)
        # where *_rel is rebased against shift_start.
        tw_by_client: Dict[str, List[Tuple[int, int, int]]] = {}
        for _, tw_row in time_windows_df.iterrows():
            cid   = tw_row.get('Client_ID')
            dname = str(tw_row.get('Day_of_Week', '')).strip()
            try:
                open_abs  = int(tw_row.get('Open_Min'))
                close_abs = int(tw_row.get('Close_Min'))
            except (TypeError, ValueError):
                continue
            if dname not in DAYS:
                continue
            d_idx = DAYS.index(dname)
            open_rel  = max(0, open_abs  - shift_start)
            close_rel = max(0, close_abs - shift_start)
            if close_rel <= open_rel:
                continue
            tw_by_client.setdefault(cid, []).append((d_idx, open_rel, close_rel))

        n_tw_applied = 0
        n_day_restricted = 0
        pool_ids = pool['ID'].tolist()
        id_to_pool_idx = {cid: i for i, cid in enumerate(pool_ids)}

        for cid, windows in tw_by_client.items():
            if cid not in id_to_pool_idx:
                continue   # client not in this week's pool — skip
            pool_i = id_to_pool_idx[cid]
            ni     = manager.NodeToIndex(pool_i + 1)   # +1 because depot is 0

            # Union envelope: widest open, narrowest close across this client's
            # days. This is conservative: a visit will satisfy the union
            # envelope AND the per-day vehicle restriction together.
            widest_open   = min(w[1] for w in windows)
            narrowest_close = max(w[2] for w in windows)
            time_dim.CumulVar(ni).SetRange(int(widest_open), int(narrowest_close))
            n_tw_applied += 1

            # Forbid vehicles whose day-index isn't in the client's window set.
            allowed_days = {w[0] for w in windows}
            if len(allowed_days) < NUM_DAYS:
                for v in range(n_vehicles):
                    _, day_idx, _ = vehicle_to_truck_day_config(v)
                    if day_idx not in allowed_days:
                        routing.VehicleVar(ni).RemoveValue(v)
                n_day_restricted += 1

        if n_tw_applied:
            print(f"\n  Time windows: applied to {n_tw_applied} client(s) "
                  f"({n_day_restricted} with day-restrictions)")

    # ── Total-capacity dimension (truck physical cap = 10,000 lbs) ───────
    def _demand_cb(from_idx):
        return _rf[manager.IndexToNode(from_idx)]

    dcb = routing.RegisterUnaryTransitCallback(_demand_cb)
    total_caps = [
        TRUCKS[vehicle_to_truck_day(v)[0]]['capacity_lbs']
        for v in range(n_vehicles)
    ]
    routing.AddDimensionWithVehicleCapacity(dcb, 0, total_caps, True, 'Capacity')

    # ── Per-product capacity dimensions ──────────────────────────────────
    # Each truck-day has 3 config-variants (SPLIT / A_ONLY / B_ONLY).
    # For each product P, cap[v] depends on that vehicle's config.
    # Client i at product P contributes refill if its product matches, else 0.
    node_products = [PRODUCTS[0]]  # depot defaults to A, irrelevant (demand=0)
    for i in range(n_clients):
        prod = pool.iloc[i].get('Product', PRODUCTS[0])
        if prod not in PRODUCTS:
            prod = PRODUCTS[0]
        node_products.append(prod)

    for p_idx, product in enumerate(PRODUCTS):
        def _make_product_cb(_mgr, _rf, _np, _product):
            def _cb(from_idx):
                n = _mgr.IndexToNode(from_idx)
                return _rf[n] if _np[n] == _product else 0
            return _cb

        pcb = routing.RegisterUnaryTransitCallback(
            _make_product_cb(manager, _rf, node_products, product)
        )
        prod_caps = [
            CONFIG_CAP[(vehicle_to_truck_day_config(v)[2], p_idx)]
            for v in range(n_vehicles)
        ]
        routing.AddDimensionWithVehicleCapacity(
            pcb, 0, prod_caps, True, f'Cap_{product.replace(" ", "_")}'
        )

    # ── Geographic clusters (computed now, reused below) ─────────────────
    # IMPORTANT: this replaces the legacy `Zone` column. The Zone field in
    # the source spreadsheet is unreliable (e.g., Flagstaff clients labeled
    # "Zone 4" alongside SW Phoenix). We compute clusters from lat/lon.
    client_clusters, client_is_far = _compute_geo_clusters(pool, DEPOT_LAT, DEPOT_LON)
    # Align with node indices (node 0 = depot). First entry reserved for depot.
    node_clusters = ['DEPOT'] + client_clusters
    node_is_far   = [False]   + client_is_far

    n_far = sum(client_is_far)
    if n_far:
        far_cluster_counts: Dict[str, int] = {}
        for c, f in zip(client_clusters, client_is_far):
            if f:
                far_cluster_counts[c] = far_cluster_counts.get(c, 0) + 1
        print(f"\n  Geography: {n_clients - n_far} metro + {n_far} far-cluster clients")
        for c, n in sorted(far_cluster_counts.items(), key=lambda x: -x[1]):
            print(f"    {c:<30} {n} clients")

    # ── Disjunctions: each client visited at most once ───────────────────
    # Penalty for dropping a client. Higher = more "mandatory".
    # Far-cluster non-urgent clients get a LOWER drop-penalty, so the solver
    # is willing to skip them unless the trip is worth it. Combined with the
    # large FAR cluster-crossing penalty below, this produces the intended
    # behaviour: visit a whole far cluster in one trip, or skip it this week.
    #
    # Use the WORST-CASE urgency across all days: a client that's fine today
    # but hits stockout by Day 4 must still be served.
    #
    # Two paper-driven amplifications on the base penalty:
    #   1. Contractual service cadence (Aksen, Kaya, Salman & Akça 2012 —
    #      Selective & Periodic IRP): if the gap since last delivery would
    #      exceed MAX_SERVICE_INTERVAL_DAYS by the end of the planning window,
    #      escalate to stockout tier regardless of tank level. Plugs the gap
    #      the audit found: prior model had no hard upper bound on visit
    #      spacing — a client "on vacation" could silently slip past 14 days.
    #   2. Profit / fill weighting (Cornillier, Boctor, Laporte & Renaud 2009
    #      PSRPTW, Archetti et al. TOP-IRP): multiply the penalty by
    #      (1 + EFFICIENCY_WEIGHT × fill_pct_at_visit). A near-full tank has
    #      more revenue at stake than a half-empty one, so dropping it costs
    #      more. Net effect: solver prefers dense, high-fill routes without
    #      abandoning distance minimisation.
    #
    # `Last_Date` (days-since-last-delivery) comes from forecast_consumption.
    # Clients with no prior delivery history get FALLBACK_DAYS_SINCE applied
    # there, so `Days_Since_Last` / `Days_Since_Used` is always populated.
    pool_idx_to_days_since: Dict[int, float] = {}
    if 'Days_Since_Last' in pool.columns or 'Days_Since_Used' in pool.columns:
        for i in range(n_clients):
            row = pool.iloc[i]
            dsl = row.get('Days_Since_Used', row.get('Days_Since_Last'))
            try:
                pool_idx_to_days_since[i] = float(dsl) if pd.notna(dsl) else 0.0
            except (TypeError, ValueError):
                pool_idx_to_days_since[i] = 0.0

    n_contract = 0
    for i in range(n_clients):
        ni  = manager.NodeToIndex(i + 1)      # +1 because depot is 0
        far = client_is_far[i]

        # Worst-case days-to-empty across the planning window
        worst_dte = min(dte_by_day[d][i + 1] for d in range(NUM_DAYS))

        # Best (= highest) projected fill across the week — this is what the
        # truck would actually deliver at the best-case visit day for revenue.
        tank_lbs = max(float(pool.iloc[i]['Tank_lbs']), 1.0)
        best_fill_pct = max(
            (refills_by_day[d][i + 1] / tank_lbs) for d in range(NUM_DAYS)
        )
        best_fill_pct = min(max(best_fill_pct, 0.0), 1.0)

        # Contract escalation: will the client exceed MAX_SERVICE_INTERVAL_DAYS
        # by the end of the planning window? If so, treat as mandatory.
        # Read dynamically so A/B benchmarks can toggle contract enforcement.
        _max_gap = getattr(_cfg, 'MAX_SERVICE_INTERVAL_DAYS', MAX_SERVICE_INTERVAL_DAYS)
        days_since = pool_idx_to_days_since.get(i, 0.0)
        contract_overdue_by_eow = (
            days_since + NUM_DAYS >= _max_gap
        )

        if worst_dte <= 1 or contract_overdue_by_eow:
            base_penalty = 10_000_000  # Stockout or contract-overdue → mandatory
            if contract_overdue_by_eow and worst_dte > 1:
                n_contract += 1
        elif worst_dte <= 5:
            base_penalty = 2_000_000   # Urgent — strongly prefer to serve
        elif far:
            base_penalty = 150_000     # Normal far — cheap to skip solo
        else:
            base_penalty = 1_500_000   # Normal metro — don't chase single stops

        # Fill-efficiency amplifier: a higher-fill stop is worth more dropped-
        # revenue, so dropping it costs the solver more. Scales base by up to
        # (1 + EFFICIENCY_WEIGHT) at fill=100%; by 1.0 at fill=0%.
        _eff_w = float(getattr(_cfg, 'EFFICIENCY_WEIGHT', EFFICIENCY_WEIGHT))
        penalty = int(round(base_penalty * (1.0 + _eff_w * best_fill_pct)))

        routing.AddDisjunction([ni], penalty)

    if n_contract:
        print(f"  Contractual cadence: {n_contract} client(s) elevated to "
              f"mandatory (>{MAX_SERVICE_INTERVAL_DAYS}-day interval)")

    # ── Urgency → early-day hard constraint ────────────────────────────
    # Only clients already IN STOCKOUT today (DTE ≤ 0) get a hard "must
    # be Day 0" constraint. Everything else — including critical — is
    # handled purely through disjunction penalties. This keeps the model
    # feasible even with a backlogged fleet where many clients are overdue.
    # The 10M penalty on stockout + 2M on critical already ensures the
    # solver strongly prioritises them; hard constraints are a last resort.
    n_hard = 0
    for i in range(n_clients):
        today_dte = float(pool_meta.iloc[i]['days_to_empty'])
        if today_dte <= 0:
            # Already stocked out TODAY — must be served on the first day
            ni = manager.NodeToIndex(i + 1)
            for v in range(n_vehicles):
                _, day_idx, _ = vehicle_to_truck_day_config(v)
                if day_idx > 0:
                    routing.VehicleVar(ni).RemoveValue(v)
            n_hard += 1
    if n_hard:
        print(f"\n  Hard Day-0 constraint: {n_hard} clients (already stocked out)")

    # ── Closure-based day exclusion ──────────────────────────────────────
    # For each client with partial closures (not all-week), mark which days
    # they're closed and store for later constraint application.
    # Note: We skip full-week closures as those clients were already filtered out.
    # Partial-week closures are handled by forbidding arc creation via arc costs.

    # ── At-most-one-config-per-truck-day constraint ──────────────────────
    # Each physical truck can only run one load-config per day. For each
    # (truck, day) group of 3 config-vehicles, require at most 1 to be used.
    # "Used" = the start's NextVar is not the immediate End (i.e., has ≥1 stop).
    cp_solver = routing.solver()
    for truck in TRUCK_NAMES:
        for d in range(NUM_DAYS):
            v_list = truck_day_to_vehicles(truck, d)
            # used[v] == 1 iff vehicle v has any stops
            used_flags = []
            for v in v_list:
                start_idx = routing.Start(v)
                end_idx   = routing.End(v)
                nxt       = routing.NextVar(start_idx)
                # Constraint (nxt != end_idx) reified as a 0/1 IntVar
                used_flags.append((nxt != end_idx).Var())
            cp_solver.Add(cp_solver.Sum(used_flags) <= 1)

    # ── Cluster-crossing penalty (soft): charges extra for inter-cluster arcs ──
    # Uses the geography-based clusters (NOT the legacy Zone column).
    # Three tiers:
    #   • same cluster            → real distance only
    #   • metro-quadrant swap     → small nudge (~5 mi)  — prefer quadrant-cohesive
    #   • into/out of a far cluster → huge charge (~155 mi)
    #     This deters casual zigzags into Flagstaff/Tucson/Prescott.  Combined
    #     with the lowered far-client disjunction penalty above, this means:
    #     once a truck-day "enters" a far cluster, same-cluster siblings are
    #     essentially free (so it picks them all up), while one-off trips to
    #     a far cluster for a single client become economically irrational.
    METRO_CROSS_PENALTY = 8_000     # ~5 mi of extra cost
    FAR_CROSS_PENALTY   = 250_000   # ~155 mi of extra cost

    def _cost_cb(from_idx, to_idx):
        fn = manager.IndexToNode(from_idx)
        tn = manager.IndexToNode(to_idx)
        d  = int(_sd[fn, tn])
        # Depot transitions are free (start/end of route).
        if fn == 0 or tn == 0:
            return d
        fc = node_clusters[fn]
        tc = node_clusters[tn]
        if fc == tc:
            return d
        # Any arc touching a far cluster pays the big toll.
        if node_is_far[fn] or node_is_far[tn]:
            return d + FAR_CROSS_PENALTY
        # Metro-quadrant swap: mild nudge.
        return d + METRO_CROSS_PENALTY

    cost_cb_idx = routing.RegisterTransitCallback(_cost_cb)
    routing.SetArcCostEvaluatorOfAllVehicles(cost_cb_idx)

    # ── Solver parameters ────────────────────────────────────────────────
    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = (
        routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    )
    params.local_search_metaheuristic = (
        routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    )
    params.time_limit.seconds = solve_seconds
    params.log_search = False

    print(f"\n  Solving... (time limit: {solve_seconds}s)")
    solution = routing.SolveWithParameters(params)

    if solution is None:
        print(f"  ✗ No feasible solution. Status: {routing.status()}")
        empty = {d: pd.DataFrame() for d in range(start_day, NUM_DAYS)}
        return empty, clients_df.copy()

    print(f"  ✓ Solution found.  Objective: {solution.ObjectiveValue():,}")

    # ── Step 4: Extract routes ───────────────────────────────────────────
    routes = _extract_routes(
        solution, routing, manager,
        pool, pool_meta, clients_df,
        n_vehicles, sub_dist, sub_time, start_day,
        plan_dates=plan_dates,
        refills_by_day=refills_by_day,
        dte_by_day=dte_by_day,
        urgency_by_day=urgency_by_day,
    )

    # ── Deferred ─────────────────────────────────────────────────────────
    visited_ids = set()
    for d, df in routes.items():
        if not df.empty:
            visited_ids.update(df['ID'].tolist())

    deferred = clients_df[~clients_df['ID'].isin(visited_ids)].copy()

    # Add reason columns to deferred DataFrame
    deferred['Reason'] = deferred['ID'].map(
        lambda cid: deferred_reasons.get(cid, ('NO_CAPACITY', 'Solver could not fit'))[0]
        if cid in deferred_reasons
        else 'NO_CAPACITY'
    )
    deferred['Reason_Detail'] = deferred['ID'].map(
        lambda cid: deferred_reasons.get(cid, ('NO_CAPACITY', 'Solver could not fit'))[1]
        if cid in deferred_reasons
        else 'Solver could not fit in weekly routes'
    )

    _print_solution_summary(routes, deferred, start_day)
    return routes, deferred


# ── Pool builder ─────────────────────────────────────────────────────────────

def _build_pool(
    clients_df:       pd.DataFrame,
    node_index_map:   dict,
    deferred_reasons: dict = None,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    Filter to eligible clients: routable, in matrix, fill ≥ threshold.
    Excludes clients already marked as deferred by earlier checks.

    Returns
    -------
    pool      : DataFrame subset of clients_df (eligible only), reset index
    pool_meta : DataFrame with extra columns: matrix_idx, days_to_empty, urgency
    """
    if deferred_reasons is None:
        deferred_reasons = {}

    df = clients_df.copy()

    # Exclude already-deferred clients
    df = df[~df['ID'].isin(deferred_reasons)].copy()

    # Must be routable
    mask = (
        df['Lat'].notna() & df['Lon'].notna()
        & df['Tank_lbs'].notna() & (df['Tank_lbs'] > 0)
        & df['Avg_LbsPerDay'].notna() & (df['Avg_LbsPerDay'] > 0)
        & df['Current_lbs'].notna()
        & df['ID'].isin(node_index_map)
    )
    df = df[mask].copy()

    # Refill: how much we'd deliver right now (fills to 100%)
    df['Refill_lbs'] = (df['Tank_lbs'] - df['Current_lbs']).clip(lower=0).round()

    # Fill efficiency (current)
    df['Fill_Pct'] = df['Refill_lbs'] / df['Tank_lbs']

    # Eligibility: include anyone who will need service within the week.
    # Either they already need ≥50% fill today, OR they'll hit stockout
    # within 7 days (matching the notebook's VISIT_WINDOW approach), OR
    # their contractual cadence would be violated by end of week (Aksen 2012).
    # The third clause closes the pool-filter gap: without it, a client with
    # Days_Since_Last=13 and a near-full tank gets dropped before the solver
    # can honor the contract.
    contract_due = pd.Series(False, index=df.index)
    _max_gap = getattr(_cfg, 'MAX_SERVICE_INTERVAL_DAYS', MAX_SERVICE_INTERVAL_DAYS)
    if 'Days_Since_Last' in df.columns or 'Days_Since_Used' in df.columns:
        dsl = df.get('Days_Since_Used', df.get('Days_Since_Last'))
        if dsl is not None:
            contract_due = pd.to_numeric(dsl, errors='coerce').fillna(0) + NUM_DAYS \
                           >= _max_gap
    eligible = (
        (df['Fill_Pct'] >= OPPORTUNISTIC_FILL_PCT)
        | (df['Days_Until_Stockout'] <= 7)
        | contract_due
    )

    # ── Far-cluster sweep: if ANY client in a distant city qualifies,
    # pull in ALL siblings from that cluster. The truck is already making
    # the 2+ hour trip — the marginal cost of one more stop is trivial
    # compared to a separate round trip later.
    all_routable = df.copy()   # full routable set before filtering
    df = df[eligible].copy()

    # Tag every routable client with its geo-cluster
    all_routable['_cluster'], all_routable['_is_far'] = zip(
        *[_compute_geo_clusters_single(row, DEPOT_LAT, DEPOT_LON)
          for _, row in all_routable.iterrows()]
    )
    # Which far clusters have at least one qualifying client?
    qualified_ids = set(df['ID'])
    active_far_clusters = set(
        all_routable.loc[
            all_routable['_is_far'] & all_routable['ID'].isin(qualified_ids),
            '_cluster'
        ]
    )
    # Pull in unqualified siblings from those clusters
    if active_far_clusters:
        siblings = all_routable[
            all_routable['_is_far']
            & all_routable['_cluster'].isin(active_far_clusters)
            & ~all_routable['ID'].isin(qualified_ids)
        ]
        if not siblings.empty:
            n_pulled = len(siblings)
            clusters_hit = siblings['_cluster'].unique()
            print(f"  Far-cluster sweep: pulled {n_pulled} extra client(s) from "
                  f"{', '.join(clusters_hit)}")
            sibs_clean = siblings.drop(columns=['_cluster', '_is_far'])
            df = pd.concat([df, sibs_clean], ignore_index=True)

    # Clean up any temp columns that may have leaked
    for _col in ('_cluster', '_is_far'):
        if _col in df.columns:
            df = df.drop(columns=[_col])

    df = df.reset_index(drop=True)

    # For clients with low current fill, use projected end-of-week refill
    for i in range(len(df)):
        if df.iloc[i]['Fill_Pct'] < OPPORTUNISTIC_FILL_PCT:
            row = df.iloc[i]
            eow_level = max(
                row['Current_lbs'] - NUM_DAYS * row['Avg_LbsPerDay'],
                row['Tank_lbs'] * 0.03,
            )
            eow_refill = max(row['Tank_lbs'] - eow_level, 0)
            df.at[df.index[i], 'Refill_lbs'] = round(eow_refill)
            df.at[df.index[i], 'Fill_Pct'] = eow_refill / row['Tank_lbs']

    if df.empty:
        return pd.DataFrame(), pd.DataFrame()

    # Metadata for the solver
    meta = pd.DataFrame({
        'matrix_idx':   df['ID'].map(node_index_map),
        'days_to_empty': df['Days_Until_Stockout'],
        'urgency':      df['Urgency'],
    })

    # Print summary
    uc = meta['urgency'].value_counts()
    print(f"\n  Eligible client pool: {len(df)}")
    for tier in ['stockout', 'critical', 'urgent', 'normal']:
        n = uc.get(tier, 0)
        if n:
            marker = '  ← MUST SERVE' if tier in ('stockout', 'critical') else ''
            print(f"    {tier:<10}: {n}{marker}")

    return df, meta


# ── Route extraction ─────────────────────────────────────────────────────────

def _extract_routes(
    solution, routing, manager,
    pool:       pd.DataFrame,
    pool_meta:  pd.DataFrame,
    clients_df: pd.DataFrame,
    n_vehicles: int,
    sub_dist:   np.ndarray,
    sub_time:   np.ndarray,
    start_day:  int,
    plan_dates:      Optional[List[pd.Timestamp]] = None,
    refills_by_day:  Optional[List[List[int]]]    = None,
    dte_by_day:      Optional[List[List[float]]]  = None,
    urgency_by_day:  Optional[List[List[str]]]    = None,
) -> Dict[int, pd.DataFrame]:
    """Convert OR-Tools solution → per-day route DataFrames.
    Uses day-projected refills, urgency, and days-to-empty when available."""

    day_records: Dict[int, list] = {d: [] for d in range(NUM_DAYS)}

    for v in range(n_vehicles):
        truck_name, day, cfg = vehicle_to_truck_day_config(v)

        setup = TRUCKS[truck_name]['fixed_setup_min']
        rate  = TRUCKS[truck_name]['pump_rate_lbs_per_min']

        idx       = routing.Start(v)
        stop_num  = 0
        cum_dist  = 0
        cum_time  = 0
        prev_node = 0

        route_stops = []

        while not routing.IsEnd(idx):
            model_node = manager.IndexToNode(idx)

            if model_node != 0:
                stop_num  += 1
                pool_row   = pool.iloc[model_node - 1]
                meta_row   = pool_meta.iloc[model_node - 1]

                travel_m   = int(sub_dist[prev_node, model_node])
                travel_min = int(sub_time[prev_node, model_node])

                # Day-projected refill / urgency / DTE at actual visit day.
                # Display matches what the driver will physically pump: tank
                # level has depleted further by the time the route runs, so the
                # Day-d projection is the accurate number (not today's snapshot).
                if refills_by_day and day < len(refills_by_day):
                    refill = int(refills_by_day[day][model_node])
                    dte    = dte_by_day[day][model_node]
                    urg    = urgency_by_day[day][model_node]
                else:
                    refill = int(pool_row['Refill_lbs'])
                    dte    = float(meta_row['days_to_empty'])
                    urg    = meta_row['urgency']

                svc_min = setup + math.ceil(refill / rate)

                cum_dist  += travel_m
                cum_time  += travel_min + svc_min

                tank_lbs = float(pool_row['Tank_lbs'])
                fill_pct = pool_row['Fill_Pct'] if 'Fill_Pct' in pool_row.index else (
                    refill / tank_lbs if tank_lbs > 0 else 0
                )

                # Resolve day label + date from plan_dates if available
                if plan_dates and day < len(plan_dates):
                    _day_label = plan_dates[day].strftime('%a')
                    _day_date  = plan_dates[day].strftime('%Y-%m-%d')
                else:
                    _day_label = DAYS[day]
                    _day_date  = ''

                route_stops.append({
                    'Truck':               truck_name,
                    'Day':                 _day_label,
                    'Date':                _day_date,
                    'DayIndex':            day,
                    'Stop':                stop_num,
                    'ID':                  pool_row['ID'],
                    'Customer':            pool_row.get('Customer', ''),
                    'Zone':                pool_row.get('Zone', ''),
                    'Address':             pool_row.get('Address', ''),
                    'Lat':                 pool_row['Lat'],
                    'Lon':                 pool_row['Lon'],
                    'Product':             pool_row.get('Product', ''),
                    'Tank_lbs':            int(tank_lbs),
                    'Current_lbs':         round(float(pool_row['Current_lbs'])),
                    'Refill_lbs':          refill,
                    'Fill_Pct':            round(fill_pct * 100, 1),
                    'Avg_LbsPerDay':       round(float(pool_row['Avg_LbsPerDay']), 1),
                    'Days_Until_Stockout': round(float(pool_row.get('Days_Until_Stockout', 0)), 1),
                    'DaysToStockoutAtVisit': round(dte, 1),
                    'Urgency':             urg,
                    'VisitScore':          0,
                    'Service_Min':         svc_min,
                    'Travel_To_Min':       travel_min,
                    'Dist_To_m':           travel_m,
                    'Dist_To_mi':          round(travel_m / METERS_PER_MILE, 2),
                    'Cum_Dist_mi':         round(cum_dist / METERS_PER_MILE, 2),
                })
                prev_node = model_node

            idx = solution.Value(routing.NextVar(idx))

        if not route_stops:
            continue

        # Return leg to depot
        ret_dist  = int(sub_dist[prev_node, 0])
        ret_min   = int(sub_time[prev_node, 0])
        cum_dist += ret_dist
        cum_time += ret_min

        route_load = sum(r['Refill_lbs'] for r in route_stops)
        cap        = TRUCKS[truck_name]['capacity_lbs']

        # ── Compartment assignment ────────────────────────────────────────
        # Sum lbs by product for this truck-day, then assign to compartments.
        # Each compartment holds one product (max COMPARTMENT_CAPACITY_LBS).
        # Any compartment can hold any product — they are interchangeable.
        product_lbs = {p: 0 for p in PRODUCTS}
        for r in route_stops:
            prod = r.get('Product', PRODUCTS[0])
            if prod not in product_lbs:
                prod = PRODUCTS[0]
            product_lbs[prod] += r['Refill_lbs']

        compartments = _assign_compartments_by_config(product_lbs, cfg)

        # Overtime accounting: minutes beyond SHIFT_MIN are OT, billed 1.5x.
        # Read dynamic values so A/B benchmarks that mutate cfg get accurate numbers.
        _base_labor = getattr(_cfg, 'LABOR_COST_PER_MIN', LABOR_COST_PER_MIN)
        _ot_mult    = getattr(_cfg, 'OT_MULTIPLIER', OT_MULTIPLIER)
        reg_min = min(cum_time, SHIFT_MIN)
        ot_min  = max(cum_time - SHIFT_MIN, 0)
        labor_cost = reg_min * _base_labor + ot_min * _base_labor * _ot_mult

        for rec in route_stops:
            rec['Route_Dist_mi']    = round(cum_dist / METERS_PER_MILE, 1)
            rec['Route_Time_min']   = cum_time
            rec['Reg_Min']          = reg_min
            rec['OT_Min']           = ot_min
            rec['Labor_Cost']       = labor_cost
            rec['Shift_Pct']        = round(cum_time / SHIFT_MIN * 100, 1)
            rec['Return_Depot_Min'] = ret_min
            rec['Route_Load_lbs']   = route_load
            rec['Cap_Pct']          = round(route_load / cap * 100, 1)
            rec['Load_Config']      = cfg
            rec['Comp_A_Product']   = compartments[0]['product']
            rec['Comp_A_lbs']       = compartments[0]['lbs']
            rec['Comp_B_Product']   = compartments[1]['product']
            rec['Comp_B_lbs']       = compartments[1]['lbs']

        day_records[day].extend(route_stops)

    result: Dict[int, pd.DataFrame] = {}
    for d in range(NUM_DAYS):
        result[d] = pd.DataFrame(day_records[d]) if day_records[d] else pd.DataFrame()

    return result


# ── Zone-aware slot pre-assignment ───────────────────────────────────────────

def _pre_assign_slots(pool, pool_meta, n_vehicles, caps_by_vehicle):
    """
    Bin-pack clients into (truck, day) slots to minimize zone-day fragmentation.

    Strategy:
      1. Sort clients: urgent first, then group by zone, largest first.
      2. For each client, pick the slot with the best score:
         - STRONG bonus if the slot already contains this zone (consolidation)
         - Penalty if slot's day-index is later than urgency allows
         - Tie-break: prefer fuller slots (tight packing)

    Slot index mapping:
      Slot 0..NUM_DAYS-1    = Truck2 on Tue..Sat
      Slot NUM_DAYS..2N-1   = Truck9 on Tue..Sat

    Returns: {client_id: slot_index}
    """
    # Align pool_meta with pool by position
    pool = pool.reset_index(drop=True)
    pool_meta = pool_meta.reset_index(drop=True)

    urgency_by_id = dict(zip(pool['ID'], pool_meta['urgency']))
    dte_by_id     = dict(zip(pool['ID'], pool_meta['days_to_empty']))

    # Max day-index (0=Tue, 4=Sat) allowed by urgency.
    # Stockout / critical should be earlier in the week.
    URGENCY_DAY_CAP = {'stockout': 0, 'critical': 1, 'urgent': 3, 'normal': 4}

    urg_rank = {'stockout': 0, 'critical': 1, 'urgent': 2, 'normal': 3}

    work = pool.copy()
    work['_urg']  = work['ID'].map(lambda x: urg_rank.get(urgency_by_id.get(x, 'normal'), 3))
    work['_dte']  = work['ID'].map(lambda x: dte_by_id.get(x, 99))
    # Critical / largest first; same zone stays together via secondary sort.
    work = work.sort_values(
        ['_urg', '_dte', 'Zone', 'Refill_lbs'],
        ascending=[True, True, True, False],
    ).reset_index(drop=True)

    slot_loads = [0] * n_vehicles
    slot_zones = [set() for _ in range(n_vehicles)]
    assignments = {}

    for _, row in work.iterrows():
        zone   = str(row.get('Zone', ''))
        lbs    = int(row['Refill_lbs'])
        cid    = row['ID']
        urg    = urgency_by_id.get(cid, 'normal')
        max_d  = URGENCY_DAY_CAP.get(urg, 4)

        best_slot  = -1
        best_score = -float('inf')

        for v in range(n_vehicles):
            cap = caps_by_vehicle[v]
            if slot_loads[v] + lbs > cap:
                continue

            _, day_idx, _ = vehicle_to_truck_day_config(v)
            score = 0.0

            # Strong bonus if zone already in this slot (consolidation)
            if zone in slot_zones[v]:
                score += 1000.0

            # Urgency-day constraint: HARD block for stockout/critical,
            # soft penalty for urgent/normal.
            if day_idx > max_d:
                if urg in ('stockout', 'critical'):
                    continue   # hard block — never schedule past cap
                score -= 500.0 * (day_idx - max_d)

            # Tight packing: prefer fuller slots (among valid ones)
            score += (slot_loads[v] / cap) * 10.0

            if score > best_score:
                best_score = score
                best_slot  = v

        if best_slot >= 0:
            assignments[cid] = best_slot
            slot_loads[best_slot] += lbs
            slot_zones[best_slot].add(zone)
        # else: unassigned; OR-Tools disjunction will drop it naturally

    return assignments


# ── Compartment assignment ────────────────────────────────────────────────────

def _assign_compartments_by_config(product_lbs: dict, cfg: str) -> list:
    """
    Translate a route's per-product demand into a physical compartment plan,
    given the config the solver picked.

    cfg == 'SPLIT'  →  [{A:  lbs_A(≤5k)}, {B: lbs_B(≤5k)}]
    cfg == 'A_ONLY' →  [{A: first 5k of A}, {A: remainder of A}]
    cfg == 'B_ONLY' →  [{B: first 5k of B}, {B: remainder of B}]

    The per-product solver caps guarantee the lbs fit — this function just
    formats them. Both compartment lbs always sum to the total delivered.
    """
    a_lbs = int(product_lbs.get(PRODUCTS[0], 0))
    b_lbs = int(product_lbs.get(PRODUCTS[1], 0))
    cap1  = COMPARTMENT_CAPACITY_LBS

    if cfg == 'SPLIT':
        return [
            {'product': PRODUCTS[0] if a_lbs > 0 else '—', 'lbs': a_lbs},
            {'product': PRODUCTS[1] if b_lbs > 0 else '—', 'lbs': b_lbs},
        ]
    if cfg == 'A_ONLY':
        first  = min(a_lbs, cap1)
        second = max(0, a_lbs - cap1)
        return [
            {'product': PRODUCTS[0] if first  > 0 else '—', 'lbs': first},
            {'product': PRODUCTS[0] if second > 0 else '—', 'lbs': second},
        ]
    if cfg == 'B_ONLY':
        first  = min(b_lbs, cap1)
        second = max(0, b_lbs - cap1)
        return [
            {'product': PRODUCTS[1] if first  > 0 else '—', 'lbs': first},
            {'product': PRODUCTS[1] if second > 0 else '—', 'lbs': second},
        ]
    # Fallback
    return [
        {'product': '—', 'lbs': 0},
        {'product': '—', 'lbs': 0},
    ]


# Kept for backward compat (callers outside this module, if any)
def _assign_compartments(product_lbs: dict) -> list:
    a_lbs = int(product_lbs.get(PRODUCTS[0], 0))
    b_lbs = int(product_lbs.get(PRODUCTS[1], 0))
    if a_lbs > 0 and b_lbs == 0:
        return _assign_compartments_by_config(product_lbs, 'A_ONLY')
    if b_lbs > 0 and a_lbs == 0:
        return _assign_compartments_by_config(product_lbs, 'B_ONLY')
    return _assign_compartments_by_config(product_lbs, 'SPLIT')


# ── Summary printer ──────────────────────────────────────────────────────────

def _print_solution_summary(
    routes:    Dict[int, pd.DataFrame],
    deferred:  pd.DataFrame,
    start_day: int,
):
    print(f"\n{'═' * 68}")
    print(f"  Weekly Schedule (Unified Solver)")
    print(f"{'═' * 68}")
    print(f"  {'Slot':<22} {'Stops':>5} {'Load lbs':>10} {'Cap%':>5} "
          f"{'Time':>5} {'Shift%':>6} {'Dist mi':>8}")
    print(f"  {'─' * 71}")

    total_stops = 0
    total_miles = 0.0

    for d in range(NUM_DAYS):
        df = routes.get(d, pd.DataFrame())
        if df.empty:
            continue
        for truck in TRUCK_NAMES:
            sub = df[df['Truck'] == truck]
            if sub.empty:
                continue
            load   = sub['Refill_lbs'].sum()
            cap_p  = sub['Cap_Pct'].iloc[0]
            time_v = sub['Route_Time_min'].iloc[0]
            shift  = sub['Shift_Pct'].iloc[0]
            dist   = sub['Route_Dist_mi'].iloc[0]
            n      = len(sub)
            total_stops += n
            total_miles += dist
            # Use actual date from route if available, else legacy DAYS
            day_label = sub['Day'].iloc[0] if 'Day' in sub.columns else DAYS[d]
            date_str  = sub['Date'].iloc[0] if 'Date' in sub.columns and sub['Date'].iloc[0] else ''
            slot_lbl  = f"{truck}/{day_label}" + (f" {date_str}" if date_str else '')
            print(f"  {slot_lbl:<22} {n:>5} {load:>10,} {cap_p:>5.0f}% "
                  f"{time_v:>5} {shift:>5.0f}% {dist:>8.1f}")

    print(f"  {'─' * 71}")
    print(f"  Scheduled: {total_stops} stops | {total_miles:.0f} mi total")
    print(f"  Deferred:  {len(deferred)} clients")

    if not deferred.empty:
        # Print deferral reasons summary
        if 'Reason' in deferred.columns:
            reason_counts = deferred['Reason'].value_counts()
            print(f"\n  Deferral reasons:")
            for reason_code in ['NO_GPS', 'NO_TANK_SIZE', 'NO_CONSUMPTION_DATA', 'CLOSED_ALL_WEEK',
                               'TIME_WINDOW_INFEASIBLE', 'NO_CAPACITY', 'SHIFT_OVERFLOW']:
                count = reason_counts.get(reason_code, 0)
                if count > 0:
                    print(f"    {reason_code:<25} {count}")

        # Flag critical clients
        if 'Urgency' in deferred.columns:
            uc = deferred['Urgency'].value_counts()
            crit = uc.get('critical', 0) + uc.get('stockout', 0)
            if crit:
                crit_clients = deferred[deferred['Urgency'].isin(['critical', 'stockout'])]
                print(f"\n  ⚠  {crit} critical/stockout deferred:")
                for _, row in crit_clients.iterrows():
                    reason_str = f"({row.get('Reason', 'NO_CAPACITY')})"
                    detail = row.get('Reason_Detail', '')
                    if detail:
                        reason_str += f" — {detail[:40]}"
                    print(f"    {row['ID']} {row.get('Customer', '')[:35]:<35} {reason_str}")
    print()
