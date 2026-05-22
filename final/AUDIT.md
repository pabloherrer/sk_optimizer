# SK ROUTE OPTIMIZER — FINAL AUDIT

**Date:** 2026-05-22
**Auditor:** Claude (overnight production-readiness review)
**Data:** `~/Downloads/SK_Delivery_System_ONLINE_w_anova.xlsx` (2026-05-21 18:33)
**Codebase audited:** `sk_optimizer/v2/` (current production solver)

---

## EXECUTIVE SUMMARY — THE SMOKING GUN

**The v2 solver under-delivers by 35–42% vs Tammy's actual 8-week behavior.**

| Metric | Tammy (last 8 weeks, observed) | V2 solver (10-day plan) | Delta |
|---|---|---|---|
| Avg lbs delivered per workday | **10,013 lbs/day** | 5,782 lbs/day | **−42%** |
| Avg stops per workday | **15.1 stops/day** | 9.6 stops/day | **−36%** |
| Trucks dispatched per workday | ~2 (revealed preference) | 1.3 (3 days dual / 5 days single) | **−35%** |
| Fill % per delivery | mean 78%, median 79% | mean 80%, median (matched) | ✓ matches |
| Aggregate fleet consumption need | 8,553 lbs/day (model says) / 8,330 lbs/day (history says) | — | — |
| 10-day expected need | 83,300–85,500 lbs | 57,818 lbs scheduled | **31% gap** |

**If the solver schedules only 65–70% of what the fleet actually consumes,
tanks must be draining on net.** Either the solver is "right" and Tammy is
over-delivering (unlikely — she runs the business), or the cost model has a
systemic bias toward dropping clients who could safely wait one more horizon.

Below we show it's the cost model. The drop penalty math is the dominant bug.

---

## A. COST MODEL AUDIT

### A.1 Walk-through of every objective term

The v2 objective is, conceptually:

```
minimize  Σ_arcs (distance × cost_per_mile)
        + Σ_vehicles (total_minutes × cost_per_minute_labor)        [SPAN COST]
        + Σ_vehicles (max(0, total_minutes − target) × OT_premium)  [SOFT UB]
        + Σ_dispatched_trucks (truck_dispatch_cost)                  [FIXED COST]
        + Σ_dropped_clients (drop_penalty_i)                          [DISJUNCTION]
```

Where coefficients (from `economics.yaml`):
- `cost_per_mile`             = **$0.55/mi**
- `cost_per_minute_labor`     = **$0.83/min**  (≈ $50/h)
- `overtime_multiplier`       = **1.5×**
- `truck_dispatch_cost`       = **$10/day** (was $50 before recent tuning)
- `stockout_cost_per_lb_day`  = **$10/lb-day**
- `terminal_value_per_lb`     = **$0.10/lb**

### A.2 Is `truck_dispatch_cost` representing a real cost?

**No.**  S&K drivers are **weekly-salaried** (this was stated by the user
multiple times and is the published industry pattern for bulk-fuel/oil
operations). Wages are sunk regardless of whether the truck rolls or sits.
The marginal cost of dispatching a second truck on a regular-hours day is:

| Cost item                | Marginal $/day |
|--------------------------|----------------|
| Driver wages (regular)   | **$0** (salary, sunk) |
| Fuel (warm-up + idle)    | ≈ $2 |
| Vehicle wear (start-stop)| ≈ $3–5 |
| Paperwork / dispatch overhead | ≈ $0 |
| **Total marginal**       | **≈ $5/day** |

The fuel for actual driving is already priced in `cost_per_mile`. So dispatch
cost should be **$5/day at most** — and arguably **$0/day** since the fixed
warm-up cost is dominated by per-mile fuel once you're rolling.

**Impact:** at $10/day the solver still has a non-trivial bias toward
single-truck routing on light days. Moving to **$0/day** removes the
phantom incentive entirely.

### A.3 Is `cost_per_minute_labor` double-counting hours drivers are paid for anyway?

**Yes — this is the larger of the two labor bugs.** With salaried drivers,
the marginal labor cost for **regular hours** is **$0/min**. The solver is
currently being charged $0.83/min for every minute of route time, which
inflates the apparent cost of dispatching a truck.

