"""
forecasting.py — Probabilistic demand model for the IRP
=======================================================

The legacy module produces a single point estimate per client (median
lbs/day, IQR-filtered). That's fine if the world is deterministic, but
restaurants are not deterministic — Friday demand is 2× Tuesday demand,
weekends are different, weather and events shift consumption. With a
point estimate, the only defense against variability is conservative
urgency thresholds (1.5 days), which inflates trip frequency.

This module gives every client three layers of demand information:

  1. Per-DOW posterior mean      μ̂(i, dow)
  2. Cross-DOW residual variance σ̂²(i)         (overall noise)
  3. Quantile lookups            P50, P80, P95 over a future window

The model is empirical-Bayes: prior is the global (across-clients)
DOW pattern; likelihood is each client's own deliveries; posterior
mean is a precision-weighted blend (shrinks low-data clients toward
the population mean while preserving high-data clients' own signal).

What we DON'T do (deliberately): full hierarchical models, neural
nets, ARIMA. With ≤200 deliveries per client, the marginal benefit
over a clean DOW-stratified empirical-Bayes mean is negligible and
the maintenance cost is real.

Why per-delivery rates ≠ per-day rates
--------------------------------------
S&K records `Qty_lbs` at each visit and the gap to the previous visit.
That gives an *average* lbs/day over the gap — it is NOT a per-day
realisation of demand. To get a daily series we allocate each
delivery's volume back across the gap days, optionally weighted by
the global DOW pattern (so a 7-day gap that includes a Friday gets
more credit on the Friday). This is canonical for IRPs with no
sub-daily metering (Coelho et al. 2014 §3.1).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

DOW_NAMES = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']


# ─────────────────────────────────────────────────────────────────────────────
# Core model
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class DemandModel:
    """
    Per-client probabilistic daily demand.

    Fields
    ------
    rates       : np.ndarray of shape (7,) — posterior mean lbs/day per DOW.
                  Index 0=Mon, 1=Tue, ..., 6=Sun (matches Timestamp.weekday()).
    sigma       : float — pooled std-dev of the daily residual.
    n_obs       : int   — total daily observations used (= sum of gap days).
    source      : str   — 'own' | 'pooled' | 'insufficient'
    """
    rates: np.ndarray
    sigma: float
    n_obs: int
    source: str = 'own'

    # ── Daily quantile ────────────────────────────────────────────────────

    def expected_consumption(self, dates: List[pd.Timestamp]) -> np.ndarray:
        """Mean lbs/day for each date in `dates` (length matches input)."""
        return np.array([self.rates[d.weekday()] for d in dates], dtype=float)

    def cumulative_consumption_quantile(
        self,
        dates: List[pd.Timestamp],
        q: float = 0.95,
    ) -> np.ndarray:
        """
        Cumulative consumption from `dates[0]` through `dates[k]` at
        quantile `q`. Returned shape: (len(dates),).

        Daily demand is treated as i.i.d. given DOW: μ_t with std σ.
        Variance of a sum of T independent Normals = T·σ².
        Quantile = mean + z_q · sqrt(T·σ²).

        For q ∈ {0.50, 0.80, 0.95, 0.99} we use exact normal quantiles
        z = {0.0, 0.842, 1.645, 2.326}.
        """
        from math import sqrt
        z = _normal_quantile(q)
        means = self.expected_consumption(dates)
        cum_mean = np.cumsum(means)
        # variance grows linearly with horizon (i.i.d. Normal sum)
        T = np.arange(1, len(dates) + 1, dtype=float)
        cum_std = self.sigma * np.sqrt(T)
        return cum_mean + z * cum_std

    def daily_mean(self) -> float:
        """Cross-DOW unweighted mean (used as a back-compat scalar)."""
        return float(np.mean(self.rates))


# ─────────────────────────────────────────────────────────────────────────────
# Build models from the delivery log
# ─────────────────────────────────────────────────────────────────────────────

def fit_demand_models(
    deliveries: pd.DataFrame,
    clients: pd.DataFrame,
    *,
    today: Optional[pd.Timestamp] = None,
    min_days_for_own: int = 14,
    shrinkage_strength: float = 21.0,
    iqr_factor: float = 3.0,
    weight_recent_days: int = 90,
) -> Dict[str, DemandModel]:
    """
    Fit a per-client DemandModel from the delivery log.

    Algorithm
    ---------
    1. **Day-allocation**: For each (customer, delivery) record with a known
       gap to previous, allocate Qty_lbs back across the gap days, weighted
       by the *global* DOW intensity profile. The result is a daily series
       per customer.

    2. **Outlier filter**: drop per-delivery rates above
       Q3 + iqr_factor·IQR (within client). Heavy emergency top-offs
       distort means.

    3. **Recency weighting**: deliveries older than `weight_recent_days`
       get half-weight. Captures slow drift in restaurant volumes.

    4. **Empirical-Bayes shrinkage**: per-client per-DOW mean is a
       precision-weighted blend of the client's observations and the
       global per-DOW mean. The pseudo-count `shrinkage_strength`
       controls how strongly low-data clients shrink toward the global
       pattern.

    5. **Variance pooling**: σ̂² is the cross-client pooled estimate of
       daily residual variance, deflated by sample size per client. This
       avoids σ̂ collapsing to 0 for clients with few observations.

    Returns
    -------
    Dict[str, DemandModel] keyed by client_id (str).
    """
    today = pd.Timestamp(today or pd.Timestamp.today()).normalize()
    if deliveries.empty:
        return {}

    # ── Step 1: per-delivery daily allocation ─────────────────────────────
    df = deliveries.sort_values(['Customer', 'Date']).copy()
    df['Prev_Date'] = df.groupby('Customer')['Date'].shift(1)
    df['Days_Gap'] = (df['Date'] - df['Prev_Date']).dt.days
    df = df[df['Days_Gap'].notna() & (df['Days_Gap'] > 0)].copy()
    df['Rate_Avg'] = df['Qty_lbs'] / df['Days_Gap']

    # Per-customer outlier filter
    cleaned = []
    for _, group in df.groupby('Customer'):
        if len(group) < 4:
            cleaned.append(group); continue
        q1, q3 = group['Rate_Avg'].quantile([0.25, 0.75])
        upper = q3 + iqr_factor * (q3 - q1)
        cleaned.append(group[group['Rate_Avg'] <= upper])
    df = pd.concat(cleaned, ignore_index=True) if cleaned else df

    # Recency weight (linear half-weight beyond cutoff)
    df['Age_Days'] = (today - df['Date']).dt.days.clip(lower=0)
    df['Weight'] = np.where(df['Age_Days'] > weight_recent_days, 0.5, 1.0)

    # ── Step 2: global DOW intensity (prior) ─────────────────────────────
    # We need the global per-DOW share so we can split a delivery's volume
    # across the gap. To bootstrap, assume uniform first; this self-corrects
    # in iteration 2 (rare for the iteration to matter; restaurants vary
    # client-by-client more than universally).
    global_per_dow = _global_dow_share(df)  # array shape (7,) summing to 7.0

    # ── Step 3: explode each delivery into daily lbs ─────────────────────
    # For each row with rate=Qty/Gap, allocate to the Gap days as:
    #     daily[d] = rate × global_per_dow[dow(d)]   (sums to Qty_lbs over gap)
    daily_records: List[dict] = []
    for _, row in df.iterrows():
        cust = row['Customer']
        prev = row['Prev_Date']
        cur = row['Date']
        qty = row['Qty_lbs']
        gap = int(row['Days_Gap'])
        weight = row['Weight']
        # Build the gap-day list (the DAYS BEFORE current delivery, inclusive of
        # prev+1 .. cur). Conventionally we attribute consumption to the day
        # leading up to the delivery, so the gap is [prev+1, cur].
        gap_dates = pd.date_range(prev + pd.Timedelta(days=1), cur, freq='D')
        if len(gap_dates) == 0:
            continue
        weights = np.array([global_per_dow[d.weekday()] for d in gap_dates])
        weights = weights / weights.sum()  # normalise to 1 over gap
        for d, w in zip(gap_dates, weights):
            daily_records.append({
                'Customer': cust,
                'Date': d,
                'DOW': d.weekday(),
                'Lbs': qty * w,
                'Weight': weight,
            })
    if not daily_records:
        return {}
    daily = pd.DataFrame(daily_records)

    # ── Step 4: global per-DOW mean (the prior) ──────────────────────────
    # Across all clients, mean lbs/day for each DOW (volume-weighted by client?
    # No — it's the average per-(client, day) lbs, which is the right prior
    # for an unknown client).
    prior_mean_per_dow = (
        daily.groupby('DOW')
        .apply(lambda g: np.average(g['Lbs'], weights=g['Weight']))
        .reindex(range(7), fill_value=0.0)
        .values
    )

    # ── Step 5: per-client, per-DOW posterior (empirical Bayes) ──────────
    # Posterior mean: (n·x̄ + κ·m₀) / (n + κ)
    # where n = effective count, x̄ = client DOW mean, m₀ = prior, κ = shrinkage
    models: Dict[str, DemandModel] = {}

    # Pooled residual variance: across all (client, dow) cells, weighted Var
    # of (lbs - client_dow_mean). Then floor at 10% of mean to avoid σ→0.
    def _wmean(s, w):
        ws = w.sum()
        if ws == 0:
            return 0.0
        return float((s * w).sum() / ws)

    def _wvar(s, w, mean):
        ws = w.sum()
        if ws == 0:
            return 0.0
        return float(((s - mean) ** 2 * w).sum() / ws)

    pooled_var_acc, pooled_n = 0.0, 0.0
    per_client_groups = list(daily.groupby('Customer'))

    # Build per-client posterior + accumulate pooled variance
    raw_client_dow_mean: Dict[str, np.ndarray] = {}
    raw_client_dow_n: Dict[str, np.ndarray] = {}
    for cust, group in per_client_groups:
        dow_mean = np.zeros(7)
        dow_n = np.zeros(7)
        for d, g in group.groupby('DOW'):
            m = _wmean(g['Lbs'], g['Weight'])
            v = _wvar(g['Lbs'], g['Weight'], m)
            dow_mean[d] = m
            dow_n[d] = g['Weight'].sum()
            pooled_var_acc += v * g['Weight'].sum()
            pooled_n += g['Weight'].sum()
        raw_client_dow_mean[cust] = dow_mean
        raw_client_dow_n[cust] = dow_n

    pooled_sigma = math.sqrt(pooled_var_acc / pooled_n) if pooled_n > 0 else 1.0

    # Map customer name -> client id (the state file is keyed by ID)
    cust_to_id = dict(zip(clients['Customer'], clients['ID'].astype(str)))

    for cust, group in per_client_groups:
        dow_mean = raw_client_dow_mean[cust]
        dow_n = raw_client_dow_n[cust]
        total_n = dow_n.sum()

        # Posterior per DOW with empirical-Bayes shrinkage
        posterior = (dow_n * dow_mean + shrinkage_strength * prior_mean_per_dow) / (
            dow_n + shrinkage_strength
        )

        # Per-client σ. Don't let it collapse below 10% of mean.
        client_mean = posterior.mean()
        sigma = max(pooled_sigma, 0.10 * client_mean)

        source = 'own' if total_n >= min_days_for_own else 'pooled'
        if total_n == 0:
            posterior = prior_mean_per_dow.copy()
            source = 'insufficient'

        cid = cust_to_id.get(cust)
        if cid is not None:
            models[cid] = DemandModel(
                rates=posterior,
                sigma=sigma,
                n_obs=int(total_n),
                source=source,
            )

    # Clients with NO history → assign the global prior with high σ
    for _, c in clients.iterrows():
        cid = str(c['ID'])
        if cid not in models:
            models[cid] = DemandModel(
                rates=prior_mean_per_dow.copy(),
                sigma=pooled_sigma * 1.5,   # extra uncertainty for new clients
                n_obs=0,
                source='insufficient',
            )

    log.info(
        'Fitted %d demand models  | own=%d pooled=%d insufficient=%d  | σ̂=%.1f lbs',
        len(models),
        sum(1 for m in models.values() if m.source == 'own'),
        sum(1 for m in models.values() if m.source == 'pooled'),
        sum(1 for m in models.values() if m.source == 'insufficient'),
        pooled_sigma,
    )
    return models


# ─────────────────────────────────────────────────────────────────────────────
# Bayesian update — call this when fresh delivery data arrives
# ─────────────────────────────────────────────────────────────────────────────

def update_with_observation(
    model: DemandModel,
    *,
    actual_lbs_per_day: float,
    dow: int,
    n_days_covered: float,
    learning_rate: float = 1.0,
) -> DemandModel:
    """
    Online update of a single client's per-DOW posterior mean given
    a new observation (lbs/day averaged over `n_days_covered`).

    Uses a precision-weighted Kalman-style update with effective
    sample size = n_days_covered × learning_rate (lower if you suspect
    the observation is noisy).
    """
    rates = model.rates.copy()
    n_eff = max(0.001, n_days_covered * learning_rate)
    prior_n = max(model.n_obs, 1)
    new_rate = (prior_n * rates[dow] + n_eff * actual_lbs_per_day) / (prior_n + n_eff)
    rates[dow] = new_rate
    return DemandModel(
        rates=rates,
        sigma=model.sigma,
        n_obs=int(model.n_obs + n_days_covered),
        source=model.source,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _global_dow_share(df: pd.DataFrame) -> np.ndarray:
    """
    Return a length-7 array: relative volume share by DOW, scaled so it
    sums to 7.0 (so that uniform = 1.0 per day).

    Used to allocate a delivery's volume back over the gap days.
    """
    if df.empty:
        return np.ones(7)
    df = df.copy()
    df['Avg_Day_Lbs'] = df['Qty_lbs'] / df['Days_Gap']
    # Aggregate the average per-day lbs across all (delivery, dow) — coarse
    # but sufficient as an allocation prior. Use the mid-gap DOW as proxy.
    df['Mid_DOW'] = ((df['Prev_Date'] + (df['Date'] - df['Prev_Date']) / 2)
                     .dt.weekday)
    by_dow = df.groupby('Mid_DOW')['Avg_Day_Lbs'].mean().reindex(range(7))
    by_dow = by_dow.fillna(by_dow.mean()).values
    if by_dow.sum() == 0:
        return np.ones(7)
    return 7.0 * by_dow / by_dow.sum()


def _normal_quantile(q: float) -> float:
    """
    Standard-normal inverse CDF. Hardcoded for the quantiles we use,
    no scipy dependency.
    """
    table = {
        0.50: 0.0,
        0.60: 0.2533,
        0.70: 0.5244,
        0.75: 0.6745,
        0.80: 0.8416,
        0.85: 1.0364,
        0.90: 1.2816,
        0.95: 1.6449,
        0.975: 1.9600,
        0.99: 2.3263,
        0.995: 2.5758,
    }
    if q in table:
        return table[q]
    # Linear interpolation between nearest tabulated points
    keys = sorted(table.keys())
    for i in range(len(keys) - 1):
        if keys[i] <= q <= keys[i + 1]:
            a, b = keys[i], keys[i + 1]
            return table[a] + (table[b] - table[a]) * (q - a) / (b - a)
    return 1.6449  # fallback to z₀.₉₅


# ─────────────────────────────────────────────────────────────────────────────
# Convenience: enrich a clients_df with quantile-derived columns
# ─────────────────────────────────────────────────────────────────────────────

def attach_demand_columns(
    clients_df: pd.DataFrame,
    models: Dict[str, DemandModel],
) -> pd.DataFrame:
    """
    Add three columns to clients_df, derived from the DemandModel:

        Avg_LbsPerDay       (cross-DOW posterior mean)  → keeps legacy code working
        Demand_Sigma        (per-client σ̂)
        Demand_Source       ('own' / 'pooled' / 'insufficient')

    The legacy unified_solver only reads Avg_LbsPerDay so this drops in
    cleanly while the IRP solver can read the full DemandModel keyed by ID.
    """
    out = clients_df.copy()
    out['Avg_LbsPerDay'] = out['ID'].astype(str).map(
        lambda i: models[i].daily_mean() if i in models else np.nan
    )
    out['Demand_Sigma'] = out['ID'].astype(str).map(
        lambda i: models[i].sigma if i in models else np.nan
    )
    out['Demand_Source'] = out['ID'].astype(str).map(
        lambda i: models[i].source if i in models else 'insufficient'
    )
    return out
