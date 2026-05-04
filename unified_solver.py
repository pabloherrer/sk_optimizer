"""
unified_solver.py — Single-Call Unified Weekly Solver
=====================================================
One OR-Tools CVRP call schedules the entire week:
  30 virtual vehicles = 2 trucks × 5 days × 3 load configs (Tue–Sat)

Design
------
  • ONE node per eligible client (no day-copies).
  • DISTANCE + fill-economics are the objective → geographic coherence
    AND demand-driven day assignment emerge naturally.
  • TIME is a constraint (shift cap), not the objective.
  • Each client is visited by at most one vehicle (disjunction).
  • Per-vehicle cost callbacks add lateness + earliness penalties so
    the solver prefers visiting clients when their tanks are emptier.
  • Urgency drives disjunction penalties (critical = mandatory).
  • Cluster-crossing penalties keep far-cluster trips consolidated.

Vehicle mapping (3 load configs per truck-day)
----------------------------------------------
  V0–V2  = Truck2/Tue/{SPLIT, A_ONLY, B_ONLY}
  V3–V5  = Truck2/Wed/{SPLIT, A_ONLY, B_ONLY}
  ...
  V15–V17 = Truck9/Tue/{SPLIT, A_ONLY, B_ONLY}
  ...
  V27–V29 = Truck9/Sat/{SPLIT, A_ONLY, B_ONLY}
  At-most-one-config constraint ensures only 1 of 3 configs active per truck-day.
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
    EFFICIENCY_WEIGHT, ENFORCE_TIME_WINDOWS,
    USE_FORWARD_REFILLS, HORIZON_DAYS, COMMIT_DAYS, HORIZON_BUFFER,
    SATURDAY_TRUCKS,
)
import config as _cfg
from inventory import (
    compute_refill, fill_efficiency, days_until_stockout,
    project_level, project_level_dow, urgency_tier,
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

def vehicle_to_truck_day_config(v: int, n_days: int = NUM_DAYS,
                                truck_names: List[str] = None) -> Tuple[str, int, str]:
    """Virtual vehicle index → (truck_name, day_index, config_name).

    Parameters
    ----------
    v           : virtual vehicle index
    n_days      : number of planning days (default NUM_DAYS=5 for backward compat)
    truck_names : list of active truck names (default TRUCK_NAMES)
    """
    _trucks = truck_names or TRUCK_NAMES
    truck_idx = v // (n_days * NUM_CONFIGS)
    rem       = v %  (n_days * NUM_CONFIGS)
    day       = rem // NUM_CONFIGS
    cfg_idx   = rem %  NUM_CONFIGS
    return _trucks[truck_idx], day, LOAD_CONFIGS[cfg_idx]


def vehicle_to_truck_day(v: int, n_days: int = NUM_DAYS,
                         truck_names: List[str] = None) -> Tuple[str, int]:
    """Backward-compat: (truck, day) without config."""
    t, d, _ = vehicle_to_truck_day_config(v, n_days, truck_names)
    return t, d


def truck_day_config_to_vehicle(truck_name: str, day: int, cfg: str,
                                n_days: int = NUM_DAYS,
                                truck_names: List[str] = None) -> int:
    _trucks = truck_names or TRUCK_NAMES
    return (
        _trucks.index(truck_name) * n_days * NUM_CONFIGS
        + day * NUM_CONFIGS
        + LOAD_CONFIGS.index(cfg)
    )


def truck_day_to_vehicles(truck_name: str, day: int, n_days: int = NUM_DAYS,
                           truck_names: List[str] = None) -> List[int]:
    """All 3 config-vehicles for a given (truck, day)."""
    _trucks = truck_names or TRUCK_NAMES
    base = _trucks.index(truck_name) * n_days * NUM_CONFIGS + day * NUM_CONFIGS
    return [base + c for c in range(NUM_CONFIGS)]


# ── Rolling Horizon API ─────────────────────────────────────────────────────

_WEEKDAY_SHORT = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']


def compute_horizon_dates(
    today:        pd.Timestamp,
    horizon_days: int = None,
    work_days:    Set[str] = None,
) -> List[pd.Timestamp]:
    """
    Compute the next `horizon_days` working-day dates strictly after `today`.

    This is the date generator for rolling-horizon planning. Called each
    afternoon, it produces e.g. [Wed, Thu, Fri, Sat, Tue] if today is Tuesday
    and horizon_days=5.

    Parameters
    ----------
    today        : The current planning date. INCLUDED in the plan if
                   it's a workday — "today" is the first day we plan FOR.
    horizon_days : How many working days to include (default from config).
    work_days    : Set of weekday short-names (default from config.DAYS).
    """
    if horizon_days is None:
        horizon_days = int(getattr(_cfg, 'HORIZON_DAYS', 5))
    if work_days is None:
        work_days = set(DAYS)

    dates: List[pd.Timestamp] = []
    cur = pd.Timestamp(today).normalize()
    for _ in range(60):   # hard safety cap
        if len(dates) >= horizon_days:
            break
        if _WEEKDAY_SHORT[cur.weekday()] in work_days:
            dates.append(cur)
        cur += pd.Timedelta(days=1)
    return dates


def solve_horizon(
    clients_df:      pd.DataFrame,
    dist_matrix:     np.ndarray,
    time_matrix_min: np.ndarray,
    node_index_map:  dict,
    today:           pd.Timestamp,
    horizon_days:    int = None,
    commit_days:     int = None,
    solve_seconds:   int = None,
    time_windows_df: pd.DataFrame = None,
    closures_df:     pd.DataFrame = None,
    depot_config:    dict = None,
    skip_ids:        Optional[Set[str]] = None,
    must_visit_ids:  Optional[Set[str]] = None,
    active_trucks:   Optional[List[str]] = None,
    initial_routes_by_vehicle: Optional[Dict[int, List[str]]] = None,
) -> Tuple[Dict[int, pd.DataFrame], Dict[int, pd.DataFrame], pd.DataFrame]:
    """
    Rolling-horizon solver (Campbell & Savelsbergh 2004).

    Plans `horizon_days` working days starting from tomorrow, but only the
    first `commit_days` are dispatched to drivers. The remaining days are
    tentative lookahead — used for capacity planning and end-of-horizon
    penalty computation to prevent the "cliff effect".

    Designed to be called each afternoon:
      1. Load current inventory state (actual levels from today's deliveries).
      2. Call solve_horizon() → committed routes + tentative preview.
      3. Dispatch committed routes to drivers (via Smart Service / iFleet).
      4. Save state for next afternoon's re-plan.

    Parameters
    ----------
    today         : Planning date. Routes start from tomorrow.
    horizon_days  : Total planning window (working days). Default from config.
    commit_days   : How many days to commit (dispatch). Default from config.
    solve_seconds : Solver time limit. Default from config.

    Returns
    -------
    committed : {day_index: DataFrame} — routes for days 0..commit_days-1
                These go to drivers.
    tentative : {day_index: DataFrame} — routes for days commit_days..horizon-1
                These are preview/lookahead only.
    deferred  : DataFrame of clients not scheduled in the horizon.
    """
    if horizon_days is None:
        horizon_days = int(getattr(_cfg, 'HORIZON_DAYS', 5))
    if commit_days is None:
        commit_days = int(getattr(_cfg, 'COMMIT_DAYS', 1))

    # Compute working-day dates for the full horizon
    plan_dates = compute_horizon_dates(today, horizon_days)
    actual_horizon = len(plan_dates)

    if actual_horizon == 0:
        print("  No working days in horizon.")
        return {}, {}, clients_df.copy()

    print(f"\n{'═' * 80}")
    print(f"  Rolling Horizon Plan")
    print(f"  Today: {today.strftime('%a %b %d %Y')}")
    print(f"  Horizon: {actual_horizon} working days "
          f"({plan_dates[0].strftime('%a %b %d')} → {plan_dates[-1].strftime('%a %b %d')})")
    commit_n = min(commit_days, actual_horizon)
    print(f"  Commit: Day{'s' if commit_n > 1 else ''} 0"
          f"{'–' + str(commit_n - 1) if commit_n > 1 else ''} "
          f"({', '.join(d.strftime('%a %b %d') for d in plan_dates[:commit_n])})")
    print(f"  Tentative: Day{'s' if actual_horizon - commit_n > 1 else ''} "
          f"{commit_n}–{actual_horizon - 1} "
          f"({', '.join(d.strftime('%a %b %d') for d in plan_dates[commit_n:])})"
          if actual_horizon > commit_n else "  Tentative: none")
    print(f"{'═' * 80}")

    # Delegate to the unified solver with variable horizon
    routes, deferred = solve_week(
        clients_df=clients_df,
        dist_matrix=dist_matrix,
        time_matrix_min=time_matrix_min,
        node_index_map=node_index_map,
        start_day=0,
        solve_seconds=solve_seconds,
        time_windows_df=time_windows_df,
        closures_df=closures_df,
        today=today,
        depot_config=depot_config,
        plan_dates=plan_dates,
        n_plan_days=actual_horizon,
        skip_ids=skip_ids,
        must_visit_ids=must_visit_ids,
        active_trucks=active_trucks,
        initial_routes_by_vehicle=initial_routes_by_vehicle,
    )

    # Split into committed and tentative
    committed = {}
    tentative = {}
    for d, df in routes.items():
        if d < commit_n:
            committed[d] = df
        else:
            tentative[d] = df

    # Summary
    c_stops = sum(len(df) for df in committed.values() if not df.empty)
    t_stops = sum(len(df) for df in tentative.values() if not df.empty)
    print(f"\n  Committed: {c_stops} stops")
    print(f"  Tentative: {t_stops} stops (lookahead)")
    print(f"  Deferred:  {len(deferred)} clients")

    return committed, tentative, deferred


# ── Weekly solver (backward-compatible) ─────────────────────────────────────

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
    n_plan_days:       Optional[int] = None,
    skip_ids:          Optional[Set[str]] = None,
    must_visit_ids:    Optional[Set[str]] = None,
    active_trucks:     Optional[List[str]] = None,
    initial_routes_by_vehicle: Optional[Dict[int, List[str]]] = None,
) -> Tuple[Dict[int, pd.DataFrame], pd.DataFrame]:
    """
    Solve a multi-day horizon in one OR-Tools call.

    Supports both the legacy weekly mode (n_plan_days=NUM_DAYS=5) and the
    rolling-horizon mode (n_plan_days=1..7+, set dynamically each afternoon).

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
    plan_dates        : List of pd.Timestamp, one per planning slot.
                        Length determines n_plan_days if n_plan_days not given.
                        If None, falls back to legacy Tue-Sat weekday labeling.
    n_plan_days       : Number of planning days (default: len(plan_dates) or NUM_DAYS).
                        Controls number of virtual vehicles:
                          NUM_TRUCKS × n_plan_days × NUM_CONFIGS

    Returns
    -------
    routes   : {day_index: DataFrame} with one row per stop
    deferred : DataFrame of clients not scheduled this week with Reason and Reason_Detail
    """
    if solve_seconds is None:
        solve_seconds = SOLVE_SEC_WEEK

    # ── Resolve number of planning days ──────────────────────────────────
    # Rolling horizon: n_plan_days is set by the caller (1–7+).
    # Weekly mode: defaults to NUM_DAYS (5).
    if n_plan_days is None:
        if plan_dates is not None:
            n_plan_days = len(plan_dates)
        else:
            n_plan_days = NUM_DAYS
    _npd = n_plan_days   # short alias used throughout

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

        # Do_Not_Schedule (per-client manual flag from Client_List sheet)
        # Source of truth: a 'Y' in the Do_Not_Schedule column means
        # "irregular service — handle manually, do not auto-route."
        dns = row.get('Do_Not_Schedule')
        if dns is not None and not pd.isna(dns) and str(dns).strip().upper() in ('Y', 'YES', '1', 'TRUE'):
            deferred_reasons[cid] = ('DO_NOT_SCHEDULE', 'Marked Do_Not_Schedule in Client_List')
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

    # ── Operator overrides: skip clients ─────────────────────────────────
    if skip_ids:
        for idx, row in clients_df.iterrows():
            cid = row['ID']
            if str(cid) in skip_ids and cid not in deferred_reasons:
                deferred_reasons[cid] = ('OPERATOR_SKIP',
                    'Skipped by operator via pre-solve review')
        print(f"  Operator skip: {len(skip_ids)} client(s) excluded")

    # ── Step 1: Build eligible client pool ───────────────────────────────
    pool, pool_meta = _build_pool(clients_df, node_index_map, deferred_reasons,
                                   n_plan_days=_npd)

    if pool.empty:
        print("  No eligible clients.")
        empty = {d: pd.DataFrame() for d in range(start_day, _npd)}
        return empty, clients_df.copy()

    # ── Multi-visit expansion for high-consumers ───────────────────────
    # A high-consumer (cycle ≤ 0.7 × horizon, tank ≥ 700 lbs) gets a
    # SECOND virtual visit slot in the plan. Both copies of HAROLDS
    # (cycle ~6 days, horizon 10 days) need to be visited or the tank
    # runs dry before horizon end.
    #
    # Constraint: copy 1 cannot be scheduled before day = ceil(cycle * 0.7).
    # For HAROLDS cycle 6 → copy 1 forbidden on days 0-3, allowed day 4+.
    # (Previous bug: int(cycle * 0.5) truncated 2.98 → 2, allowing copy 1
    # on day 2. Fixed by using ceil + 70% of cycle so visits are
    # genuinely a half-cycle+ apart.)
    pool['_visit_copy'] = 0
    pool['_orig_id'] = pool['ID'].astype(str)
    pool_meta['_orig_id'] = pool['_orig_id']

    enable_multi_visit = bool(getattr(_cfg, 'ENABLE_MULTI_VISIT_PER_HORIZON', True))
    multi_visit_added = 0
    if enable_multi_visit and _npd >= 7:
        extras = []
        extras_meta = []
        for i, row in pool.iterrows():
            tank = float(row.get('Tank_lbs', 0) or 0)
            rate = float(row.get('Avg_LbsPerDay', 0) or 0)
            if tank <= 0 or rate <= 0:
                continue
            cycle_days = tank / rate
            if cycle_days >= _npd * 0.70:
                continue
            if tank < 700:
                continue
            extra = row.copy()
            extra['_visit_copy'] = 1
            extras.append(extra)
            extras_meta.append(pool_meta.iloc[i].copy())
            multi_visit_added += 1

        if extras:
            pool = pd.concat([pool, pd.DataFrame(extras)], ignore_index=True)
            pool_meta = pd.concat([pool_meta, pd.DataFrame(extras_meta)],
                                  ignore_index=True)
            print(f"  Multi-visit expansion: {multi_visit_added} high-consumer "
                  f"client(s) get a second virtual visit slot")
            # We'll forbid copy-1 from being scheduled before its earliest
            # useful day (cycle_days from now); that block lives later in
            # the routing model after vehicles are created. Stash the
            # min-day-per-copy here for that step.
            pool['_min_day'] = 0
            for i in pool.index[pool['_visit_copy'] >= 1]:
                tank_i = float(pool.at[i, 'Tank_lbs'])
                rate_i = float(pool.at[i, 'Avg_LbsPerDay'])
                cycle_i = tank_i / rate_i if rate_i > 0 else _npd
                # Copy 1 cannot start before 70% of a tank cycle has
                # elapsed since (presumed) copy-0 visit at day 0.
                # Use math.ceil to avoid the int() truncation that
                # previously let copy 1 land on day 2 (Tue+Thu HAROLDS).
                pool.at[i, '_min_day'] = max(math.ceil(cycle_i * 0.7), 2)

    # ── Operator overrides: determine active truck set ───────────────────
    _active_truck_names = active_trucks if active_trucks else TRUCK_NAMES
    _n_trucks = len(_active_truck_names)
    if _n_trucks < NUM_TRUCKS:
        print(f"  Fleet override: using {_n_trucks} truck(s) — {_active_truck_names}")

    # Local helpers that bind the active truck list for this solve
    def _v2td(v): return vehicle_to_truck_day(v, _npd, _active_truck_names)
    def _v2tdc(v): return vehicle_to_truck_day_config(v, _npd, _active_truck_names)
    def _td2v(truck, day): return truck_day_to_vehicles(truck, day, _npd, _active_truck_names)
    def _tdc2v(truck, day, cfg): return truck_day_config_to_vehicle(truck, day, cfg, _npd, _active_truck_names)

    n_clients = len(pool)
    n_nodes   = n_clients + 1   # +1 for depot at index 0
    n_vehicles = _n_trucks * _npd * NUM_CONFIGS

    # ── Saturday fleet restriction ──────────────────────────────────────
    # Every Saturday Truck9 is on the long out-of-metro run (Tucson one
    # week, Flagstaff the next). Metro Saturdays = Truck2 only.
    _sat_trucks = getattr(_cfg, 'SATURDAY_TRUCKS', SATURDAY_TRUCKS)
    _saturday_disabled_vehicles: Set[int] = set()
    if plan_dates:
        for d in range(_npd):
            if d < len(plan_dates) and plan_dates[d].day_name() == 'Saturday':
                for truck in _active_truck_names:
                    if truck not in _sat_trucks:
                        for v in _td2v(truck, d):
                            _saturday_disabled_vehicles.add(v)
        if _saturday_disabled_vehicles:
            sat_days = [plan_dates[d].strftime('%a %b %d')
                        for d in range(min(_npd, len(plan_dates)))
                        if plan_dates[d].day_name() == 'Saturday']
            disabled_trucks = [t for t in _active_truck_names if t not in _sat_trucks]
            print(f"  Saturday mode: {', '.join(disabled_trucks)} on far-cluster run "
                  f"({', '.join(sat_days)}) — {len(_saturday_disabled_vehicles)} vehicles zeroed")

    # ── Per-day projected refills & urgency ──────────────────────────────
    # Each planning day is N days into the future.  Project each client's
    # tank level forward so the solver sees realistic demand and urgency.
    # plan_dates[0] = tomorrow → days_ahead=1, plan_dates[4] → days_ahead=5-ish.
    # If plan_dates is None, assume days_ahead = day_index + 1 (tomorrow-based).
    refills_by_day: List[List[int]] = []     # [day][node] → lbs
    dte_by_day:     List[List[float]] = []   # [day][node] → days-to-empty at visit
    urgency_by_day: List[List[str]] = []     # [day][node] → urgency tier at visit

    for d in range(_npd):
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
            visit_copy = int(row.get('_visit_copy', 0))

            if visit_copy == 0:
                # First visit: refill from current state forward
                rate_by_dow = row.get('Rate_By_DOW') if 'Rate_By_DOW' in pool.columns else None
                if rate_by_dow is not None and isinstance(rate_by_dow, (list, tuple)) and len(rate_by_dow) == 7:
                    anchor = today or pd.Timestamp.today().normalize()
                    projected_level = project_level_dow(
                        cur, list(rate_by_dow), anchor, days_ahead, tank,
                    )
                else:
                    projected_level = project_level(cur, rate, days_ahead, tank)
                refill = max(int(round(tank - projected_level)), 0)
                dte    = days_until_stockout(projected_level, rate, tank)
            else:
                # Second visit: assume copy 0 was visited at day 0 (tank
                # filled to 100%). Refill at day d = d × rate (capped at
                # tank). This is 0 at day 0 (no-op) and grows to a full
                # tank by cycle_days. The solver only finds value for
                # this copy late in the horizon.
                cycle_days = (tank / rate) if rate > 0 else 999
                refill = int(round(min(days_ahead * rate, tank)))
                # If we're earlier than half a cycle in, this copy is wasteful
                if days_ahead < cycle_days * 0.5:
                    refill = 0
                # DTE for copy 1: assume tank was just filled at day 0,
                # so DTE = cycle_days - days_ahead from now.
                dte = max(cycle_days - days_ahead, 0)
            urg = urgency_tier(dte)

            day_refills.append(refill)
            day_dte.append(dte)
            day_urg.append(urg)

        refills_by_day.append(day_refills)
        dte_by_day.append(day_dte)
        urgency_by_day.append(day_urg)

    # Print demand summary by day
    print(f"\n  Model: {n_clients} clients, {n_vehicles} virtual vehicles "
          f"({_n_trucks} trucks × {_npd} days)")
    for d in range(_npd):
        day_total = sum(refills_by_day[d])
        day_label = plan_dates[d].strftime('%a %b %d') if plan_dates and d < len(plan_dates) else f'Day {d}'
        n_crit = sum(1 for u in urgency_by_day[d][1:] if u in ('stockout', 'critical'))
        n_urg  = sum(1 for u in urgency_by_day[d][1:] if u == 'urgent')
        print(f"    {day_label}: {day_total:>8,} lbs demand  |  "
              f"{n_crit} critical/stockout, {n_urg} urgent")
    print(f"  Horizon capacity: "
          f"{sum(TRUCKS[t]['capacity_lbs'] for t in _active_truck_names) * _npd:,} lbs")

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
        refills = list(refills_by_day[_npd - 1])
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
        truck_name, _ = _v2td(v)
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
        # cid → list of pool indices (multi-visit copies share an ID)
        id_to_pool_idxs: dict = {}
        for i, cid in enumerate(pool['ID'].tolist()):
            id_to_pool_idxs.setdefault(str(cid), []).append(i)

        for cid, windows in tw_by_client.items():
            cid_str = str(cid)
            if cid_str not in id_to_pool_idxs:
                continue
            pool_idxs = id_to_pool_idxs[cid_str]

            widest_open    = min(w[1] for w in windows)
            narrowest_close = max(w[2] for w in windows)
            allowed_days   = {w[0] for w in windows}

            for pool_i in pool_idxs:
                ni = manager.NodeToIndex(pool_i + 1)
                time_dim.CumulVar(ni).SetRange(int(widest_open), int(narrowest_close))
                n_tw_applied += 1
                if len(allowed_days) < _npd:
                    for v in range(n_vehicles):
                        _, day_idx, _ = _v2tdc(v)
                        if day_idx not in allowed_days:
                            routing.VehicleVar(ni).RemoveValue(v)
                    n_day_restricted += 1

        if n_tw_applied:
            print(f"\n  Time windows: applied to {n_tw_applied} client(s) "
                  f"({n_day_restricted} with day-restrictions)")

    # ── Per-day closure enforcement ─────────────────────────────────────
    # A client that is fully-closed on all work days is already deferred
    # earlier (CLOSED_ALL_WEEK). PARTIAL closures (e.g. closed Mon–Wed
    # only) need to forbid those specific vehicles per pool node.
    # Multi-visit clients have duplicate pool entries (same Client_ID,
    # different _visit_copy), so we group by ID and apply the closure
    # to ALL copies.
    if closures_df is not None and not closures_df.empty and plan_dates:
        n_partial_closure = 0
        n_partial_closure_days = 0
        # cid → list of pool indices (one per visit copy)
        id_to_pool_idxs: dict = {}
        for i, cid in enumerate(pool['ID'].tolist()):
            id_to_pool_idxs.setdefault(str(cid), []).append(i)

        for cid, pool_idxs in id_to_pool_idxs.items():
            closed_day_indices = [
                d for d in range(_npd)
                if d < len(plan_dates)
                and is_client_closed_on(cid, plan_dates[d], closures_df)
            ]
            if not closed_day_indices:
                continue
            if len(closed_day_indices) >= _npd:
                continue   # already deferred as CLOSED_ALL_WEEK earlier

            for pool_i in pool_idxs:
                ni = manager.NodeToIndex(pool_i + 1)
                for v in range(n_vehicles):
                    _, day_idx, _ = _v2tdc(v)
                    if day_idx in closed_day_indices:
                        routing.VehicleVar(ni).RemoveValue(v)
            n_partial_closure += 1
            n_partial_closure_days += len(closed_day_indices) * len(pool_idxs)

        if n_partial_closure:
            print(f"  Partial closures: {n_partial_closure} client(s) blocked from "
                  f"{n_partial_closure_days} closure-day(s)")

    # ── Multi-visit copy day restrictions ───────────────────────────────
    # The "second visit" copies are useful only after a half-cycle has
    # passed; visiting them too early means refilling a near-full tank
    # (wasted truck-time). Forbid copy-1 from being assigned to vehicles
    # for days < its `_min_day`.
    if '_min_day' in pool.columns:
        n_copy1_blocked = 0
        for i in pool.index[pool.get('_visit_copy', 0) >= 1]:
            min_day = int(pool.at[i, '_min_day'])
            if min_day <= 0:
                continue
            ni = manager.NodeToIndex(i + 1)   # +1 because depot is 0
            for v in range(n_vehicles):
                _, day_idx, _ = _v2tdc(v)
                if day_idx < min_day:
                    routing.VehicleVar(ni).RemoveValue(v)
            n_copy1_blocked += 1
        if n_copy1_blocked:
            print(f"  Multi-visit constraint: {n_copy1_blocked} second-visit "
                  f"copy(ies) blocked from too-early days")

    # ── Total-capacity dimension (truck physical cap = 10,000 lbs) ───────
    def _demand_cb(from_idx):
        return _rf[manager.IndexToNode(from_idx)]

    dcb = routing.RegisterUnaryTransitCallback(_demand_cb)
    total_caps = [
        TRUCKS[_v2td(v)[0]]['capacity_lbs']
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
            0 if v in _saturday_disabled_vehicles
            else CONFIG_CAP[(_v2tdc(v)[2], p_idx)]
            for v in range(n_vehicles)
        ]
        routing.AddDimensionWithVehicleCapacity(
            pcb, 0, prod_caps, True, f'Cap_{product.replace(" ", "_")}'
        )

    # ── Disable Saturday vehicles in the model ────────────────────────────
    # Zero capacity alone isn't enough — the solver could still route a
    # vehicle through zero-demand nodes. Fix disabled vehicles to empty
    # routes with a prohibitive fixed cost.
    if _saturday_disabled_vehicles:
        for v in _saturday_disabled_vehicles:
            routing.SetFixedCostOfVehicle(1_000_000_000, v)

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
    # Profit / fill weighting (Cornillier, Boctor, Laporte & Renaud 2009
    # PSRPTW, Archetti et al. TOP-IRP): multiply the penalty by
    # (1 + EFFICIENCY_WEIGHT × fill_pct_at_visit). A near-full tank has
    # more revenue at stake than a half-empty one, so dropping it costs
    # more. Net effect: solver prefers dense, high-fill routes without
    # abandoning distance minimisation.
    #
    # Note: contractual cadence enforcement (Aksen 2012) was removed —
    # S&K has no max service interval contract.
    #
    # Get CP solver reference early — needed for VehicleVar constraints below.
    cp_solver = routing.solver()
    #
    # ── HARD URGENCY CONSTRAINTS ───────────────────────────────────────
    # The prior design used only soft penalties (10M disjunction) for
    # stockout clients. This FAILED in production: the solver deferred
    # DTE≤0.1 clients to next week because route costs + earliness
    # penalties exceeded the 10M drop cost.
    #
    # Fix (Apr 2026): three-layer urgency enforcement:
    #   1. VEHICLE RESTRICTIONS: stockout/critical clients are hard-
    #      constrained to early-day vehicles via SetAllowedVehiclesForIndex.
    #      The solver CANNOT assign them to later days — it's infeasible.
    #   2. PROHIBITIVE PENALTIES: stockout disjunction = 1 billion.
    #      Dropping a stockout client costs more than any possible route.
    #   3. EARLINESS BYPASS: stockout/critical clients are flagged so the
    #      fill-economics callback does NOT penalize their Day 0 visits.
    #
    # Vehicle restriction tiers (how many days the solver may choose from):
    #   Stockout  (DTE ≤ 0):   Day 0 only          (committed day)
    #   Critical  (DTE ≤ 1.5): Day 0–1             (first two days)
    #   Urgent    (DTE ≤ 3):   Day 0–2             (first three days)
    #   Normal:                any day              (no restriction)
    #
    # Safety valve: if stockout count > daily capacity (~20 stops), we
    # expand the window by 1 day to avoid infeasibility.

    # Track urgency for earliness-bypass in fill-economics callback
    client_is_mandatory = [False] * n_clients  # stockout or critical → skip earliness penalty

    n_stockout = 0
    n_critical = 0
    n_urgent_day = 0

    HORIZON_BUFFER_DAYS = int(getattr(_cfg, 'HORIZON_BUFFER', HORIZON_BUFFER))

    for i in range(n_clients):
        ni  = manager.NodeToIndex(i + 1)      # +1 because depot is 0
        far = client_is_far[i]

        # Worst-case days-to-empty across the planning window
        worst_dte = min(dte_by_day[d][i + 1] for d in range(_npd))

        # Day-0 DTE: what matters for immediate urgency
        day0_dte = dte_by_day[0][i + 1]

        # Best (= highest) projected fill across the horizon
        tank_lbs = max(float(pool.iloc[i]['Tank_lbs']), 1.0)
        best_fill_pct = max(
            (refills_by_day[d][i + 1] / tank_lbs) for d in range(_npd)
        )
        best_fill_pct = min(max(best_fill_pct, 0.0), 1.0)

        # End-of-horizon penalty (Jaillet et al. 2002)
        cur_lbs  = float(pool.iloc[i].get('Current_lbs', 0))
        rate_lbs = float(pool.iloc[i].get('Avg_LbsPerDay', 0))
        horizon_end_days = _npd + 1
        if plan_dates and len(plan_dates) == _npd:
            horizon_end_days = (plan_dates[-1] - today).days + 1
        dte_at_horizon_end = (cur_lbs / rate_lbs - horizon_end_days) if rate_lbs > 0 else 999

        # ── Penalty tiers (rolling-horizon graduated) ───────────────────
        # Penalty decays as DTE grows so that far-future clients are
        # genuinely cheap to defer. Without this taper, including all
        # routable clients (the rolling-horizon fix) would over-pack
        # routes with nice-to-have visits.
        if day0_dte <= 0:
            # STOCKOUT: will be empty by tomorrow. Prohibitive penalty.
            base_penalty = 1_000_000_000   # 1 billion — never drop
            n_stockout += 1
            client_is_mandatory[i] = True
        elif day0_dte <= CRITICAL_DAYS:
            # CRITICAL: will stock out within 1.5 days. Very high penalty.
            base_penalty = 100_000_000     # 100M — almost never drop
            n_critical += 1
            client_is_mandatory[i] = True
        elif worst_dte <= URGENT_DAYS:
            base_penalty = 5_000_000       # 5M — strong preference
            n_urgent_day += 1
        elif dte_at_horizon_end <= HORIZON_BUFFER_DAYS and not far:
            base_penalty = 3_000_000       # Horizon-cliff escalation
        elif worst_dte <= _npd + 2:
            # Within horizon: real candidates for the plan
            base_penalty = 150_000 if far else 1_500_000
        elif worst_dte <= _npd + 7:
            # 1 cycle out: opportunistic only — truck is probably nearby
            # for some other client; pick this one up if cheap.
            base_penalty = 50_000 if far else 400_000
        elif worst_dte <= _npd + 14:
            # 2 cycles out: very cheap to defer; only if truck is RIGHT next door
            base_penalty = 15_000 if far else 100_000
        else:
            # Deep future: nearly free to drop. Solver will only pick them up
            # if a passing route incurs ~zero marginal cost (same neighborhood).
            base_penalty = 5_000 if far else 20_000

        # Operator must-visit override: force 1B penalty
        cid_str = str(pool.iloc[i]['ID'])
        if must_visit_ids and cid_str in must_visit_ids:
            base_penalty = 1_000_000_000
            client_is_mandatory[i] = True

        # Fill-efficiency amplifier (Cornillier PSRPTW)
        _eff_w = float(getattr(_cfg, 'EFFICIENCY_WEIGHT', EFFICIENCY_WEIGHT))
        penalty = int(round(base_penalty * (1.0 + _eff_w * best_fill_pct)))

        # Multi-visit copy: skipping the SECOND visit is much cheaper.
        # The first visit's penalty handles the must-serve guarantee;
        # the second is opportunistic — drop it freely if the routing
        # geometry doesn't make it cost-effective.
        visit_copy = int(pool.iloc[i].get('_visit_copy', 0))
        if visit_copy >= 1:
            penalty = max(int(penalty * 0.05), 5_000)
            client_is_mandatory[i] = False   # never force a second visit

        routing.AddDisjunction([ni], penalty)

        # ── Day preference enforcement ──────────────────────────────────
        # Instead of hard vehicle restrictions (which risk infeasibility
        # when Day 0 is capacity-constrained), we use graduated lateness
        # penalties that make deferral astronomically expensive but still
        # allow graceful spillover when Day 0 is physically full.
        #
        # Stockout: 500K/day late → pushing from Day 0→1 costs 500K (~300mi)
        # Critical: 200K/day late → pushing from Day 0→1 costs 200K (~120mi)
        # These dwarf any routing savings (max ~50K for a 30-mile detour),
        # so the solver will always prefer Day 0 unless capacity forces
        # spillover. The 1B/100M disjunction penalties ensure the client
        # is served SOMEWHERE in the window, never dropped entirely.
        # (Lateness penalty applied in the fill-economics callback below)

    # Print urgency summary
    n_normal = n_clients - n_stockout - n_critical - n_urgent_day
    print(f"\n  Urgency triage (penalty-enforced, soft spillover):")
    print(f"    Stockout (DTE≤0):     {n_stockout:>3} → Day 0 preferred (1B penalty, 500K/day late)")
    print(f"    Critical (DTE≤1.5):   {n_critical:>3} → Day 0–1 preferred (100M penalty, 500K/day late)")
    print(f"    Urgent   (DTE≤3):     {n_urgent_day:>3} → Day 0–2 preferred (5M penalty)")
    print(f"    Normal:               {n_normal:>3} → any day (1.5M penalty)")
    if n_stockout + n_critical > 0:
        mandatory_lbs = sum(
            refills_by_day[0][i + 1] for i in range(n_clients)
            if client_is_mandatory[i]
        )
        cap = sum(TRUCKS[t]['capacity_lbs'] for t in _active_truck_names)
        pct = mandatory_lbs / cap * 100
        print(f"    Mandatory Day 0 demand: {mandatory_lbs:,} lbs "
              f"({pct:.0f}% of {cap:,} lbs daily capacity)")

    # ── Closure-based day exclusion ──────────────────────────────────────
    # For each client with partial closures (not all-week), mark which days
    # they're closed and store for later constraint application.
    # Note: We skip full-week closures as those clients were already filtered out.
    # Partial-week closures are handled by forbidding arc creation via arc costs.

    # ── At-most-one-config-per-truck-day constraint ──────────────────────
    # Each physical truck can only run one load-config per day. For each
    # (truck, day) group of 3 config-vehicles, require at most 1 to be used.
    # "Used" = the start's NextVar is not the immediate End (i.e., has ≥1 stop).
    # (cp_solver already obtained above for VehicleVar constraints)
    for truck in _active_truck_names:
        for d in range(_npd):
            v_list = _td2v(truck, d)
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

    # ── Fill-economics day preference (replaces artificial stop caps) ────
    # Each client has a "preferred day" — the earliest day when their tank
    # is ≥ SOFT_MIN_FILL_PCT empty (good fill economics). Serving BEFORE
    # that day is wasteful (low fill = wasted trip); serving AFTER means
    # stockout risk grows. The penalty nudges the solver toward each
    # client's economically optimal day without artificial caps.
    #
    # This works because demand naturally shifts right through the week:
    # tanks empty → refills grow → urgency rises. Clients whose tanks
    # empty fast have early preferred-days; slow burners have later ones.
    # The solver sees the COST of visiting too early (bad fill economics)
    # AND too late (growing urgency penalty from disjunctions).
    _late_penalty = int(getattr(_cfg, 'LATE_PENALTY_PER_DAY', 15_000))
    _fill_thresh = float(getattr(_cfg, 'SOFT_MIN_FILL_PCT', 0.60))
    if _late_penalty > 0:
        # Compute preferred day for each client based on fill economics
        client_deadline = []
        for i in range(n_clients):
            node = i + 1
            tank_lbs = max(float(pool.iloc[i]['Tank_lbs']), 1.0)
            dl = _npd - 1  # default: last day (always good to serve)
            for d in range(_npd):
                refill = refills_by_day[d][node]
                fill_pct = refill / tank_lbs
                if fill_pct >= _fill_thresh:
                    dl = d
                    break
            # Override: mandatory clients always have Day 0 preferred
            # (stockout DTE≤0 OR critical DTE≤1.5)
            if client_is_mandatory[i]:
                dl = 0
            client_deadline.append(dl)

        # ── Neighbor-sweep: cross-day cluster cohesion ───────────────────
        # Problem: per-client preferred days drift apart when consumption
        # rates differ inside one micro-area, sending trucks back to the
        # same neighborhood on different days (e.g., Lake Pleasant Pkwy:
        # DILLONS BAYOU on Sat, TAILGATERS / SARDELLA'S on Wed).
        #
        # Fix: for each client, look at neighbors within radius. If a
        # neighbor has an earlier preferred day AND visiting this client
        # on that earlier day is feasible (won't stock out + fill ≥ min),
        # pull the preferred day earlier. One-directional (earlier only)
        # so we never push a client toward stockout. Mandatory clients
        # are skipped (their preferred day is already Day 0).
        _sweep_enabled    = bool(getattr(_cfg, 'NEIGHBOR_SWEEP_ENABLED', True))
        _sweep_radius_mi  = float(getattr(_cfg, 'NEIGHBOR_SWEEP_RADIUS_MI', 12.0))
        _sweep_min_fill   = float(getattr(_cfg, 'NEIGHBOR_SWEEP_MIN_FILL', 0.20))
        if _sweep_enabled and n_clients > 1:
            base_deadline = list(client_deadline)  # snapshot before sweep
            n_pulled = 0
            sweep_examples = []
            # Pre-extract lat/lon/tank/d2so to avoid repeated DataFrame lookups
            lats  = [float(pool.iloc[i]['Lat']) for i in range(n_clients)]
            lons  = [float(pool.iloc[i]['Lon']) for i in range(n_clients)]
            tanks = [max(float(pool.iloc[i]['Tank_lbs']), 1.0) for i in range(n_clients)]
            d2sos = [float(pool.iloc[i].get('Days_Until_Stockout') or 999)
                     for i in range(n_clients)]
            for i in range(n_clients):
                if client_is_mandatory[i]:
                    continue  # already pinned to Day 0
                target_day = client_deadline[i]
                target_via = None
                for j in range(n_clients):
                    if i == j:
                        continue
                    if base_deadline[j] >= target_day:
                        continue  # j is not earlier — nothing to pull toward
                    # Distance gate
                    dist = _haversine_mi(lats[i], lons[i], lats[j], lons[j])
                    if dist > _sweep_radius_mi:
                        continue
                    j_day = base_deadline[j]
                    # Feasibility: i must not stock out before j's day.
                    # d2sos[i] is days-from-today; j_day is also days-from-today
                    # (Day 0 = today's commit day in the horizon).
                    if d2sos[i] < j_day:
                        continue
                    # Fill economics on j's day — don't waste a stop on a
                    # nearly-full tank.
                    if j_day < len(refills_by_day) and (i + 1) < len(refills_by_day[j_day]):
                        refill_on_j = refills_by_day[j_day][i + 1]
                        fill_on_j = refill_on_j / tanks[i] if tanks[i] > 0 else 0.0
                        if fill_on_j < _sweep_min_fill:
                            continue
                    target_day = j_day
                    target_via = j
                if target_day < client_deadline[i]:
                    if len(sweep_examples) < 3:
                        sweep_examples.append((
                            str(pool.iloc[i].get('Customer', pool.iloc[i].get('ID', '?')))[:30],
                            client_deadline[i], target_day,
                            str(pool.iloc[target_via].get('Customer',
                                pool.iloc[target_via].get('ID', '?')))[:30],
                        ))
                    client_deadline[i] = target_day
                    n_pulled += 1

            if n_pulled:
                print(f"  Neighbor-sweep: pulled {n_pulled} client(s) to earlier "
                      f"days (radius={_sweep_radius_mi:.1f} mi, "
                      f"min_fill={_sweep_min_fill:.0%})")
                for name, was, now, anchor in sweep_examples:
                    print(f"    {name:<30} day {was}→{now}  (via {anchor})")

        # Per-vehicle cost callback that adds lateness penalty
        for v in range(n_vehicles):
            _, day_idx, _ = _v2tdc(v)

            def _make_day_cb(_mgr, _sd, _day, _deadlines, _pen, _node_clusters,
                             _node_is_far, _metro_cross, _far_cross,
                             _refills, _tanks, _n_clients, _mandatory):
                def _cb(from_idx, to_idx):
                    fn = _mgr.IndexToNode(from_idx)
                    tn = _mgr.IndexToNode(to_idx)
                    d = int(_sd[fn, tn])
                    # Cluster crossing penalty (same logic as _cost_cb)
                    if fn != 0 and tn != 0:
                        fc = _node_clusters[fn]
                        tc = _node_clusters[tn]
                        if fc != tc:
                            if _node_is_far[fn] or _node_is_far[tn]:
                                d += _far_cross
                            else:
                                d += _metro_cross
                    # Fill-economics penalty: penalize both early AND late visits
                    if tn != 0:  # not depot
                        ci = tn - 1  # client index
                        if ci < _n_clients:
                            # Lateness: days past preferred fill day.
                            # Mandatory clients (stockout/critical) get 100x
                            # lateness penalty — 500K/day at base 5K. This makes
                            # deferral from Day 0→1 cost ~300 miles equivalent,
                            # far exceeding any routing savings. But it's still
                            # finite, so if Day 0 is physically full (time/capacity),
                            # clients spill to Day 1 instead of crashing the solver.
                            days_late = max(0, _day - _deadlines[ci])
                            late_mult = 100 if _mandatory[ci] else 1
                            d += days_late * _pen * late_mult

                            # Earliness (fill economics) — BYPASSED for mandatory
                            # clients (stockout/critical). These clients MUST be
                            # served on Day 0 regardless of fill level. Penalizing
                            # their low fill on Day 0 was the root cause of the
                            # "skip Wednesday" bug: the solver avoided Day 0
                            # because earliness costs made it look expensive.
                            if not _mandatory[ci]:
                                if _day < len(_refills) and (ci + 1) < len(_refills[_day]):
                                    refill = _refills[_day][ci + 1]
                                    tank = _tanks[ci]
                                    fill_pct = min(refill / tank, 1.0) if tank > 0 else 0
                                    underfill = max(0.0, 1.0 - fill_pct)
                                    d += int(_pen * underfill * underfill * 5)
                    return d
                return _cb

            # Pre-compute tank sizes for fill economics
            _tank_sizes = [max(float(pool.iloc[ci]['Tank_lbs']), 1.0)
                           for ci in range(n_clients)]

            day_cb = _make_day_cb(
                manager, _sd, day_idx, client_deadline, _late_penalty,
                node_clusters, node_is_far,
                METRO_CROSS_PENALTY, FAR_CROSS_PENALTY,
                refills_by_day, _tank_sizes, n_clients, client_is_mandatory,
            )
            day_cb_idx = routing.RegisterTransitCallback(day_cb)
            routing.SetArcCostEvaluatorOfVehicle(day_cb_idx, v)

        # Override the shared cost callback with per-vehicle ones
        # (SetArcCostEvaluatorOfVehicle takes precedence over
        #  SetArcCostEvaluatorOfAllVehicles for vehicle v)

    # ── Solver parameters ────────────────────────────────────────────────
    # Read strategy names from config so bench_ab.py can override them.
    _fss_name = getattr(_cfg, 'FIRST_SOLUTION_STRATEGY', 'PARALLEL_CHEAPEST_INSERTION')
    _lsm_name = getattr(_cfg, 'LOCAL_SEARCH_METAHEURISTIC', 'GUIDED_LOCAL_SEARCH')
    _sol_limit = int(getattr(_cfg, 'SOLUTION_LIMIT', 1_000))

    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = getattr(
        routing_enums_pb2.FirstSolutionStrategy, _fss_name,
        routing_enums_pb2.FirstSolutionStrategy.PARALLEL_CHEAPEST_INSERTION,
    )
    params.local_search_metaheuristic = getattr(
        routing_enums_pb2.LocalSearchMetaheuristic, _lsm_name,
        routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH,
    )
    params.time_limit.seconds = solve_seconds
    if _sol_limit > 0:
        params.solution_limit = _sol_limit
    params.log_search = False

    # ── Optional warm-start from a prior solution ────────────────────────
    initial_assignment = None
    if initial_routes_by_vehicle:
        try:
            # Build {client_id (str): pool node index} for translation.
            id_to_node = {
                str(cid): i + 1   # +1 because depot is node 0
                for i, cid in enumerate(pool['ID'].astype(str).tolist())
            }
            ot_routes: List[List[int]] = [[] for _ in range(n_vehicles)]
            ws_matched, ws_missed = 0, 0
            for v_idx, ids in initial_routes_by_vehicle.items():
                if v_idx < 0 or v_idx >= n_vehicles:
                    continue
                for cid in ids:
                    n = id_to_node.get(str(cid))
                    if n is None:
                        ws_missed += 1
                        continue
                    ot_routes[v_idx].append(n)
                    ws_matched += 1
            if ws_matched > 0:
                initial_assignment = routing.ReadAssignmentFromRoutes(
                    ot_routes, True   # ignore_inactive_indices
                )
                print(f"  ↻ Warm start: {ws_matched} visits matched "
                      f"({ws_missed} missed), seeding solver search.")
            else:
                print('  ↻ Warm start: no usable visits — cold start.')
        except Exception as e:
            print(f'  ↻ Warm start skipped: {e}')
            initial_assignment = None

    print(f"\n  Solving... (time limit: {solve_seconds}s)")
    if initial_assignment is not None:
        solution = routing.SolveFromAssignmentWithParameters(
            initial_assignment, params,
        )
    else:
        solution = routing.SolveWithParameters(params)

    if solution is None:
        print(f"  ✗ No feasible solution. Status: {routing.status()}")
        empty = {d: pd.DataFrame() for d in range(start_day, _npd)}
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
        n_plan_days=_npd,
        truck_names=_active_truck_names,
        shift_start_min=int(depot_config.get('shift_start_min', 6 * 60)),
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

    _print_solution_summary(routes, deferred, start_day, n_plan_days=_npd)
    return routes, deferred


# ── Pool builder ─────────────────────────────────────────────────────────────

def _build_pool(
    clients_df:       pd.DataFrame,
    node_index_map:   dict,
    deferred_reasons: dict = None,
    n_plan_days:      int  = NUM_DAYS,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    Filter to eligible clients: routable, in matrix, fill ≥ threshold.
    Excludes clients already marked as deferred by earlier checks.

    Parameters
    ----------
    n_plan_days : Planning horizon length (work-days). Controls the DTE
                  eligibility threshold: clients whose stockout falls within
                  the horizon (+2 buffer days) are included even if their
                  tank isn't 55%+ empty yet.

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

    # ── Eligibility (rolling-horizon correct) ─────────────────────────────
    # PRINCIPLE: a true rolling-horizon IRP must SEE every routable client,
    # not just today's emergencies. Otherwise a client whose stockout falls
    # 2 days BEYOND the horizon (e.g. day 12 with a 10-day plan) gets
    # silently dropped today, dropped tomorrow, dropped the day after —
    # until DTE finally crosses the threshold and they're "discovered" as
    # urgent. By then the solver has lost every opportunity to fold them
    # into a passing route.
    #
    # The fix: include EVERY routable client. Far-future clients get a
    # large drop penalty (cheap to defer), so the solver only schedules
    # them if the geography is cheap or capacity is available. Near-future
    # clients get small drop penalties (expensive to defer). The solver
    # decides — that's the whole point of an OR-Tools CVRP with disjunctions.
    #
    # Cost: pool grows from ~60 to ~171 (~3×). Solve time grows roughly
    # linearly per pool size for OR-Tools' local search; with the existing
    # 30-vehicle structure the increase is manageable for 10-min solves.
    # Default: every routable client is eligible. The solver decides via
    # disjunction penalties (small for non-urgent, large for urgent) which
    # ones get scheduled. To restore the legacy "only urgent" filter for
    # debugging or speed, set ENABLE_FORWARD_EMPTY_CLIENTS = False in config.
    enable_forward = getattr(_cfg, 'ENABLE_FORWARD_EMPTY_CLIENTS', True)
    if enable_forward:
        eligible = pd.Series(True, index=df.index)
    else:
        dte_threshold = n_plan_days + 2
        eligible = (
            (df['Fill_Pct'] >= OPPORTUNISTIC_FILL_PCT)
            | (df['Days_Until_Stockout'] <= dte_threshold)
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
    n_plan_days:     int = NUM_DAYS,
    truck_names:     Optional[List[str]] = None,
    shift_start_min: int = 360,
) -> Dict[int, pd.DataFrame]:
    """Convert OR-Tools solution → per-day route DataFrames.
    Uses day-projected refills, urgency, and days-to-empty when available."""

    day_records: Dict[int, list] = {d: [] for d in range(n_plan_days)}

    for v in range(n_vehicles):
        truck_name, day, cfg = vehicle_to_truck_day_config(v, n_plan_days, truck_names)

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

                # Driver leaves the warehouse at shift_start_min (default
                # 6:00 AM = 360 min). Arrival time at this stop = shift
                # start + cumulative time so far + travel to this stop.
                arrival_min = shift_start_min + cum_time + travel_min
                depart_min  = arrival_min + svc_min

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
                    'Arrival_Min':         arrival_min,
                    'Depart_Min':          depart_min,
                    'Arrival_HHMM':        f'{arrival_min // 60:02d}:{arrival_min % 60:02d}',
                    'Depart_HHMM':         f'{depart_min // 60:02d}:{depart_min % 60:02d}',
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
    for d in range(n_plan_days):
        result[d] = pd.DataFrame(day_records[d]) if day_records[d] else pd.DataFrame()

    return result


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
    n_plan_days: int = NUM_DAYS,
):
    n_days_label = f"{n_plan_days}-Day" if n_plan_days != NUM_DAYS else "Weekly"
    print(f"\n{'═' * 68}")
    print(f"  {n_days_label} Schedule (Unified Solver)")
    print(f"{'═' * 68}")
    print(f"  {'Slot':<22} {'Stops':>5} {'Load lbs':>10} {'Cap%':>5} "
          f"{'Time':>5} {'Shift%':>6} {'Dist mi':>8}")
    print(f"  {'─' * 71}")

    total_stops = 0
    total_miles = 0.0

    for d in range(n_plan_days):
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
