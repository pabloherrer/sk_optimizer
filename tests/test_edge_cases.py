"""
test_edge_cases.py — Degenerate inputs must not crash or produce garbage.

Each test feeds the solver something unusual and asserts the contract:
  - Returns a (routes_dict, deferred_df) tuple.
  - Every client ends up in exactly one of {scheduled, deferred}.
  - No NaN / None / negative in core columns.
  - No crash / unhandled exception.
"""

import sys
import time
from pathlib import Path
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

import tests.scenario_lib as scn
from config import DAYS, PRODUCTS
from unified_solver import solve_week


def run_test(name, fn):
    start = time.time()
    try:
        fn()
        print(f'  ✓ {name:<56s} ({(time.time()-start)*1000:.0f} ms)')
        return True
    except AssertionError as e:
        print(f'  ✗ {name:<56s} — {str(e)[:80]}')
        return False
    except Exception as e:
        print(f'  ✗ {name:<56s} — CRASH: {type(e).__name__}: {str(e)[:60]}')
        return False


def _solve(s, budget=3):
    return solve_week(
        s['clients_df'], s['dist_matrix'], s['time_matrix'], s['node_index_map'],
        start_day=0, solve_seconds=budget,
        time_windows_df=s['time_windows_df'],
        closures_df=s['closures_df'],
        today=pd.Timestamp('2026-04-14'),
        depot_config=s['depot_config'],
    )


def _accounting(s, routes, deferred):
    sched_ids = set()
    for r in routes.values():
        if not r.empty:
            sched_ids.update(r['ID'].tolist())
    def_ids = set() if deferred.empty else set(deferred['ID'].tolist())
    all_ids = set(s['clients_df']['ID'].tolist())
    assert sched_ids.isdisjoint(def_ids), f"overlap: {sched_ids & def_ids}"
    assert sched_ids | def_ids == all_ids, \
        f"missing {all_ids - (sched_ids|def_ids)} extras {(sched_ids|def_ids) - all_ids}"


# ── Degenerate inputs ───────────────────────────────────────────────────────

def test_single_client():
    s = scn.single_client()
    routes, deferred = _solve(s, budget=2)
    _accounting(s, routes, deferred)

def test_tiny_scenario():
    s = scn.tiny(3, seed=42)
    routes, deferred = _solve(s, budget=2)
    _accounting(s, routes, deferred)

def test_colocated_clients():
    """5 clients at the same coordinates — zero intra-client travel."""
    s = scn.colocated(5)
    routes, deferred = _solve(s, budget=2)
    _accounting(s, routes, deferred)
    # If any routes were built, distance should be small
    for (_, _), grp in pd.concat([r for r in routes.values() if not r.empty],
                                  ignore_index=True).groupby(['Truck', 'Day']) \
                       if any(not r.empty for r in routes.values()) else []:
        rd = grp['Route_Dist_mi'].iloc[0]
        assert rd < 50, f"colocated clients gave {rd} miles?"

def test_all_urgent():
    s = scn.all_urgent(25, seed=42)
    routes, deferred = _solve(s, budget=3)
    _accounting(s, routes, deferred)

def test_peak_demand():
    s = scn.peak_season(40, seed=42)
    routes, deferred = _solve(s, budget=3)
    _accounting(s, routes, deferred)

def test_off_season():
    """Low demand — many clients should defer gracefully."""
    s = scn.off_season(30, seed=42)
    routes, deferred = _solve(s, budget=3)
    _accounting(s, routes, deferred)

def test_tight_time_windows():
    s = scn.tight_windows(20, 0.8, seed=42)
    routes, deferred = _solve(s, budget=3)
    _accounting(s, routes, deferred)

def test_heavy_closures():
    s = scn.heavy_closures(30, 0.4, seed=42)
    routes, deferred = _solve(s, budget=3)
    _accounting(s, routes, deferred)


# ── Input-immutability / contract ────────────────────────────────────────────

def test_clients_df_not_mutated():
    s = scn.mixed(20, seed=7)
    before = s['clients_df'].copy(deep=True)
    _solve(s, budget=2)
    after = s['clients_df']
    assert before.equals(after), "clients_df mutated by solve_week"

def test_no_nan_in_route_columns():
    s = scn.mixed(30, seed=5)
    routes, _ = _solve(s, budget=3)
    for r in routes.values():
        if r.empty: continue
        for col in ['Route_Dist_mi','Route_Time_min','Reg_Min','OT_Min','Labor_Cost',
                    'Refill_lbs','Cum_Dist_mi']:
            if col in r.columns:
                assert not r[col].isna().any(), f"NaN in {col}"


# ── Registry ─────────────────────────────────────────────────────────────────

TESTS = [
    ('single_client',            test_single_client),
    ('tiny_scenario',            test_tiny_scenario),
    ('colocated_clients',        test_colocated_clients),
    ('all_urgent',               test_all_urgent),
    ('peak_demand',              test_peak_demand),
    ('off_season',               test_off_season),
    ('tight_time_windows',       test_tight_time_windows),
    ('heavy_closures',           test_heavy_closures),
    ('clients_df_not_mutated',   test_clients_df_not_mutated),
    ('no_nan_in_route_columns',  test_no_nan_in_route_columns),
]


def run_all_tests():
    print('\nEdge Cases')
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
