"""
forecast_consumption.py
=======================
Estimates each client's average daily oil consumption (lbs/day) from
the historical delivery log.

Because S&K always fills tanks to 100 %, the quantity delivered at each
visit is exactly the oil consumed since the previous visit:

    Qty_delivered = Tank_lbs - Level_before_visit
                  = consumption_rate × days_since_last

So:   rate(i, delivery k) = Qty_k / Days_since_last_k

This is algebraically exact — no "tank was full" assumption needed
(it IS always full after delivery, by policy).

Outlier filtering uses a statistical IQR gate (configurable) rather
than a hard cap, so high-volume commercial clients are never silently
penalised.
"""

import numpy as np
import pandas as pd
from config import (
    MIN_DELIVERIES_FOR_OWN_RATE,
    OUTLIER_IQR_FACTOR,
    FALLBACK_DAYS_SINCE,
)


def estimate_consumption_rates(
    deliveries_df: pd.DataFrame,
    clients_df: pd.DataFrame,
    today: pd.Timestamp | None = None,
) -> pd.DataFrame:
    """
    Compute Avg_LbsPerDay for every client in clients_df.

    Fallback hierarchy (applied per client):
        1. Client's own historical average (needs ≥ MIN_DELIVERIES_FOR_OWN_RATE)
        2. Zone median
        3. Global median

    Returns clients_df extended with:
        Avg_LbsPerDay    : estimated daily consumption (lbs/day)
        Rate_Source      : 'own' | 'zone_median' | 'global_median'
        Delivery_Count   : number of deliveries used
        Last_Date        : date of most-recent delivery
        Days_Since_Last  : calendar days since that delivery
        Est_Current_lbs  : estimated current tank level (lbs)
    """
    if today is None:
        today = pd.Timestamp.today().normalize()

    # ── Step 1: Per-delivery lbs/day rates ────────────────────────────────────
    dl = deliveries_df.sort_values(['Customer', 'Date']).copy()
    dl['Prev_Date'] = dl.groupby('Customer')['Date'].shift(1)
    dl['Days_Gap']  = (dl['Date'] - dl['Prev_Date']).dt.days

    # Rate is only defined for deliveries with a known prior delivery
    dl['Rate'] = np.where(
        (dl['Days_Gap'] > 0) & dl['Days_Gap'].notna(),
        dl['Qty_lbs'] / dl['Days_Gap'],
        np.nan,
    )

    # ── Step 2: Remove statistical outliers per client ────────────────────────
    # Use IQR method; a hard cap would silently exclude large accounts.
    clean_rates = _remove_outliers(dl[dl['Rate'].notna()].copy())

    # ── Step 3: Per-client summary ────────────────────────────────────────────
    # Use MEDIAN rate for clients with ≥3 deliveries (robust against one-off
    # spikes from early re-deliveries or emergency top-offs).  Fall back to
    # 'last' only for 1-2 delivery clients where median isn't meaningful.
    #
    # History: original code used ('Rate', 'last') to match SK's own
    # methodology. Backtest showed this inflates demand ~18% — 24 clients
    # had rates >1.5x their actual consumption because a single short gap
    # (early re-delivery) produced an artificially high rate. Median is
    # resistant to these one-off spikes while still reflecting true demand.
    clean_rates_sorted = clean_rates.sort_values(['Customer', 'Date'])
    client_stats = (
        clean_rates_sorted.groupby('Customer')
        .agg(
            Rate_Last        = ('Rate', 'last'),
            Rate_Median      = ('Rate', 'median'),
            Delivery_Count   = ('Rate', 'count'),
        )
        .reset_index()
    )
    # Primary rate: median if we have ≥3 data points, else last (only option)
    client_stats['Avg_LbsPerDay'] = np.where(
        client_stats['Delivery_Count'] >= 3,
        client_stats['Rate_Median'],
        client_stats['Rate_Last'],
    )

    # Consumption-trend flag: if the most-recent gap differs from the client's
    # historical median by more than CONSUMPTION_SHIFT_PCT, mark as 'shift' so
    # operators can review (could be real seasonal change OR a noisy one-off).
    _CONSUMPTION_SHIFT_PCT = 0.50  # 50% deviation threshold
    client_stats['Consumption_Shift'] = np.where(
        (client_stats['Rate_Median'] > 0)
        & (client_stats['Delivery_Count'] >= 3)
        & (abs(client_stats['Rate_Last'] - client_stats['Rate_Median'])
           / client_stats['Rate_Median'] > _CONSUMPTION_SHIFT_PCT),
        'shift',
        'stable',
    )

    last_delivery = (
        dl.groupby('Customer')['Date'].max().reset_index()
        .rename(columns={'Date': 'Last_Date'})
    )
    client_stats = client_stats.merge(last_delivery, on='Customer', how='left')

    # ── Step 4: Merge into clients_df ─────────────────────────────────────────
    result = clients_df.copy()
    result = result.merge(
        client_stats[['Customer', 'Avg_LbsPerDay', 'Rate_Last', 'Rate_Median',
                      'Delivery_Count', 'Consumption_Shift', 'Last_Date']],
        on='Customer', how='left',
    )
    # Clients with no/insufficient history get 'stable' default for Shift flag
    result['Consumption_Shift'] = result['Consumption_Shift'].fillna('stable')
    result = result.merge(last_delivery, on='Customer', how='left', suffixes=('', '_dup'))
    if 'Last_Date_dup' in result.columns:
        result['Last_Date'] = result['Last_Date'].fillna(result['Last_Date_dup'])
        result.drop(columns='Last_Date_dup', inplace=True)

    # ── Step 5: Tag clients without enough data — DO NOT fabricate a rate ────
    # Rationale (per SK): a single delivery with no prior visit gives no rate
    # observation at all (we can't divide by an unknown gap), and a made-up
    # zone/global median routinely over-estimates slow consumers (e.g., 51ST
    # at 6.9 lbs/day got pushed to 50.7 by zone median). Better to surface the
    # client for human review than to schedule on a fabricated rate.
    result['Delivery_Count'] = result['Delivery_Count'].fillna(0).astype(int)
    result['Rate_Source'] = np.where(
        result['Delivery_Count'] >= 3, 'own_median', 'own_latest'
    )

    insufficient = result['Avg_LbsPerDay'].isna() | (result['Delivery_Count'] < 1)
    result.loc[insufficient, 'Avg_LbsPerDay'] = np.nan
    result.loc[insufficient, 'Rate_Source']   = 'INSUFFICIENT_DATA'

    global_median = _safe_median(result.loc[~insufficient, 'Avg_LbsPerDay'])

    # ── Step 6: Current inventory estimate ────────────────────────────────────
    result['Days_Since_Last'] = (today - result['Last_Date']).dt.days
    # Brand-new clients with no delivery history get FALLBACK_DAYS_SINCE,
    # which means we conservatively assume they are somewhat depleted.
    result['Days_Since_Used'] = result['Days_Since_Last'].fillna(FALLBACK_DAYS_SINCE)

    # For INSUFFICIENT_DATA clients we cannot estimate current level — use the
    # conservative assumption that tank is ~half full (they were delivered
    # recently enough to still be on our books) so downstream code still runs.
    # These clients will be excluded from the optimizer via the Rate_Source flag.
    rate_for_calc = result['Avg_LbsPerDay'].fillna(0)
    result['Est_Current_lbs'] = (
        result['Tank_lbs']
        - result['Days_Since_Used'] * rate_for_calc
    ).clip(lower=result['Tank_lbs'] * 0.03,
           upper=result['Tank_lbs'])         # Can't exceed tank capacity
    # Override: insufficient-data clients default to 50% tank
    mask_insuf = result['Rate_Source'] == 'INSUFFICIENT_DATA'
    result.loc[mask_insuf, 'Est_Current_lbs'] = (result.loc[mask_insuf, 'Tank_lbs'] * 0.5).round()
    result['Est_Current_lbs'] = result['Est_Current_lbs'].round()

    # ── Summary ────────────────────────────────────────────────────────────────
    src_counts = result['Rate_Source'].value_counts()
    print(f"  Consumption rates:  "
          f"own_median={src_counts.get('own_median', 0)}  "
          f"own_latest={src_counts.get('own_latest', 0)}  "
          f"INSUFFICIENT_DATA={src_counts.get('INSUFFICIENT_DATA', 0)}")
    if global_median is not None and not np.isnan(global_median):
        print(f"  Global median rate (reference only): {global_median:.1f} lbs/day")

    return result


# ── Helpers ───────────────────────────────────────────────────────────────────

def _remove_outliers(df: pd.DataFrame) -> pd.DataFrame:
    """
    Remove per-delivery rates that are extreme outliers (IQR method),
    computed within each client group to preserve large-account rates.
    """
    cleaned = []
    for customer, group in df.groupby('Customer'):
        rates = group['Rate'].dropna()
        if len(rates) < 3:
            cleaned.append(group)
            continue
        q1, q3 = rates.quantile([0.25, 0.75])
        iqr    = q3 - q1
        upper  = q3 + OUTLIER_IQR_FACTOR * iqr
        # No lower bound — very small rates are legitimate (slow consumption)
        cleaned.append(group[group['Rate'] <= upper])
    return pd.concat(cleaned, ignore_index=True)


def _safe_median(series: pd.Series, fallback: float = 50.0) -> float:
    m = series.dropna().median()
    return float(m) if not np.isnan(m) else fallback
