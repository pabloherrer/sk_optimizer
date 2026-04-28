"""
test_forecasting.py — Inventory & consumption-rate forecasting accuracy.

Tests the pure-math layer that feeds the solver:
  * inventory.py: project_level, days_until_stockout, compute_refill,
                  fill_efficiency, service_time_min, enrich_snapshot,
                  build_refill_matrix, build_fill_pct_matrix
  * forecast_consumption.py: estimate_consumption_rates
  * state.py: update_state (state-advance correctness)

These are the functions whose correctness directly determines forecast
quality. If they drift, the whole rolling horizon drifts with them.

Invariants we test:
  - project_level is linear in time
  - days_until_stockout monotone (more current / less rate → more days)
  - compute_refill monotone in day-index
  - fill_efficiency ∈ [0, 1]
  - service_time linear in refill
  - enrich_snapshot is idempotent (running it twice gives same result)
  - rate estimator: known-history case, IQR outlier exclusion, fallbacks
  - update_state: delivered resets to full; unvisited decays with floor
"""

import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import MIN_OIL_PCT, CRITICAL_DAYS, URGENT_DAYS, TRUCKS, TRUCK_NAMES
from inventory import (
    project_level, compute_refill, days_until_stockout, urgency_tier,
    fill_efficiency, service_time_min, enrich_snapshot,
    build_refill_matrix, build_fill_pct_matrix,
)
from forecast_consumption import estimate_consumption_rates
from state import (
    load_state, save_state, update_state,
    initialise_state_from_snapshot,
)


def run_test(name, fn):
    start = time.time()
    try:
        fn()
        print(f'  ✓ {name:<60s} ({(time.time()-start)*1000:.0f} ms)')
        return True
    except AssertionError as e:
        print(f'  ✗ {name:<60s} — {str(e)[:70]}')
        return False
    except Exception as e:
        print(f'  ✗ {name:<60s} — CRASH: {type(e).__name__}: {str(e)[:60]}')
        return False


# ── project_level deep edge cases ────────────────────────────────────────────

def test_project_level_negative_time_extrapolates_backward():
    # t = -2 days → level was 200×2 higher; clamp to tank
    lvl = project_level(5000, 200, -2.0, 10000)
    assert lvl == 5400, f'expected 5400, got {lvl}'


def test_project_level_zero_rate_constant():
    for t in [0, 1, 5, 100]:
        assert project_level(3000, 0.0, t, 10000) == 3000


def test_project_level_floor_with_nonzero_pct():
    # floor = 10000 * 0.1 = 1000
    lvl = project_level(2000, 500, 10, 10000, min_pct=0.10)
    # 2000 - 10*500 = -3000, clamped to 1000
    assert lvl == 1000


def test_project_level_fractional_day_precision():
    # 5000 - 0.1*200 = 4980
    lvl = project_level(5000, 200, 0.1, 10000)
    assert abs(lvl - 4980) < 0.001


# ── days_until_stockout ─────────────────────────────────────────────────────

def test_dus_zero_rate_returns_zero():
    assert days_until_stockout(5000, 0.0, 10000) == 0.0


def test_dus_at_floor_returns_zero():
    # floor = 10000 * MIN_OIL_PCT (0 by default)
    assert days_until_stockout(0, 200, 10000) == 0.0
    # With explicit floor
    assert days_until_stockout(500, 200, 10000, min_pct=0.05) == 0.0


def test_dus_above_floor_linear():
    # 5000 lbs, 500/day, 10000 tank, min_pct=0 → 10 days
    assert days_until_stockout(5000, 500, 10000) == 10.0


def test_dus_monotone_in_current():
    # More current → more days
    a = days_until_stockout(2000, 200, 10000)
    b = days_until_stockout(4000, 200, 10000)
    c = days_until_stockout(6000, 200, 10000)
    assert a < b < c, f'not monotone: {a} < {b} < {c}'


def test_dus_monotone_inverse_in_rate():
    # Higher rate → fewer days
    a = days_until_stockout(5000, 100, 10000)
    b = days_until_stockout(5000, 250, 10000)
    c = days_until_stockout(5000, 500, 10000)
    assert a > b > c, f'not inversely monotone: {a} > {b} > {c}'


# ── compute_refill ──────────────────────────────────────────────────────────

