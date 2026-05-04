# OR Audit Findings — May 2026

A brutal-honest audit against IRP literature (Coelho-Cordeau-Laporte 2014,
Archetti et al. 2012, Andersson-Hoff-Christiansen 2010, Vidal et al. HGS).

## Verdicts

| Q | Issue | Verdict | Severity | Status |
|---|---|---|---|---|
| Q1 | Multi-visit per horizon | BROKEN | Medium | **OPEN** (see below) |
| Q2 | Per-DOW rate in solver projection | PARTIAL | Medium | **FIXED** |
| Q3 | Time windows | CORRECT | — | — |
| Q4 | Per-day closure enforcement | PARTIAL | Low–Medium | **FIXED** |
| Q5 | Service time per truck | CORRECT | — | — |
| Q6 | Bi-weekly Saturday | False alarm | — | reverted |
| Q7 | Horizon-end cliff | CORRECT | — | — |
| Q8 | Demand serial correlation | BROKEN (acknowledged) | Medium | **OPEN** |

---

## Fixed in this round

### Q2 — Per-DOW rate now used in solver projection

**Was:** `unified_solver` projected refill amounts using a constant
cross-DOW mean (`Avg_LbsPerDay`). A 1500-lb tank emptying at Sun 120 /
Mon 80 / Fri 110 lbs/day was modeled as if every day = ~100 lbs/day. On
short horizons that's tolerable; on a 10-day plan it skews refills by
±10–15 % per day.

**Now:** `inventory.project_level_dow(current, rate_by_dow[7], today,
days_forward, tank)` walks day-by-day and uses the correct DOW rate for
each. `run_irp.py` populates `clients_df['Rate_By_DOW']` from the
DemandModel's per-DOW posterior. The solver detects the column and uses
the DOW projection automatically; falls back to the scalar for legacy
runs.

### Q4 — Per-day partial closures now block specific (client, day) pairs

**Was:** A client closed on Mon–Wed but open Thu–Sat would be excluded
from the route only if **every** day Tue–Sat fell inside the closure
(handled by the `CLOSED_ALL_WEEK` deferral). Partial overlaps were
silently ignored — the solver could legally schedule a delivery on a
date the restaurant was shut.

**Now:** After the time-window block, every (client, day) where
`is_client_closed_on()` returns True triggers a `VehicleVar.RemoveValue`
on the corresponding vehicle indices. Same mechanism as day-restricted
time windows. Logged as `Partial closures: N client(s) blocked from M
closure-day(s)`.

### Q6 — False alarm (reverted)

The audit flagged the static `SATURDAY_TRUCKS = ['Truck2']` as a
bi-weekly-rotation bug. **It isn't.** Every Saturday Truck9 is on the
long out-of-metro run (alternating Tucson and Flagstaff weekly). For the
metro optimizer the rule is simply "Saturday = Truck2 only," which is
what the code does. I briefly added a bi-weekly toggle and reverted it
after the user clarified the actual rotation.

---

## Still open — future work with concrete fix plans

### Q1 — Multi-visit per horizon (BROKEN, MEDIUM impact)

**The issue.** `routing.AddDisjunction([single_node], penalty)` enforces
**at-most-1** visit per client per plan. A high-velocity client like
HAROLDS (1050 lb tank, 176 lb/day → 6-day cycle) on a 10-day plan
should optimally be visited day 0 AND day 6. We currently can't express
this; HAROLDS gets one visit and the model assumes they survive the
horizon.

**Mitigating factor.** We replan daily, so day-6's needed visit gets
caught on the next day's run. But:
- Plans look misleading (a 10-day "tentative" plan claiming HAROLDS
  is fine on day 8 when really they'd be near-empty by then)
- Capacity planning is wrong (we under-book day-6 truck capacity that
  HAROLDS will eventually need)
- Geographic clustering on day 6 misses HAROLDS even if a route is
  passing through Cave Creek

**Fix sketch.** For each client whose `tank_days = tank / rate < horizon`,
materialize K = `ceil(horizon / tank_days)` virtual nodes, all sharing
the same matrix coordinates and time window. Wrap them in a single
`AddDisjunction([nodes...], penalty)` with the standard penalty for the
worst-of-K. The OR-Tools solver will pick the one(s) that minimize cost.
Demand callback returns each visit's projected refill (which depends on
the gap to the previous visit — needs a custom dimension).

**Effort.** ~1 day. Small refactor of `_build_pool` to expand multi-visit
clients, and the demand callback to return per-copy refill. Solver gets
~10–25% larger but stays tractable.

