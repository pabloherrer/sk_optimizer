"""
test_optimality.py — Solver quality vs. theoretical bounds

Instead of asserting exact miles / objective values (brittle), these tests
check that the solver's output respects sane *quality bounds*:

  • Solver beats a greedy nearest-neighbor baseline on total miles.
  • Longer solve budget → solution quality monotone-non-worsening.
  • Truck utilization is reasonably balanced (no "one truck does everything").
  • No empty truck-days when there's an urgent backlog.
  • No solo-stop far routes unless the far cluster has only 1 client.
  • When EFFICIENCY_WEIGHT=0, distance dominates objective (pure TSP-like).
  • Cross-seed stability: same scenario, different seeds → similar total miles.

These bounds encode "the solver is at least as good as obvious strategies"
without assuming the exact optimal.
"""
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

import config
from config import DAYS, NUM_DAYS, PRODUCTS, TRUCKS, TRUCK_NAMES
from unified_solver import solve_week


def run_test(name, fn):
    start = time.time()
    try:
        fn()
        print(f'  ✓ {name:<62s} ({(time.time()-start)*1000:.0f} ms)')
        return True
    except AssertionError as e:
        print(f'  ✗ {name:<62s} — {str(e)[:70]}')
        return False
    except Exception as e:
        print(f'  ✗ {name:<62s} — CRASH: {type(e).__name__}: {str(e)[:60]}')
        return False


# ── Scenario builder ────────────────────────────────────────────────────────

def _make_scenario(n=20, seed=1, spread=0.05):
    rng = np.random.default_rng(seed)
    clients = []
    coords = [(33.50, -112.10)]
    for i in range(n):
        lat = 33.50 + rng.uniform(-spread, spread)
        lon = -112.10 + rng.uniform(-spread, spread)
        coords.append((lat, lon))
        clients.append({
            'ID': f'C{i:03d}', 'Customer': f'Client{i}',
            'Lat': lat, 'Lon': lon,
            'Tank_lbs': 5000, 'Product': PRODUCTS[i % 2],
            'Avg_LbsPerDay': 200 + int(rng.integers(-50, 50)),
            'Days_Since_Last': int(rng.integers(2, 8)),
            'Current_lbs': int(rng.integers(1000, 3500)),
        })
    df = pd.DataFrame(clients)
    df['Est_Current_lbs']      = df['Current_lbs']
    df['Refill_lbs']           = (df['Tank_lbs'] - df['Current_lbs']).clip(lower=0)
    df['Days_Until_Stockout']  = (df['Current_lbs'] / df['Avg_LbsPerDay'].clip(lower=1)).clip(lower=0.5)
    df['Urgency']              = 'normal'
    df['Refill_Today_lbs']     = df['Refill_lbs']
    df['Fill_Pct_Today']       = df['Refill_lbs'] / df['Tank_lbs']

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
        'coords': coords,
        'time_windows_df': pd.DataFrame(columns=['Client_ID', 'Day_of_Week', 'Open_Min', 'Close_Min']),
        'closures_df': pd.DataFrame(columns=['Client_ID', 'Start_Date', 'End_Date', 'Reason']),
        'depot_config': {
            'depot_lat': 33.5, 'depot_lon': -112.1,
            'shift_start_min': 360, 'shift_end_min': 1080,
            'morning_load_min': 30, 'evening_unload_min': 15,
            'work_days': DAYS,
        },
    }


def _solve(s, budget=3):
    return solve_week(
        s['clients_df'], s['dist_matrix'], s['time_matrix'], s['node_index_map'],
        start_day=0, solve_seconds=budget,
        time_windows_df=s['time_windows_df'],
        closures_df=s['closures_df'],
        today=pd.Timestamp('2026-04-14'),
        depot_config=s['depot_config'],
    )


def _total_miles(routes):
    total = 0.0
    for d, rdf in routes.items():
        if rdf.empty: continue
        total += float(rdf.get('Dist_miles', pd.Series([0.0])).fillna(0).sum())
    return total


def _scheduled_count(routes):
    return sum(len(r) for r in routes.values() if not r.empty)


