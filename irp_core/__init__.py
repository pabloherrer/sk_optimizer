"""
irp_core — Inventory Routing Problem redesign

A stateful, stochastic, economic IRP layered on top of the existing
unified_solver. See IRP_DESIGN.md for theory and architecture.

Modules:
  state_manager  — atomic state I/O, plan persistence, delivery reconciliation
  forecasting    — quantile demand model with day-of-week effects
  safety_stock   — chance-constrained reorder point + visit-by deadline
  economics      — real-$ cost coefficients
  warm_start     — plan continuity from prior solution
  objective      — $-denominated objective callbacks for the solver
"""

__version__ = '0.1.0'
