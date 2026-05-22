# SK ROUTE OPTIMIZER — ROOT CAUSES & PROPOSED FIXES

Ranked. Critical-first. Each entry: evidence → mechanism → fix → expected
impact → risk if wrong.

---

## RC-1 (CRITICAL) — Drop penalty is ~$0.10 for clients beyond horizon supply

### Evidence
- v2 plan: 88 stops over 10 days. Tammy averages 15.1/day × 9 workdays = 136.
- 83 clients deferred; 59 with reason `NOT_NEEDED_THIS_HORIZON`.
- Code (`v2/solver/model.py` line ~370):
  ```python
  days_dry = max(0.0, horizon_days_count - days_supply)
  drop_penalty = int(days_dry * rate * stockout_cost_units)
  drop_penalty = max(drop_penalty, 100)   # $0.10 floor
  ```
  For a client with 12d of supply on a 10d horizon → `days_dry = 0` → penalty = $0.10.

### Mechanism
The model treats "this horizon" in isolation. Any client whose tank survives
10 days is "fine to skip." But IRP is rolling: a deferred client must be
served in a future horizon at roughly equivalent route cost. The drop
penalty must price that future delivery, not zero it.

### Fix
Replace the linear "days_dry" penalty with a piecewise function that costs:

```
drop_penalty(client) =
    P_HARD   if days_supply < 2                  (stockout-imminent: mandatory)
    P_HIGH   if days_supply < horizon_days       (will dry within horizon)
    P_MED    if next_target_visit <= horizon_end (would-visit-this-week)
    P_LOW    otherwise                           (truly safe to defer)

where next_target_visit = days until tank drops to target_empty_fraction
(=30% of tank), NOT zero. Tammy stays ahead of empty, not at empty.
```

Concrete values (in dollars, will be scaled by `COST_SCALE = 1000`):
- `P_HARD` = $10,000      (effectively mandatory)
- `P_HIGH` = $500         (will stock out — must serve)
- `P_MED`  = **$25–30**   (deferable but next horizon costs the same)
- `P_LOW`  = $5           (truly low priority; only serve if free)