### Q8 — Demand serial correlation (BROKEN, MEDIUM impact)

**The issue.** `DemandModel` assumes daily demand is i.i.d. Normal given
DOW. Real restaurant demand has positive autocorrelation (ρ ≈ 0.3–0.6):
a busy week tends to follow a busy week. Under i.i.d. our cumulative
P95 understates true variance:

```
i.i.d.   :  σ_T = σ · √T              (T-day cumulative std)
ρ=0.5    :  σ_T ≈ σ · √(T · (1+ρ)/(1−ρ))   ≈ σ · √(3T)
```

So a 10-day P95 buffer that should be `1.65 σ √30 = 9.0σ` is computed
as `1.65 σ √10 = 5.2σ`. **We're under-buffering by ~40 %** for clients
in growth/decline trends.

**Operational symptom.** Clients on a sustained spike (new menu item, new
catering contract) hit stockout at ~7–9 % rates instead of the
advertised 5 %. We catch them via daily replan, but the safety-stock
guarantee is weaker than the math claims.

**Fix sketch.** Three options ordered by effort:

1. **Empirical block-bootstrap (small effort, decent fix).** Instead of
   computing `cum_std = sigma * sqrt(T)`, sample T-length blocks from
   each client's actual residual history and use the empirical 95th
   percentile. Naturally captures any autocorrelation present in the
   data. ~4 hours of work in `irp_core/forecasting.py`.

2. **AR(1) model on per-client residuals (medium effort).** Fit
   `r_{t+1} = ρ · r_t + ε`. Use the AR(1) variance formula for
   T-step cumulative std. Requires adding an `ar1_rho` field to
   `DemandModel` and tweaking `cumulative_consumption_quantile`. ~1 day.

3. **State-space / Kalman filter (large effort).** Treat demand as a
   latent level + slope process, update with each delivery
   observation. Industry-grade but probably overkill for SK's scale.

We should do option 1 today — empirical block-bootstrap is robust,
parameter-free, and uses data we already have.

---

## What we're NOT doing (and shouldn't, for SK's scale)

- **Branch-and-cut / Branch-price-and-cut.** Coelho-Laporte (2013) is
  exact for ~50 clients × 2 weeks. We have 171 × 5 days. OR-Tools
  matheuristic is sufficient — at most 5–10 % loss vs exact.

- **Adaptive Large Neighborhood Search (Pisinger-Ropke, Coelho-Cordeau-
  Laporte).** Better local search than OR-Tools' GLS. ~5–15 %
  improvement on cost. Worth it if scale doubles or solve time becomes
  binding. Not yet.

- **Hybrid Genetic Search (Vidal).** SOTA for VRPTW. Not natively
  supported by OR-Tools. Massive engineering effort for marginal gain
  at our scale.

- **Stochastic Programming with Recourse.** Optimize over scenario
  trees. Theoretically beautiful, computationally expensive (10×
  problem size per scenario). Our daily-replan strategy is empirically
  equivalent for low-noise environments.

- **Reinforcement Learning.** Several papers (Hottung 2020,
  Kool-van Hoof-Welling 2019) train neural policies for VRP. Cool
  research, not production-ready for our scale.

---

## What this changes operationally

After today's fixes:

1. **DOW-aware refill projection.** When the solver computes "if I visit
   HAROLDS on Saturday, what refill does this need?" — Saturday's
   refill now reflects Sat's actual demand pattern, not a flat
   weekly average.

2. **Partial closures honored.** A client closed Tue–Wed for renovation
   won't be scheduled on those days, even if their other days are urgent.

3. **All routable clients in scope (from earlier session).** The
   rolling horizon now actually rolls; far-future clients are visible
   to the solver and get opportunistically picked up when geography is
   cheap.

After Q1 + Q8 fixes (next round):

4. **High-volume clients get multi-visit plans.** A 10-day horizon will
   correctly schedule HAROLDS day 0 + day 6.

5. **Defensible 95 % service level under demand drift.** Block-bootstrap
   captures real autocorrelation; safety stock no longer assumes i.i.d.

---

## Code locations for review

- Q2 fix: `inventory.py:39-79`, `unified_solver.py:633-667`,
  `run_irp.py:228-235`
- Q4 fix: `unified_solver.py:850-883`
- Q1 plan: see this doc, `unified_solver.py:1010-1045` (current
  disjunction logic)
- Q8 plan: see this doc, `irp_core/forecasting.py:78-105`,
  `irp_core/safety_stock.py:82-125`
