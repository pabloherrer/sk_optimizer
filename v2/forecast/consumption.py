"""
v2.forecast.consumption — per-client consumption rate estimator.

Computes lbs/day consumption for each client from the historical delivery
log. The math is exact (no "tank was full" guess): because S&K policy is
to top each tank to 100%, the quantity delivered at visit k equals the
oil consumed between visit k-1 and visit k. So:

    rate_k = qty_lbs_k / days_since_previous_delivery_k

We aggregate these per-delivery rates into a single client rate using a
configurable percentile (default 75th). The 75th percentile is a single
parameter that doubles as built-in safety stock: by planning against a
slightly-busier-than-typical day we naturally cover ~75% of variability
without needing a separate "safety days" knob.

Outlier rates are removed per-client via IQR (factor=3.0) before
aggregation, so a one-off short gap (emergency top-off) does not bias
the rate upward. Placeholder rows (Qty == 200 lbs or Is_Placeholder=True)
are excluded entirely — they record that a visit happened but not how
much was pumped.

Returned dict keys: client_id (str). Value: (rate_lbs_per_day, std_dev).
Clients with zero usable observations get (nan, nan); callers must
treat them as "insufficient data" rather than fabricate a rate.
"""
from __future__ import annotations

import math
from typing import Tuple

import numpy as np
import pandas as pd

from v2.domain import Client


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def estimate_consumption(
    deliveries_df: pd.DataFrame,
    clients: tuple[Client, ...],
    today: pd.Timestamp,
    percentile: float = 0.75,
) -> dict[str, tuple[float, float]]:
    """
    Estimate per-client consumption rate (lbs/day) and its std dev.

    Parameters
    ----------
    deliveries_df : DataFrame
        Must contain columns: Customer, Date, Qty_lbs. May contain
        Is_Placeholder (bool); if absent, rows with Qty_lbs == 200.0 are
        treated as placeholders.
    clients : tuple[Client, ...]
        The set of clients we care about. The returned dict is keyed by
        client.id; clients absent from the delivery log return (nan, nan).
    today : pd.Timestamp
        Reference date (unused for the rate itself, but accepted so the
        function signature is stable for callers that pass it).
    percentile : float, default 0.75
        Percentile of cleaned rates to use as the client's rate. 0.5 =
        median, 0.75 = mild safety stock, etc.

    Returns
    -------
    dict[str, tuple[float, float]]
        {client_id: (rate_lbs_per_day, rate_std_dev)}. Clients with zero
        observations after cleaning return (nan, nan); 1-2 observations
        return (last_observed_rate, 0.0).
    """
    if not (0.0 < percentile < 1.0):
        raise ValueError(f'percentile must be in (0, 1); got {percentile}')

    client_ids = tuple(c.id for c in clients)
    result: dict[str, tuple[float, float]] = {cid: (float('nan'), float('nan')) for cid in client_ids}

    if deliveries_df is None or deliveries_df.empty:
        return result

    df = deliveries_df.copy()
    # Normalize types
    df['Customer'] = df['Customer'].astype(str)
    df['Date'] = pd.to_datetime(df['Date'])
    df['Qty_lbs'] = pd.to_numeric(df['Qty_lbs'], errors='coerce')

    # Exclude placeholders
    if 'Is_Placeholder' in df.columns:
        is_placeholder = df['Is_Placeholder'].astype(bool)
    else:
        is_placeholder = df['Qty_lbs'] == 200.0
    df = df.loc[~is_placeholder].copy()

    # Per-delivery rate = Qty / Days_Since_Previous_For_Same_Customer
    df = df.sort_values(['Customer', 'Date'])
    df['Prev_Date'] = df.groupby('Customer')['Date'].shift(1)
    df['Days_Gap'] = (df['Date'] - df['Prev_Date']).dt.days
    df['Rate'] = np.where(
        df['Days_Gap'].notna() & (df['Days_Gap'] > 0) & df['Qty_lbs'].notna(),
        df['Qty_lbs'] / df['Days_Gap'],
        np.nan,
    )

    rated = df.loc[df['Rate'].notna(), ['Customer', 'Date', 'Rate']].copy()
    if rated.empty:
        return result

    # Per-client outlier removal (IQR, factor 3.0) then aggregate
    for cid in client_ids:
        rates = rated.loc[rated['Customer'] == cid, 'Rate'].to_numpy(dtype=float)
        if rates.size == 0:
            continue
        cleaned = _iqr_filter(rates, factor=3.0)
        if cleaned.size == 0:
            # Outlier filter wiped everything (shouldn't happen with factor=3)
            # — fall back to last observed.
            last_rate = float(rates[-1])
            result[cid] = (last_rate, 0.0)
            continue
        if cleaned.size >= 3:
            rate = float(np.quantile(cleaned, percentile))
            std = float(np.std(cleaned, ddof=1)) if cleaned.size >= 2 else 0.0
            result[cid] = (rate, std)
        else:
            # 1 or 2 observations: percentile not meaningful → use last.
            # cleaned preserves time order because rated is sorted.
            last_rate = float(cleaned[-1])
            result[cid] = (last_rate, 0.0)

    return result


def compute_current_level(
    tank_lbs: float,
    last_delivery_date,
    today,
    rate_lbs_per_day: float,
) -> float:
    """
    Linear projection of current tank level from last-known-full to today.

    `level = tank_lbs - days_since_last_delivery * rate`. No floor: we
    let it go to zero (and even negative; the caller is responsible for
    clamping if needed). Returns nan if last_delivery_date is missing.
    """
    if last_delivery_date is None or (isinstance(last_delivery_date, float) and math.isnan(last_delivery_date)):
        return float('nan')
    try:
        last = pd.Timestamp(last_delivery_date)
        now = pd.Timestamp(today)
    except (TypeError, ValueError):
        return float('nan')
    if pd.isna(last) or pd.isna(now):
        return float('nan')
    days_since = (now - last).days
    return float(tank_lbs) - float(days_since) * float(rate_lbs_per_day)


def project_forward(
    current_lbs: float,
    rate_lbs_per_day: float,
    days_forward: float,
    tank_lbs: float,
) -> float:
    """
    Project a tank level `days_forward` days into the future, clamped
    to [0, tank_lbs].
    """
    projected = float(current_lbs) - float(days_forward) * float(rate_lbs_per_day)
    if projected < 0.0:
        projected = 0.0
    if projected > tank_lbs:
        projected = float(tank_lbs)
    return projected


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _iqr_filter(values: np.ndarray, factor: float = 3.0) -> np.ndarray:
    """
    Drop entries whose value > Q3 + factor*IQR. No lower cap: very low
    rates are legitimate (slow consumers). Below 3 observations we
    return values unchanged (IQR is not meaningful at n<3).
    """
    if values.size < 3:
        return values
    q1, q3 = np.quantile(values, [0.25, 0.75])
    iqr = q3 - q1
    upper = q3 + factor * iqr
    return values[values <= upper]
