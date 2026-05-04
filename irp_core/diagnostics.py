"""
diagnostics.py — Plan-quality metrics that operators actually care about
========================================================================

Three signals make a plan "make sense":

  1. **Fill rate.** Average % of tank capacity filled per visit. A plan
     with 95% avg fill is operationally efficient (truck-time per pound
     delivered is small). 50% fill means we're driving a lot to put a
     little oil in tanks that didn't need much. SK's actual delivery
     log has mean fill 78% — that's the bar.

  2. **Geographic coherence.** Same micro-area visited multiple days
     in a week is wasteful. A coherent plan keeps Peoria-area clients
     on Tuesday, west-valley on Wednesday, etc. We measure this by
     "neighborhood revisits" — pairs of clients within N miles that
     were assigned to different days.

  3. **Plan stability.** What % of yesterday's tentative day-1 visits
     became today's day-0 visits unchanged. High stability = drivers
     trust the system; low = whiplash.

These are plan-level metrics, computed from the routes DataFrame.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import radians, cos, sin, asin, sqrt
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd


# ─────────────────────────────────────────────────────────────────────────────
# Per-plan quality report
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class PlanQuality:
    avg_fill_pct: float                 # mean fill % across all visits
    pct_low_fill_visits: float          # share of visits with fill < 50%
    pct_high_fill_visits: float         # share with fill ≥ 80%
    neighborhood_splits: int            # client pairs <8mi apart on different days
    neighborhood_split_pct: float       # / total nearby pairs
    same_day_avg_dist_mi: float         # avg pairwise dist for same-day stops
    diff_day_min_dist_mi: float         # min pairwise dist for cross-day pairs (smaller = bigger smell)
    visits_per_day: Dict[int, int]      # day_idx -> count
    deferred_with_low_dts: int          # deferred clients whose P95 stockout was small (real risk)
    fill_dollars_per_mile: float        # $-equivalent of lbs filled / mile driven (efficiency)


def compute_plan_quality(
    *,
    routes: Dict[int, pd.DataFrame],
    clients_df: pd.DataFrame,
    deferred: Optional[pd.DataFrame] = None,
    nearby_radius_mi: float = 8.0,
) -> PlanQuality:
    """Compute the metrics above. Pure function, no I/O."""
    visits_rows = []
    for d, df in routes.items():
        if df is None or df.empty:
            continue
        for _, r in df.iterrows():
            cid = str(r.get('ID', ''))
            tank = float(r.get('Tank_lbs', 0) or 0)
            refill = float(r.get('Refill_lbs', 0) or 0)
            visits_rows.append({
                'day': int(d),
                'client_id': cid,
                'tank_lbs': tank,
                'refill_lbs': refill,
                'fill_pct': (refill / tank) if tank > 0 else 0.0,
            })

    if not visits_rows:
        return PlanQuality(
            avg_fill_pct=0.0, pct_low_fill_visits=0.0, pct_high_fill_visits=0.0,
            neighborhood_splits=0, neighborhood_split_pct=0.0,
            same_day_avg_dist_mi=0.0, diff_day_min_dist_mi=0.0,
            visits_per_day={}, deferred_with_low_dts=0,
            fill_dollars_per_mile=0.0,
        )

    visits = pd.DataFrame(visits_rows)
    avg_fill = float(visits['fill_pct'].mean())
    low = float((visits['fill_pct'] < 0.50).mean())
    high = float((visits['fill_pct'] >= 0.80).mean())

    # ── Geographic coherence ───────────────────────────────────────────
    coords = clients_df[['ID', 'Lat', 'Lon']].copy()
    coords['ID'] = coords['ID'].astype(str)
    visits = visits.merge(coords, left_on='client_id', right_on='ID', how='left')

    # Pairwise distances (only for clients with valid coords)
    valid = visits[visits['Lat'].notna() & visits['Lon'].notna()].reset_index(drop=True)
    n = len(valid)
    near_pairs = 0
    near_split = 0
    same_day_dists = []
    diff_day_dists = []
    if n > 1:
        for i in range(n):
            for j in range(i + 1, n):
                d = _haversine_mi(
                    valid.loc[i, 'Lat'], valid.loc[i, 'Lon'],
                    valid.loc[j, 'Lat'], valid.loc[j, 'Lon'],
                )
                same_day = valid.loc[i, 'day'] == valid.loc[j, 'day']
                if d <= nearby_radius_mi:
                    near_pairs += 1
                    if not same_day:
                        near_split += 1
                if same_day:
                    same_day_dists.append(d)
                else:
                    diff_day_dists.append(d)

    same_day_avg = float(np.mean(same_day_dists)) if same_day_dists else 0.0
    diff_day_min = float(np.min(diff_day_dists)) if diff_day_dists else 0.0

    visits_per_day = visits.groupby('day').size().to_dict()
    visits_per_day = {int(k): int(v) for k, v in visits_per_day.items()}

    deferred_low = 0
    if deferred is not None and not deferred.empty:
        if 'Days_Until_Stockout_P95' in deferred.columns:
            deferred_low = int((deferred['Days_Until_Stockout_P95'].fillna(99) < 5).sum())
        elif 'Days_Until_Stockout' in deferred.columns:
            deferred_low = int((deferred['Days_Until_Stockout'].fillna(99) < 3).sum())

    # Fill dollars per mile: rough efficiency proxy
    total_lbs = float(visits['refill_lbs'].sum())
    total_miles = 0.0
    for d, df in routes.items():
        if df is None or df.empty:
            continue
        if 'Truck' in df.columns and 'Cum_Dist_mi' in df.columns:
            total_miles += float(df.groupby('Truck')['Cum_Dist_mi'].max().sum())
        elif 'Cum_Dist_mi' in df.columns:
            total_miles += float(df['Cum_Dist_mi'].max() or 0)
    fill_per_mi = (total_lbs / total_miles) if total_miles > 0 else 0.0

    return PlanQuality(
        avg_fill_pct=avg_fill,
        pct_low_fill_visits=low,
        pct_high_fill_visits=high,
        neighborhood_splits=near_split,
        neighborhood_split_pct=(near_split / near_pairs) if near_pairs > 0 else 0.0,
        same_day_avg_dist_mi=same_day_avg,
        diff_day_min_dist_mi=diff_day_min,
        visits_per_day=visits_per_day,
        deferred_with_low_dts=deferred_low,
        fill_dollars_per_mile=fill_per_mi,
    )


def pretty_print_quality(q: PlanQuality) -> None:
    """One-screen summary suitable for stdout or a Slack post."""
    print('\n  ── Plan Quality ────────────────────────────────────────────')
    print(f'  Avg fill / visit:     {q.avg_fill_pct:>5.0%}   '
          f'(target ≥75% to match SK manual)')
    print(f'  Low-fill (<50%):      {q.pct_low_fill_visits:>5.0%}   '
          f'(target ≤10%)')
    print(f'  High-fill (≥80%):     {q.pct_high_fill_visits:>5.0%}   '
          f'(higher is better)')
    print(f'  Neighborhood splits:  {q.neighborhood_splits} pair(s) '
          f'within 8mi on different days  ({q.neighborhood_split_pct:.0%})')
    print(f'  Same-day pair dist:   {q.same_day_avg_dist_mi:>5.1f} mi avg')
    print(f'  Lbs filled / mile:    {q.fill_dollars_per_mile:>5.1f}')
    print(f'  Visits per day:       {dict(sorted(q.visits_per_day.items()))}')
    if q.deferred_with_low_dts > 0:
        print(f'  ⚠  Deferred-but-at-risk: {q.deferred_with_low_dts} client(s) '
              f'with P95 stockout < 5 days')


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _haversine_mi(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in miles."""
    lat1, lon1, lat2, lon2 = map(radians, (lat1, lon1, lat2, lon2))
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = sin(dlat/2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon/2) ** 2
    return 2 * 3958.8 * asin(sqrt(a))