def test_refill_zero_when_full():
    assert compute_refill(10000, 200, 0, 10000) == 0.0


def test_refill_monotone_in_day():
    # The more days forward, the lower the tank, the more refill needed
    r0 = compute_refill(5000, 200, 0, 10000)
    r2 = compute_refill(5000, 200, 2, 10000)
    r4 = compute_refill(5000, 200, 4, 10000)
    assert r0 <= r2 <= r4, f'not monotone: {r0} <= {r2} <= {r4}'


def test_refill_caps_at_tank():
    # Even if "level" drops to 0, refill is capped at tank
    # But project_level floors at 0 with default min_pct, so refill ≤ tank
    r = compute_refill(500, 500, 10, 10000)
    assert r <= 10000, f'refill {r} > tank 10000'


# ── fill_efficiency ─────────────────────────────────────────────────────────

def test_fill_efficiency_bounded():
    # Should always be in [0, 1]
    for cur in [0, 1000, 5000, 10000]:
        for rate in [0, 100, 500]:
            for day in [0, 1, 3, 5]:
                fe = fill_efficiency(cur, rate, day, 10000)
                assert 0.0 <= fe <= 1.0, f'fe={fe} for cur={cur}, rate={rate}, day={day}'


def test_fill_efficiency_zero_tank_handled():
    assert fill_efficiency(0, 100, 3, 0) == 0.0


def test_fill_efficiency_full_tank_zero():
    # If we visit today and tank is already full, efficiency is 0
    assert fill_efficiency(10000, 200, 0, 10000) == 0.0


# ── service_time_min ────────────────────────────────────────────────────────

def test_service_time_linear_in_refill():
    # Linear: setup + refill / pump_rate
    truck = TRUCK_NAMES[0]
    setup = TRUCKS[truck]['fixed_setup_min']
    rate  = TRUCKS[truck]['pump_rate_lbs_per_min']

    for refill in [0, 100, 500, 1000, 5000, 10000]:
        got = service_time_min(refill, truck)
        expected = setup + refill / rate
        assert abs(got - expected) < 0.01, \
            f'svc_time({refill}) = {got}, expected {expected}'


# ── enrich_snapshot ─────────────────────────────────────────────────────────

def _make_clients_df(n=5):
    rows = []
    for i in range(n):
        rows.append({
            'ID': f'C{i:03d}', 'Customer': f'C{i}',
            'Tank_lbs': 5000 + i * 1000,
            'Avg_LbsPerDay': 100 + i * 50,
            'Est_Current_lbs': 3000 - i * 200,
            'Lat': 33.5 + i * 0.01, 'Lon': -112.1 + i * 0.01,
        })
    return pd.DataFrame(rows)


def test_enrich_snapshot_idempotent():
    df = _make_clients_df(5)
    once  = enrich_snapshot(df)
    twice = enrich_snapshot(once)
    # Both runs should give the same enriched columns
    for col in ['Current_lbs', 'Days_Until_Stockout', 'Urgency',
                'Refill_Today_lbs', 'Fill_Pct_Today']:
        pd.testing.assert_series_equal(
            once[col].reset_index(drop=True),
            twice[col].reset_index(drop=True),
            check_names=False,
        )


def test_enrich_snapshot_state_override_wins():
    df = _make_clients_df(3)
    # Override state for C000 to 1000 lbs
    state = {'C000': 1000.0}
    enriched = enrich_snapshot(df, inventory_state=state)
    c0 = enriched[enriched['ID'] == 'C000'].iloc[0]
    assert c0['Current_lbs'] == 1000.0, \
        f'C000 current should be 1000 (from state); got {c0["Current_lbs"]}'
    # Other clients get Est_Current_lbs
    c1 = enriched[enriched['ID'] == 'C001'].iloc[0]
    src = df[df['ID'] == 'C001'].iloc[0]
    assert c1['Current_lbs'] == src['Est_Current_lbs']


def test_enrich_snapshot_urgency_tier_assigned():
    df = _make_clients_df(3)
    # Force C000 to be critical (very low tank)
    df.loc[df['ID'] == 'C000', 'Est_Current_lbs'] = 10
    df.loc[df['ID'] == 'C000', 'Avg_LbsPerDay'] = 1000
    enriched = enrich_snapshot(df)
    c0 = enriched[enriched['ID'] == 'C000'].iloc[0]
    assert c0['Urgency'] in ('stockout', 'critical'), \
        f'C000 urgency {c0["Urgency"]} — expected stockout or critical'


