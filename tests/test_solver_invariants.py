"""
test_solver_invariants.py — properties that any legal solution must satisfy

We solve one medium synthetic scenario once (module-level fixture) and then
assert each invariant as a separate, named test. This keeps the suite fast
(~5s total) while giving named pass/fail rows for each property.

Design: fast but meaningful. Uses ~15 clients across 5 days so the solver
has enough flexibility to exercise real paths (capacity, compartments,
shift time, overtime, depot delimiters).
"""

import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import (
    TRUCKS, TRUCK_NAMES, DAYS, NUM_DAYS, PRODUCTS, COMPARTMENT_CAPACITY_LBS,
    SHIFT_MIN, MAX_SHIFT_MIN,
)
from unified_solver import solve_week, LOAD_CONFIGS


# ── Fixture: build a single scenario and solve it once for the whole file ───

def _build_scenario():
    """15-client scenario with a mix of products, distances, and loads."""
    clients = []
    for i in range(15):
        clients.append({
            'ID': f'C{i:03d}',
            'Customer': f'Client {i}',
            'Lat': 33.51 + (i % 5) * 0.05,
            'Lon': -112.16 + (i // 5) * 0.05,
            'Tank_lbs': 5000 if i % 2 == 0 else 8000,
            'Product': PRODUCTS[0] if i % 3 != 0 else PRODUCTS[1],
            'Avg_LbsPerDay': 100 + (i * 20) % 300,
        })

    clients_df = pd.DataFrame(clients)
    clients_df['Refill_lbs']          = 2000
    clients_df['Current_lbs']         = clients_df['Tank_lbs'] * 0.4
    clients_df['Est_Current_lbs']     = clients_df['Current_lbs']
    clients_df['Days_Until_Stockout'] = (clients_df['Current_lbs'] / clients_df['Avg_LbsPerDay']).clip(lower=0.5)
    clients_df['Urgency']             = 'normal'
    clients_df['Refill_Today_lbs']    = clients_df['Refill_lbs']
    clients_df['Fill_Pct_Today']      = clients_df['Refill_lbs'] / clients_df['Tank_lbs']

    n_nodes = len(clients_df) + 1
    dist_matrix = np.zeros((n_nodes, n_nodes), dtype=int)
    time_matrix = np.zeros((n_nodes, n_nodes), dtype=int)
    for i in range(n_nodes):
        for j in range(n_nodes):
            if i != j:
                dist_matrix[i, j] = abs(i - j) * 1200
                time_matrix[i, j] = abs(i - j) * 3

    node_index_map = {'DEPOT': 0}
    for idx, cid in enumerate(clients_df['ID'].tolist(), 1):
        node_index_map[cid] = idx

    return {
        'clients_df': clients_df,
        'time_windows_df': pd.DataFrame(columns=['Client_ID', 'Day_of_Week', 'Open_Min', 'Close_Min']),
        'closures_df': pd.DataFrame(columns=['Client_ID', 'Start_Date', 'End_Date', 'Reason']),
        'dist_matrix': dist_matrix,
        'time_matrix': time_matrix,
        'node_index_map': node_index_map,
        'depot_config': {
            'depot_lat': 33.5, 'depot_lon': -112.1,
            'shift_start_min': 360, 'shift_end_min': 1080,
            'morning_load_min': 30, 'evening_unload_min': 15,
            'work_days': DAYS,
        },
    }


def _solve_once():
    s = _build_scenario()
    routes, deferred = solve_week(
        s['clients_df'], s['dist_matrix'], s['time_matrix'], s['node_index_map'],
        start_day=0, solve_seconds=8,
        time_windows_df=s['time_windows_df'],
        closures_df=s['closures_df'],
        today=pd.Timestamp('2026-04-14'),  # Tuesday
        depot_config=s['depot_config'],
    )
    return s, routes, deferred


# Solved once, checked many times
_SCENARIO, _ROUTES, _DEFERRED = _solve_once()
_ALL = pd.concat([r for r in _ROUTES.values() if not r.empty], ignore_index=True) \
       if any(not r.empty for r in _ROUTES.values()) else pd.DataFrame()


def run_test(test_name, test_fn):
    start = time.time()
    try:
        test_fn()
        elapsed = time.time() - start
        print(f'  ✓ {test_name:<52s} ({elapsed*1000:.0f} ms)')
        return True
    except AssertionError as e:
        msg = str(e)[:80]
        print(f'  ✗ {test_name:<52s} — {msg}')
        return False
    except Exception as e:
        msg = f'{type(e).__name__}: {str(e)[:60]}'
        print(f'  ✗ {test_name:<52s} — CRASH: {msg}')
        return False


# ── Invariants ──────────────────────────────────────────────────────────────

def test_solver_did_produce_something():
    # At minimum we expect some routes to exist or else the invariant checks
    # become vacuous. If empty, surface clearly.
    total_scheduled = 0 if _ALL.empty else len(_ALL)
    total_deferred  = 0 if _DEFERRED is None or _DEFERRED.empty else len(_DEFERRED)
    assert total_scheduled + total_deferred == len(_SCENARIO['clients_df']), \
        f"clients {len(_SCENARIO['clients_df'])} != scheduled {total_scheduled} + deferred {total_deferred}"

def test_capacity_total_per_vehicle():
    if _ALL.empty:
        return
    # Group by (Truck, Day) — each such group is one vehicle's actual route
    for (truck, day), grp in _ALL.groupby(['Truck', 'Day']):
        cap = TRUCKS[truck]['capacity_lbs']
        load = grp['Refill_lbs'].sum()
        assert load <= cap, f"{truck} {day}: load {load} > cap {cap}"

def test_capacity_per_product():
    if _ALL.empty:
        return
    # Aggregate by (Truck, Day, Product) and check against config cap. With
    # the route's actual Load_Config we enforce the looser/correct cap.
    if 'Product' not in _ALL.columns:
        return
    for (truck, day, prod), grp in _ALL.groupby(['Truck', 'Day', 'Product']):
        total = grp['Refill_lbs'].sum()
        cfg = grp['Load_Config'].iloc[0] if 'Load_Config' in grp.columns else 'SPLIT'
        # SPLIT: one compartment per product, 5k max. A_ONLY: both compartments A.
        # B_ONLY: both compartments B. If the product doesn't match the config, it's 0.
        if cfg == 'SPLIT':
            cap = COMPARTMENT_CAPACITY_LBS
        elif cfg == 'A_ONLY':
            cap = 2 * COMPARTMENT_CAPACITY_LBS if prod == PRODUCTS[0] else 0
        else:  # B_ONLY
            cap = 2 * COMPARTMENT_CAPACITY_LBS if prod == PRODUCTS[1] else 0
        assert total <= cap, f"{truck} {day} {prod} under {cfg}: {total} > {cap}"

def test_shift_time_within_hard_ceiling():
    if _ALL.empty:
        return
    # Route_Time_min MAY exceed SHIFT_MIN (overtime), but not MAX_SHIFT_MIN.
    for (truck, day), grp in _ALL.groupby(['Truck', 'Day']):
        rt = grp['Route_Time_min'].iloc[0]  # uniform across stops of one route
        assert rt <= MAX_SHIFT_MIN, f"{truck} {day}: time {rt} > hard cap {MAX_SHIFT_MIN}"

def test_overtime_accounting_is_correct():
    if _ALL.empty:
        return
    for (truck, day), grp in _ALL.groupby(['Truck', 'Day']):
        rt  = grp['Route_Time_min'].iloc[0]
        reg = grp['Reg_Min'].iloc[0]
        ot  = grp['OT_Min'].iloc[0]
        assert reg + ot == rt, f"{truck} {day}: reg+ot {reg+ot} != total {rt}"
        assert reg <= SHIFT_MIN, f"{truck} {day}: reg {reg} > SHIFT_MIN"
        assert ot  >= 0

def test_single_visit_per_client():
    if _ALL.empty:
        return
    vc = _ALL['ID'].value_counts()
    dups = vc[vc > 1]
    assert len(dups) == 0, f"double visits: {dups.to_dict()}"

def test_compartment_math_matches_refill():
    if _ALL.empty:
        return
    for (truck, day), grp in _ALL.groupby(['Truck', 'Day']):
        if 'Comp_A_lbs' not in grp.columns:
            return
        comps_total = grp['Comp_A_lbs'].iloc[0] + grp['Comp_B_lbs'].iloc[0]
        refills_total = grp['Refill_lbs'].sum()
        assert comps_total == refills_total, \
            f"{truck} {day}: compartments {comps_total} != refills {refills_total}"

def test_at_most_one_config_per_truck_day():
    if _ALL.empty:
        return
    if 'Load_Config' not in _ALL.columns:
        return
    for (truck, day), grp in _ALL.groupby(['Truck', 'Day']):
        uniq = grp['Load_Config'].unique()
        assert len(uniq) == 1, f"{truck} {day} has multiple configs: {uniq}"

def test_deferred_accounting_exhaustive():
    # Every client must be in scheduled XOR deferred, not both, not neither
    scheduled = set() if _ALL.empty else set(_ALL['ID'].unique())
    deferred  = set() if _DEFERRED is None or _DEFERRED.empty else set(_DEFERRED['ID'].unique())
    overlap = scheduled & deferred
    assert len(overlap) == 0, f"clients both scheduled and deferred: {overlap}"
    union = scheduled | deferred
    all_ids = set(_SCENARIO['clients_df']['ID'].unique())
    missing = all_ids - union
    assert len(missing) == 0, f"clients missing from both scheduled and deferred: {missing}"

def test_refill_lbs_positive():
    if _ALL.empty:
        return
    neg = _ALL[_ALL['Refill_lbs'] <= 0]
    assert len(neg) == 0, f"{len(neg)} stops with non-positive refill"

def test_route_distance_nonneg():
    if _ALL.empty:
        return
    neg = _ALL[_ALL['Route_Dist_mi'] < 0]
    assert len(neg) == 0


# ── Suite runner ─────────────────────────────────────────────────────────────

TESTS = [
    ('solver_did_produce_something',             test_solver_did_produce_something),
    ('capacity_total_per_vehicle',               test_capacity_total_per_vehicle),
    ('capacity_per_product',                     test_capacity_per_product),
    ('shift_time_within_hard_ceiling',           test_shift_time_within_hard_ceiling),
    ('overtime_accounting_is_correct',           test_overtime_accounting_is_correct),
    ('single_visit_per_client',                  test_single_visit_per_client),
    ('compartment_math_matches_refill',          test_compartment_math_matches_refill),
    ('at_most_one_config_per_truck_day',         test_at_most_one_config_per_truck_day),
    ('deferred_accounting_exhaustive',           test_deferred_accounting_exhaustive),
    ('refill_lbs_positive',                      test_refill_lbs_positive),
    ('route_distance_nonneg',                    test_route_distance_nonneg),
]


def run_all_tests():
    print('\nSolver Invariants')
    print('━' * 70)
    print(f'  (scenario: {len(_SCENARIO["clients_df"])} clients, '
          f'scheduled={0 if _ALL.empty else len(_ALL)}, '
          f'deferred={0 if _DEFERRED.empty else len(_DEFERRED)})')
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
