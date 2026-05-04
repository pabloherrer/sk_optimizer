# SK Route Optimizer — IRP Redesign

**Status:** Implementation in progress
**Author:** OR redesign
**Date:** 2026-05-02

---

## 1. Diagnosis

The current system is a **multi-day VRP with urgency penalties**, not a true Inventory Routing Problem (IRP) solver. It exhibits three fundamental gaps relative to the IRP literature (Coelho, Cordeau & Laporte 2014; Campbell & Savelsbergh 2004):

| IRP element | Current behaviour | Bug |
|---|---|---|
| **Inventory state across runs** | `inventory_state.json` is `{}`. `save_state()` is implemented but **never called** from production entry points. Every run starts cold from the delivery log, recomputing levels from "days since last delivery × median rate." | **Critical.** Rolling horizon doesn't roll. The system is daily VRP with multi-day lookahead, not a stateful IRP. |
| **Demand model** | Median lbs/day, IQR-filtered. Deterministic. No prediction interval, no day-of-week effect, no Bayesian update as new data arrives. | **High.** Stockouts are mitigated only by aggressive 1.5-day urgency thresholds, which inflates trip frequency. |
| **Objective** | Total distance + soft "lateness" + 1B disjunction penalties. Magic constants (`EFFICIENCY_WEIGHT=2.5`, `LATE_PENALTY_PER_DAY=5000`) tuned by trial-and-error. | **Medium.** No defensible $ cost. Tradeoffs between urgency and distance are opaque. |

Additional issues (pre-clustering heuristics, no warm-start, no backtest harness) compound but are downstream of the three above.

---

## 2. Target Architecture

A **stateful, stochastic, economic IRP** with rolling horizon, executed by an OR-Tools matheuristic with proper warm-starting.

```
                  ┌──────────────────────────────────┐
                  │  Excel input + Delivery log      │
                  └──────────────┬───────────────────┘
                                 ▼
        ┌───────────────────────────────────────────────────┐
        │  state_manager   (atomic JSON, append-only log)   │
        │  ───────────────────────────────────────────      │
        │  • plan.json      ← yesterday's solution          │
        │  • state.json     ← per-client lbs + last_seen    │
        │  • deliveries.log ← actuals, append-only          │
        └──────────────┬────────────────────────────────────┘
                       ▼
        ┌──────────────────────────────────────────────────┐
        │  forecasting   (quantile + DOW + Bayes update)   │
        │  ─────────────────────────────────────────────   │
        │  P50, P80, P95 daily consumption per client      │
        │  Posterior mean updated each delivery             │
        └──────────────┬───────────────────────────────────┘
                       ▼
        ┌──────────────────────────────────────────────────┐
        │  safety_stock  (chance-constrained reorder pt)   │
        │  ─────────────────────────────────────────────   │
        │  Reorder when P(stockout in next H days) ≥ 1−α   │
        │  Visit-by deadline computed from P95 demand path │
        └──────────────┬───────────────────────────────────┘
                       ▼
        ┌──────────────────────────────────────────────────┐
        │  economics    (real-$ cost coefficients)         │
        │  ─────────────────────────────────────────────   │
        │  fuel $/mi, labor $/min, OT, stockout penalty $  │
        │  Translates routing decisions to comparable $    │
        └──────────────┬───────────────────────────────────┘
                       ▼
        ┌──────────────────────────────────────────────────┐
        │  irp_solver   (extended unified_solver)          │
        │  ─────────────────────────────────────────────   │
        │  Same 30-vehicle CVRP frame, but:                │
        │   • Objective in $ (fuel + labor + OT + risk)   │
        │   • Warm-start from previous plan                │
        │   • Disjunction penalties = expected stockout $ │
        │   • Hard deadline = P95 stockout date            │
        └──────────────┬───────────────────────────────────┘
                       ▼
        ┌──────────────────────────────────────────────────┐
        │  state_manager.commit()                          │
        │  ─────────────────────────────────────────────   │
        │  • write plan.json (committed + tentative)       │
        │  • atomic state save (ready for next run)        │
        └──────────────────────────────────────────────────┘
```

**Key principle:** every layer is a pure function with explicit inputs/outputs. The state manager is the only source of mutable state; everything else is functional.

---

## 3. The IRP Formulation

For client `i ∈ N`, period `t ∈ {0..H-1}` of horizon `H`, truck `k ∈ K`:

**Decision variables**
- `x[i,t,k] ∈ {0,1}`: visit i on day t with truck k (implicit via OR-Tools vehicle assignment)
- `q[i,t]`: refill quantity (S&K policy: `Tank_i − I[i,t]` if visited, else 0)
- `I[i,t] ≥ 0`: inventory at start of day t (deterministic given visits + forecast mean)

