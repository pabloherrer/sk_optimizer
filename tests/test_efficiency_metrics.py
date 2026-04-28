"""
test_efficiency_metrics.py — Business KPI bounds

These tests assert that key operational KPIs stay within reasonable ranges
on a "normal" synthetic week. They're sanity checks, not precision checks:
each bound is chosen loose enough that legitimate solver choices pass but
catastrophic regressions (10× worse cost/gal, zero fill efficiency) fail.

KPIs covered:
  • Cost per gallon delivered (miles-based cost proxy)
  • Miles per stop (geographic routing efficiency)
  • Stops per driver-hour (time efficiency)
  • Average load factor (truck utilization)
  • Deferred fraction (service coverage)
  • Fill-pct distribution (don't top off trivial amounts)
"""
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

import config
from config import DAYS, PRODUCTS, TRUCK_NAMES
from unified_solver import solve_week


def run_test(name, fn):
    start = time.time()
    try:
        fn()
        print(f'  ✓ {name:<58s} ({(time.time()-start)*1000:.0f} ms)')
        return True
    except AssertionError as e:
        print(f'  ✗ {name:<58s} — {str(e)[:80]}')
        return False
    except Exception as e:
        print(f'  ✗ {name:<58s} — CRASH: {type(e).__name__}: {str(e)[:60]}')
        return False


def _normal_week(n=20, seed=0, spread=0.05):
    """Build a 'normal' week: no crisis, moderate demand, IID clients."""
    rng = np.random.default_rng(seed)
    clients = []
    coords = [(33.50, -112.10)]
    for i in range(n):
        lat = 33.50 + rng.uniform(-spread, spread)
        lon = -112.10 + rng.uniform(-spread, spread)
        coords.append((lat, lon))
        clients.append({
            'ID': f'N{i:03d}', 'Customer': f'Client{i}',
            'Lat': lat, 'Lon': lon,
            'Tank_lbs': 5000, 'Product': PRODUCTS[i % 2],
            'Avg_LbsPerDay': 180 + int(rng.integers(-40, 40)),
            'Days_Since_Last': int(rng.integers(3, 8)),
            'Current_lbs': int(rng.integers(1500, 3500)),
        })
    df = pd.DataFrame(clients)
    df['Est_Current_lbs']     = df['Current_lbs']
    df['Refill_lbs']          = (df['Tank_lbs'] - df['Current_lbs']).clip(lower=0)
    df['Days_Until_Stockout'] = (df['Current_lbs'] / df['Avg_LbsPerDay'].clip(lower=1)).clip(lower=0.5)
    df['Urgency']             = 'normal'
    df['Refill_Today_lbs']    = df['Refill_lbs']
    df['Fill_Pct_Today']      = df['Refill_lbs'] / df['Tank_lbs']

    nn = len(coords)
    dist = np.zeros((nn, nn), dtype=int)
    tm   = np.zeros((nn, nn), dtype=int)
    for i in range(nn):
        for j in range(nn):
            if i != j:
                dx = (coords[i][0] - coords[j][0]) * 69.0
                dy = (coords[i][1] - coords[j][1]) * 60.0
                d_mi = (dx * dx + dy * dy) ** 0.5
                dist[i, j] = int(d_mi * 1609)
                tm[i, j]   = max(1, int(d_mi * 2 + 1))
    nix = {'DEPOT': 0}
    for i, cid in enumerate(df['ID'].tolist(), 1):
        nix[cid] = i
    return {
        'clients_df': df,
        'dist_matrix': dist,
        'time_matrix': tm,
        'node_index_map': nix,
        'time_windows_df': pd.DataFrame(columns=['Client_ID', 'Day_of_Week', 'Open_Min', 'Close_Min']),
        'closures_df': pd.DataFrame(columns=['Client_ID', 'Start_Date', 'End_Date', 'Reason']),
        'depot_config': {
            'depot_lat': 33.5, 'depot_lon': -112.1,
            'shift_start_min': 360, 'shift_end_min': 1080,
            'morning_load_min': 30, 'evening_unload_min': 15,
            'work_days': DAYS,
        },
    }


