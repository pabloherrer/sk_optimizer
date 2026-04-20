"""
router.py — Phase 2: Single-Day CVRP / TSP Solve
=================================================
Takes the set of clients assigned to one day by the scheduler and finds
the best routes for Truck2 and Truck9 using OR-Tools.

Key design decisions vs. the original notebook
-----------------------------------------------
1. Objective: minimise total route TIME (not distance).
   Travel time is the real binding resource (10-hour shift cap).

2. Vehicle-specific service times.
   Each stop's service time = fixed_setup + refill / pump_rate.
   Truck9 is 35% faster at the pump, so the same route genuinely takes
   less time on Truck9.

3. No disjunctions — all assigned clients are MANDATORY.
   Phase 1 (scheduler.py) decides inclusion; Phase 2 only routes.

4. Demands are day-specific.
   Refill amounts are computed for the actual visit day.

5. Two routing modes:
   a. Per-truck TSP (default when AssignedTruck column is present)
      Phase 1 already separated clients geographically per truck, so
      we solve a single-vehicle TSP for each truck independently.
      This is faster and guarantees geographic coherence is preserved.
   b. Two-truck CVRP (legacy fallback, used when AssignedTruck is absent)
      Full two-vehicle solve; kept for backward compatibility.
"""

import math
import numpy as np
import pandas as pd
from ortools.constraint_solver import routing_enums_pb2, pywrapcp
from config import (
    TRUCKS, TRUCK_NAMES, NUM_TRUCKS, DAYS, SHIFT_MIN, SOLVE_SEC,
)
from inventory import service_time_min


# ── Public API ────────────────────────────────────────────────────────────────

def solve_day_routes(
    day_clients:     pd.DataFrame,   # Output of scheduler for one day
    day_index:       int,            # 0=Mon … 4=Fri
    dist_matrix:     np.ndarray,     # Full (N×N) metres, integer
    time_matrix_min: np.ndarray,     # Full (N×N) minutes, integer
    node_index_map:  dict,           # client_id → matrix row/col index
) -> pd.DataFrame:
    """
    Solve routes for one day.

    If 'AssignedTruck' column is present and populated (new geo-aware
    scheduler), routes each truck's clients independently as a TSP.
    Otherwise falls back to the legacy two-truck CVRP.

    Returns a DataFrame with one row per stop.
    Returns an empty DataFrame if no feasible solution is found.
    """
    if len(day_clients) == 0:
        return pd.DataFrame()

    has_truck_assignment = (
        'AssignedTruck' in day_clients.columns
        and day_clients['AssignedTruck'].isin(TRUCK_NAMES).any()
    )

    if has_truck_assignment:
        # ── Per-truck TSP: preserves Phase-1 geographic clustering ────────
        results = []
        for truck_name in TRUCK_NAMES:
            tc = day_clients[day_clients['AssignedTruck'] == truck_name].copy()
            if len(tc) == 0:
                continue
            route = _solve_single_truck(
                tc, day_index, truck_name,
                dist_matrix, time_matrix_min, node_index_map,
            )
            if not route.empty:
                results.append(route)

        if not results:
            return pd.DataFrame()
        return pd.concat(results, ignore_index=True)

    # ── Legacy two-truck CVRP ──────────────────────────────────────────────
    return _solve_two_truck(
        day_clients, day_index, dist_matrix, time_matrix_min, node_index_map
    )


# ── Single-truck TSP solver ───────────────────────────────────────────────────