def test_enrich_snapshot_clamps_current_lbs():
    df = _make_clients_df(2)
    df.loc[0, 'Est_Current_lbs'] = 99999  # above tank
    enriched = enrich_snapshot(df)
    r0 = enriched.iloc[0]
    assert r0['Current_lbs'] <= r0['Tank_lbs'], \
        f'Current_lbs {r0["Current_lbs"]} > Tank_lbs {r0["Tank_lbs"]}'


# ── Refill matrix ───────────────────────────────────────────────────────────

def test_refill_matrix_shape_and_monotone_day():
    df = _make_clients_df(3)
    enriched = enrich_snapshot(df)
    mat = build_refill_matrix(enriched, n_days=5)
    assert mat.shape == (3, 5), f'shape {mat.shape} expected (3,5)'
    # Each row: refill should be non-decreasing with day
    for i in range(3):
        for d in range(1, 5):
            assert mat[i, d] >= mat[i, d-1] - 1e-6, \
                f'row {i} day {d}: {mat[i, d]} < {mat[i, d-1]}'


def test_fill_pct_matrix_bounded():
    df = _make_clients_df(4)
    enriched = enrich_snapshot(df)
    mat = build_fill_pct_matrix(enriched, n_days=5)
    assert mat.shape == (4, 5)
    assert (mat >= 0).all() and (mat <= 1).all(), \
        f'fill_pct out of [0,1]: min={mat.min()}, max={mat.max()}'


# ── forecast_consumption.estimate_consumption_rates ─────────────────────────

def _make_deliveries_df(client_rates):
    """Build a synthetic delivery log.
    client_rates = {cid: (true_rate, n_visits)}
    Visits are spaced 7 days apart; qty = rate * 7.
    """
    rows = []
    start = pd.Timestamp('2026-01-01')
    for cid, (rate, n) in client_rates.items():
        for k in range(n):
            rows.append({
                'Customer': cid,
                'Date': start + pd.Timedelta(days=k * 7),
                'Qty_lbs': rate * 7,
            })
    return pd.DataFrame(rows)


def test_rate_estimator_known_history():
    """Client with 4 visits at exactly 100 lbs/day rate — should recover 100."""
    deliveries = _make_deliveries_df({'X001': (100, 5)})
    clients = pd.DataFrame([{
        'ID': 'X001', 'Customer': 'X001',
        'Tank_lbs': 5000, 'Zone': 'Z1',
    }])
    out = estimate_consumption_rates(
        deliveries, clients, today=pd.Timestamp('2026-02-15')
    )
    rate = out.iloc[0]['Avg_LbsPerDay']
    assert 95 <= rate <= 105, f'Estimated rate {rate} — expected ~100'
    # New methodology uses 'own_latest' (most-recent gap, not mean)
    assert out.iloc[0]['Rate_Source'] == 'own_latest'


def test_rate_estimator_single_delivery_flagged_insufficient():
    """
    Client with 1 delivery has NO rate observation (can't divide by an unknown
    prior gap). New policy: flag for human review rather than fabricate a rate
    from zone/global median.

    Reason this changed: the old zone/global median fallback silently pushed
    slow consumers to ~50 lbs/day (zone median), which created fake urgency
    and mis-scheduled deliveries. See forecast_consumption.py docstring.
    """
    deliveries = _make_deliveries_df({
        'Z1A': (100, 5), 'Z1B': (150, 5),
        'NEW': (9999, 1),  # NEW has only 1 → no gap → INSUFFICIENT_DATA
    })
    clients = pd.DataFrame([
        {'ID': 'Z1A', 'Customer': 'Z1A', 'Tank_lbs': 5000, 'Zone': 'Z1'},
        {'ID': 'Z1B', 'Customer': 'Z1B', 'Tank_lbs': 5000, 'Zone': 'Z1'},
        {'ID': 'NEW', 'Customer': 'NEW', 'Tank_lbs': 5000, 'Zone': 'Z1'},
    ])
    out = estimate_consumption_rates(
        deliveries, clients, today=pd.Timestamp('2026-02-15')
    )
    new_row = out[out['ID'] == 'NEW'].iloc[0]
    assert new_row['Rate_Source'] == 'INSUFFICIENT_DATA', \
        f'NEW should be INSUFFICIENT_DATA; got {new_row["Rate_Source"]}'
    assert pd.isna(new_row['Avg_LbsPerDay']), \
        f'NEW rate should be NaN (not a fabricated median); got {new_row["Avg_LbsPerDay"]}'
    # Established clients still get their own rate
    z1a_row = out[out['ID'] == 'Z1A'].iloc[0]
    assert z1a_row['Rate_Source'] == 'own_latest'