# ─────────────────────────────────────────────────────────────────────────────
# Historical fill-rate audit (delivery log)
# ─────────────────────────────────────────────────────────────────────────────

def audit_historical_fill_rates(
    deliveries: pd.DataFrame,
    clients: pd.DataFrame,
    *,
    horizon_days: int = 90,
) -> pd.DataFrame:
    """
    Per-client report of historical fill rates from the delivery log.
    Useful for ops review: "which clients are we systematically
    under-filling?"

    Returns DataFrame: ID, Customer, n_deliveries, mean_fill_pct,
    median_fill_pct, p25_fill_pct, low_fill_count, low_fill_pct.
    """
    cutoff = pd.Timestamp.today() - pd.Timedelta(days=horizon_days)
    recent = deliveries[deliveries['Date'] >= cutoff].copy()
    cust_to_id = dict(zip(clients['Customer'], clients['ID'].astype(str)))
    cust_to_tank = dict(zip(clients['Customer'], clients['Tank_lbs']))

    rows = []
    for cust, group in recent.groupby('Customer'):
        cid = cust_to_id.get(cust)
        tank = cust_to_tank.get(cust, 0)
        if not cid or not tank:
            continue
        fills = (group['Qty_lbs'] / tank).clip(lower=0, upper=2)  # >100% = multi-compartment
        rows.append({
            'ID': cid,
            'Customer': cust,
            'n_deliveries': len(fills),
            'mean_fill_pct': float(fills.mean()),
            'median_fill_pct': float(fills.median()),
            'p25_fill_pct': float(fills.quantile(0.25)),
            'low_fill_count': int((fills < 0.50).sum()),
            'low_fill_pct': float((fills < 0.50).mean()),
        })
    return pd.DataFrame(rows).sort_values('mean_fill_pct').reset_index(drop=True)
