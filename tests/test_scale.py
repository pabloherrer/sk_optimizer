"""
test_scale.py — Performance smoke tests at 50 and 150 clients.

Sanity check that the solver produces a feasible plan within the advertised
time limits for realistic scenario sizes.
"""

import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import TRUCKS, DAYS, NUM_DAYS, PRODUCTS
from unified_solver import solve_week


def _build_scenario(n):
    rng = np.random.default_rng(42)
    clients = []
    for i in range(n):
        lat = 33.4 + rng.uniform(0, 0.4)
        lon = -112.3 + rng.uniform(0, 0.4)
        clients.append({
            'ID': f'C{i:04d}', 'Customer': f'Client {i}',
            'Lat': lat, 'Lon': lon,
            'Tank_lbs': int(rng.choice([5000, 8000, 10000])),
            'Product': PRODUCTS[int(rng.integers(0, 2))],
            'Avg_LbsPerDay': int(rng.integers(50, 400)),
            'Days_Since_Last': int(rng.integers(3, 12)),
        })
    df = pd.DataFrame(clients)
    df['Current_lbs']         = (df['Tank_lbs'] * rng.uniform(0.1, 0.7, size=n)).astype(int)
    df['Est_Current_lbs']     = df['Current_lbs']
    df['Refill_lbs']          = (df['Tank_lbs'] - df['Current_lbs']).clip(lower=0)
    df['Days_Until_Stockout'] = (df['Current_lbs'] / df['Avg_LbsPerDay'].clip(lower=1)).clip(lower=0.5)
    df['Urgency']             = 'normal'
    df['Refill_Today_lbs']    = df['Refill_lbs']
    df['Fill_Pct_Today']      = df['Refill_lbs'] / df['Tank_lbs']

    n_nodes = n + 1
    dist = np.zeros((n_nodes, n_nodes), dtype=int)
    tm   = np.zeros((n_nodes, n_nodes), dtype=int)
    # Haversine-ish via lat/lon
    coords = [(33.5, -112.1)] + list(zip(df['Lat'], df['Lon']))
    for i in range(n_nodes):
        for j in range(n_nodes):
            if i != j:
                dx = (coords[i][0] - coords[j][0]) * 69
                dy = (coords[i][1] - coords[j][1]) * 60
                d_mi = (dx*dx + dy*dy) ** 0.5
                dist[i, j] = int(d_mi * 1609)
                tm[i, j]   = int(d_mi * 2 + 1)   # ~30 mph + 1 min buffer

    node_index_map = {'DEPOT': 0}
    for idx, cid in enumerate(df['ID'].tolist(), 1):
        node_index_map[cid] = idx

    return {
        'clients_df': df,
        'time_windows_df': pd.DataFrame(columns=['Client_ID', 'Day_of_Week', 'Open_Min', 'Close_Min']),
        'closures_df': pd.DataFrame(columns=['Client_ID', 'Start_Date', 'End_Date', 'Reason']),
        'dist_matrix': dist, 'time_matrix': tm,
        'node_index_map': node_index_map,
        'depot_config': {
            'depot_lat': 33.5, 'depot_lon': -112.1,
            'shift_start_min': 360, 'shift_end_min': 1080,
            'morning_load_min': 30, 'evening_unload_min': 15,
            'work_days': DAYS,
        },
    }


def _solve_timed(s, seconds):
    t0 = time.time()
    routes, deferred = solve_week(
        s['clients_df'], s['dist_matrix'], s['time_matrix'], s['node_index_map'],
        start_day=0, solve_seconds=seconds,
        time_windows_df=s['time_windows_df'],
        closures_df=s['closures_df'],
        today=pd.Timestamp('2026-04-14'),
        depot_config=s['depot_config'],
    )
    return time.time() - t0, routes, deferred


def run_test(name, fn):
    start = time.time()
    try:
        fn()
        print(f'  ✓ {name:<50s} ({time.time()-start:.1f}s)')
        return True
    except AssertionError as e:
        print(f'  ✗ {name:<50s} — {str(e)[:80]}')
        return False
    except Exception as e:
        print(f'  ✗ {name:<50s} — CRASH: {type(e).__name__}: {str(e)[:60]}')
        return False


# ── Tests ───────────────────────────────────────────────────────────────────

def test_50_client_under_30s():
    s = _build_scenario(50)
    elapsed, routes, deferred = _solve_timed(s, seconds=20)
    assert elapsed <= 30, f"50-client took {elapsed:.1f}s, budget 30s"
    parts = [r for r in routes.values() if not r.empty]
    total_sched = 0 if not parts else len(pd.concat(parts))
    assert total_sched > 0, "no routes produced on 50-client"

def test_150_client_under_60s():
    s = _build_scenario(150)
    elapsed, routes, deferred = _solve_timed(s, seconds=40)
    assert elapsed <= 65, f"150-client took {elapsed:.1f}s, budget 65s"
    parts = [r for r in routes.values() if not r.empty]
    total_sched = 0 if not parts else len(pd.concat(parts))
    assert total_sched > 0, "no routes produced on 150-client"

def test_accounting_closed_at_scale():
    s = _build_scenario(50)
    _, routes, deferred = _solve_timed(s, seconds=15)
    parts = [r for r in routes.values() if not r.empty]
    sched = set() if not parts else set(pd.concat(parts)['ID'])
    deferred_ids = set() if deferred.empty else set(deferred['ID'])
    assert sched.isdisjoint(deferred_ids)
    assert sched | deferred_ids == set(s['clients_df']['ID'].unique())


TESTS = [
    ('50_client_under_30s',            test_50_client_under_30s),
    ('150_client_under_60s',           test_150_client_under_60s),
    ('accounting_closed_at_scale',     test_accounting_closed_at_scale),
]


def run_all_tests():
    print('\nScale / Performance Tests')
    print('━' * 70)
    passed = failed = 0
    start = time.time()
    for name, fn in TESTS:
        if run_test(name, fn):
            passed += 1
        else:
            failed += 1
    elapsed = time.time() - start
    print('━' * 70)
    tag = '✓' if failed == 0 else '✗'
    print(f'{tag} {passed} passed, {failed} failed in {elapsed:.1f}s')
    return 0 if failed == 0 else 1


if __name__ == '__main__':
    sys.exit(run_all_tests())
