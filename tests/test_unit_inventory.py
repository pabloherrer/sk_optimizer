"""
test_unit_inventory.py — pure unit tests for inventory.py

No solver, no network. Deterministic, fast (< 1 sec total).
Covers every public function in sk_optimizer/inventory.py and the edge cases
that matter for rolling-horizon correctness.
"""

import sys
import time
from pathlib import Path
import math

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import (
    MIN_OIL_PCT, CRITICAL_DAYS, URGENT_DAYS, TRUCKS, TRUCK_NAMES,
)
from inventory import (
    project_level, compute_refill, days_until_stockout, urgency_tier,
    fill_efficiency, service_time_min, enrich_snapshot,
    build_refill_matrix, build_fill_pct_matrix,
)


def run_test(test_name, test_fn):
    start = time.time()
    try:
        test_fn()
        elapsed = time.time() - start
        print(f'  ✓ {test_name:<50s} ({elapsed*1000:.0f} ms)')
        return True
    except AssertionError as e:
        msg = str(e)[:80]
        print(f'  ✗ {test_name:<50s} — {msg}')
        return False
    except Exception as e:
        msg = f'{type(e).__name__}: {str(e)[:60]}'
        print(f'  ✗ {test_name:<50s} — CRASH: {msg}')
        return False


# ── project_level ────────────────────────────────────────────────────────────

def test_project_level_today():
    # t=0 → current level unchanged
    assert project_level(5000, 200, 0, 10000) == 5000

def test_project_level_future_linear():
    # Linear draw: 5000 - 3*200 = 4400
    assert project_level(5000, 200, 3, 10000) == 4400

def test_project_level_fractional_day():
    # 5000 - 0.5*200 = 4900
    assert project_level(5000, 200, 0.5, 10000) == 4900

def test_project_level_floor_clamp():
    # Heavy consumption → clamped at MIN_OIL_PCT*tank floor
    # floor = 10000 * 0.0 = 0 (MIN_OIL_PCT=0 in config); force clamp via min_pct arg
    lvl = project_level(1000, 500, 5, 10000, min_pct=0.05)
    # 1000 - 5*500 = -1500, clamped at 10000*0.05 = 500
    assert lvl == 500

def test_project_level_ceiling_clamp():
    # Weird negative rate → still clamped at tank_lbs
    lvl = project_level(5000, -200, 10, 10000)
    # 5000 - 10*(-200) = 7000; still below tank_lbs=10000
    assert lvl == 7000

def test_project_level_overfill_clamp():
    # Level above tank_lbs must clamp to tank_lbs
    lvl = project_level(5000, -1000, 10, 10000)  # 5000+10000=15000 -> clamp to 10000
    assert lvl == 10000

def test_project_level_zero_tank_edge():
    # Zero tank, should still be well-defined (all args clamp to 0)
    lvl = project_level(0, 0, 0, 0)
    assert lvl == 0


# ── compute_refill ───────────────────────────────────────────────────────────

def test_compute_refill_fills_to_tank():
    # Half-full, zero consumption, visit today → fill to tank
    assert compute_refill(5000, 0, 0, 10000) == 5000

def test_compute_refill_never_negative():
    # Tank already full → 0
    assert compute_refill(10000, 100, 0, 10000) == 0

def test_compute_refill_zero_rate_client():
    # Zero consumption → refill only grows until level clamps at tank
    assert compute_refill(5000, 0, 3, 10000) == 5000

def test_compute_refill_future_day_larger():
    # Day 4 visit after 200 lbs/day draw: level = 4200 → refill = 5800
    assert compute_refill(5000, 200, 4, 10000) == 5800


# ── days_until_stockout ──────────────────────────────────────────────────────

def test_days_until_stockout_zero_current():
    assert days_until_stockout(0, 100, 10000) == 0.0

def test_days_until_stockout_zero_rate():
    assert days_until_stockout(5000, 0, 10000) == 0.0

def test_days_until_stockout_at_floor():
    # At floor → 0
    assert days_until_stockout(500, 100, 10000, min_pct=0.05) == 0.0

def test_days_until_stockout_simple():
    # (5000-0)/100 = 50 days (floor=0 by default)
    assert days_until_stockout(5000, 100, 10000) == 50.0

def test_days_until_stockout_with_min_pct():
    # (5000 - 10000*0.1) / 100 = 40 days
    assert days_until_stockout(5000, 100, 10000, min_pct=0.1) == 40.0


# ── urgency_tier ─────────────────────────────────────────────────────────────

def test_urgency_stockout_zero():
    assert urgency_tier(0) == 'stockout'

def test_urgency_stockout_negative():
    assert urgency_tier(-1) == 'stockout'

def test_urgency_critical_boundary():
    assert urgency_tier(CRITICAL_DAYS) == 'critical'

def test_urgency_critical_below():
    assert urgency_tier(CRITICAL_DAYS - 0.1) == 'critical'