**State dynamics**
```
I[i, 0]    = state.json[i]                       (loaded from disk)
I[i, t+1]  = I[i,t] − μ[i,t] + q[i,t]·∑_k x[i,t,k]
```
where `μ[i,t]` is the forecast mean for client `i` on day `t` (handles day-of-week effects).

**Constraints**
```
chance:    P(I[i, τ_i*] ≥ 0) ≥ 1 − α       where τ_i* = next visit day
                                            ↳ enforced as: visit-by-deadline =
                                              first day where I[i,t] − Σ μ + zα·σ < 0
truck:     Σ_i q[i,t]·x[i,t,k] ≤ Cap_k
compart.:  per-product capacity ≤ 5000 lbs each
shift:     route duration ≤ shift_max
visit-1:   each client visited ≤ 1 time per horizon (disjunction)
```

**Objective (in dollars)**
```
min  Σ_{t,k} (fuel_$_per_mi · dist[t,k]   +
             labor_$_per_min · time[t,k] +
             OT_$_per_min · max(0, time[t,k] − shift))
   + Σ_i  expected_stockout_cost(i)·(1 − served_in_horizon[i])
```

Critically:
- **No magic constants.** Every coefficient has a `$` interpretation.
- **Disjunction penalty = expected stockout cost.** Real economic tradeoff between deferring a client and serving them.
- **Warm-start = yesterday's tentative day-1 plan becomes today's day-0 starting solution.**

---

## 4. What "Perfect" Means (and Doesn't)

### Achievable in this redesign
- ✅ Stateful rolling horizon that actually rolls
- ✅ Defensible $ objective
- ✅ Quantile-based safety stock (no more 1.5-day urgency cliff)
- ✅ Warm-start for solution stability
- ✅ Backtest harness with stockout/cost metrics
- ✅ Clear separation: state ↔ forecast ↔ economics ↔ solver

### Out of scope (deliberately)
- Refill quantity as continuous decision variable. S&K's operational policy is "fill to 100% on visit" (driver simplicity, hose mechanics). Making this a free variable adds 5× problem size for marginal benefit. Punted.
- Branch-price-and-cut. OR-Tools matheuristic is sufficient for 100 clients × 2 trucks. Revisit if scale 5×.
- Demand learning beyond Bayesian update of mean. Hierarchical models (zone-pooling for new clients) is a Phase 2 win.

---

## 5. Implementation Plan

| Module | Lines | Purpose |
|---|---|---|
| `irp_core/state_manager.py` | ~250 | Atomic state I/O, plan persistence, delivery reconciliation |
| `irp_core/forecasting.py` | ~300 | Quantile forecasting with DOW seasonality + Bayesian update |
| `irp_core/economics.py` | ~150 | Real-$ cost coefficients, stockout valuation |
| `irp_core/safety_stock.py` | ~200 | Chance-constrained reorder point + visit-by deadline |
| `irp_core/warm_start.py` | ~150 | Read plan.json, prepare OR-Tools initial assignment |
| `irp_core/objective.py` | ~200 | $ objective callbacks for unified_solver |
| `run_irp.py` | ~250 | New entry point — wires everything together |
| `backtest_irp.py` | ~400 | Replay history, simulate stockouts, compare to baseline |

Total: ~1900 LOC of new code. Existing code is **not modified** except for one hook in `unified_solver.py` to accept overridable cost callbacks.

---

## 6. Validation Strategy

A backtest is the only honest measure. Replay 90 days of history:

1. Snapshot state at day T (use only data ≤ T).
2. Run the optimizer.
3. Compare its plan against actual deliveries on day T+1.
4. Roll forward, repeat.

Metrics:
- **Service level**: % of days each client had inventory > 0 (P95 ≥ 99.5%)
- **$ cost / week**: fuel + labor + OT (lower is better)
- **Stockout events**: count of forecast P(stockout) > 5% that materialised
- **Plan stability**: % overlap between today's day-1 plan and yesterday's day-2 plan (higher = more drivers happy)

Old vs new gets compared on every metric. Hard numbers, no marketing.

---

## 7. Build Order

1. `state_manager.py` — keystone. Nothing else works without persistent state.
2. `economics.py` — small, unblocks everything downstream.
3. `forecasting.py` — quantile + DOW. Standalone, testable.
4. `safety_stock.py` — uses forecasting + economics.
5. `warm_start.py` — uses state_manager.
6. `objective.py` — uses economics + safety_stock.
7. `run_irp.py` — orchestrator.
8. `backtest_irp.py` — proves it works.

Implementation begins now.
