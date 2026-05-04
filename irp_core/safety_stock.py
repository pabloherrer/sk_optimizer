"""
safety_stock.py — Chance-constrained reorder points and visit-by deadlines
==========================================================================

The legacy code uses three hard urgency tiers based on point-estimate
days-until-stockout:
    stockout : ≤ 0    → must serve today
    critical : ≤ 1.5  → must serve today/tomorrow
    urgent   : ≤ 3    → high priority

These tiers are fragile under demand variability. A client at "3.1
days" gets normal priority, but if demand spikes 25%, they stock out
in 2.4 days. Setting CRITICAL_DAYS=1.5 buys safety, but it also forces
a flood of trucks for clients that are still 30% full — wasted miles.

The right tool is a **chance constraint**: visit each client BY a
deadline τ such that P(stockout before τ) ≤ α. With our quantile
demand model:
    τ_i = max{ t : I_i,0 − Σ_{s≤t} q_α(μ_i,s, σ_i) > 0 }

where q_α is the α-quantile of cumulative consumption (i.i.d. Normal
sum from the DemandModel). For α=0.05 (5% stockout tolerance) we use
P95, the canonical "1-in-20" service level.

What this gives us
------------------
  • A continuous "days until P95 stockout" replacing the 3-tier hack.
  • A `visit_by_day` integer per client → hard deadline for the solver.
  • An economic urgency score: $ value at risk per day of delay.
  • A reorder-point method for clients that don't yet have a P95 stockout
    in the planning window (they're "safe but worth opportunistic visit").
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from .economics import CostModel, DEFAULT_COSTS, expected_stockout_cost
from .forecasting import DemandModel, _normal_quantile

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Per-client urgency profile
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class UrgencyProfile:
    """
    All time-to-stockout information for a single client.

    p50_days_to_stockout : float — expected days until tank floor at mean demand
    p95_days_to_stockout : float — DAYS at which P(stockout) = α (the deadline)
    visit_by_day_index   : int    — first horizon day where P95 stockout would
                                    occur if not served (for solver hard deadline)
    expected_stockout_$  : float  — cost if NOT visited within horizon
    daily_at_risk_$      : float  — $ added per day of delay past P95 deadline
    is_mandatory         : bool   — P95 stockout ≤ horizon end
    is_opportunistic     : bool   — fill ≥ threshold and within radius today
    """
    p50_days_to_stockout: float
    p95_days_to_stockout: float
    visit_by_day_index: int
    expected_stockout_dollars: float
    daily_at_risk_dollars: float
    is_mandatory: bool
    is_opportunistic: bool
    # Diagnostic: tier name compatible with legacy reporting
    legacy_tier: str = 'normal'


# ─────────────────────────────────────────────────────────────────────────────
# Core math
# ─────────────────────────────────────────────────────────────────────────────

def days_to_stockout_quantile(
    *,
    current_lbs: float,
    floor_lbs: float,
    model: DemandModel,
    start_date: pd.Timestamp,
    quantile: float = 0.95,
    max_days: int = 60,
) -> float:
    """
    Solve for the largest T such that
        I₀ − Σ_{t=0..T-1} q_α(daily_demand on date+t) ≥ floor

    Returns a fractional T by linear interpolation between the last
    safe day and the first stockout day. Returns max_days if the tank
    is safe over the entire horizon.

    Why fractional: the solver wants a smooth signal, not a step
    function. "2.7 days" is more informative than "≤ 3 days".
    """
    headroom = current_lbs - floor_lbs
    if headroom <= 0:
        return 0.0
    z = _normal_quantile(quantile)
    cum_mean = 0.0
    cum_var = 0.0
    last_safe = 0.0
    for t in range(max_days):
        d = start_date + pd.Timedelta(days=t)
        mean_t = model.rates[d.weekday()]
        sigma_t = model.sigma  # same across DOW (residual); tweak if heteroskedastic
        cum_mean += mean_t
        cum_var += sigma_t ** 2
        cum_quant = cum_mean + z * np.sqrt(cum_var)
        if cum_quant >= headroom:
            # We crossed the floor between t-1 and t. Linearly interpolate.
            prev_quant = cum_quant - mean_t - z * (
                np.sqrt(cum_var) - np.sqrt(max(cum_var - sigma_t ** 2, 1e-9))
            )
            denom = cum_quant - prev_quant
            frac = (headroom - prev_quant) / denom if denom > 1e-9 else 0.0
            return float(last_safe + max(0.0, min(1.0, frac)))
        last_safe = t + 1
    return float(max_days)


def days_to_stockout_mean(
    *,
    current_lbs: float,
    floor_lbs: float,
    model: DemandModel,
    start_date: pd.Timestamp,
    max_days: int = 60,
) -> float:
    """Same as quantile version but at the mean (P50). For diagnostics."""
    return days_to_stockout_quantile(
        current_lbs=current_lbs, floor_lbs=floor_lbs,
        model=model, start_date=start_date,
        quantile=0.50, max_days=max_days,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Build urgency profiles for every client
# ─────────────────────────────────────────────────────────────────────────────

def build_urgency_profiles(
    *,
    clients_df: pd.DataFrame,
    state_lookup: Dict[str, float],
    models: Dict[str, DemandModel],
    plan_dates: List[pd.Timestamp],
    cost: CostModel = DEFAULT_COSTS,
    floor_pct: float = 0.0,
    opportunistic_fill_pct: float = 0.55,
) -> Dict[str, UrgencyProfile]:
    """
    Build one UrgencyProfile per client.

    Inputs:
      clients_df    : DataFrame with at least ID, Tank_lbs
      state_lookup  : {client_id: current_lbs} (from InventoryState.as_dict())
      models        : {client_id: DemandModel}
      plan_dates    : the ordered list of planning days; len = horizon_days
      cost          : CostModel (for stockout $)

    Logic:
      1. Compute P50 + P95 days-to-stockout from current level.
      2. Find the integer day index by which P95 stockout would happen
         if no visit occurs (= visit_by_day).
      3. Mandatory if visit_by_day ≤ len(plan_dates) - 1.
      4. Compute expected stockout $ if deferred past horizon.
      5. Opportunistic if fill-fraction-on-day-0 ≥ threshold.
    """
    horizon_days = len(plan_dates)
    if horizon_days == 0:
        return {}
    start_date = plan_dates[0]

    profiles: Dict[str, UrgencyProfile] = {}
    for _, row in clients_df.iterrows():
        cid = str(row['ID'])
        tank = float(row['Tank_lbs'])
        floor = tank * floor_pct
        current = float(state_lookup.get(cid, tank * 0.5))
        model = models.get(cid)

        if model is None or model.daily_mean() <= 0:
            # No demand info → treat as safe (will be excluded by validator
            # upstream if truly unknown). Use a large value.
            profiles[cid] = UrgencyProfile(
                p50_days_to_stockout=999.0,
                p95_days_to_stockout=999.0,
                visit_by_day_index=horizon_days + 1,
                expected_stockout_dollars=0.0,
                daily_at_risk_dollars=0.0,
                is_mandatory=False,
                is_opportunistic=False,
                legacy_tier='normal',
            )
            continue

        p50 = days_to_stockout_mean(
            current_lbs=current, floor_lbs=floor,
            model=model, start_date=start_date,
        )
        p95 = days_to_stockout_quantile(
            current_lbs=current, floor_lbs=floor,
            model=model, start_date=start_date,
            quantile=1.0 - cost.service_alpha,
        )

        # visit_by_day: the latest day index t such that we COULD still
        # visit on day t and avoid a P95 stockout. Conservatively floor.
        visit_by = int(np.floor(p95))
        is_mandatory = visit_by <= horizon_days - 1

        # Expected $ at risk if we DON'T visit in horizon
        days_late = max(0.0, horizon_days - p95)
        e_cost = expected_stockout_cost(
            cost=cost,
            days_until_stockout=p95,
            horizon_days=horizon_days,
            days_late_if_visited_last=horizon_days,
        )
        # Per-day at-risk = full event cost / max(p95, 1)  (so closer = steeper)
        daily_at_risk = cost.late_dollars_per_day() if is_mandatory else (
            e_cost / max(horizon_days * 1.5 - p95, 1.0)
        )

        # Opportunistic = if visited TODAY, would refill ≥ threshold of tank
        fill_today = max(tank - current, 0.0) / tank
        is_opportunistic = (not is_mandatory) and (fill_today >= opportunistic_fill_pct)

        # Legacy tier mapping (for downstream reports that still expect it)
        if p95 <= 0:
            tier = 'stockout'
        elif p95 <= 1.5:
            tier = 'critical'
        elif p95 <= 3.0:
            tier = 'urgent'
        else:
            tier = 'normal'

        profiles[cid] = UrgencyProfile(
            p50_days_to_stockout=p50,
            p95_days_to_stockout=p95,
            visit_by_day_index=visit_by,
            expected_stockout_dollars=e_cost,
            daily_at_risk_dollars=daily_at_risk,
            is_mandatory=is_mandatory,
            is_opportunistic=is_opportunistic,
            legacy_tier=tier,
        )

    log.info(
        'Urgency profiles built: %d total | mandatory=%d opportunistic=%d',
        len(profiles),
        sum(1 for p in profiles.values() if p.is_mandatory),
        sum(1 for p in profiles.values() if p.is_opportunistic),
    )
    return profiles


# ─────────────────────────────────────────────────────────────────────────────
# Convenience: attach profile columns to clients_df for downstream
# ─────────────────────────────────────────────────────────────────────────────

def attach_urgency_columns(
    clients_df: pd.DataFrame,
    profiles: Dict[str, UrgencyProfile],
) -> pd.DataFrame:
    """
    Add quantile-derived columns to clients_df so the existing
    unified_solver can consume them. Backwards-compatible:
        Days_Until_Stockout    ← P50 (matches legacy)
        Days_Until_Stockout_P95 ← new column
        Urgency                ← derived from P95 (legacy tier names)
        Visit_By_Day           ← integer deadline
        Expected_Stockout_USD  ← $ at risk
    """
    out = clients_df.copy()

    def _get(cid: str, attr: str, default=None):
        p = profiles.get(str(cid))
        return getattr(p, attr) if p else default

    # Keep P50 in the legacy column (legacy solver's urgency-tier
    # classifier expects mean-projection semantics). Expose P95 in a
    # parallel column for IRP-specific use.
    out['Days_Until_Stockout'] = out['ID'].map(
        lambda i: _get(i, 'p50_days_to_stockout', float('nan'))
    )
    out['Days_Until_Stockout_P95'] = out['ID'].map(
        lambda i: _get(i, 'p95_days_to_stockout', float('nan'))
    )
    out['Visit_By_Day'] = out['ID'].map(
        lambda i: _get(i, 'visit_by_day_index', 999)
    )
    out['Expected_Stockout_USD'] = out['ID'].map(
        lambda i: _get(i, 'expected_stockout_dollars', 0.0)
    )
    # Urgency tier: derive from P50 so legacy thresholds (1.5 / 3.0 days)
    # apply with their original semantics. The IRP's chance-constraint
    # safety lives in Visit_By_Day + Expected_Stockout_USD, not in the
    # legacy tier.
    from inventory import urgency_tier
    out['Urgency'] = out['Days_Until_Stockout'].apply(
        lambda d: urgency_tier(d) if d == d else 'normal'   # NaN-safe
    )
    return out