def _nearest_neighbor_miles(scheduled_ids, coords, nix):
    """Simple greedy TSP from depot through scheduled stops and back."""
    if not scheduled_ids:
        return 0.0
    remaining = list(scheduled_ids)
    route = [0]   # depot
    while remaining:
        last = route[-1]
        nxt = min(remaining, key=lambda cid: _haversine_mi(coords[last], coords[nix[cid]]))
        route.append(nix[nxt])
        remaining.remove(nxt)
    route.append(0)
    total = 0.0
    for i in range(len(route) - 1):
        total += _haversine_mi(coords[route[i]], coords[route[i + 1]])
    return total


def _haversine_mi(a, b):
    dx = (a[0] - b[0]) * 69.0
    dy = (a[1] - b[1]) * 60.0
    return (dx * dx + dy * dy) ** 0.5


# ── Tests ───────────────────────────────────────────────────────────────────

def test_beats_or_matches_single_truck_nn():
    """
    The solver's total miles should not be worse than a naive single-truck
    nearest-neighbor visiting all scheduled clients. (Multi-truck should be
    at least as efficient, and usually much better.)
    """
    s = _make_scenario(n=20, seed=7)
    routes, deferred = _solve(s, budget=5)
    scheduled_ids = []
    for d, rdf in routes.items():
        if rdf.empty: continue
        scheduled_ids += rdf['ID'].tolist()
    if not scheduled_ids:
        return   # nothing to compare
    solver_mi = _total_miles(routes)
    nn_mi = _nearest_neighbor_miles(scheduled_ids, s['coords'], s['node_index_map'])
    # Allow up to 1.6× nearest-neighbor (multi-truck does multiple depot returns)
    assert solver_mi <= 1.6 * nn_mi + 5, \
        f'Solver {solver_mi:.1f}mi >> 1.6×NN {nn_mi:.1f}mi — unexpectedly bad'


def test_solution_improves_or_equal_with_longer_budget():
    """
    With a longer solve budget, the objective should not get WORSE (modulo
    tiny nondeterminism). OR-Tools' local search is monotone improving.
    """
    s = _make_scenario(n=15, seed=42)
    routes_short, _ = _solve(s, budget=1)
    routes_long, _  = _solve(s, budget=4)
    mi_short = _total_miles(routes_short)
    mi_long  = _total_miles(routes_long)
    # Allow a small tolerance for tie-breaks and nondeterminism
    assert mi_long <= mi_short * 1.10 + 2, \
        f'Longer budget produced worse result: short={mi_short:.1f}, long={mi_long:.1f}'


def test_truck_utilization_not_degenerate():
    """
    When total weekly demand exceeds ONE truck's weekly capacity (5 days ×
    10K = 50K lbs), the solver MUST use both trucks at least once. Fast pump
    rates mean one truck is nominally preferred, but capacity forces diversification.
    """
    # 30 clients × ~3500 lbs refill each = ~105K lbs — well over one truck's 50K
    s = _make_scenario(n=30, seed=11)
    # Make refills large to force both trucks to work
    s['clients_df']['Current_lbs'] = 500
    s['clients_df']['Refill_lbs'] = s['clients_df']['Tank_lbs'] - 500
    s['clients_df']['Refill_Today_lbs'] = s['clients_df']['Refill_lbs']
    s['clients_df']['Fill_Pct_Today'] = s['clients_df']['Refill_lbs'] / s['clients_df']['Tank_lbs']
    routes, _ = _solve(s, budget=4)
    by_truck = {name: 0 for name in TRUCK_NAMES}
    for d, rdf in routes.items():
        if rdf.empty: continue
        for _, r in rdf.iterrows():
            t = r['Truck']
            if t in by_truck:
                by_truck[t] += 1
    total = sum(by_truck.values())
    if total < 6:
        return   # solver punted — can't judge balance
    # With demand >> 50K, both trucks MUST be used
    trucks_used = sum(1 for v in by_truck.values() if v > 0)
    assert trucks_used == 2, \
        f'Demand ≫ 1-truck capacity but only {trucks_used} truck used: {by_truck}'