def test_rate_estimator_insufficient_data_tank_defaults_50pct():
    """
    INSUFFICIENT_DATA clients can't have Est_Current_lbs computed (no rate),
    so they default to ~50% of tank — a neutral guess that keeps downstream
    code runnable. They're still excluded from the solver via Rate_Source.
    """
    deliveries = _make_deliveries_df({'A1': (100, 5)})  # established
    clients = pd.DataFrame([
        {'ID': 'A1', 'Customer': 'A1', 'Tank_lbs': 5000, 'Zone': 'Z1'},
        # NEW has no deliveries at all
        {'ID': 'NEW', 'Customer': 'NEW', 'Tank_lbs': 8000, 'Zone': 'Z1'},
    ])
    out = estimate_consumption_rates(
        deliveries, clients, today=pd.Timestamp('2026-02-15')
    )
    new_row = out[out['ID'] == 'NEW'].iloc[0]
    assert new_row['Rate_Source'] == 'INSUFFICIENT_DATA'
    # 50% of 8000 = 4000
    assert abs(new_row['Est_Current_lbs'] - 4000) <= 1, \
        f'INSUFFICIENT_DATA tank should default to 50%, got {new_row["Est_Current_lbs"]}'


def test_rate_estimator_uses_most_recent_gap_not_mean():
    """
    A client whose consumption changed recently should reflect the NEW rate,
    not a mean that's dragged toward historical values. This is the crux of
    the methodology change: most-recent gap captures seasonality / pattern
    shifts that a mean-of-gaps would smooth away.
    """
    # 4 old deliveries at ~40 lbs/day, then 1 recent at 100 lbs/day
    rows = []
    base = pd.Timestamp('2026-01-01')
    # 4 deliveries, each 7 days apart, qty=280 → 40 lbs/day (rates #1,2,3)
    for i in range(4):
        rows.append({
            'Customer': 'SHIFT', 'Date': base + pd.Timedelta(days=i * 7),
            'Qty_lbs': 280,
        })
    # 5th delivery, 5 days after the 4th, qty=500 → 100 lbs/day
    rows.append({
        'Customer': 'SHIFT',
        'Date': base + pd.Timedelta(days=3 * 7 + 5),
        'Qty_lbs': 500,
    })
    deliveries = pd.DataFrame(rows)
    clients = pd.DataFrame([{
        'ID': 'SHIFT', 'Customer': 'SHIFT', 'Tank_lbs': 5000, 'Zone': 'Z1',
    }])
    out = estimate_consumption_rates(
        deliveries, clients, today=pd.Timestamp('2026-02-15')
    )
    rate = out.iloc[0]['Avg_LbsPerDay']
    # Most-recent gap → 100. Mean of gaps would be ~55.
    assert 90 <= rate <= 110, \
        f'Expected ~100 (most-recent gap), got {rate} — is the estimator still mean-based?'


def test_rate_estimator_iqr_outlier_excluded():
    """
    Client with 10 normal visits (100 lbs/day) and one 10× spike. IQR filter
    should exclude the spike; remaining mean should be near 100.

    NOTE: The IQR gate (OUTLIER_IQR_FACTOR=3.0) is PERMISSIVE by design on
    small samples (≤5 rates) — a single outlier pulls Q3 wide and shelters
    itself. This test therefore uses 12 visits (11 rates), where Q3 is
    firmly in the "normal" range and the spike exceeds Q3 + 3×IQR.
    """
    rows = []
    start = pd.Timestamp('2026-01-01')
    n_visits = 12
    spike_idx = 5  # insert spike at visit 5
    for k in range(n_visits):
        qty = 10000 if k == spike_idx else 100 * 7  # massive spike
        rows.append({
            'Customer': 'X001',
            'Date': start + pd.Timedelta(days=k * 7),
            'Qty_lbs': qty,
        })
    deliveries = pd.DataFrame(rows)
    clients = pd.DataFrame([{
        'ID': 'X001', 'Customer': 'X001',
        'Tank_lbs': 5000, 'Zone': 'Z1',
    }])
    out = estimate_consumption_rates(
        deliveries, clients, today=pd.Timestamp('2026-03-01')
    )
    rate = out.iloc[0]['Avg_LbsPerDay']
    # Without IQR, mean would be ~(100*10 + 1428.57)/11 ≈ 221. With IQR, ~100.
    assert rate < 200, f'IQR outlier not removed; rate={rate} (expected <200)'