### Expected impact
- Lifts stop count from ~88 to ~125–140 (matching Tammy's pace).
- Forces serving the 50 clients in the 5–10 day window (currently deferred).
- Keeps 83 truly-safe clients optional (no false-positive scheduling).

### Risk if wrong
- Too high → over-delivers, wastes route time on full tanks.
- Mitigated by: keeping the `min_stop_lbs` soft penalty so the model
  won't visit a near-full tank for a 30-lb top-off.

---

## RC-2 (CRITICAL) — Labor cost double-counts salary on regular hours

### Evidence
- `economics.yaml`: `cost_per_minute_labor: 0.83` (~$50/h).
- User stated drivers are weekly-salaried.
- Model applies this to **every** minute via `SetSpanCostCoefficientForVehicle`.
- Worked example (AUDIT §A.3): 4-hour route shows $199 phantom labor cost,
  85% of total apparent cost.

### Mechanism
With salaried drivers, regular-hours labor is **sunk** — paid regardless of
truck activity. Charging $0.83/min to the optimizer makes the second truck
look ~$200/day more expensive than it really is, biasing toward single-truck
routes.

### Fix
- `cost_per_minute_labor` (regular hours) → **$0.00/min**
- OT minutes only: `ot_cost_per_min = $0.83 × (1.5 − 1) = $0.42/min`
- Mechanically:
  - Remove `SetSpanCostCoefficientForVehicle` (or set to 0).
  - Keep `SetCumulVarSoftUpperBound(end_var, target, ot_premium_units)` where
    `ot_premium_units = $0.42 × COST_SCALE`.

### Expected impact
- Solver no longer "saves money" by collapsing onto one truck.
- Will dispatch both trucks on regular days (matching Tammy).
- Single-truck still favored on Saturdays (rule) and very-light days
  (no stops to spread).

### Risk if wrong
- If drivers are NOT actually salaried (hourly), we'd be under-pricing
  labor and might over-route.
- Mitigated by: keeping fuel ($0.55/mi) and OT premium ($0.42/min over 8h)
  intact, which still bound total route time.

---

## RC-3 (HIGH) — `truck_dispatch_cost = $10` retains anti-second-truck bias

### Evidence
- Reducing this from $50 → $10 already moved the plan from "1 truck most
  days" to "2 trucks on 3 of 9 days." Still not "2 trucks every workday."
- Marginal dispatch cost analysis (AUDIT §A.2): real cost is fuel warm-up
  (~$2) + minor wear (~$3) = $5/day.

### Fix
- `truck_dispatch_cost` → **$0/day** (the warm-up fuel is already in
  `cost_per_mile` once the truck starts driving).
- Saturday-no-Truck9 is enforced via `truck_available` matrix, not via
  fixed cost.

### Expected impact
- Final nudge toward both-trucks-daily on regular workdays.
- Solver freely splits even single-stop days across trucks when convenient.

### Risk if wrong
- If there IS a real $10–15 per dispatch (uniform setup), we under-price
  it. Mitigation: monitor in backtest; can re-tune if needed.

---

## RC-4 (HIGH) — Capacity callback over-counts via `max_refill_per_client`

### Evidence
- `v2/solver/model.py` lines 281–293.
- Mean MAX refill across pool ≈ 825 lbs; mean actual refill ≈ 657 lbs.
- 26% over-counting.

### Mechanism
Shared callbacks (required for OR-Tools disjunctions) can't be per-vehicle.
The code defaults to the conservative max-across-horizon. Result: the
capacity dimension over-estimates demand → trucks are "full" earlier than
reality → fewer stops scheduled.

### Fix
Two options, in order of preference:

**(a) Smarter aggregate** (low-risk): use a percentile across feasible
days rather than max. The 75th percentile is a sensible upper bound that's
still meaningfully smaller than max for high-rate clients.

```python
def _demand_cb(from_idx):
    n = manager.IndexToNode(from_idx)
    if n == 0: return 0
    feasible = [refills_by_day[d][n] for d in range(n_days)
                if refills_by_day[d][n] >= min_stop_lbs]
    if not feasible: return max_refill_per_client[n]
    return int(np.quantile(feasible, 0.75))
```

**(b) Per-vehicle callbacks** (correct but riskier): use day-specific
demand callbacks. Requires verifying that disjunctions still work.

We use **(a)** initially. Switch to (b) only if (a) still over-constrains.

### Expected impact
- Capacity dimension realistic → solver can pack more stops per truck-day.
- Slightly higher OT risk on heavy days; the OT premium handles that.

### Risk if wrong
- True peak demand exceeds 75p → truck overflows in extraction.
  Mitigated by: `_check_no_tank_overflow` invariant catching it pre-write.

---

## RC-5 (HIGH) — Consumption rate estimator uses 75p of full history

### Evidence
- HAROLDS: model 80 lpd, historical 169 lpd — solver misses recent
  acceleration entirely → DTE 11.9d (actual ~5d).
- 7/161 clients have model rate <50% of recent history.

### Mechanism
`forecast/consumption.py` aggregates ALL gaps observed → 75p over the
whole period. A client whose business doubled three months ago is rated
on the average of "pre-double" and "post-double" gaps → 75p sits in the
middle → underestimate.

### Fix
Recency-weighted rate, plus a "recent acceleration" floor:

```python
rate_60d = 75p of gaps in last 60 days   (recency-weighted)
rate_all = 75p of gaps in all available history
rate     = max(rate_60d, rate_all)
```

This way:
- Steady customers: same rate as before.
- Accelerating customers: use the higher recent rate.
- Decelerating customers: stay with the cautious all-time rate (safety).

### Expected impact
- HAROLDS rate → ~150 lpd → DTE → ~6d → scheduled in committed window.
- Eliminates the recurring stockout pattern for high-velocity clients.

### Risk if wrong
- Over-counts demand for a customer that genuinely accelerated then
  stabilized at a new high. Acceptable — minor over-scheduling beats
  recurring stockouts.

---

## RC-6 (MEDIUM) — `min_stop_lbs` is a hard forbid, not a soft penalty

### Evidence
- `v2/solver/model.py` lines 161–167 set refill = 0 when below threshold
  AND non-urgent, then line 405 forbids that (client, day) entirely.
- Real ops: "we're already at the strip mall, drop 80 lbs at the bakery
  next door" is a routinely-good idea.

### Fix
Keep the `min_stop_lbs` threshold but apply it as a **soft service-time
penalty** instead of a hard forbid:
- If the client needs <`min_stop_lbs`, charge an extra "uneconomic stop
  fee" of $5–10 in the cost callback.
- Solver will skip the stop *unless* the geographic detour cost is less
  than the fee.
- Urgent clients (DTE ≤ 3): no penalty.

### Expected impact
- Adds opportunistic top-offs on geographic clusters.
- Lifts stops/day by ~5–10% without driving up miles.

### Risk if wrong
- Pump setup time (18 min) on a 50-lb stop is genuinely uneconomic.
  Mitigation: tune the penalty to match the labor-equivalent of 18 min
  even with $0 base labor (use the OT-shadow cost as the basis).

---

## RC-7 (MEDIUM) — Committed window is unenforced

### Evidence
- `policy.yaml`: `commit_days = 2`. No corresponding constraint in
  `model.py`. Code search confirms it's used only in reporting.

### Mechanism
Operator expects days 0–1 ("today + tomorrow") to be locked-in plans
they can dispatch with confidence. If the cost model says "defer to
day 3," it does so, leaving the committed window thin/empty.

### Fix
Add a constraint: any client with DTE ≤ (commit_days + safety_buffer)
**must** be visited within the commit_days window. Implementation: for
those clients, remove all vehicle indices outside days 0–1 from their
`VehicleVar`.

### Expected impact
- Tomorrow + day-after schedules look like "what Tammy would actually
  dispatch" — full coverage of urgent customers.

### Risk if wrong
- A bad consumption-rate estimate produces a "false urgent" forced into
  day 0 → small inefficiency. Acceptable.

---

## RC-8 (LOW) — Saturday rule via 10⁹ fixed cost

### Evidence
- `v2/solver/model.py` lines 326–334.

### Fix
Use `truck_available[(date,truck)]` (already exists in `ProblemInstance`):
for any (date, truck) flagged unavailable, remove every node-on-that-day
from that truck's vehicle var. Cleaner, no magic numbers.

### Impact
- No behavior change, just maintenance hygiene.

---

## RC-9 (DATA / OPERATOR) — Anova coverage 25%

### Evidence
- 43 / 169 clients have Anova readings.
- 75% of tank levels derived from "days since last delivery × rate"
  → compounds rate errors.

### Fix
- **Not a software bug.** Operator action: deploy more sensors.
- Software side: improve the recency-weighted rate (RC-5) to reduce
  drift impact on un-sensored tanks.

### Impact
- Sensors are the durable fix. Software workaround reduces the gap by
  perhaps 30–40% but won't eliminate it.

---

## RC-10 (LOW) — Diagnostics sheet message misleading

### Evidence
- Excel Diagnostics sheet shows: "(invariants not run at write time)"
- But `pipeline.py` does run `check_plan()` before writing → message stale.

### Fix
Surface the actual check result in the Diagnostics sheet.

---

# IMPLEMENTATION PRIORITY

Apply RC-1, RC-2, RC-3, RC-4 first (the model surgery). Then RC-5, RC-6,
RC-7 (data + soft-constraint tuning). RC-8/9/10 are polish. The first
four alone should close ~80% of the Tammy-gap.

The final solver (`sk_solver_final.py`) implements all of them.
