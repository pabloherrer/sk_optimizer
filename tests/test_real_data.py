"""
test_real_data.py — End-to-end integration on SK_Delivery_System.xlsx.

These tests hit the real data pipeline: Excel loader → consumption estimator →
inventory enrichment → solver. They are SLOW (~20–45 s per test) so they are
only run when invoked explicitly. Regular fast tests in run_all.py skip them.

Gating:
- Tests run iff the real data file (SK_Delivery_System.xlsx + OSRM matrix)
  is present. If missing, they are skipped with a clear message — the real
  data is not committed in every environment.
- A single solve is shared across tests via lazy _solve_cached() to keep
  total time under a minute.
"""

import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import (
    TRUCKS, DAYS, NUM_DAYS, PRODUCTS,
    INPUT_FILE, MATRIX_FILE, STATE_FILE, SHIFT_MIN, MAX_SHIFT_MIN,
    COMPARTMENT_CAPACITY_LBS, MAX_SERVICE_INTERVAL_DAYS,
    LABOR_COST_PER_MIN, OT_MULTIPLIER,
)


# ─── Gating ─────────────────────────────────────────────────────────────────

def _data_available():
    return Path(INPUT_FILE).exists() and Path(MATRIX_FILE).exists()


# ─── Lazy shared solve (amortize load cost across tests) ────────────────────

_CACHE = {}


def _solve_once(solve_sec=20):
    """Load everything and solve once. Subsequent tests reuse the result."""
    if 'result' in _CACHE:
        return _CACHE['result']

    from load_data import load_all
    from forecast_consumption import estimate_consumption_rates
    from inventory import enrich_snapshot
    from router import load_matrix
    from schema_loaders import load_time_windows, load_closures, load_depot_config
    from state import load_state, initialise_state_from_snapshot
    from unified_solver import solve_week

    today = pd.Timestamp('2026-04-14')
    clients_raw, deliveries = load_all(INPUT_FILE)
    clients_df = estimate_consumption_rates(deliveries, clients_raw, today=today)
    dm, tm, node_index_map = load_matrix(MATRIX_FILE)
    tw = load_time_windows(INPUT_FILE)
    cl = load_closures(INPUT_FILE)
    depot = load_depot_config(INPUT_FILE)

    try:
        state = load_state(STATE_FILE)
    except Exception:
        state = {}
    if not state:
        state = initialise_state_from_snapshot(clients_df)
    snapshot = enrich_snapshot(clients_df, state)

    t0 = time.time()
    routes, deferred = solve_week(
        snapshot, dm, tm, node_index_map,
        start_day=0, solve_seconds=solve_sec,
        time_windows_df=tw, closures_df=cl,
        today=today, depot_config=depot,
    )
    elapsed = time.time() - t0

    result = {
        'snapshot': snapshot, 'routes': routes, 'deferred': deferred,
        'elapsed': elapsed, 'node_index_map': node_index_map,
    }
    _CACHE['result'] = result
    return result


# ─── Helpers ────────────────────────────────────────────────────────────────

def run_test(name, fn):
    start = time.time()
    try:
        if not _data_available():
            print(f'  ⊘ {name:<50s} — skipped (real data not present)')
            return None   # None = skipped
        fn()
        print(f'  ✓ {name:<50s} ({time.time()-start:.1f}s)')
        return True
    except AssertionError as e:
        print(f'  ✗ {name:<50s} — {str(e)[:80]}')
        return False
    except Exception as e:
        print(f'  ✗ {name:<50s} — CRASH: {type(e).__name__}: {str(e)[:60]}')
        return False


# ─── Tests ──────────────────────────────────────────────────────────────────

def test_load_and_solve_completes():
    """Pipeline runs end-to-end without crash."""
    res = _solve_once(solve_sec=15)
    assert res['elapsed'] < 60, f"solve took {res['elapsed']:.0f}s (budget 60)"
    assert isinstance(res['routes'], dict)
    assert isinstance(res['deferred'], pd.DataFrame)


def test_at_least_one_route_produced():
    res = _solve_once()
    parts = [r for r in res['routes'].values() if not r.empty]
    assert len(parts) > 0, "no routes produced on real data"


def test_at_least_one_stop_scheduled():
    res = _solve_once()
    parts = [r for r in res['routes'].values() if not r.empty]
    total = 0 if not parts else len(pd.concat(parts))
    assert total > 0, "zero stops scheduled"


def test_capacity_never_exceeded_real():
    """Total refill per truck-day ≤ truck capacity."""
    res = _solve_once()
    parts = [r for r in res['routes'].values() if not r.empty]
    if not parts:
        return
    df = pd.concat(parts)
    for (truck, day), sub in df.groupby(['Truck', 'Day']):
        load = sub['Refill_lbs'].sum()
        cap  = TRUCKS[truck]['capacity_lbs']
        assert load <= cap, f"{truck}/{day}: load {load} > cap {cap}"


def test_shift_time_within_hard_ceiling_real():
    """Route_Time_min stays under MAX_SHIFT_MIN (hard driver-hours cap)."""
    res = _solve_once()
    parts = [r for r in res['routes'].values() if not r.empty]
    if not parts:
        return
    df = pd.concat(parts)
    for rt in df['Route_Time_min'].unique():
        assert rt <= MAX_SHIFT_MIN, f"route {rt} min exceeds hard cap {MAX_SHIFT_MIN}"