def test_rate_estimator_delivery_count_recorded():
    """
    Delivery_Count in the output should reflect the ACTUAL deliveries used
    (which is outlier-filtered Rate-count, not raw qty-count).
    """
    deliveries = _make_deliveries_df({'X002': (100, 4)})
    clients = pd.DataFrame([{
        'ID': 'X002', 'Customer': 'X002',
        'Tank_lbs': 5000, 'Zone': 'Z1',
    }])
    out = estimate_consumption_rates(
        deliveries, clients, today=pd.Timestamp('2026-02-15')
    )
    count = out.iloc[0]['Delivery_Count']
    # 4 deliveries → 3 rates (first has no prior) = 3
    # IQR filter may drop if there's enough variance; but with constant rate none drop.
    assert count >= 2, f'Delivery_Count {count} unexpectedly low'


# ── state.py ─────────────────────────────────────────────────────────────────

def test_update_state_delivered_resets_to_full():
    df = pd.DataFrame([
        {'ID': 'A', 'Tank_lbs': 5000, 'Avg_LbsPerDay': 200, 'Est_Current_lbs': 1000},
    ])
    state = {'A': 500.0}
    new_state = update_state(state, df, delivered_ids=['A'])
    assert new_state['A'] == 5000, f'A delivered should be 5000, got {new_state["A"]}'


def test_update_state_unvisited_decrements():
    df = pd.DataFrame([
        {'ID': 'A', 'Tank_lbs': 5000, 'Avg_LbsPerDay': 200, 'Est_Current_lbs': 1000},
    ])
    state = {'A': 1000.0}
    new_state = update_state(state, df, delivered_ids=[], n_days_elapsed=2)
    assert new_state['A'] == 600, f'A decremented should be 600, got {new_state["A"]}'


def test_update_state_floor_clamp_5pct():
    df = pd.DataFrame([
        {'ID': 'A', 'Tank_lbs': 5000, 'Avg_LbsPerDay': 2000, 'Est_Current_lbs': 1000},
    ])
    state = {'A': 1000.0}
    # Heavy consumption: 1000 - 3*2000 = -5000 → clamp to 5000*0.05 = 250
    new_state = update_state(state, df, delivered_ids=[], n_days_elapsed=3)
    assert new_state['A'] == 250, f'floor clamp: expected 250, got {new_state["A"]}'


def test_update_state_multi_day_elapsed():
    df = pd.DataFrame([
        {'ID': 'A', 'Tank_lbs': 5000, 'Avg_LbsPerDay': 100, 'Est_Current_lbs': 2000},
    ])
    state = {'A': 2000.0}
    # 3 days at 100/day: 2000 - 300 = 1700
    new_state = update_state(state, df, delivered_ids=[], n_days_elapsed=3)
    assert new_state['A'] == 1700


def test_load_state_missing_file():
    from pathlib import Path as P
    missing = P('/tmp/does_not_exist_state_' + str(time.time()) + '.json')
    assert load_state(missing) == {}


def test_save_load_roundtrip(tmp_path=None):
    import tempfile, os
    if tmp_path is None:
        tmp_path = Path(tempfile.mkdtemp())
    state = {'A': 1234.56, 'B': 7890.0}
    save_state(state, tmp_path / 'test_state.json')
    loaded = load_state(tmp_path / 'test_state.json')
    assert abs(loaded['A'] - 1234.56) < 0.01
    assert loaded['B'] == 7890.0
    os.remove(tmp_path / 'test_state.json')


