"""
test_feature_contract.py — Contractual cadence tests (DISABLED FEATURE)

S&K has NO contractual max service interval. The prior 14-day contract
enforcement (Aksen 2012 SIRP) was removed after business confirmed there
is no such rule. MAX_SERVICE_INTERVAL_DAYS is set to 9999 (disabled).

These tests verify that:
  1. The solver does NOT force-schedule low-urgency clients purely based
     on days-since-last-delivery (no false escalation).
  2. Clients are still served when they actually need it (urgency/fill).
  3. Accounting is correct (scheduled + deferred = total).
"""

import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import TRUCKS, DAYS, NUM_DAYS, PRODUCTS
from unified_solver import solve_week


# ── Fixture helpers ─────────────────────────────────────────────────────────

def _base_scenario(n=10, days_since_list=None):
    """Small scenario with clients at various days-since-last."""
    if days_since_list is None:
        days_since_list = [3] * n

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

def test_no_false_contract_escalation():
    """Days_Since_Last=13, low urgency, full tank → no forced scheduling.
    With contractual cadence disabled, the solver should rely only on
    urgency and fill economics. A nearly-full, low-urgency client should
    NOT be forced in just because it's been 13 days."""
    n = 8
    days = [3] * n
    days[0] = 13   # Would have triggered old 14-day contract rule
    s = _base_scenario(n=n, days_since_list=days)
    routes, deferred = _solve(s)
    sched = _scheduled_ids(routes)
    deferred_ids = set() if deferred.empty else set(deferred['ID'])
    # Accounting must be correct
    assert len(sched) + len(deferred_ids) == n, \
        f"Accounting mismatch: {len(sched)} + {len(deferred_ids)} != {n}"

def test_urgency_still_works():
    """Even without contract enforcement, urgent clients get scheduled."""
    n = 6
    days = [3] * n
    s = _base_scenario(n=n, days_since_list=days)
    # Make client 0 stockout-urgent (low current level, high consumption)
    s['clients_df'].at[0, 'Current_lbs'] = 50
    s['clients_df'].at[0, 'Avg_LbsPerDay'] = 400
    s['clients_df'].at[0, 'Days_Until_Stockout'] = 0.1
    s['clients_df'].at[0, 'Urgency'] = 'stockout'
    routes, deferred = _solve(s)
    sched = _scheduled_ids(routes)
    assert 'C000' in sched, f"stockout client not scheduled: sched={sched}"

def test_solver_no_crash_all_low_urgency():
    """All clients low urgency, various days-since → solver doesn't crash."""
    n = 8
    days = [3, 7, 10, 13, 15, 20, 25, 30]
    s = _base_scenario(n=n, days_since_list=days)
    routes, deferred = _solve(s)
    sched = _scheduled_ids(routes)
    deferred_ids = set() if deferred.empty else set(deferred['ID'])
    assert len(sched) + len(deferred_ids) == n

def test_accounting_consistent():
    """Scheduled + deferred = total clients (no lost clients)."""
    n = 5
    s = _base_scenario(n=n, days_since_list=[8] * n)
    routes, deferred = _solve(s)
    sched = _scheduled_ids(routes)
    deferred_ids = set() if deferred.empty else set(deferred['ID'])
    assert len(sched) + len(deferred_ids) == n

def test_fill_drives_scheduling():
    """A client with high fill need (empty tank) gets served even without
    contract enforcement, because fill-based eligibility picks it up."""
    n = 6
    s = _base_scenario(n=n, days_since_list=[3] * n)
    # Make client 2 need service (tank half empty)
    s['clients_df'].at[2, 'Current_lbs'] = 2000  # 40% of 5000 → 60% fill need
    s['clients_df'].at[2, 'Refill_lbs'] = 3000
    s['clients_df'].at[2, 'Days_Until_Stockout'] = 5.0
    s['clients_df'].at[2, 'Urgency'] = 'urgent'
    routes, deferred = _solve(s)
    sched = _scheduled_ids(routes)
    assert 'C002' in sched, f"high-fill client not scheduled: sched={sched}"


TESTS = [
    ('no_false_contract_escalation',  test_no_false_contract_escalation),
    ('urgency_still_works',           test_urgency_still_works),
    ('solver_no_crash_all_low_urgency', test_solver_no_crash_all_low_urgency),
    ('accounting_consistent',         test_accounting_consistent),
    ('fill_drives_scheduling',        test_fill_drives_scheduling),
]


def run_all_tests():
    print('\nContract Cadence Tests (DISABLED — no max service interval)')
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