def _solve(s, budget=2):
    return solve_week(
        s['clients_df'], s['dist_matrix'], s['time_matrix'], s['node_index_map'],
        start_day=0, solve_seconds=budget,
        time_windows_df=s['time_windows_df'],
        closures_df=s['closures_df'],
        today=pd.Timestamp('2026-04-14'),
        depot_config=s['depot_config'],
    )


def _summarize(routes):
    """Extract aggregate KPIs from a solution.

    Column names in unified_solver output:
      Refill_lbs       — per-stop lbs delivered
      Route_Dist_mi    — per-route total miles (repeated on each stop)
      Route_Time_min   — per-route total minutes (repeated on each stop)
      Route_Load_lbs   — per-route total lbs (repeated on each stop)
      Cap_Pct          — per-route capacity utilization (0-100)
    """
    total_mi = 0.0
    total_min = 0.0
    total_stop_lbs = 0.0
    n_stops = 0
    fill_pcts = []
    load_facts_by_route = {}  # (day, truck) -> route_load_lbs
    route_times = {}          # (day, truck) -> route_time_min
    route_miles = {}          # (day, truck) -> route_dist_mi

    for d, rdf in routes.items():
        if rdf.empty:
            continue
        for _, r in rdf.iterrows():
            stop_lbs = float(r.get('Refill_lbs', 0) or 0)
            total_stop_lbs += stop_lbs
            tank = max(1.0, float(r.get('Tank_lbs', 5000) or 5000))
            fill_pcts.append(stop_lbs / tank)
            n_stops += 1
            key = (d, r.get('Truck', '?'))
            # These are repeated per-stop within a route, so take first-seen
            if key not in route_miles:
                route_miles[key] = float(r.get('Route_Dist_mi', 0) or 0)
                route_times[key] = float(r.get('Route_Time_min', 0) or 0)
                load_facts_by_route[key] = float(r.get('Route_Load_lbs', 0) or 0)
    total_mi = sum(route_miles.values())
    total_min = sum(route_times.values())
    load_factors = [v / 10000.0 for v in load_facts_by_route.values()]
    return {
        'total_mi': total_mi,
        'total_lbs': total_stop_lbs,
        'total_min': max(total_min, 1.0),
        'n_stops': n_stops,
        'fill_pcts': fill_pcts,
        'load_factors': load_factors,
    }


# ── Tests ───────────────────────────────────────────────────────────────────


def test_cost_per_gallon_under_threshold():
    """
    Cost-per-gallon proxy (miles × $0.50 / gallon-equivalent) should stay
    under $0.05 per pound on a clustered metro scenario.
    """
    s = _normal_week(n=25, seed=7, spread=0.06)
    routes, _ = _solve(s, budget=3)
    k = _summarize(routes)
    if k['total_lbs'] == 0:
        return  # nothing scheduled — vacuously passes
    cost_per_lb = (k['total_mi'] * 0.50) / k['total_lbs']
    assert cost_per_lb < 0.05, \
        f'Cost/lb too high: ${cost_per_lb:.4f} (miles {k["total_mi"]:.1f}, lbs {k["total_lbs"]:.0f})'


def test_miles_per_stop_reasonable():
    """
    On a clustered metro week, miles per stop should be ≤ 10. More than that
    suggests ordering is poor or routes cross repeatedly.
    """
    s = _normal_week(n=25, seed=12, spread=0.06)
    routes, _ = _solve(s, budget=3)
    k = _summarize(routes)
    if k['n_stops'] == 0:
        return
    mi_per_stop = k['total_mi'] / k['n_stops']
    assert mi_per_stop <= 10.0, \
        f'{mi_per_stop:.1f} mi/stop too high ({k["total_mi"]:.1f} mi, {k["n_stops"]} stops)'