def _solve_single_truck(
    truck_clients:   pd.DataFrame,
    day_index:       int,
    truck_name:      str,
    dist_matrix:     np.ndarray,
    time_matrix_min: np.ndarray,
    node_index_map:  dict,
) -> pd.DataFrame:
    """
    Solve a single-vehicle TSP for one truck's pre-assigned clients.

    With geographic clustering already done in Phase 1, this is purely
    a sequencing problem: find the optimal visit order for this truck's
    geographic cluster.
    """
    n_stops = len(truck_clients)
    if n_stops == 0:
        return pd.DataFrame()

    # ── Build compact local matrices ──────────────────────────────────────
    depot_matrix_idx = node_index_map.get('DEPOT', 0)
    local_ids        = ['DEPOT'] + truck_clients['ID'].tolist()
    global_indices   = [depot_matrix_idx] + [
        node_index_map[cid] for cid in truck_clients['ID']
    ]
    n_nodes = len(local_ids)

    sub_dist = np.array(
        [[int(dist_matrix[global_indices[i], global_indices[j]])
          for j in range(n_nodes)]
         for i in range(n_nodes)],
        dtype=np.int64,
    )
    sub_time = np.array(
        [[int(time_matrix_min[global_indices[i], global_indices[j]])
          for j in range(n_nodes)]
         for i in range(n_nodes)],
        dtype=np.int64,
    )

    node_refills = [0] + [
        int(row['ProjectedRefill_lbs'])
        for _, row in truck_clients.iterrows()
    ]

    # ── OR-Tools: 1 vehicle ───────────────────────────────────────────────
    manager = pywrapcp.RoutingIndexManager(n_nodes, 1, 0)
    routing = pywrapcp.RoutingModel(manager)

    setup = TRUCKS[truck_name]['fixed_setup_min']
    rate  = TRUCKS[truck_name]['pump_rate_lbs_per_min']
    cap   = TRUCKS[truck_name]['capacity_lbs']

    # Time transit: travel + service at each stop
    _sub_time  = sub_time    # captured by closure
    _refills   = node_refills

    def _time_cb(from_idx: int, to_idx: int) -> int:
        from_node = manager.IndexToNode(from_idx)
        travel    = int(_sub_time[from_node, manager.IndexToNode(to_idx)])
        if from_node == 0:
            return travel
        svc = setup + math.ceil(_refills[from_node] / rate)
        return travel + svc

    tcb = routing.RegisterTransitCallback(_time_cb)
    routing.SetArcCostEvaluatorOfVehicle(tcb, 0)  # minimise time

    # Time dimension (shift cap)
    routing.AddDimension(tcb, 0, SHIFT_MIN, True, 'Time')

    # Capacity dimension
    def _demand_cb(from_idx: int) -> int:
        return _refills[manager.IndexToNode(from_idx)]

    dcb = routing.RegisterUnaryTransitCallback(_demand_cb)
    routing.AddDimensionWithVehicleCapacity(dcb, 0, [cap], True, 'Cap')

    # Distance dimension (reporting only, not objective)
    _sub_dist = sub_dist

    def _dist_cb(from_idx: int, to_idx: int) -> int:
        return int(_sub_dist[manager.IndexToNode(from_idx),
                              manager.IndexToNode(to_idx)])

    dist_cb = routing.RegisterTransitCallback(_dist_cb)
    routing.AddDimension(dist_cb, 0, 10_000_000, True, 'Distance')

    # Solver params
    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = (
        routing_enums_pb2.FirstSolutionStrategy.GLOBAL_CHEAPEST_ARC
    )
    params.local_search_metaheuristic = (
        routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    )
    params.time_limit.seconds = SOLVE_SEC

    solution = routing.SolveWithParameters(params)

    if solution is None:
        total_load = sum(node_refills)
        print(f"  ✗ {truck_name}/{DAYS[day_index]}: OR-Tools infeasible. "
              f"  Load={total_load:,} lbs  Cap={cap:,} lbs  Stops={n_stops}")
        return pd.DataFrame()

    return _extract_single_truck_route(
        solution, routing, manager,
        truck_name, day_index,
        truck_clients, local_ids, node_refills,
        sub_dist, sub_time,
    )


