# IRP Redesign — Quick Reference

The new Inventory Routing Problem layer for the SK Route Optimizer.

## TL;DR

**Old setup was a deterministic VRP with a broken rolling-horizon claim.**
**New setup is a real, stateful, stochastic-aware IRP.**

The two run side-by-side; pick which one to use day-to-day.

## Three documents

1. **[IRP_DESIGN.md](IRP_DESIGN.md)** — the OR theory and architecture.
2. **[IRP_RESULTS.md](IRP_RESULTS.md)** — what was built, with usage examples.
3. **[IRP_FINAL_REPORT.md](IRP_FINAL_REPORT.md)** — full report with metrics.

## Three commands

```bash
# Daily plan (this is the new entry point)
python run_irp.py --solve-sec 600

# End-of-day reconcile (replace planned with actual deliveries)
python run_irp.py --confirm path/to/actuals.csv

# Compare to legacy (apples-to-apples)
python backtest_irp.py --start 2026-04-01 --days 14 --closed-loop --demand-noise 0.30
```

## What "stateful rolling horizon" means in practice

Every run of `run_irp.py` writes:

- `data/state.json` — current inventory level per client (atomic write)
- `data/plan.json` — the full plan (committed + tentative)
- `data/deliveries.log.jsonl` — append-only audit trail

Every NEXT run reads them. Tomorrow's day-0 plan is genuinely a continuation
of today's day-1 tentative plan, not a cold restart.

Compared to the previous setup where `state.json` was empty `{}` and never
written.

## What "$-denominated objective" means

Every routing decision can be priced:

```python
from irp_core.economics import CostModel
c = CostModel()      # defensible defaults
print(c.fuel_per_mi)         # $0.55
print(c.labor_per_min)       # $0.50
print(c.stockout_dollars)    # $800
print(c.late_dollars_per_day())  # $160
```

Then the solver minimizes:

```
fuel_$/mi × distance
+ labor_$/min × time
+ ot_$/min × (time − shift_max if positive)
+ expected_stockout_$ × (1 − served_in_horizon)
```

No magic constants. Replace any field if real ops data differs.

## What chance-constrained safety stock means

Each client gets:

- `p50_days_to_stockout` — mean projection (point estimate)
- `p95_days_to_stockout` — chance constraint: P(stockout) ≤ 5%
- `visit_by_day_index` — hard deadline derived from P95
- `is_mandatory` — P95 stockout falls within horizon

Set `service_alpha=0.10` for P90 instead. Set `service_alpha=0.01` for P99.
The legacy 1.5-day urgency cliff is gone.

## What's still rough

1. Cost defaults are defensible but not validated against SK's actual P&L.
   Tweak `CostModel` once accounting confirms.
2. The 19-day deterministic backtest has IRP at +3.9% cost vs legacy. The
   IRP wins should appear under stochastic demand (run with
   `--closed-loop --demand-noise 0.30`).
3. Production migration: shadow-run for 2 weeks before promoting to default.

## Tests

```bash
.venv/bin/python -m pytest tests/test_irp_core.py -v
# 27 passed in 0.4s
```