def test_urgency_urgent_boundary():
    assert urgency_tier(URGENT_DAYS) == 'urgent'

def test_urgency_urgent_between():
    assert urgency_tier((CRITICAL_DAYS + URGENT_DAYS) / 2) == 'urgent'

def test_urgency_normal_above():
    assert urgency_tier(URGENT_DAYS + 1) == 'normal'


# ── fill_efficiency ──────────────────────────────────────────────────────────

def test_fill_efficiency_zero_tank():
    assert fill_efficiency(0, 100, 0, 0) == 0.0

def test_fill_efficiency_matches_refill_ratio():
    r = compute_refill(5000, 200, 2, 10000)
    e = fill_efficiency(5000, 200, 2, 10000)
    assert abs(e - r / 10000) < 1e-9

def test_fill_efficiency_bounded_0_1():
    for cur in [0, 1000, 5000, 10000]:
        for rate in [0, 100, 500]:
            for d in [0, 2, 4]:
                e = fill_efficiency(cur, rate, d, 10000)
                assert 0.0 <= e <= 1.0, f"bounds broken at cur={cur} rate={rate} d={d}"


# ── service_time_min ─────────────────────────────────────────────────────────

def test_service_time_truck2_vs_truck9():
    # Truck2 and Truck9 have different pump rates — verify deliver-500 differs
    t2 = service_time_min(500, TRUCK_NAMES[0])
    t9 = service_time_min(500, TRUCK_NAMES[-1])
    # Different trucks, should differ if pump rates differ (always true in prod)
    assert t2 != t9 or TRUCKS[TRUCK_NAMES[0]]['pump_rate_lbs_per_min'] == \
           TRUCKS[TRUCK_NAMES[-1]]['pump_rate_lbs_per_min']

def test_service_time_scales_linearly():
    t500   = service_time_min(500,  TRUCK_NAMES[0])
    t1000  = service_time_min(1000, TRUCK_NAMES[0])
    setup  = TRUCKS[TRUCK_NAMES[0]]['fixed_setup_min']
    # (t1000 - setup) / (t500 - setup) should equal 1000/500 = 2
    if t500 > setup:
        ratio = (t1000 - setup) / (t500 - setup)
        assert abs(ratio - 2.0) < 1e-9

def test_service_time_zero_lbs_equals_setup():
    setup = TRUCKS[TRUCK_NAMES[0]]['fixed_setup_min']
    assert service_time_min(0, TRUCK_NAMES[0]) == setup


# ── enrich_snapshot ──────────────────────────────────────────────────────────

def _mini_clients_df():
    return pd.DataFrame([
        {'ID': 'A', 'Tank_lbs': 10000, 'Avg_LbsPerDay': 200, 'Est_Current_lbs': 5000},
        {'ID': 'B', 'Tank_lbs':  5000, 'Avg_LbsPerDay': 500, 'Est_Current_lbs': 1000},
        {'ID': 'C', 'Tank_lbs':  8000, 'Avg_LbsPerDay':   0, 'Est_Current_lbs': 4000},
    ])

def test_enrich_snapshot_adds_all_columns():
    df = enrich_snapshot(_mini_clients_df())
    for col in ['Current_lbs', 'Days_Until_Stockout', 'Urgency',
                'Refill_Today_lbs', 'Fill_Pct_Today']:
        assert col in df.columns, f"missing column {col}"

def test_enrich_snapshot_respects_inventory_state():
    df = enrich_snapshot(_mini_clients_df(), inventory_state={'A': 9000})
    assert df.loc[df['ID'] == 'A', 'Current_lbs'].iloc[0] == 9000

def test_enrich_snapshot_state_fallback_to_est():
    df = enrich_snapshot(_mini_clients_df(), inventory_state={'A': 9000})
    # B not in state → fallback to Est_Current_lbs=1000
    assert df.loc[df['ID'] == 'B', 'Current_lbs'].iloc[0] == 1000

def test_enrich_snapshot_bounded_clip():
    df = _mini_clients_df()
    df.loc[0, 'Est_Current_lbs'] = 99999  # over tank
    out = enrich_snapshot(df)
    assert out.loc[out['ID'] == 'A', 'Current_lbs'].iloc[0] == 10000

def test_enrich_snapshot_urgency_populated():
    # Client B at 1000 with 500/day → 2 days to stockout → urgent/critical
    df = enrich_snapshot(_mini_clients_df())
    urg = df.loc[df['ID'] == 'B', 'Urgency'].iloc[0]
    assert urg in {'critical', 'urgent', 'stockout'}


# ── build_refill_matrix ──────────────────────────────────────────────────────

def test_build_refill_matrix_shape():
    df = enrich_snapshot(_mini_clients_df())
    m = build_refill_matrix(df, n_days=5)
    assert m.shape == (3, 5)

def test_build_refill_matrix_nonneg():
    df = enrich_snapshot(_mini_clients_df())
    m = build_refill_matrix(df, n_days=5)
    assert (m >= 0).all(), f"negative refill found: {m}"

