"""
economics.py — Real-$ cost coefficients for the IRP objective
=============================================================

The legacy unified solver minimises a hybrid scalar:
    distance + LATE_PENALTY_PER_DAY × days_late + 1B × dropped

This module replaces those magic constants with a single, defensible
$-denominated cost model. Every routing decision can be priced against
every other one.

Cost components
---------------
  fuel_per_mi      $/mi for the truck (diesel + maintenance)
  labor_per_min    $/min for driver wages on shift
  ot_per_min       $/min EXTRA cost for overtime (beyond shift_min)
  truck_fixed_cost $/route — depreciation, insurance, fixed daily cost
                   (charged once if a route is taken at all)
  stockout_dollars $/event — average revenue + goodwill loss when a
                   restaurant runs out of oil mid-service
  late_per_day     $/day late beyond P95 stockout deadline
                   (smaller than full stockout — driver may rescue)

These default to defensible numbers for an Arizona QSR oil-delivery
business in 2026. They live here, not in config.py, so the IRP layer
remains self-contained.

Integer scaling
---------------
OR-Tools requires integer arc costs. We scale by COST_SCALE = 100 to
keep two cents of precision. All `to_solver_units()` helpers do this
conversion. Solver objective values divided by COST_SCALE = real $.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Optional


# OR-Tools integer scaling. 1 unit = 1 cent.
COST_SCALE = 100


@dataclass(frozen=True)
class CostModel:
    """Real-$ cost parameters. All defaults reviewable and defensible."""

    # --- Routing costs ----------------------------------------------------
    fuel_per_mi: float = 0.55          # $/mi diesel + tires + brakes for a
                                       # 26' delivery truck (~7 mpg, $4.50/gal,
                                       # plus $0.10/mi maintenance)
    labor_per_min: float = 0.50        # $/min — $30/hr loaded driver wage
    ot_multiplier: float = 1.5         # 1.5× overtime premium over labor_per_min
    shift_min: int = 600               # 10-hour soft shift; OT beyond this
    max_shift_min: int = 720           # Hard 12-hour ceiling (no route exceeds)
    truck_fixed_cost: float = 35.0     # $/route — daily share of insurance,
                                       # depreciation, registration

    # --- Inventory costs --------------------------------------------------
    stockout_dollars: float = 800.0    # $/event — typical lost contribution
                                       # margin for a QSR running out of oil
                                       # mid-shift (≈ 4 hr lost frying capacity
                                       # × $200/hr contribution).
    late_per_day_pct: float = 0.20     # Lateness beyond P95 deadline costs
                                       # this fraction of stockout_dollars per
                                       # day late (driver may rescue with a
                                       # special trip; not full loss).
    holding_per_lb_day: float = 0.0    # S&K doesn't pay client holding cost
                                       # (clients own their tanks). Kept here
                                       # for theoretical completeness.

    # --- Service-level target --------------------------------------------
    service_alpha: float = 0.05        # P(stockout | next visit) ≤ alpha
                                       # 0.05 ⇒ aim for 95% in-stock per cycle.
                                       # P95 demand path drives the deadline.

    # --- Derived helpers --------------------------------------------------

    @property
    def ot_per_min(self) -> float:
        """Cost of one OVERTIME minute *beyond* base labor."""
        return self.labor_per_min * (self.ot_multiplier - 1.0)

    @property
    def loaded_labor_per_min(self) -> float:
        """Total cost (base + premium) for an OT minute."""
        return self.labor_per_min * self.ot_multiplier

    def late_dollars_per_day(self) -> float:
        return self.stockout_dollars * self.late_per_day_pct

    # --- Solver scaling ---------------------------------------------------

    def fuel_per_meter_units(self) -> int:
        """Cost (cents) per metre of travel."""
        # $/mi → $/m → cents/m → integer (kept as float, scaled at use site)
        return self.fuel_per_mi / 1609.34 * COST_SCALE  # ≈ 0.034 cents/m

    def labor_per_min_units(self) -> int:
        return int(round(self.labor_per_min * COST_SCALE))

    def ot_per_min_units(self) -> int:
        return int(round(self.ot_per_min * COST_SCALE))

    def stockout_units(self) -> int:
        return int(round(self.stockout_dollars * COST_SCALE))

    def late_per_day_units(self) -> int:
        return int(round(self.late_dollars_per_day() * COST_SCALE))

    def truck_fixed_units(self) -> int:
        return int(round(self.truck_fixed_cost * COST_SCALE))

    # --- I/O --------------------------------------------------------------

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> 'CostModel':
        # Filter to known fields so older saved configs don't break.
        keep = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**keep)


# ─────────────────────────────────────────────────────────────────────────────
# Stockout valuation — when a client is deferred outside the horizon
# ─────────────────────────────────────────────────────────────────────────────

def expected_stockout_cost(
    *,
    cost: CostModel,
    days_until_stockout: Optional[float],
    horizon_days: int,
    days_late_if_visited_last: float = 0.0,
) -> float:
    """
    Expected $ cost of NOT serving a client within the horizon.

    days_until_stockout: P95 forecast (None ⇒ unknown ⇒ no penalty).
    horizon_days: planning window (days).
    days_late_if_visited_last: additional days past stockout if the
        solver pushes them all the way to the last horizon day.

    Logic:
      • If P(stockout in horizon) ≈ 0 → cost = 0 (deferring is free)
      • If P95 stockout falls inside horizon → cost = stockout_$ + late_$/day × days late
      • Beyond horizon → graded ramp by how close they are to running out
    """
    if days_until_stockout is None:
        return 0.0
    if days_until_stockout >= horizon_days * 1.5:
        # Way out — deferring is cheap.
        return 0.0
    if days_until_stockout <= 0:
        # Already out of stock — full event cost + per-day rescue cost.
        return cost.stockout_dollars + cost.late_dollars_per_day() * abs(days_until_stockout)

    days_late = max(0.0, days_late_if_visited_last - days_until_stockout)
    base = cost.stockout_dollars * (1 - days_until_stockout / (horizon_days * 1.5))
    return float(base + cost.late_dollars_per_day() * days_late)


# Default singleton — used by callers that don't override.
DEFAULT_COSTS = CostModel()