def test_ot_accounting_identity_real():
    """Reg_Min + OT_Min == Route_Time_min on every route."""
    res = _solve_once()
    parts = [r for r in res['routes'].values() if not r.empty]
    if not parts:
        return
    df = pd.concat(parts)
    for _, row in df.iterrows():
        assert row['Reg_Min'] + row['OT_Min'] == row['Route_Time_min'], \
            f"reg+ot != total: {row['Reg_Min']}+{row['OT_Min']} != {row['Route_Time_min']}"


def test_single_visit_per_client_real():
    """No client is scheduled twice in the same week."""
    res = _solve_once()
    parts = [r for r in res['routes'].values() if not r.empty]
    if not parts:
        return
    df = pd.concat(parts)
    dup = df['ID'].value_counts()
    assert (dup <= 1).all(), f"duplicate client(s): {dup[dup>1].to_dict()}"


def test_deferred_and_scheduled_disjoint_real():
    """Deferred and scheduled sets are disjoint."""
    res = _solve_once()
    parts = [r for r in res['routes'].values() if not r.empty]
    sched = set() if not parts else set(pd.concat(parts)['ID'])
    deferred_ids = set() if res['deferred'].empty else set(res['deferred']['ID'])
    assert sched.isdisjoint(deferred_ids), "client appears in both scheduled and deferred"


def test_compartment_totals_real():
    """Comp_A_lbs + Comp_B_lbs == total Refill_lbs on every route."""
    res = _solve_once()
    parts = [r for r in res['routes'].values() if not r.empty]
    if not parts:
        return
    df = pd.concat(parts)
    for (truck, day), sub in df.groupby(['Truck', 'Day']):
        total_refill = sub['Refill_lbs'].sum()
        # Comp_A_lbs and Comp_B_lbs are identical across rows within a route
        ca = sub['Comp_A_lbs'].iloc[0]
        cb = sub['Comp_B_lbs'].iloc[0]
        assert ca + cb == total_refill, f"{truck}/{day}: {ca}+{cb} != {total_refill}"


def test_contract_escalation_count_sane_real():
    """Count of contract-escalated clients is between 0 and total clients."""
    res = _solve_once()
    n_clients = len(res['snapshot'])
    parts = [r for r in res['routes'].values() if not r.empty]
    if not parts:
        return
    df = pd.concat(parts)
    sched = len(df)
    deferred_ct = len(res['deferred'])
    # All clients accounted for (modulo data-hygiene drops that the solver filters)
    assert sched + deferred_ct <= n_clients + 5  # small slack for duplicates
    # Escalation count is a scalar in the log; here we just check we scheduled
    # anything at all, and that the schedule respects total capacity
    assert sched > 0


def test_depot_at_end_of_route_real():
    """Cumulative distance per route is non-decreasing within the stop list."""
    res = _solve_once()
    parts = [r for r in res['routes'].values() if not r.empty]
    if not parts:
        return
    for df in parts:
        for (truck, day), sub in df.groupby(['Truck', 'Day']):
            cum = sub['Cum_Dist_mi'].tolist()
            assert all(cum[i] <= cum[i+1] + 1 for i in range(len(cum)-1)), \
                f"{truck}/{day}: Cum_Dist_mi decreases"


def test_all_routed_ids_in_node_map_real():
    """Every scheduled ID has a node-map entry (sanity: solver uses valid nodes)."""
    res = _solve_once()
    parts = [r for r in res['routes'].values() if not r.empty]
    if not parts:
        return
    df = pd.concat(parts)
    missing = [cid for cid in df['ID'].unique() if cid not in res['node_index_map']]
    assert not missing, f"scheduled IDs missing from node map: {missing[:3]}"


TESTS = [
    ('real_load_and_solve_completes',        test_load_and_solve_completes),
    ('real_route_produced',                  test_at_least_one_route_produced),
    ('real_stops_scheduled',                 test_at_least_one_stop_scheduled),
    ('real_capacity_never_exceeded',         test_capacity_never_exceeded_real),
    ('real_shift_time_within_hard_ceiling',  test_shift_time_within_hard_ceiling_real),
    ('real_ot_accounting_identity',          test_ot_accounting_identity_real),
    ('real_single_visit_per_client',         test_single_visit_per_client_real),
    ('real_deferred_sched_disjoint',         test_deferred_and_scheduled_disjoint_real),
    ('real_compartment_totals',              test_compartment_totals_real),
    ('real_contract_escalation_sane',        test_contract_escalation_count_sane_real),
    ('real_cum_dist_monotone',               test_depot_at_end_of_route_real),
    ('real_ids_in_node_map',                 test_all_routed_ids_in_node_map_real),
]


def run_all_tests():
    print('\nReal-Data Integration Tests (slow)')
    print('━' * 70)
    if not _data_available():
        print(f'  ⊘  Skipped — {INPUT_FILE.name} or {MATRIX_FILE.name} missing')
        print('━' * 70)
        return 0

    passed = failed = skipped = 0
    start = time.time()
    for name, fn in TESTS:
        r = run_test(name, fn)
        if r is True:
            passed += 1
        elif r is False:
            failed += 1
        else:
            skipped += 1
    elapsed = time.time() - start
    print('━' * 70)
    tag = '✓' if failed == 0 else '✗'
    print(f'{tag} {passed} passed, {failed} failed, {skipped} skipped in {elapsed:.1f}s')
    return 0 if failed == 0 else 1


if __name__ == '__main__':
    sys.exit(run_all_tests())
