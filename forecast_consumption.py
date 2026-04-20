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
    client_stats = (
        clean_rates.groupby('Customer')
        .agg(
            Avg_LbsPerDay   = ('Rate', 'mean'),
            Delivery_Count  = ('Rate', 'count'),
        )
        .reset_index()
    )

    last_delivery = (
        dl.groupby('Customer')['Date'].max().reset_index()
        .rename(columns={'Date': 'Last_Date'})
    )
    client_stats = client_stats.merge(last_delivery, on='Customer', how='left')

    # ── Step 4: Merge into clients_df ─────────────────────────────────────────
    result = clients_df.copy()
    result = result.merge(
        client_stats[['Customer', 'Avg_LbsPerDay', 'Delivery_Count', 'Last_Date']],
        on='Customer', how='left',
    )
    result = result.merge(last_delivery, on='Customer', how='left', suffixes=('', '_dup'))
    if 'Last_Date_dup' in result.columns:
        result['Last_Date'] = result['Last_Date'].fillna(result['Last_Date_dup'])
        result.drop(columns='Last_Date_dup', inplace=True)

    # ── Step 5: Fill missing rates with zone → global medians ─────────────────
    # Clients with fewer than MIN_DELIVERIES_FOR_OWN_RATE are treated as missing.
    needs_fallback = result['Delivery_Count'].isna() | (
        result['Delivery_Count'] < MIN_DELIVERIES_FOR_OWN_RATE
    )
    result.loc[needs_fallback, 'Avg_LbsPerDay'] = np.nan

    global_median = _safe_median(result['Avg_LbsPerDay'])
    result['Rate_Source'] = 'own'

    for zone in result['Zone'].unique():
        mask_zone    = result['Zone'] == zone
        zone_median  = _safe_median(result.loc[mask_zone, 'Avg_LbsPerDay'], global_median)
        needs_zone   = mask_zone & result['Avg_LbsPerDay'].isna()
        result.loc[needs_zone, 'Avg_LbsPerDay'] = zone_median
        result.loc[needs_zone, 'Rate_Source']   = 'zone_median'

    still_missing = result['Avg_LbsPerDay'].isna()
    result.loc[still_missing, 'Avg_LbsPerDay'] = global_median
    result.loc[still_missing, 'Rate_Source']   = 'global_median'
    result['Delivery_Count'] = result['Delivery_Count'].fillna(0).astype(int)

    # ── Step 6: Current inventory estimate ────────────────────────────────────
    result['Days_Since_Last'] = (today - result['Last_Date']).dt.days
    # Brand-new clients with no delivery history get FALLBACK_DAYS_SINCE,
    # which means we conservatively assume they are somewhat depleted.
    result['Days_Since_Used'] = result['Days_Since_Last'].fillna(FALLBACK_DAYS_SINCE)

    result['Est_Current_lbs'] = (
        result['Tank_lbs']
        - result['Days_Since_Used'] * result['Avg_LbsPerDay']
    ).clip(lower=result['Tank_lbs'] * 0.03,
           upper=result['Tank_lbs'])         # Can't exceed tank capacity
    result['Est_Current_lbs'] = result['Est_Current_lbs'].round()

    # ── Summary ────────────────────────────────────────────────────────────────
    src_counts = result['Rate_Source'].value_counts()
    print(f"  Consumption rates:  "
          f"own={src_counts.get('own', 0)}  "
          f"zone_median={src_counts.get('zone_median', 0)}  "
          f"global_median={src_counts.get('global_median', 0)}")
    print(f"  Global median rate: {global_median:.1f} lbs/day")

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
