"""
Tests for v2.forecast.consumption.

Run: ./sk_venv/bin/python3 -m pytest v2/tests/test_forecast.py -v
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from v2.domain import Client  # noqa: E402
from v2.forecast.consumption import (  # noqa: E402
    estimate_consumption,
    compute_current_level,
    project_forward,
)


def _client(cid: str, tank: int = 1000) -> Client:
    return Client(
        id=cid, customer=f'Customer {cid}',
        lat=33.5, lon=-112.1,
        tank_capacity_lbs=tank, product='CANOLA',
    )


def test_steady_consumer_recovers_known_rate():
    """A client consuming exactly 100 lbs/day, refilled every 5 days at
    500 lbs, should produce a rate of 100 lbs/day."""
    dates = pd.date_range('2026-01-01', periods=10, freq='5D')
    df = pd.DataFrame({
        'Customer': ['A001'] * len(dates),
        'Date': dates,
        'Qty_lbs': [500.0] * len(dates),
    })
    clients = (_client('A001'),)
    out = estimate_consumption(df, clients, today=pd.Timestamp('2026-03-15'))
    assert 'A001' in out
    rate, std = out['A001']
    assert rate == pytest.approx(100.0, abs=0.01)
    assert std == pytest.approx(0.0, abs=0.01)


def test_placeholder_rows_excluded_by_flag():
    """Rows flagged Is_Placeholder=True must be excluded from rate math."""
    dates = pd.date_range('2026-01-01', periods=6, freq='5D')
    df = pd.DataFrame({
        'Customer': ['A001'] * 6,
        'Date': dates,
        'Qty_lbs': [500, 500, 200, 500, 500, 500],
        'Is_Placeholder': [False, False, True, False, False, False],
    })
    clients = (_client('A001'),)
    out = estimate_consumption(df, clients, today=pd.Timestamp('2026-03-15'))
    rate, _ = out['A001']
    # If the placeholder were included it would create a 10-day gap with
    # only 500 lbs → rate ~50 (and inflate the previous gap as well).
    # With it excluded, we still get 100.
    assert rate == pytest.approx(100.0, abs=0.01)


def test_placeholder_rows_excluded_by_qty_200():
    """If Is_Placeholder column is missing, Qty == 200 should be treated
    as placeholder."""
    dates = pd.date_range('2026-01-01', periods=6, freq='5D')
    df = pd.DataFrame({
        'Customer': ['A001'] * 6,
        'Date': dates,
        'Qty_lbs': [500.0, 500.0, 200.0, 500.0, 500.0, 500.0],
    })
    clients = (_client('A001'),)
    out = estimate_consumption(df, clients, today=pd.Timestamp('2026-03-15'))
    rate, _ = out['A001']
    assert rate == pytest.approx(100.0, abs=0.01)


def test_outlier_rate_removed_by_iqr():
    """A single huge spike should not pull the rate up."""
    # 9 deliveries of 500/5d → 100 lbs/day. One delivery of 5000 over
    # the same 5-day gap → 1000 lbs/day, a clear outlier.
    base_dates = pd.date_range('2026-01-01', periods=9, freq='5D')
    df = pd.DataFrame({
        'Customer': ['A001'] * 9,
        'Date': base_dates,
        'Qty_lbs': [500, 500, 500, 500, 5000, 500, 500, 500, 500],
    })
    clients = (_client('A001'),)
    out = estimate_consumption(df, clients, today=pd.Timestamp('2026-03-15'))
    rate, _ = out['A001']
    # Without outlier removal the 75th percentile would include the 1000.
    # With it removed we get back to the 100 baseline.
    assert rate == pytest.approx(100.0, abs=1.0)


def test_one_delivery_returns_zero_std():
    """A client with only one usable rate observation returns
    (last_rate, 0.0)."""
    # Two deliveries → 1 rate observation
    df = pd.DataFrame({
        'Customer': ['A001', 'A001'],
        'Date': pd.to_datetime(['2026-01-01', '2026-01-06']),
        'Qty_lbs': [500.0, 500.0],
    })
    clients = (_client('A001'),)
    out = estimate_consumption(df, clients, today=pd.Timestamp('2026-03-15'))
    rate, std = out['A001']
    assert rate == pytest.approx(100.0)
    assert std == 0.0


def test_zero_deliveries_returns_nan():
    """A client with no entries in the delivery log returns (nan, nan)."""
    df = pd.DataFrame({
        'Customer': ['A001'],
        'Date': pd.to_datetime(['2026-01-01']),
        'Qty_lbs': [500.0],
    })
    clients = (_client('A001'), _client('B999'))
    out = estimate_consumption(df, clients, today=pd.Timestamp('2026-03-15'))
    # A001 has only one delivery → zero rate observations (need a gap)
    assert math.isnan(out['A001'][0])
    assert math.isnan(out['A001'][1])
    # B999 has zero deliveries
    assert math.isnan(out['B999'][0])
    assert math.isnan(out['B999'][1])


def test_percentile_above_median_for_variable_consumer():
    """For a noisy consumer, 75th percentile should be > median."""
    # Rates: 60, 80, 100, 120, 140 lbs/day. Median = 100, 75th = 120.
    rates = [60, 80, 100, 120, 140]
    dates = pd.date_range('2026-01-01', periods=6, freq='5D')
    # Reconstruct qty values: gap=5, so qty = rate*5
    qtys = [None] + [r * 5 for r in rates]
    df = pd.DataFrame({
        'Customer': ['A001'] * 6,
        'Date': dates,
        'Qty_lbs': [500.0] + [q for q in qtys[1:]],
    })
    clients = (_client('A001'),)
    out_75 = estimate_consumption(df, clients, today=pd.Timestamp('2026-03-15'), percentile=0.75)
    out_50 = estimate_consumption(df, clients, today=pd.Timestamp('2026-03-15'), percentile=0.50)
    assert out_75['A001'][0] > out_50['A001'][0]


def test_compute_current_level_simple():
    # 1000-lb tank, last delivery 10 days ago, 50 lbs/day → current 500
    today = pd.Timestamp('2026-05-21')
    last = pd.Timestamp('2026-05-11')
    assert compute_current_level(1000, last, today, 50.0) == pytest.approx(500.0)


def test_compute_current_level_no_floor():
    # Same tank, very high consumption: should go negative (no floor)
    today = pd.Timestamp('2026-05-21')
    last = pd.Timestamp('2026-05-11')
    assert compute_current_level(1000, last, today, 150.0) == pytest.approx(-500.0)


def test_compute_current_level_nan_on_missing_date():
    today = pd.Timestamp('2026-05-21')
    assert math.isnan(compute_current_level(1000, None, today, 50.0))
    assert math.isnan(compute_current_level(1000, float('nan'), today, 50.0))


def test_project_forward_clamps():
    # 200 lbs, 100/day, 5 days forward → 0 (clamped)
    assert project_forward(200, 100, 5, 1000) == 0.0
    # Negative days would push above tank → clamped to tank
    assert project_forward(900, 100, -10, 1000) == 1000.0
    # Normal case
    assert project_forward(800, 50, 4, 1000) == pytest.approx(600.0)
