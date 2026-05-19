"""
objective.py — Translate IRP $-cost model into solver-compatible knobs
======================================================================

Strategy
--------
We do **not** rewrite the OR-Tools cost callbacks in unified_solver.
That's a large surgery for moderate benefit. Instead, we calibrate the
existing knobs (LATE_PENALTY_PER_DAY, drop-disjunction penalties, OT
penalty) so they correspond to dollars under our CostModel.

The legacy solver uses an integer cost unit. By rule of thumb in the
codebase:
    1 cost unit ≈ 1 metre of travel
    LATE_PENALTY_PER_DAY = 5000 ≈ 3 miles equivalent

We map this to dollars via fuel_per_mi:
    1 mile = $0.55 fuel  →  $0.55 / 1609.34 m = 3.4·10⁻⁴ $/m
    1 cost unit = 1 m   →  3.4·10⁻⁴ $ per cost unit
    1 dollar      = 2920 cost units

So when the legacy solver minimises N cost units, that's N / 2920 $.
We can plug our $-denominated model in by:

    LATE_PENALTY_PER_DAY  = late_per_day_$ × COST_UNITS_PER_DOLLAR
    DISJUNCTION_PENALTY   = expected_stockout_$ × COST_UNITS_PER_DOLLAR
    OT_PENALTY_PER_MIN    = ot_per_min_$ × COST_UNITS_PER_DOLLAR

The result: the solver's objective value, divided by COST_UNITS_PER_DOLLAR,
is the total expected cost in dollars. Reportable, defensible, debuggable.
"""

from __future__ import annotations

import logging
from typing import Dict, Optional

import pandas as pd

from .economics import CostModel, DEFAULT_COSTS, expected_stockout_cost
from forecast.safety_stock import UrgencyProfile

log = logging.getLogger(__name__)


def cost_units_per_dollar(cost: CostModel) -> float:
    """
    Conversion factor: 1 dollar = N legacy cost units.

    Derivation: legacy solver uses metres for distance. fuel_per_mi
    determines the $/m equivalence.
    """
    dollar_per_meter = cost.fuel_per_mi / 1609.34
    return 1.0 / dollar_per_meter   # ≈ 2926 for fuel=$0.55/mi


def calibrate_legacy_knobs(cost: CostModel = DEFAULT_COSTS) -> Dict[str, int]:
    """
    Compute the integer values for legacy config knobs that match the
    dollar-denominated CostModel. Caller pokes these into config or
    passes them as runtime overrides.

    Returns a dict of:
        LATE_PENALTY_PER_DAY  : per-day-late penalty in cost units
        OT_PENALTY_PER_MIN    : OT cost in cost units / minute
        LABOR_COST_PER_MIN    : labor base cost in cost units / minute
    """
    units = cost_units_per_dollar(cost)
    return {
        'LATE_PENALTY_PER_DAY': int(round(cost.late_dollars_per_day() * units)),
        'OT_PENALTY_PER_MIN':   int(round(cost.ot_per_min * units)),
        'LABOR_COST_PER_MIN':   int(round(cost.labor_per_min * units)),
        'COST_UNITS_PER_DOLLAR': float(units),
    }


def build_per_client_disjunction_penalties(
    *,
    profiles: Dict[str, UrgencyProfile],
    cost: CostModel = DEFAULT_COSTS,
    horizon_days: int,
    floor_unit_value: int = 1_000,
) -> Dict[str, int]:
    """
    For each client, compute the disjunction (drop) penalty in solver
    cost units. This replaces the legacy 1B/100M/10M urgency-tier
    constants with smooth $-denominated values.

    Logic:
      • Mandatory client (P95 stockout in horizon)  → expected_stockout_$
      • Opportunistic                               → small bonus to entice
      • Normal                                      → minimal floor

    Returns {client_id: penalty_units}.
    """
    units = cost_units_per_dollar(cost)
    out: Dict[str, int] = {}
    for cid, p in profiles.items():
        dollars = p.expected_stockout_dollars
        if p.is_mandatory and dollars < cost.stockout_dollars * 0.5:
            # Floor mandatory clients to half the full event cost — never
            # let the solver decide a deferral is "cheaper" than fuel.
            dollars = cost.stockout_dollars * 0.5
        elif p.is_opportunistic and dollars < cost.stockout_dollars * 0.05:
            dollars = cost.stockout_dollars * 0.05
        penalty = max(int(round(dollars * units)), floor_unit_value)
        out[cid] = penalty
    return out


def attach_solver_penalty_column(
    clients_df: pd.DataFrame,
    penalties: Dict[str, int],
    column: str = 'Drop_Penalty_Units',
) -> pd.DataFrame:
    """Add a per-client column with the calibrated drop penalty."""
    out = clients_df.copy()
    out[column] = out['ID'].astype(str).map(penalties).fillna(1_000).astype(int)
    return out


def explain_objective_in_dollars(
    *,
    objective_value: int,
    cost: CostModel = DEFAULT_COSTS,
) -> str:
    """Pretty-print the solver's integer objective as dollars."""
    units = cost_units_per_dollar(cost)
    dollars = objective_value / units
    return f'{objective_value:,} cost units ≈ ${dollars:,.2f} (at fuel=${cost.fuel_per_mi}/mi)'