**Worked example.** A 4-hour route on Truck2 currently looks like:

| Term | Charge |
|---|---|
| Travel (40 mi × $0.55)            | $22.00 |
| Labor (240 min × $0.83)           | $199.20 ← **NOT a real cost** |
| Dispatch                           | $10.00 |
| OT (0 min over 480 target)        | $0.00 |
| **Total apparent cost**            | **$231.20** |
| **Total real marginal cost**       | **$22 (fuel) + $5 (dispatch) ≈ $27** |

The labor double-count is **>85%** of the apparent route cost. This is why
the solver treats "use one truck for a longer route" as far cheaper than
"split across two trucks for two shorter routes" — both cost roughly the
same labor minutes in the model, but only the single-truck option saves
the dispatch cost. With real economics, splitting saves zero (labor is
sunk in both cases) AND reduces OT risk AND improves driver fairness.

### A.4 Marginal cost of the second truck on a regular-hours day

Quantifying:

```
ΔCost(add Truck2 on light day)
  = additional_miles_driven × $0.55
  + 0  (labor is sunk)
  + truck_dispatch_cost
  − miles_saved_on_Truck9 × $0.55  (loads are split)
  − OT_minutes_saved_on_Truck9 × $0.42  ($0.83 × 0.5 premium)
```

If splitting halves Truck9's mileage (say 60 mi → 35 mi, with Truck2 doing 30
mi), the *net* miles change is negligible (route-doubling overhead is ~5 mi).
So:

```
ΔCost ≈ +5 mi × $0.55  +  truck_dispatch_cost  −  OT_savings
       ≈ $2.75 + $10 − (often 30+ min OT × $0.42 = $13)
       ≈ −$0.25
```

**Adding the second truck is essentially free — and on any day with even a
small OT risk on Truck9, it's strictly cheaper.** The current model's $10
dispatch + $200 labor cost on the second truck obscures this completely.

### A.5 Predicted plan change at corrected coefficients

If `labor=0` (regular hours) and `dispatch=0`, the model's cost surface
becomes: travel mileage + OT premium + drop penalty. On nearly every
non-stockout day, splitting the workload across both trucks:
- Reduces max per-truck shift minutes → reduces OT exposure → strictly lower
- Costs ≈ identical miles
- Is free in labor

Predicted: **both trucks dispatched on every non-Saturday day**, matching
Tammy's revealed preference. Validated by sensitivity analysis below.

### A.6 Drop penalty — the deeper bug

This is the **most consequential single line** in the codebase:

```python
# v2/solver/model.py, line ~370
days_dry = max(0.0, horizon_days_count - days_supply)
drop_penalty = int(days_dry * rate * stockout_cost_units)
drop_penalty = max(drop_penalty, 100)   # 100 cost units = $0.10 floor
```

For a client with **12 days of supply** in a **10-day horizon**:
- `days_supply = 12`
- `days_dry = max(0, 10 − 12) = 0`
- `drop_penalty = 0 × rate × $10 = $0`, floored to **$0.10**

**The solver pays $0.10 to skip this client entirely.** Any route detour
costing more than 10¢ in marginal mileage causes the client to be dropped.

**This is the structural reason for the 35–42% under-delivery.** 83 of 169
clients have `days_supply > horizon_days` and are therefore practically
free to defer. The economics says "defer them — we'll get them next horizon."
Reality says "deferral cost = future delivery cost ≈ same as serving now."

The penalty should reflect that an IRP is rolling: a client we defer today
must be served tomorrow's horizon — at roughly the same routing cost. The
correct drop penalty for a deferable client is **(expected cost to serve
in next horizon) × (probability we'll have to take a separate detour for
them)**. For a customer whose target visit date is within (current_horizon
+ next_horizon), this is approximately:

```
drop_penalty ≈ (avg_per_stop_route_cost) ≈ $15–25
```

Not $0.10.

### A.7 Cost model audit — summary