def test_build_refill_matrix_monotone_nondecreasing_over_time():
    # With zero current_lbs replenishment, refill must be non-decreasing over days
    df = enrich_snapshot(_mini_clients_df())
    m = build_refill_matrix(df, n_days=5)
    for i in range(len(df)):
        for d in range(1, 5):
            assert m[i, d] + 1e-9 >= m[i, d-1], \
                f"row {i} refill decreased day {d-1}→{d}: {m[i]}"


# ── build_fill_pct_matrix ────────────────────────────────────────────────────

def test_build_fill_pct_matrix_bounded():
    df = enrich_snapshot(_mini_clients_df())
    m = build_fill_pct_matrix(df, n_days=5)
    assert (m >= 0).all() and (m <= 1).all()

def test_build_fill_pct_matches_ratio():
    df = enrich_snapshot(_mini_clients_df())
    r = build_refill_matrix(df, n_days=5)
    p = build_fill_pct_matrix(df, n_days=5)
    for i in range(len(df)):
        tank = df['Tank_lbs'].iloc[i]
        if tank > 0:
            for d in range(5):
                assert abs(p[i, d] - r[i, d] / tank) < 1e-9


# ── Suite runner ─────────────────────────────────────────────────────────────

TESTS = [
    ('project_level_today',                      test_project_level_today),
    ('project_level_future_linear',              test_project_level_future_linear),
    ('project_level_fractional_day',             test_project_level_fractional_day),
    ('project_level_floor_clamp',                test_project_level_floor_clamp),
    ('project_level_ceiling_clamp',              test_project_level_ceiling_clamp),
    ('project_level_overfill_clamp',             test_project_level_overfill_clamp),
    ('project_level_zero_tank_edge',             test_project_level_zero_tank_edge),
    ('compute_refill_fills_to_tank',             test_compute_refill_fills_to_tank),
    ('compute_refill_never_negative',            test_compute_refill_never_negative),
    ('compute_refill_zero_rate_client',          test_compute_refill_zero_rate_client),
    ('compute_refill_future_day_larger',         test_compute_refill_future_day_larger),
    ('days_until_stockout_zero_current',         test_days_until_stockout_zero_current),
    ('days_until_stockout_zero_rate',            test_days_until_stockout_zero_rate),
    ('days_until_stockout_at_floor',             test_days_until_stockout_at_floor),
    ('days_until_stockout_simple',               test_days_until_stockout_simple),
    ('days_until_stockout_with_min_pct',         test_days_until_stockout_with_min_pct),
    ('urgency_stockout_zero',                    test_urgency_stockout_zero),
    ('urgency_stockout_negative',                test_urgency_stockout_negative),
    ('urgency_critical_boundary',                test_urgency_critical_boundary),
    ('urgency_critical_below',                   test_urgency_critical_below),
    ('urgency_urgent_boundary',                  test_urgency_urgent_boundary),
    ('urgency_urgent_between',                   test_urgency_urgent_between),
    ('urgency_normal_above',                     test_urgency_normal_above),
    ('fill_efficiency_zero_tank',                test_fill_efficiency_zero_tank),
    ('fill_efficiency_matches_refill_ratio',     test_fill_efficiency_matches_refill_ratio),
    ('fill_efficiency_bounded_0_1',              test_fill_efficiency_bounded_0_1),
    ('service_time_truck2_vs_truck9',            test_service_time_truck2_vs_truck9),
    ('service_time_scales_linearly',             test_service_time_scales_linearly),
    ('service_time_zero_lbs_equals_setup',       test_service_time_zero_lbs_equals_setup),
    ('enrich_snapshot_adds_all_columns',         test_enrich_snapshot_adds_all_columns),
    ('enrich_snapshot_respects_inventory_state', test_enrich_snapshot_respects_inventory_state),
    ('enrich_snapshot_state_fallback_to_est',    test_enrich_snapshot_state_fallback_to_est),
    ('enrich_snapshot_bounded_clip',             test_enrich_snapshot_bounded_clip),
    ('enrich_snapshot_urgency_populated',        test_enrich_snapshot_urgency_populated),
    ('build_refill_matrix_shape',                test_build_refill_matrix_shape),
    ('build_refill_matrix_nonneg',               test_build_refill_matrix_nonneg),
    ('build_refill_matrix_monotone_nondecreasing', test_build_refill_matrix_monotone_nondecreasing_over_time),
    ('build_fill_pct_matrix_bounded',            test_build_fill_pct_matrix_bounded),
    ('build_fill_pct_matches_ratio',             test_build_fill_pct_matches_ratio),
]


def run_all_tests():
    print('\nInventory Math — Unit Tests')
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
    print(f'{tag} {passed} passed, {failed} failed in {elapsed*1000:.0f} ms')
    return 0 if failed == 0 else 1


if __name__ == '__main__':
    sys.exit(run_all_tests())