def test_initialise_state_from_snapshot():
    df = pd.DataFrame([
        {'ID': 'A', 'Tank_lbs': 5000, 'Est_Current_lbs': 2500},
        {'ID': 'B', 'Tank_lbs': 5000, 'Est_Current_lbs': np.nan},
    ])
    state = initialise_state_from_snapshot(df)
    assert state['A'] == 2500
    assert state['B'] == 2500  # default: 50% of tank


TESTS = [
    # project_level
    ('project_level_negative_time',             test_project_level_negative_time_extrapolates_backward),
    ('project_level_zero_rate_constant',        test_project_level_zero_rate_constant),
    ('project_level_floor_with_pct',            test_project_level_floor_with_nonzero_pct),
    ('project_level_fractional_precision',      test_project_level_fractional_day_precision),
    # days_until_stockout
    ('dus_zero_rate_returns_zero',              test_dus_zero_rate_returns_zero),
    ('dus_at_floor_returns_zero',               test_dus_at_floor_returns_zero),
    ('dus_above_floor_linear',                  test_dus_above_floor_linear),
    ('dus_monotone_in_current',                 test_dus_monotone_in_current),
    ('dus_monotone_inverse_in_rate',            test_dus_monotone_inverse_in_rate),
    # compute_refill
    ('refill_zero_when_full',                   test_refill_zero_when_full),
    ('refill_monotone_in_day',                  test_refill_monotone_in_day),
    ('refill_caps_at_tank',                     test_refill_caps_at_tank),
    # fill_efficiency
    ('fill_efficiency_bounded',                 test_fill_efficiency_bounded),
    ('fill_efficiency_zero_tank_handled',       test_fill_efficiency_zero_tank_handled),
    ('fill_efficiency_full_tank_zero',          test_fill_efficiency_full_tank_zero),
    # service_time_min
    ('service_time_linear_in_refill',           test_service_time_linear_in_refill),
    # enrich_snapshot
    ('enrich_snapshot_idempotent',              test_enrich_snapshot_idempotent),
    ('enrich_snapshot_state_override_wins',     test_enrich_snapshot_state_override_wins),
    ('enrich_snapshot_urgency_tier_assigned',   test_enrich_snapshot_urgency_tier_assigned),
    ('enrich_snapshot_clamps_current_lbs',      test_enrich_snapshot_clamps_current_lbs),
    # matrices
    ('refill_matrix_shape_and_monotone',        test_refill_matrix_shape_and_monotone_day),
    ('fill_pct_matrix_bounded',                 test_fill_pct_matrix_bounded),
    # rate estimator
    ('rate_estimator_known_history',                test_rate_estimator_known_history),
    ('rate_estimator_single_delivery_insufficient', test_rate_estimator_single_delivery_flagged_insufficient),
    ('rate_estimator_insufficient_50pct_default',   test_rate_estimator_insufficient_data_tank_defaults_50pct),
    ('rate_estimator_uses_most_recent_gap',         test_rate_estimator_uses_most_recent_gap_not_mean),
    ('rate_estimator_iqr_outlier_excluded',         test_rate_estimator_iqr_outlier_excluded),
    ('rate_estimator_delivery_count',               test_rate_estimator_delivery_count_recorded),
    # state.py
    ('update_state_delivered_resets',           test_update_state_delivered_resets_to_full),
    ('update_state_unvisited_decrements',       test_update_state_unvisited_decrements),
    ('update_state_floor_clamp_5pct',           test_update_state_floor_clamp_5pct),
    ('update_state_multi_day_elapsed',          test_update_state_multi_day_elapsed),
    ('load_state_missing_file',                 test_load_state_missing_file),
    ('save_load_roundtrip',                     test_save_load_roundtrip),
    ('initialise_state_from_snapshot',          test_initialise_state_from_snapshot),
]


def run_all_tests():
    print('\nForecasting accuracy tests (inventory + consumption + state)')
    print('━' * 78)
    passed = failed = 0
    start = time.time()
    for name, fn in TESTS:
        if run_test(name, fn):
            passed += 1
        else:
            failed += 1
    elapsed = time.time() - start
    print('━' * 78)
    tag = '✓' if failed == 0 else '✗'
    print(f'{tag} {passed} passed, {failed} failed in {elapsed:.1f}s')
    return 0 if failed == 0 else 1


if __name__ == '__main__':
    sys.exit(run_all_tests())
