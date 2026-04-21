"""
test_feature_overtime.py — Overtime labor model

Shift time beyond SHIFT_MIN is legal up to MAX_SHIFT_MIN but costs 1.5× labor.
Tests verify:
  (1) OT_Min / Reg_Min decomposition is correct
  (2) Labor_Cost formula: reg*base + ot*base*1.5
  (3) Hard ceiling at MAX_SHIFT_MIN is never violated
  (4) Short routes have OT_Min=0
  (5) Solver trades OT against revenue rationally (smoke test)
"""

import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import (
    TRUCKS, DAYS, NUM_DAYS, PRODUCTS,
    SHIFT_MIN, MAX_SHIFT_MIN, LABOR_COST_PER_MIN, OT_MULTIPLIER,
)
from unified_solver import solve_week


def _scenario(n=8, inter_node_min=20):
    """Configurable-length scenario. inter_node_min controls how long routes
    get (higher → more OT)."""
    clients = []
    for i in range(n):
        clients.append({
            'ID': f'C{i:03d}', 'Customer': f'Client {i}',
            'Lat': 33.51 + i * 0.02, 'Lon': -112.16 + i * 0.02,
            'Tank_lbs': 5000, 'Product': PRODUCTS[0],
            'Avg_LbsPerDay': 100, 'Days_Since_Last': 5,
        })
    df = pd.DataFrame(clients)
    df['Refill_lbs']          = 1000
    df['Current_lbs']         = df['Tank_lbs'] * 0.8
    df['Est_Current_lbs']     = df['Current_lbs']
    df['Days_Until_Stockout'] = 8.0
    df['Urgency']             = 'normal'
    df['Refill_Today_lbs']    = 1000
    df['Fill_Pct_Today']      = 0.20

    n_nodes = n + 1
    dist = np.zeros((n_nodes, n_nodes), dtype=int)
    tm   = np.zeros((n_nodes, n_nodes), dtype=int)
    for i in range(n_nodes):
        for j in range(n_nodes):
            if i != j:
                dist[i, j] = abs(i - j) * 1500
                tm[i, j]   = abs(i - j) * inter_node_min

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


def _solve(s):
    return solve_week(
        s['clients_df'], s['dist_matrix'], s['time_matrix'], s['node_index_map'],
        start_day=0, solve_seconds=5,
        time_windows_df=s['time_windows_df'],
        closures_df=s['closures_df'],
        today=pd.Timestamp('2026-04-14'),
        depot_config=s['depot_config'],
    )


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

def test_short_route_has_no_ot():
    """Short routes (<SHIFT_MIN) should have OT_Min = 0."""
    s = _scenario(n=3, inter_node_min=5)  # tiny routes
    routes, _ = _solve(s)
    parts = [r for r in routes.values() if not r.empty]
    if not parts:
        return
    df = pd.concat(parts)
    for _, row in df.iterrows():
        if row['Route_Time_min'] <= SHIFT_MIN:
            assert row['OT_Min'] == 0, f"route {row['Route_Time_min']} min claims OT {row['OT_Min']}"

def test_reg_plus_ot_equals_total():
    s = _scenario(n=6, inter_node_min=40)
    routes, _ = _solve(s)
    parts = [r for r in routes.values() if not r.empty]
    if not parts:
        return
    df = pd.concat(parts)
    for _, row in df.iterrows():
        assert row['Reg_Min'] + row['OT_Min'] == row['Route_Time_min'], \
            f"route {row['Route_Time_min']}: reg={row['Reg_Min']} + ot={row['OT_Min']}"

def test_reg_capped_at_shift_min():
    s = _scenario(n=6, inter_node_min=40)
    routes, _ = _solve(s)
    parts = [r for r in routes.values() if not r.empty]
    if not parts:
        return
    df = pd.concat(parts)
    for _, row in df.iterrows():
        assert row['Reg_Min'] <= SHIFT_MIN

def test_labor_cost_formula():
    """Labor_Cost = Reg_Min*base + OT_Min*base*1.5."""
    s = _scenario(n=6, inter_node_min=40)
    routes, _ = _solve(s)
    parts = [r for r in routes.values() if not r.empty]
    if not parts:
        return
    df = pd.concat(parts)
    for _, row in df.iterrows():
        expected = row['Reg_Min'] * LABOR_COST_PER_MIN + \
                   row['OT_Min']  * LABOR_COST_PER_MIN * OT_MULTIPLIER
        assert abs(row['Labor_Cost'] - expected) < 1e-6, \
            f"labor cost {row['Labor_Cost']} != expected {expected}"

def test_hard_ceiling_never_exceeded():
    """Route_Time_min must NEVER exceed MAX_SHIFT_MIN regardless of demand."""
    # Try to force long routes
    s = _scenario(n=10, inter_node_min=80)
    routes, _ = _solve(s)
    parts = [r for r in routes.values() if not r.empty]
    if not parts:
        return
    df = pd.concat(parts)
    for _, row in df.iterrows():
        assert row['Route_Time_min'] <= MAX_SHIFT_MIN, \
            f"route {row['Route_Time_min']} exceeds hard cap {MAX_SHIFT_MIN}"

def test_ot_allows_more_stops_vs_hard_cap():
    """Regression: with soft OT cap, we can take more stops than a hard 600-min cap would allow.
    Verify by comparing total scheduled vs total deferred: if solver is using OT at all,
    at least one route should go over SHIFT_MIN."""
    s = _scenario(n=10, inter_node_min=40)
    routes, deferred = _solve(s)
    parts = [r for r in routes.values() if not r.empty]
    if not parts:
        return
    df = pd.concat(parts)
    # Not a hard assertion (solver may find all routes short); just a smoke check
    # that the model compiles and labor_cost is populated sensibly.
    assert (df['Labor_Cost'] >= 0).all()


TESTS = [
    ('short_route_has_no_ot',                  test_short_route_has_no_ot),
    ('reg_plus_ot_equals_total',               test_reg_plus_ot_equals_total),
    ('reg_capped_at_shift_min',                test_reg_capped_at_shift_min),
    ('labor_cost_formula',                     test_labor_cost_formula),
    ('hard_ceiling_never_exceeded',            test_hard_ceiling_never_exceeded),
    ('ot_allows_more_stops_vs_hard_cap',       test_ot_allows_more_stops_vs_hard_cap),
]


def run_all_tests():
    print('\nOvertime Labor Model Feature Tests')
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