def _extract_single_truck_route(
    sol, routing, manager,
    truck_name: str, day_index: int,
    truck_clients: pd.DataFrame,
    local_ids: list, node_refills: list,
    sub_dist: np.ndarray, sub_time: np.ndarray,
) -> pd.DataFrame:

    setup = TRUCKS[truck_name]['fixed_setup_min']
    rate  = TRUCKS[truck_name]['pump_rate_lbs_per_min']

    idx      = routing.Start(0)
    stop_num = 0
    cum_dist = 0
    cum_time = 0
    prev     = 0

    records = []

    while not routing.IsEnd(idx):
        node = manager.IndexToNode(idx)
        if node != 0:
            stop_num   += 1
            client_id   = local_ids[node]
            cdf         = truck_clients[truck_clients['ID'] == client_id].iloc[0]

            travel_m    = int(sub_dist[prev, node])
            travel_min  = int(sub_time[prev, node])
            refill      = node_refills[node]
            svc_min     = setup + math.ceil(refill / rate)

            cum_dist   += travel_m
            cum_time   += travel_min + svc_min

            records.append({
                'Truck':               truck_name,
                'Day':                 DAYS[day_index],
                'DayIndex':            day_index,
                'Stop':                stop_num,
                'ID':                  client_id,
                'Customer':            cdf['Customer'],
                'Zone':                cdf['Zone'],
                'Address':             cdf.get('Address', ''),
                'Lat':                 cdf['Lat'],
                'Lon':                 cdf['Lon'],
                'Product':             cdf.get('Product', ''),
                'Tank_lbs':            int(cdf['Tank_lbs']),
                'Current_lbs':         round(float(cdf['Current_lbs'])),
                'Refill_lbs':          refill,
                'Fill_Pct':            round(refill / cdf['Tank_lbs'] * 100, 1),
                'Avg_LbsPerDay':       round(float(cdf['Avg_LbsPerDay']), 1),
                'Days_Until_Stockout': round(float(cdf['Days_Until_Stockout']), 1),
                'DaysToStockoutAtVisit': round(float(cdf.get('DaysToStockoutAtVisit', 0)), 1),
                'Urgency':             cdf.get('Urgency', ''),
                'VisitScore':          round(float(cdf.get('VisitScore', 0)), 4),
                'Service_Min':         svc_min,
                'Travel_To_Min':       travel_min,
                'Dist_To_m':           travel_m,
                'Dist_To_km':          round(travel_m / 1000, 2),
                'Cum_Dist_km':         round(cum_dist / 1000, 2),
            })
            prev = node

        idx = sol.Value(routing.NextVar(idx))

    if not records:
        return pd.DataFrame()

    # Return leg to depot
    ret_dist  = int(sub_dist[prev, 0])
    ret_min   = int(sub_time[prev, 0])
    cum_dist += ret_dist
    cum_time += ret_min

    route_load = sum(r['Refill_lbs'] for r in records)
    for rec in records:
        rec['Route_Dist_km']    = round(cum_dist / 1000, 1)
        rec['Route_Time_min']   = cum_time
        rec['Shift_Pct']        = round(cum_time / SHIFT_MIN * 100, 1)
        rec['Return_Depot_Min'] = ret_min
        rec['Route_Load_lbs']   = route_load
        rec['Cap_Pct']          = round(route_load / TRUCKS[truck_name]['capacity_lbs'] * 100, 1)

    return pd.DataFrame(records)


# ── Legacy two-truck CVRP ─────────────────────────────────────────────────────

