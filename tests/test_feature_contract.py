"""
test_feature_contract.py — 14-day contract (Aksen, Kaya, Salman & Akça 2012 SIRP)

The solver must elevate a client whose Days_Since_Last + NUM_DAYS exceeds
MAX_SERVICE_INTERVAL_DAYS to a mandatory visit this week.
"""

import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import (
    TRUCKS, DAYS, NUM_DAYS, PRODUCTS, MAX_SERVICE_INTERVAL_DAYS,
)
from unified_solver import solve_week


# ── Fixture helpers ─────────────────────────────────────────────────────────

def _base_scenario(n=10, days_since_list=None):
    """Small scenario where one client is contract-overdue.

    days_since_list: list of Days_Since_Last values per client.
    """
    if days_since_list is None:
        days_since_list = [3] * n  # everyone recently served

    clients = []
    for i in range(n):
        clients.append({
            'ID': f'C{i:03d}',
            'Customer': f'Client {i}',
            'Lat': 33.51 + i * 0.02,
            'Lon': -112.16 + i * 0.02,
            'Tank_lbs': 5000,
            'Product': PRODUCTS[0],
            'Avg_LbsPerDay': 100,   # slow → low urgency
            'Days_Since_Last': days_since_list[i],
            'Days_Since_Used': days_since_list[i],
        })

    df = pd.DataFrame(clients)
    df['Refill_lbs']          = 1000
    df['Current_lbs']         = df['Tank_lbs'] * 0.9   # nearly full → not urgent
    df['Est_Current_lbs']     = df['Current_lbs']
    df['Days_Until_Stockout'] = 30.0                    # plenty of runway
    df['Urgency']             = 'normal'
    df['Refill_Today_lbs']    = 500
    df['Fill_Pct_Today']      = 0.10

    n_nodes = n + 1
    dist = np.zeros((n_nodes, n_nodes), dtype=int)
    tm   = np.zeros((n_nodes, n_nodes), dtype=int)
    for i in range(n_nodes):
        for j in range(n_nodes):
            if i != j:
                dist[i, j] = abs(i - j) * 1500
                tm[i, j]   = abs(i - j) * 4

    node_index_map = {'DEPOT': 0}
    for idx, cid in enumerate(df['ID'].tolist(), 1):
        node_index_map[cid] = idx

    return {
        'clients_df': df,
        'time_windows_df': pd.DataFrame(columns=['Client_ID', 'Day_of_Week', 'Open_Min', 'Close_Min']),
        'closures_df': pd.DataFrame(columns=['Client_ID', 'Start_Date', 'End_Date', 'Reason']),
        'dist_matrix': dist,
        'time_matrix': tm,
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
        start_day=0, solve_seconds=6,
        time_windows_df=s['time_windows_df'],
        closures_df=s['closures_df'],
        today=pd.Timestamp('2026-04-14'),
        depot_config=s['depot_config'],
    )


def _scheduled_ids(routes):
    parts = [r for r in routes.values() if not r.empty]
    return set() if not parts else set(pd.concat(parts)['ID'])


def run_test(name, fn):
    start = time.time()
    try:
        fn()
        elapsed = time.time() - start
        print(f'  ✓ {name:<50s} ({elapsed:.1f}s)')
        return True
    except AssertionError as e:
        print(f'  ✗ {name:<50s} — {str(e)[:80]}')
        return False
    except Exception as e:
        print(f'  ✗ {name:<50s} — CRASH: {type(e).__name__}: {str(e)[:60]}')
        return False


# ── Tests ───────────────────────────────────────────────────────────────────

def test_contract_overdue_forced_in_plan():
    """Days_Since_Last=13 with low tank urgency → still scheduled."""
    n = 8
    days = [3] * n
    days[0] = 13   # C000 is contract-overdue by end of week (13+5 >= 14)
    s = _base_scenario(n=n, days_since_list=days)
    routes, deferred = _solve(s)
    sched = _scheduled_ids(routes)
    assert 'C000' in sched, f"contract-overdue client not scheduled: sched={sched}"

def test_contract_not_overdue_may_skip():
    """Days_Since_Last=3 → no contract escalation; may or may not be scheduled.
    We only assert the solver does NOT crash, since normal-priority skipping
    is allowed when demand is dense. This test is primarily a regression."""
    n = 8
    s = _base_scenario(n=n, days_since_list=[3] * n)
    routes, deferred = _solve(s)
    # Should not crash, should produce accounting
    sched = _scheduled_ids(routes)
    deferred_ids = set() if deferred.empty else set(deferred['ID'])
    assert len(sched) + len(deferred_ids) == n

def test_contract_boundary_escalation_fires():
    """Days_Since_Last + NUM_DAYS == MAX_SERVICE_INTERVAL_DAYS exactly → fires."""
    boundary = MAX_SERVICE_INTERVAL_DAYS - NUM_DAYS  # = 9 with defaults
    n = 8
    days = [3] * n
    days[3] = boundary
    s = _base_scenario(n=n, days_since_list=days)
    routes, deferred = _solve(s)
    sched = _scheduled_ids(routes)
    assert 'C003' in sched, f"boundary client C003 (Days_Since_Last={boundary}) not scheduled"

def test_contract_beyond_boundary_fires():
    """Days_Since_Last > boundary → definitely must be served."""
    n = 8
    days = [3] * n
    days[2] = MAX_SERVICE_INTERVAL_DAYS + 1   # already over contract
    s = _base_scenario(n=n, days_since_list=days)
    routes, deferred = _solve(s)
    sched = _scheduled_ids(routes)
    assert 'C002' in sched, "client already over contract not scheduled"

def test_contract_under_boundary_no_escalation_log():
    """Days_Since_Last + NUM_DAYS < MAX_SERVICE_INTERVAL_DAYS → no escalation."""
    # Days_Since_Last=8 → 8+5=13 < 14 → NO escalation
    n = 5
    days = [8] * n
    s = _base_scenario(n=n, days_since_list=days)
    routes, deferred = _solve(s)
    sched = _scheduled_ids(routes)
    deferred_ids = set() if deferred.empty else set(deferred['ID'])
    # Just verify accounting — may or may not serve them
    assert len(sched) + len(deferred_ids) == n


TESTS = [
    ('contract_overdue_forced_in_plan',        test_contract_overdue_forced_in_plan),
    ('contract_not_overdue_may_skip',          test_contract_not_overdue_may_skip),
    ('contract_boundary_escalation_fires',     test_contract_boundary_escalation_fires),
    ('contract_beyond_boundary_fires',         test_contract_beyond_boundary_fires),
    ('contract_under_boundary_no_escalation',  test_contract_under_boundary_no_escalation_log),
]


def run_all_tests():
    print('\n14-Day Contract Feature Tests (Aksen 2012)')
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