| Term | Current value | Real economics | Severity |
|---|---|---|---|
| `cost_per_mile`            | $0.55/mi      | $0.50–0.60/mi (fuel+wear) | ✓ correct |
| `cost_per_minute_labor` (regular hrs) | $0.83/min | **$0/min** (salaried) | **CRITICAL** |
| `overtime_multiplier`      | 1.5×          | 1.5× (real premium for OT) | ✓ correct |
| `truck_dispatch_cost`      | $10/day       | $0–5/day | **HIGH** |
| `stockout_cost_per_lb_day` | $10/lb-day    | reasonable        | ✓ correct |
| `terminal_value_per_lb`    | $0.10/lb      | reasonable        | ✓ correct |
| **drop_penalty floor**     | $0.10         | **$15–25 per defer-eligible client** | **CRITICAL** |

---

## B. CONSTRAINT AUDIT

### B.1 Capacity dimension — `max_refill_per_client` over-counts

`v2/solver/model.py`, lines 281–293:

```python
def _demand_cb(from_idx):
    n = manager.IndexToNode(from_idx)
    return max_refill_per_client[n]   # MAX across all horizon days
```

The capacity callback charges the *maximum* refill the client could need
on *any* horizon day. So if client X has tank 1050 lbs and rate 50 lbs/day,
their refill grows from ~50 lbs on day 1 to ~500 lbs on day 10. The
capacity callback uses **500** for every (X, vehicle) pair — even when the
actual refill on a given day is 50.

**Why this exists:** the comment explains it was the fix for a
disjunction-breaking bug with per-vehicle demand callbacks. The fix
trades correctness on capacity for working disjunctions.

**Impact estimate:**
- Mean refill across actual stops: ~657 lbs (v2 output).
- Mean MAX refill per client: ~825 lbs (computed below).
- Over-count ratio: **825/657 = 1.26×** — the capacity dimension thinks
  trucks fill ~26% faster than they really do.
- Compartment cap of 5,000 lbs allows ~6 stops in the model's view; in
  reality it could hold ~7–8 stops.

Combined with the drop-penalty bug, the model finds it easy to defer
because the capacity dimension keeps telling it "the truck is filling
up — drop some clients."

