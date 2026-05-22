"""
Scenario library — helpers for building synthetic ProblemInstances.

Designed for STRESS-TEST scenarios: a small number of clients (typically
2–10), 1–2 trucks, 2–5 days. The Designer agent uses these helpers to
build scenarios without needing to construct the full v2.domain.problem
boilerplate by hand.

A scenario is just a function: () -> (problem, expected_dict).
The runner (runner.py) calls solve_final on the problem and compares
the resulting Plan to the expected_dict.

Example
-------
from final.stress.scenario_lib import build, client, truck, depot

def scenario_two_clients_one_truck():
    p = build(
        today='2026-05-22',
        horizon_days=3,
        depot=depot(33.5, -112.2),
        trucks=[truck('Truck2', cap=10000)],
        clients=[
            client('A', tank=1000, rate=200, current=900, lat=33.50, lon=-112.20),
            client('B', tank=1000, rate=200, current=100, lat=33.51, lon=-112.21),  # urgent
        ],
    )
    expected = {
        'must_serve': ['B'],            # urgent — must be in day 0
        'min_total_stops': 1,
        'max_total_stops': 3,
        'no_stockouts': True,
    }
    return p, expected
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date as Date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

import numpy as np

from v2.domain.client import Client, TankState
from v2.domain.fleet import Truck, Compartment, Depot
from v2.domain.overrides import Overrides
from v2.domain.problem import ProblemInstance


def client(
    id: str,
    tank: int,
    rate: float,
    current: float,
    lat: float = 33.50,
    lon: float = -112.20,
    product: str = 'CANOLA',
    do_not_schedule: bool = False,
    notes: str = '',
) -> Tuple[Client, TankState]:
    """Build a (Client, TankState) pair for one customer."""
    c = Client(
        id=id, customer=f'TEST - {id}', lat=lat, lon=lon,
        tank_capacity_lbs=tank, product=product,
        do_not_schedule=do_not_schedule,
        excluded=False, address='synthetic',
        phone='', notes=notes,
    )
    ts = TankState(
        client_id=id, current_lbs=float(current),
        as_of=datetime.now(), source='synthetic',
        rate_lbs_per_day=float(rate), rate_std_dev=0.0,
        last_delivery_date=None, last_delivery_lbs=None,
    )
    return c, ts


def truck(
    id: str = 'Truck2',
    cap: int = 10000,
    pump_rate: float = 152.6,
    setup_min: int = 18,
    compartments: int = 2,
    compartment_cap: int = 5000,
) -> Truck:
    return Truck(
        id=id, capacity_lbs=cap,
        compartments=tuple(
            Compartment(id=chr(65 + i), capacity_lbs=compartment_cap)
            for i in range(compartments)
        ),
        pump_rate_lbs_per_min=pump_rate,
        fixed_setup_min=setup_min,
    )


def depot(lat: float = 33.5152, lon: float = -112.1674) -> Depot:
    return Depot(id='DEPOT', lat=lat, lon=lon)


def _synthetic_matrix(
    depot: Depot,
    clients: List[Client],
) -> Tuple[np.ndarray, np.ndarray, Dict[str, int]]:
    """
    Build a synthetic distance/time matrix from lat/lon using straight-line.

    Distance in meters = haversine.
    Time in minutes = distance_km / 35 km/h × 60 = distance_m / 35000 × 60 .
    Both rounded to int.

    Node 0 = depot, nodes 1..N = clients (in given order).
    """
    nodes = [(depot.lat, depot.lon)] + [(c.lat, c.lon) for c in clients]
    n = len(nodes)
    dm = np.zeros((n, n), dtype=int)
    tm = np.zeros((n, n), dtype=int)
    for i, (la1, lo1) in enumerate(nodes):
        for j, (la2, lo2) in enumerate(nodes):
            if i == j:
                continue
            d_m = _haversine_m(la1, lo1, la2, lo2)
            dm[i, j] = int(d_m)
            tm[i, j] = max(1, int(d_m / 35000 * 60))   # 35 km/h average
    node_index = {'DEPOT': 0}
    for k, c in enumerate(clients):
        node_index[c.id] = k + 1
    return dm, tm, node_index


def _haversine_m(la1: float, lo1: float, la2: float, lo2: float) -> float:
    """Great-circle distance in meters."""
    R = 6371000.0
    phi1 = np.radians(la1)
    phi2 = np.radians(la2)
    dphi = np.radians(la2 - la1)
    dl = np.radians(lo2 - lo1)
    a = np.sin(dphi / 2) ** 2 + np.cos(phi1) * np.cos(phi2) * np.sin(dl / 2) ** 2
    return float(2 * R * np.arcsin(np.sqrt(a)))


_DOW_NAMES = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')


def build(
    today: str,
    horizon_days: int,
    depot: Depot,
    trucks: List[Truck],
    clients: List[Tuple[Client, TankState]],
    working_days: Tuple[str, ...] = ('Tue', 'Wed', 'Thu', 'Fri', 'Sat'),
    saturday_trucks: Tuple[str, ...] = ('Truck2',),
    products: Tuple[str, ...] = ('CANOLA', 'FRYERS CHOICE'),
    shift_start_min: int = 360,        # 6 AM
    shift_target_min: int = 304,       # ~5h
    shift_hard_max_min: int = 435,     # 7h 15m
    weekly_max_min: int = 2400,
    cost_per_mile: float = 0.55,
    cost_per_minute_labor: float = 0.00,
    overtime_multiplier: float = 1.5,
    truck_dispatch_cost: float = 0.0,
    stockout_cost_per_lb_day: float = 10.0,
    terminal_value_per_lb: float = 0.10,
    min_stop_lbs: int = 200,
    min_reserve_fraction: float = 0.10,
    target_empty_fraction: float = 0.30,
    team_overlap_penalty_dollars: float = 0.0,
    num_territory_clusters: int = 1,
    commit_days: int = 1,
    overrides: Optional[Overrides] = None,
    solve_seconds: int = 30,
    run_id: str = 'stress',
) -> ProblemInstance:
    """Construct a ProblemInstance from synthetic primitives."""
    today_d = Date.fromisoformat(today)

    # Horizon = next N working days starting from today
    dates: List[Date] = []
    cursor = today_d
    for _ in range(60):
        if len(dates) >= horizon_days:
            break
        if _DOW_NAMES[cursor.weekday()] in working_days:
            dates.append(cursor)
        cursor = cursor + timedelta(days=1)
    horizon_dates = tuple(dates)

    # Truck-available matrix
    truck_avail: Dict[Tuple[Date, str], bool] = {}
    sat_trucks = frozenset(saturday_trucks)
    for d in horizon_dates:
        is_sat = _DOW_NAMES[d.weekday()] == 'Sat'
        for t in trucks:
            truck_avail[(d, t.id)] = (t.id in sat_trucks) if is_sat else True

    client_tuple = tuple(c for c, _ in clients)
    tanks = {c.id: ts for c, ts in clients}

    dm, tm, node_index = _synthetic_matrix(depot, list(client_tuple))

    return ProblemInstance(
        run_id=run_id,
        today=today_d,
        horizon_dates=horizon_dates,
        commit_days=commit_days,
        clients=client_tuple,
        trucks=tuple(trucks),
        depot=depot,
        products=products,
        initial_tanks=tanks,
        truck_available=truck_avail,
        overrides=overrides if overrides is not None else Overrides(),
        distance_matrix_m=dm,
        time_matrix_min=tm,
        node_index=node_index,
        cost_per_mile=cost_per_mile,
        cost_per_minute_labor=cost_per_minute_labor,
        overtime_multiplier=overtime_multiplier,
        truck_dispatch_cost=truck_dispatch_cost,
        stockout_cost_per_lb_day=stockout_cost_per_lb_day,
        terminal_value_per_lb=terminal_value_per_lb,
        shift_start_min=shift_start_min,
        shift_target_min=shift_target_min,
        shift_hard_max_min=shift_hard_max_min,
        weekly_max_min=weekly_max_min,
        min_stop_lbs=min_stop_lbs,
        min_reserve_fraction=min_reserve_fraction,
        target_empty_fraction=target_empty_fraction,
        team_overlap_penalty_dollars=team_overlap_penalty_dollars,
        num_territory_clusters=num_territory_clusters,
        solve_seconds=solve_seconds,
    )
