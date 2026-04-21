"""
test_determinism.py — Solver determinism and input immutability.

Two runs with identical inputs must produce identical scheduled sets
(order-independent). The solver must not mutate the input DataFrames.
"""

import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import TRUCKS, DAYS, NUM_DAYS, PRODUCTS
from unified_solver import solve_week


def _scenario():
    clients = []
    for i in range(10):
        clients.append({
            'ID': f'C{i:03d}', 'Customer': f'Client {i}',
            'Lat': 33.51 + i * 0.015, 'Lon': -112.16 + (i // 3) * 0.02,
            'Tank_lbs': 5000, 'Product': PRODUCTS[0],
            'Avg_LbsPerDay': 200, 'Days_Since_Last': 5,
        })
    df = pd.DataFrame(clients)
    df['Refill_lbs']          = 2000
    df['Current_lbs']         = 2500
    df['Est_Current_lbs']     = df['Current_lbs']
    df['Days_Until_Stockout'] = 6.0
    df['Urgency']             = 'normal'
    df['Refill_Today_lbs']    = 2000
    df['Fill_Pct_Today']      = 0.40

    n_nodes = 11
    dist = np.zeros((n_nodes, n_nodes), dtype=int)
    tm   = np.zeros((n_nodes, n_nodes), dtype=int)
    for i in range(n_nodes):
        for j in range(n_nodes):
            if i != j:
                dist[i, j] = abs(i - j) * 1100
                tm[i, j]   = abs(i - j) * 3

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


def _solve(s, seconds=4):
    return solve_week(
        s['clients_df'], s['dist_matrix'], s['time_matrix'], s['node_index_map'],
        start_day=0, solve_seconds=seconds,
        time_windows_df=s['time_windows_df'],
        closures_df=s['closures_df'],
        today=pd.Timestamp('2026-04-14'),
        depot_config=s['depot_config'],
    )


def _scheduled(routes):
    parts = [r for r in routes.values() if not r.empty]
    return set() if not parts else set(pd.concat(parts)['ID'])


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

def test_determinism_scheduled_set_equal():
    s1 = _scenario()
    r1, d1 = _solve(s1)
    s2 = _scenario()
    r2, d2 = _solve(s2)
    sch1 = _scheduled(r1)
    sch2 = _scheduled(r2)
    assert sch1 == sch2, f"sched differs: run1={sch1} run2={sch2}"

def test_clients_df_not_mutated():
    s = _scenario()
    before_ids = list(s['clients_df']['ID'])
    before_lbs = s['clients_df']['Refill_lbs'].copy()
    _ = _solve(s)
    after_ids = list(s['clients_df']['ID'])
    after_lbs = s['clients_df']['Refill_lbs']
    assert before_ids == after_ids
    assert (before_lbs == after_lbs).all(), "Refill_lbs mutated"

def test_time_windows_df_not_mutated():
    s = _scenario()
    # Add a window
    s['time_windows_df'] = pd.DataFrame([
        {'Client_ID': 'C001', 'Day_of_Week': 'Tue', 'Open_Min': 420, 'Close_Min': 900},
    ])
    before_len = len(s['time_windows_df'])
    before_cid = s['time_windows_df']['Client_ID'].iloc[0]
    _ = _solve(s)
    assert len(s['time_windows_df']) == before_len
    assert s['time_windows_df']['Client_ID'].iloc[0] == before_cid


TESTS = [
    ('determinism_scheduled_set_equal',  test_determinism_scheduled_set_equal),
    ('clients_df_not_mutated',           test_clients_df_not_mutated),
    ('time_windows_df_not_mutated',      test_time_windows_df_not_mutated),
]


def run_all_tests():
    print('\nDeterminism & Immutability Tests')
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