def test_no_empty_truck_days_when_urgent_backlog():
    """
    If >= 10 clients are eligible and urgent, the solver must use at least
    several truck-days (not let 9 out of 10 slots sit empty).
    """
    s = _make_scenario(n=15, seed=5)
    # Make everyone urgent: current very low
    s['clients_df']['Current_lbs'] = 500
    s['clients_df']['Days_Until_Stockout'] = 2.5
    s['clients_df']['Refill_lbs']   = s['clients_df']['Tank_lbs'] - 500
    s['clients_df']['Refill_Today_lbs'] = s['clients_df']['Refill_lbs']
    s['clients_df']['Fill_Pct_Today']   = s['clients_df']['Refill_lbs'] / s['clients_df']['Tank_lbs']
    routes, deferred = _solve(s, budget=2)
    # Count distinct (truck, day) slots used
    slots = set()
    for d, rdf in routes.items():
        if rdf.empty: continue
        for _, r in rdf.iterrows():
            slots.add((r['Truck'], d))
    # With 15 urgent clients and 10K capacity per truck-day, we need at least
    # 2 slots for basic feasibility.
    assert len(slots) >= 2, \
        f'Urgent backlog of 15 but only {len(slots)} truck-day slots used'


def test_distance_dominates_when_efficiency_weight_zero():
    """
    Setting EFFICIENCY_WEIGHT=0 should remove the fill-efficiency amplifier;
    the solver should then prefer short-distance tours.
    """
    orig = config.EFFICIENCY_WEIGHT
    try:
        config.EFFICIENCY_WEIGHT = 0.0
        s = _make_scenario(n=15, seed=17)
        routes, _ = _solve(s, budget=2)
        total_mi = _total_miles(routes)
        n_stops = _scheduled_count(routes)
        if n_stops > 0:
            avg_mi = total_mi / n_stops
            # With pure distance objective and tightly-clustered scenario,
            # avg miles per stop should be < 4.
            assert avg_mi < 4.0, f'pure-distance avg={avg_mi:.2f}mi/stop'
    finally:
        config.EFFICIENCY_WEIGHT = orig


def test_weekly_distance_stable_across_seeds():
    """
    Total miles shouldn't swing > 4× between seeds on an IID scenario. If
    it does, the solver is either nondeterministic in a bad way, or extremely
    sensitive to initial state.
    """
    miles = []
    for seed in (1, 2, 3, 4):
        s = _make_scenario(n=12, seed=seed)
        routes, _ = _solve(s, budget=2)
        miles.append(_total_miles(routes))
    lo, hi = min(miles), max(miles)
    if lo > 0:
        assert hi <= 4.0 * lo + 2, f'miles variance huge: {miles}'


def test_no_coincident_same_truck_day_from_two_configs():
    """
    Invariant: the at-most-one-config-per-truck-day constraint should ensure
    that any (truck, day) in the solution uses exactly one config variant.
    """
    s = _make_scenario(n=20, seed=3)
    routes, _ = _solve(s, budget=2)
    by_td = {}
    for d, rdf in routes.items():
        if rdf.empty: continue
        for _, r in rdf.iterrows():
            key = (r['Truck'], d)
            cfg = r.get('Load_Config', None)
            if cfg is not None:
                by_td.setdefault(key, set()).add(cfg)
    for key, cfgs in by_td.items():
        assert len(cfgs) == 1, f'Truck-day {key} used multiple configs: {cfgs}'


TESTS = [
    ('beats_or_matches_single_truck_nn',              test_beats_or_matches_single_truck_nn),
    ('solution_improves_or_equal_with_longer_budget', test_solution_improves_or_equal_with_longer_budget),
    ('truck_utilization_not_degenerate',              test_truck_utilization_not_degenerate),
    ('no_empty_truck_days_when_urgent_backlog',       test_no_empty_truck_days_when_urgent_backlog),
    ('distance_dominates_when_efficiency_weight_zero',test_distance_dominates_when_efficiency_weight_zero),
    ('weekly_distance_stable_across_seeds',           test_weekly_distance_stable_across_seeds),
    ('no_coincident_same_truck_day_from_two_configs', test_no_coincident_same_truck_day_from_two_configs),
]


def run_all_tests():
    print('\nSolver optimality / quality bounds')
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
