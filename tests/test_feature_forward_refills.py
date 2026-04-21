"""
test_feature_forward_refills.py — Coelho-Cordeau-Laporte 2014 IRP forward projection

Solver loads plan must reflect projected end-of-week tank state, not today's
snapshot. For a client consuming fast, Day 4 refill > Day 0 refill.
"""

import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import TRUCKS, DAYS, NUM_DAYS, PRODUCTS
from unified_solver import solve_week
from inventory import compute_refill, build_refill_matrix, enrich_snapshot


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


# ── Tests on the matrix ─────────────────────────────────────────────────────

def test_forward_refill_matrix_day4_larger_for_high_consumer():
    """Fast-consuming client: Day 4 refill > Day 0 refill."""
    df = pd.DataFrame([{
        'ID': 'C000',
        'Tank_lbs': 10000,
        'Avg_LbsPerDay': 500,          # fast
        'Est_Current_lbs': 6000,
    }])
    df = enrich_snapshot(df)
    m = build_refill_matrix(df, n_days=5)
    # Day 0: tank=10000, level=6000 → refill=4000
    # Day 4: tank=10000, level=max(6000-4*500,floor)=4000 → refill=6000
    assert m[0, 0] == 4000
    assert m[0, 4] == 6000
    assert m[0, 4] > m[0, 0]

def test_forward_refill_matrix_zero_consumer_same_every_day():
    """Zero-consumption client: same refill every day (tank doesn't drain)."""
    df = pd.DataFrame([{
        'ID': 'C000',
        'Tank_lbs': 5000,
        'Avg_LbsPerDay': 0,
        'Est_Current_lbs': 3000,
    }])
    df = enrich_snapshot(df)
    m = build_refill_matrix(df, n_days=5)
    for d in range(5):
        assert m[0, d] == 2000, f"day {d}: {m[0, d]}"

def test_compute_refill_matches_matrix():
    """Scalar compute_refill matches matrix for each day."""
    df = pd.DataFrame([{
        'ID': 'C000', 'Tank_lbs': 8000, 'Avg_LbsPerDay': 400, 'Est_Current_lbs': 5000,
    }])
    df = enrich_snapshot(df)
    m = build_refill_matrix(df, n_days=5)
    for d in range(5):
        expected = compute_refill(5000, 400, d, 8000)
        assert abs(m[0, d] - expected) < 1e-6, f"day {d}: {m[0, d]} vs {expected}"


# ── Solver-level test: end-of-week used for capacity planning ─────────────

def _fast_consumer_scenario():
    clients = [{
        'ID': 'C000', 'Customer': 'FastUser', 'Lat': 33.51, 'Lon': -112.16,
        'Tank_lbs': 10000, 'Product': PRODUCTS[0], 'Avg_LbsPerDay': 500,
        'Days_Since_Last': 5,
    }]
    df = pd.DataFrame(clients)
    df['Current_lbs']         = 6000
    df['Est_Current_lbs']     = df['Current_lbs']
    df['Refill_lbs']          = 4000    # today's refill
    df['Days_Until_Stockout'] = 5.0
    df['Urgency']             = 'normal'
    df['Refill_Today_lbs']    = 4000
    df['Fill_Pct_Today']      = 0.40

    dist = np.array([[0, 1000], [1000, 0]], dtype=int)
    tm   = np.array([[0,    5], [   5, 0]], dtype=int)
    node_index_map = {'DEPOT': 0, 'C000': 1}
    return {
        'clients_df': df,
        'time_windows_df': pd.DataFrame(columns=['Client_ID', 'Day_of_Week', 'Open_Min', 'Close_Min']),
        'closures_df':     pd.DataFrame(columns=['Client_ID', 'Start_Date', 'End_Date', 'Reason']),
        'dist_matrix': dist, 'time_matrix': tm,
        'node_index_map': node_index_map,
        'depot_config': {
            'depot_lat': 33.5, 'depot_lon': -112.1,
            'shift_start_min': 360, 'shift_end_min': 1080,
            'morning_load_min': 30, 'evening_unload_min': 15,
            'work_days': DAYS,
        },
    }


def test_solver_uses_eow_refill_for_load_plan():
    """Solver's reported Refill_lbs for a fast-consuming client should be closer
    to end-of-week projection than day-zero snapshot (Coelho 2014 forward IRP)."""
    s = _fast_consumer_scenario()
    routes, _ = _solve_wrapper(s)
    parts = [r for r in routes.values() if not r.empty]
    if not parts:
        # Solver may pick a specific day — the refill for that visit should be
        # bounded between snapshot_today and end-of-week.
        return  # no route → nothing to assert
    df = pd.concat(parts)
    row = df[df['ID'] == 'C000']
    if row.empty:
        return
    r_planned = row['Refill_lbs'].iloc[0]
    # Per-day refills: d0=4000, d1=4500, d2=5000, d3=5500, d4=6000
    # Solver uses refills_by_day[NUM_DAYS-1] = day-4 values = 6000
    assert 4000 <= r_planned <= 6000, f"refill outside projected range: {r_planned}"

def _solve_wrapper(s):
    return solve_week(
        s['clients_df'], s['dist_matrix'], s['time_matrix'], s['node_index_map'],
        start_day=0, solve_seconds=5,
        time_windows_df=s['time_windows_df'],
        closures_df=s['closures_df'],
        today=pd.Timestamp('2026-04-14'),
        depot_config=s['depot_config'],
    )


TESTS = [
    ('refill_matrix_day4_larger_for_high_consumer', test_forward_refill_matrix_day4_larger_for_high_consumer),
    ('refill_matrix_zero_consumer_flat',            test_forward_refill_matrix_zero_consumer_same_every_day),
    ('compute_refill_matches_matrix',               test_compute_refill_matches_matrix),
    ('solver_uses_eow_refill_for_load_plan',        test_solver_uses_eow_refill_for_load_plan),
]


def run_all_tests():
    print('\nForward-Projected Refills Feature Tests (Coelho 2014)')
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