def _solve_two_truck(
    day_clients:     pd.DataFrame,
    day_index:       int,
    dist_matrix:     np.ndarray,
    time_matrix_min: np.ndarray,
    node_index_map:  dict,
) -> pd.DataFrame:
    """
    Original two-vehicle CVRP solve.  Used when AssignedTruck is not set.
    Kept for backward compatibility / debugging.
    """
    # ── Build compact local matrices ──────────────────────────────────────
    depot_matrix_idx = node_index_map.get('DEPOT', 0)
    local_ids        = ['DEPOT'] + day_clients['ID'].tolist()
    global_indices   = [depot_matrix_idx] + [
        node_index_map[cid] for cid in day_clients['ID']
    ]
    n_nodes = len(local_ids)

    sub_dist = np.array(
        [[int(dist_matrix[global_indices[i], global_indices[j]])
          for j in range(n_nodes)]
         for i in range(n_nodes)],
        dtype=np.int64,
    )
    sub_time = np.array(
        [[int(time_matrix_min[global_indices[i], global_indices[j]])
          for j in range(n_nodes)]
         for i in range(n_nodes)],
        dtype=np.int64,
    )

    node_refills = [0] + [
        int(row['ProjectedRefill_lbs'])
        for _, row in day_clients.iterrows()
    ]

    # ── OR-Tools setup ─────────────────────────────────────────────────────
    manager = pywrapcp.RoutingIndexManager(
        n_nodes, NUM_TRUCKS,
        [0] * NUM_TRUCKS,
        [0] * NUM_TRUCKS,
    )
    routing = pywrapcp.RoutingModel(manager)

    # Vehicle-specific time transit callbacks
    def _make_time_cb(truck_name: str):
        setup = TRUCKS[truck_name]['fixed_setup_min']
        rate  = TRUCKS[truck_name]['pump_rate_lbs_per_min']
        _st   = sub_time
        _rf   = node_refills

        def _cb(from_idx: int, to_idx: int) -> int:
            from_node = manager.IndexToNode(from_idx)
            travel    = int(_st[from_node, manager.IndexToNode(to_idx)])
            if from_node == 0:
                return travel
            svc = setup + math.ceil(_rf[from_node] / rate)
            return travel + svc

        return _cb

    time_cb_indices = []
    for truck_name in TRUCK_NAMES:
        cb_idx = routing.RegisterTransitCallback(_make_time_cb(truck_name))
        time_cb_indices.append(cb_idx)
        routing.SetArcCostEvaluatorOfVehicle(cb_idx, TRUCK_NAMES.index(truck_name))

    routing.AddDimensionWithVehicleTransits(
        time_cb_indices, 0, SHIFT_MIN, True, 'Time'
    )

    def _demand_cb(from_idx: int) -> int:
        return node_refills[manager.IndexToNode(from_idx)]

    dcb = routing.RegisterUnaryTransitCallback(_demand_cb)
    routing.AddDimensionWithVehicleCapacity(
        dcb, 0,
        [TRUCKS[t]['capacity_lbs'] for t in TRUCK_NAMES],
        True, 'Cap',
    )

    def _dist_cb(from_idx: int, to_idx: int) -> int:
        return int(sub_dist[manager.IndexToNode(from_idx),
                             manager.IndexToNode(to_idx)])

    dist_cb_idx = routing.RegisterTransitCallback(_dist_cb)
    routing.AddDimension(dist_cb_idx, 0, 10_000_000, True, 'Distance')

    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = (
        routing_enums_pb2.FirstSolutionStrategy.GLOBAL_CHEAPEST_ARC
    )
    params.local_search_metaheuristic = (
        routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    )
    params.time_limit.seconds = SOLVE_SEC

    solution = routing.SolveWithParameters(params)

    if solution is None:
        print(f"  ✗ {DAYS[day_index]}: OR-Tools found no feasible solution. "
              f"  Check capacity ({sum(node_refills):,} lbs vs "
              f"{sum(t['capacity_lbs'] for t in TRUCKS.values()):,} lbs total).")
        return pd.DataFrame()

    return _extract_routes(
        solution, routing, manager,
        day_clients, day_index,
        local_ids, node_refills,
        sub_dist, sub_time,
    )