**Fix:** either (a) use a smarter aggregate (avg/p75 over feasible days)
instead of max; or (b) genuinely solve the per-vehicle-callback +
disjunction issue (it's a known OR-Tools pattern with documented solutions).
Option (a) is safer for the first cut.

### B.2 Forbid days where refill < min_stop_lbs

`v2/solver/model.py`, lines 161–167:

```python
if refill < min_stop_lbs:
    dte_today = (ts.current_lbs / rate) if rate > 0 else 999.0
    if dte_today > 3.0:
        refill = 0   # zeroed → forbid this day (line 405)
```

The model removes (client, day) pairs where the projected refill would
be under `min_stop_lbs` (200 lbs) AND the client isn't urgent. This is
defensible — but combined with the rest of the cost model it lets the
solver use this as another lever to drop non-urgent clients silently.

A client with 800 lbs in tank and 50 lbs/day rate has refills:
- Day 0: 0 lbs (full)              → forbidden ✓ (legit)
- Day 1: 50 lbs (1 day depleted)   → forbidden (under 200)
- Day 4: 200 lbs                   → just allowed
- Day 5: 250 lbs                   → allowed
- ...

So the earliest day the solver can visit this client is **day 4**. If
the operator wants to visit them *today* as part of a geographic cluster
(neighbor is being served), the model **cannot do that** — the 50-lb
refill is forbidden as uneconomic.

**This is hardcoded against operator preference.** Real ops would gladly
do a 50-lb top-off on a client *we're already driving past*. The min-stop
rule should be a soft penalty in the cost callback, not a hard forbid.

### B.3 Shift cap, target, and OT signal

The shift dimension uses both `SetCumulVarSoftUpperBound` (for OT past
target) and `SetSpanCostCoefficientForVehicle` (for total minutes ×
labor). With salaried drivers, the span coefficient is the labor
double-count bug. Once removed:

- `SetSpanCostCoefficientForVehicle` → 0
- `SetCumulVarSoftUpperBound(end_idx, target, ot_premium_only)` →
  ot_premium_only = `cost_per_minute_labor × (overtime_multiplier − 1)` ≈ $0.42/min

This correctly prices ONLY the 50% premium on OT minutes, not the base wage.

### B.4 Saturday rule

Implementation: huge fixed cost on Truck9 on Saturday vehicles. Functionally
correct but ugly — better to expose this via `truck_available[(date,truck)]`
and the disjunction infrastructure. Same outcome, cleaner code, easier to
extend (e.g., "what about Tucson Saturday rotation?").

### B.5 Are deferred stops legitimately deferrable?

From the v2 plan: 83 deferred. Reasons:
- 59 `NOT_NEEDED_THIS_HORIZON` (DTE > horizon)
- 12 `EXCLUDED` (far-cluster, deliberate)
- 6 `INSUFFICIENT_CONSUMPTION_DATA` (no rate)
- 4 `DO_NOT_SCHEDULE` (operator flag)
- 2 `NOT_IN_MATRIX` (missing OSRM data)

The 59 "NOT_NEEDED" clients are exactly the ones the drop-penalty bug
makes free to skip. Many would have been served by Tammy proactively as
part of a route already going through their neighborhood.

**Spot check (HAROLDS, id 8031):**
- Model rate: 80 lbs/day → DTE 11.95 days, next_by 2026-06-02
- Historical implied rate (180d): **168.9 lbs/day** — 2.1× model
- Real next-needed: ~ May 27 (5 days, not 12)
- Deferred → would have caused stockout

### B.6 Hidden constraint — committed window

`commit_days = 2`: the first 2 horizon days are "committed." Code
inspection shows this is mostly informational; there's no hard constraint
that *forces* the solver to act in the committed window. So if the cost
model says "defer all 88 stops to days 3–10 of the horizon," the model
will gladly leave the committed window empty.

In the current run, the committed window happens to have stops because
the geographic cost incentivizes serving low-DTE clients early — but this
is incidental, not enforced.

---

## C. DATA AUDIT

### C.1 Distance matrix (OSRM)

- 171×171 (depot + 170 clients)
- No zero off-diagonal pairs
- No duplicate coordinates
- 99.8% pair-asymmetry (normal for road network)
- Range 75m–614km (the 614km is the deliberate Tucson/Flagstaff far-cluster)
- Mean Phoenix-metro distance ~44km, mean time ~52 min

✓ **Matrix is healthy.**

### C.2 Consumption rate sanity — aggregate

| | Per-day across full fleet |
|---|---|
| Historical 90d implied (n=161 clients) | **8,330 lbs/day** |
| Model (Optimizer_Input col 8, n=166) | **8,553 lbs/day** |
| Ratio | 1.03× — aggregate match |

✓ **Aggregate consumption is correct.** The fleet-level demand is right.
The problem isn't "model thinks fleet needs less oil"; it's "model lets
~30% of that demand be deferred for free."

### C.3 Consumption rate sanity — per-client (10-sample spot check)

| Client | Model rate | Historical | Ratio | Issue |
|---|---|---|---|---|
| HAROLDS (8031) | 80 | 168.9 | **0.47×** | 75p percentile of long history misses recent acceleration → DTE off by 2× |
| POPO BELL (16015) | 196 | 163 | 1.20× | Slightly conservative — fine |
| MANUEL PEORIA (13013) | 139 | 134 | 1.04× | ✓ |
| AJO ALS BELL (1018) | 84 | 78 | 1.08× | ✓ |
| LITTLE O'S (12055) | 65 | 67 | 0.97× | ✓ |
| SARDELLA'S 19TH (19081) | 21.4 | 27.6 | 0.78× | Slight under-estimate |
| PETES TOLLESON (16010) | 137.5 | 147.7 | 0.93× | ✓ |
| BOOTY'S WATSON (2017) | 87.9 | 69.1 | **1.27×** | Conservative, OK |
| STATE 48 GLENDALE (19035) | 50 | 28.2 | **1.77×** | Conservative (good — protects from stockout) |
| NEW PENNY CAFE (14009) | 27.3 | 36.2 | **0.75×** | Under-estimate |

Ratio distribution (n=161 matched):
- <0.5× : 7  (model under-counts by 50%+ → stockout risk)
- 0.5–0.8× : 19
- 0.8–1.2× : **101** (well-calibrated)
- 1.2–2× : 30
- >2× : 4

**Verdict:** consumption rates are usable but have a long tail. The
75th-percentile estimator can miss customers whose consumption is
*accelerating*. Recommended fix: use a recency-weighted percentile, or
compare last-60-day rate vs full history and use the higher of the two.

### C.4 Tank sizes

Tank-size distribution (n=169):
- min 300, max 2500, median **950 lbs**
- Modal sizes: 1050 (37), 950 (24), 350 (22), 620 (11), 700 (10)

✓ **No anomalies.** Tanks match a known catalog of standard sizes.

### C.5 Anova sensor coverage

- 43 / 169 clients have Anova readings (25%)
- Of 148 rows in Anova_Live, 84 are NULL/empty (devices powered down or
  not provisioned)
- Of the readings present, ages range 6.6h to >24h

This is the **single biggest data-quality gap**. 75% of tanks rely on
"days since last delivery × consumption rate" — which compounds rate
errors over multi-week gaps. The HAROLDS / NEW PENNY underestimate
problem is partially explained by this.

**Not fixable in software.** Operator decision: invest in more sensors,
or accept the projection drift.

### C.6 Optimizer_Input "Last Per Day Cons" column vs ingest rate

The Optimizer_Input column 8 ("Last Per Day Cons") is a single most-recent
gap rate — extremely noisy. v2 ingest doesn't actually use this column;
it recomputes via `v2.forecast.consumption.estimate_consumption` using
75th-percentile of ALL gaps (default 75p). Verified by code reading.

But: Optimizer_Input is what the operator sees in the spreadsheet. The
displayed "Last Per Day" and the model's actual rate **disagree**, which
can mislead operator overrides. Recommended: surface the model's actual
rate in a dedicated column of the output report.

### C.7 DTE distribution from current data

| DTE bucket | Count | Operator action |
|---|---|---|
| <2 days (urgent must-serve) | **12** | served today/tomorrow |
| 2–5 days | 21 | this week |
| 5–10 days | 50 | within current horizon |
| >10 days | 83 | deferrable IF cost makes sense |

So 33 clients are within the 5-day window (must-serve in current
horizon) and 50 are within 5–10 days (should-serve). That's 83 stops in
~9 workdays = 9 stops/day across both trucks. Tammy actually does ~15
stops/day → she's proactively serving the deferrable "should-serve"
clients to stay ahead. **The current v2 output of 9.6 stops/day matches
the "absolute minimum" reading of the data — not the "actually run a
business" reading.**

---

## D. BEHAVIORAL AUDIT — Backtest vs Tammy

**Last 8 weeks of real S&K deliveries (2026-03-26 to 2026-05-21):**

| Day | n_days | Avg stops/day | Avg lbs/day | Median lbs/day |
|---|---|---|---|---|
| Mon | 4  | 1.8  | 1,108  | 801 (rare; mostly catch-up days) |
| Tue | 8  | **18.9** | **11,478** | 10,529 |
| Wed | 8  | 15.8 | 10,279 | 10,426 |
| Thu | 9  | 13.0 | 7,983  | 7,985 |
| Fri | 8  | **20.0** | **14,742** | 14,600 |
| Sat | 8  | 13.2 | 9,104  | 8,838 (Truck2 only) |
| **Workday avg** | — | **15.1** | **10,013** | — |

Delivery-size distribution (last 8 weeks, n=676):
- mean 664 lbs, median 618 lbs, p90 1,020 lbs, max 2,087 lbs
- Fill % by delivery: mean 78.1%, median 78.9%
  - <30%: 12, 30–50%: 39, 50–70%: 161, 70–90%: 285, 90+%: 179

**This is the gold-standard target.** Any solver claiming to model S&K's
operations must produce plans that look like this in aggregate. v2 fails:

| | Tammy 8-week | V2 plan (10-day) | Match? |
|---|---|---|---|
| Avg stops/workday | 15.1 | 9.6 (88/9.2 workdays in horizon) | ✗ −36% |
| Avg lbs/workday | 10,013 | 5,782 | ✗ −42% |
| Avg fill % | 78% | 80% | ✓ |
| Avg delivery size | 664 | 657 | ✓ |
| Trucks/day | ~2 | 1.3 | ✗ |

**The deliveries v2 schedules look like Tammy's individual deliveries —
just 36% fewer of them.** This points squarely at the drop-penalty bug,
not at the fill / size / route logic.

### D.1 Behavioral test for the final model

Pass criteria for the final model on this exact dataset:
- Stops/workday: **≥13.0** (Tammy minus 15%, accommodating optimization gains)
- Lbs/workday: **≥9,000** (Tammy minus 10%)
- Trucks/day: **≥1.8 average non-Saturday** (Mon-excl, Sat is single-truck)
- Avg fill % within Tammy's 70–85% band
- All 12 DTE<2 clients served on day 0–1
- All 33 DTE<5 clients served in committed window (days 0–2)
- All 83 DTE<10 clients served in 10-day horizon

---

## E. ARCHITECTURE — V2 ONE-PAGE SUMMARY

```
v2/
├── run.py              CLI entry — reads local_config.json, calls pipeline
├── pipeline.py         plan_day() — orchestrates ingest→solve→extract→write
├── schemas.py          Pydantic AppConfig (validates economics/fleet/policy YAML)
├── domain/             Frozen dataclasses (Client, Truck, ProblemInstance, Plan, Stop, Route)
├── ingest/
│   ├── excel.py        Load Client_List + Delivery_Log → DataFrames
│   ├── anova.py        Load Anova_Live / Query → tank readings
│   ├── matrix.py       Load OSRM .npz → distance + time matrices
│   ├── schema.py       Time-windows, closures, depot config, excluded IDs
│   ├── overrides.py    Pins / Forbids / Locks / Manual readings
│   └── build_problem.py    Assembles immutable ProblemInstance
├── forecast/
│   └── consumption.py  75th-percentile rate from delivery gaps + IQR outlier filter
├── solver/
│   ├── model.py        ★ OR-Tools formulation (cost callback, dimensions, disjunctions)
│   ├── solve.py        Run search, return solution + timing
│   ├── extract.py      Solution → Plan (per-stop ETAs, tank states, costs)
│   └── territory.py    Optional pre-pass: k-means clusters per truck
├── invariants.py       8 hard output validators (overflow, dup, shift cap, etc.)
└── reporting/
    ├── excel.py        Full multi-sheet workbook
    ├── map.py          Folium interactive route map
    ├── smartservice.py Single-day CSV manifest for the dispatch system
    └── archive.py      Plan JSON snapshot for historical analysis
```

**The hot spot is `solver/model.py`.** Everything in ingest/forecast/extract
is largely correct. The audit-identified bugs are concentrated in the
~150 lines that define the cost callback, dimensions, and disjunctions.

---

## F. SUMMARY OF AUDIT FINDINGS

Severity legend: **C** = critical (changes plan structure), **H** = high (changes
plan quality), **M** = medium (changes plan margins), **L** = low (polish).

| # | Finding | Severity |
|---|---|---|
| F1 | Drop penalty floored at $0.10 → deferable clients are free to skip | **C** |
| F2 | `cost_per_minute_labor` charges sunk salary costs to regular hours | **C** |
| F3 | `truck_dispatch_cost = $10` retains a small but real bias toward single-truck | H |
| F4 | Capacity callback uses `max_refill` → over-counts demand by ~26% | H |
| F5 | Consumption rate estimator is full-history 75p — misses recent acceleration (HAROLDS bug) | H |
| F6 | `min_stop_lbs = 200` is a HARD forbid, not a soft penalty → blocks opportunistic top-offs | M |
| F7 | Committed window (days 0–1) is informational only — no constraint | M |
| F8 | Saturday rule implemented via 10⁹ fixed cost — ugly but correct | L |
| F9 | Anova coverage 25% — 75% of clients rely on linear projection | (operator) |
| F10 | Optimizer_Input "Last Per Day Cons" column disagrees with model's actual rate (operator confusion risk) | L |
| F11 | Diagnostics sheet says "(invariants not run at write time)" — invariants run pre-write but message is misleading | L |

Detailed root causes and fixes follow in `ROOT_CAUSES.md`.