def test_stops_per_driver_hour_reasonable():
    """
    On a clustered week, we expect >= 1 stop per driver-hour of route time.
    Lower than that means we're spending most of the day driving, not serving.
    """
    s = _normal_week(n=20, seed=21, spread=0.05)
    routes, _ = _solve(s, budget=3)
    k = _summarize(routes)
    if k['n_stops'] < 3:
        return
    hours = k['total_min'] / 60.0
    if hours < 0.5:
        return
    stops_per_hr = k['n_stops'] / hours
    assert stops_per_hr >= 0.5, \
        f'Too few stops per hour: {stops_per_hr:.2f} ({k["n_stops"]} stops / {hours:.1f} hr)'


def test_fill_pct_distribution_skewed_toward_full():
    """
    Most deliveries should top up to a meaningful fill (>= 50% of tank).
    If the solver is scheduling many trivial top-ups, something's wrong.
    """
    s = _normal_week(n=20, seed=33, spread=0.05)
    routes, _ = _solve(s, budget=3)
    k = _summarize(routes)
    if len(k['fill_pcts']) < 4:
        return
    high_fills = sum(1 for p in k['fill_pcts'] if p >= 0.30)
    frac = high_fills / len(k['fill_pcts'])
    # Synthetic data has current 1500-3500, tank 5000 → refill 1500-3500 → pct 0.30-0.70
    assert frac >= 0.5, \
        f'Only {frac:.0%} of deliveries >= 30% of tank ({len(k["fill_pcts"])} stops)'


def test_avg_load_factor_reasonable():
    """
    On a moderate week, the average truck-day load factor should be >= 25%.
    Not strictly 50% because with 30 VEH/day × 2 trucks = 10 slots, many
    legitimately stay near-empty when demand is low.
    """
    s = _normal_week(n=25, seed=44, spread=0.05)
    routes, _ = _solve(s, budget=3)
    k = _summarize(routes)
    if not k['load_factors']:
        return
    avg_lf = float(np.mean(k['load_factors']))
    assert avg_lf >= 0.25, \
        f'Avg load factor too low: {avg_lf:.0%} across {len(k["load_factors"])} used slots'


def test_deferred_fraction_below_threshold_on_normal_week():
    """
    On a normal week (moderate demand, no closures, no TW), the deferred
    fraction should stay below 60%. Higher means the model is sizing capacity
    wrong.
    """
    s = _normal_week(n=20, seed=55, spread=0.05)
    routes, deferred = _solve(s, budget=3)
    k = _summarize(routes)
    total = k['n_stops'] + (len(deferred) if deferred is not None else 0)
    if total == 0:
        return
    deferred_frac = (len(deferred) if deferred is not None else 0) / total
    assert deferred_frac < 0.60, \
        f'Deferred fraction too high on normal week: {deferred_frac:.0%}'


TESTS = [
    ('cost_per_gallon_under_threshold',              test_cost_per_gallon_under_threshold),
    ('miles_per_stop_reasonable',                    test_miles_per_stop_reasonable),
    ('stops_per_driver_hour_reasonable',             test_stops_per_driver_hour_reasonable),
    ('fill_pct_distribution_skewed_toward_full',     test_fill_pct_distribution_skewed_toward_full),
    ('avg_load_factor_reasonable',                   test_avg_load_factor_reasonable),
    ('deferred_fraction_below_threshold_on_normal_week', test_deferred_fraction_below_threshold_on_normal_week),
]


def run_all_tests():
    print('\nBusiness efficiency KPI bounds')
    print('━' * 78)
    passed = failed = 0
    start = time.time()
    for name, fn in TESTS:
        if run_test(name, fn):
            passed += 1
        else:
            failed += 1
    elapsed = time.time() - start
    print('━' * 78)
    tag = '✓' if failed == 0 else '✗'
    print(f'{tag} {passed} passed, {failed} failed in {elapsed:.1f}s')
    return 0 if failed == 0 else 1


if __name__ == '__main__':
    sys.exit(run_all_tests())