def _extract_routes(
    sol, routing, manager,
    day_clients, day_index,
    local_ids, node_refills,
    sub_dist, sub_time,
) -> pd.DataFrame:

    records = []

    for v, truck_name in enumerate(TRUCK_NAMES):
        setup = TRUCKS[truck_name]['fixed_setup_min']
        rate  = TRUCKS[truck_name]['pump_rate_lbs_per_min']

        idx      = routing.Start(v)
        stop_num = 0
        cum_dist = 0
        cum_time = 0
        prev     = 0

        route_records = []

        while not routing.IsEnd(idx):
            node = manager.IndexToNode(idx)
            if node != 0:
                stop_num   += 1
                client_id   = local_ids[node]
                cdf = day_clients[day_clients['ID'] == client_id].iloc[0]

                travel_m    = int(sub_dist[prev, node])
                travel_min  = int(sub_time[prev, node])
                refill      = node_refills[node]
                svc_min     = setup + math.ceil(refill / rate)

                cum_dist   += travel_m
                cum_time   += travel_min + svc_min

                route_records.append({
                    'Truck':               truck_name,
                    'Day':                 DAYS[day_index],
                    'DayIndex':            day_index,
                    'Stop':                stop_num,
                    'ID':                  client_id,
                    'Customer':            cdf['Customer'],
                    'Zone':                cdf['Zone'],
                    'Address':             cdf.get('Address', ''),
                    'Lat':                 cdf['Lat'],
                    'Lon':                 cdf['Lon'],
                    'Product':             cdf.get('Product', ''),
                    'Tank_lbs':            int(cdf['Tank_lbs']),
                    'Current_lbs':         round(float(cdf['Current_lbs'])),
                    'Refill_lbs':          refill,
                    'Fill_Pct':            round(refill / cdf['Tank_lbs'] * 100, 1),
                    'Avg_LbsPerDay':       round(float(cdf['Avg_LbsPerDay']), 1),
                    'Days_Until_Stockout': round(float(cdf['Days_Until_Stockout']), 1),
                    'DaysToStockoutAtVisit': round(float(cdf.get('DaysToStockoutAtVisit', 0)), 1),
                    'Urgency':             cdf.get('Urgency', ''),
                    'VisitScore':          round(float(cdf.get('VisitScore', 0)), 4),
                    'Service_Min':         svc_min,
                    'Travel_To_Min':       travel_min,
                    'Dist_To_m':           travel_m,
                    'Dist_To_km':          round(travel_m / 1000, 2),
                    'Cum_Dist_km':         round(cum_dist / 1000, 2),
                })
                prev = node

            idx = sol.Value(routing.NextVar(idx))

        if not route_records:
            continue

        ret_dist  = int(sub_dist[prev, 0])
        ret_min   = int(sub_time[prev, 0])
        cum_dist += ret_dist
        cum_time += ret_min

        for rec in route_records:
            rec['Route_Dist_km']    = round(cum_dist / 1000, 1)
            rec['Route_Time_min']   = cum_time
            rec['Shift_Pct']        = round(cum_time / SHIFT_MIN * 100, 1)
            rec['Return_Depot_Min'] = ret_min
            rec['Route_Load_lbs']   = sum(r['Refill_lbs'] for r in route_records)
            rec['Cap_Pct']          = round(
                rec['Route_Load_lbs'] / TRUCKS[truck_name]['capacity_lbs'] * 100, 1
            )

        records.extend(route_records)

    return pd.DataFrame(records)


# ── Matrix loader (called once at startup) ────────────────────────────────────

def load_matrix(matrix_file: str) -> tuple[np.ndarray, np.ndarray, dict]:
    """
    Load the precomputed OSRM matrix.

    Returns
    -------
    dist_int       : (N×N) integer metres
    time_int_min   : (N×N) integer minutes
    node_index_map : {client_id: matrix_row_index}
    """
    data = np.load(str(matrix_file), allow_pickle=True)

    from config import TRUCK_SPEED_FACTOR
    dm_m   = data['dm_meters'].astype(np.int64)
    tm_min = np.ceil(data['tm_seconds'] / 60 * TRUCK_SPEED_FACTOR).astype(np.int64)

    if 'client_ids' in data:
        ids = data['client_ids'].tolist()
    elif 'labels' in data:
        ids = data['labels'].tolist()
    else:
        raise KeyError("Matrix file has no 'client_ids' or 'labels' key.")

    node_index_map = {str(cid): i for i, cid in enumerate(ids)}
    print(f"  Matrix loaded: {dm_m.shape[0]} nodes  "
          f"| max dist {dm_m.max()/1000:.0f} km  "
          f"| max time {tm_min.max()} min")

    return dm_m, tm_min, node_index_map
