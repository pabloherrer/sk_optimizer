# FINAL PRE-PUSH REVIEW

**Verdict:** FIX FIRST — two correctness bugs in `sk_solver_final.py` will misbehave silently in production; everything else is solid.

---

## Critical issues (block push)

### C1. `truck_unavail_file` is constructed with a boolean-AND that throws away the Path on some runs
**File:** `final/sk_solver_final.py:314-317`

```python
truck_unavail_file = base.run_id.__class__ is str and (
    Path(__file__).resolve().parent.parent / 'data' / 'truck_unavailable.json'
)
if truck_unavail_file and truck_unavail_file.exists():
```

**What's wrong.** The expression `base.run_id.__class__ is str and (Path(...))` is doing pointless gating: if `run_id` is a `str` (always true — `main()` builds it from `f"FINAL_{...}"`), the AND short-circuits to the `Path(...)` object, which is what you want. If somehow it isn't a `str`, the result is `False` and `truck_unavail_file.exists()` would AttributeError on a bool — but that branch is unreachable in practice. So this is not a runtime crash; it is just a load-bearing piece of mystery code that reads like a half-removed type guard and could mask a future regression. Worse, in a deployment where `run_id` ever became non-`str` (UUID object, etc.), the dashboard's truck-unavailability widget would silently stop being honored — exactly the opposite of the failure mode you want for an operator-visible override.

**Why it matters.** Operator clicks "Truck 9 unavailable Thursday" → file is written by `server.py` → solver silently ignores it. No log. No invariant. The plan goes out the door scheduling a truck the operator just disabled.

**Suggested fix.** Just write it plainly:
```python
truck_unavail_file = Path(__file__).resolve().parent.parent / 'data' / 'truck_unavailable.json'
if truck_unavail_file.exists():
    ...
```

---

### C2. Spreadsheet `Est. Current` override has no clamp against bad data
**File:** `final/sk_solver_final.py:374-382` and `_load_sheet_est_current` at lines 245-275

The override path:
```python
if new_ts.source == 'estimated' and cid in sheet_current:
    sheet_val = sheet_current[cid]
    if sheet_val is not None and abs(sheet_val - new_ts.current_lbs) > 5:
        new_ts = replace(new_ts, current_lbs=float(sheet_val),
                          source='estimated (spreadsheet)')
```

**What's wrong.** The override accepts ANY numeric value the operator's spreadsheet emits in column L, with zero sanity checks:
- If the sheet has a **negative** value (formula bug, e.g. `tank - days*rate` with stale `last_delivery`), `current_lbs` becomes negative. Nothing downstream clamps it before `days_supply = current_lbs / rate` in §5.11(e) → `max_cal_days_allowed = days_supply + 1` becomes ≤ 1 → effectively forbids the client from every horizon day → client gets deferred when in reality they should be served URGENTLY (truly negative supply = stockout).
- If the sheet has a value **greater than `tank_capacity_lbs`** (formula bug), the projection `refill = tank - level_at_day` in §5.3 becomes negative → `max(0, ...)` clamps it to 0 → client looks tank-full all horizon → deferred. The invariant won't flag it because nothing was scheduled.
- If the sheet has `0`, the override fires (0 differs from a positive estimate by >5), and the client is treated as tank-empty even though our recency-weighted estimate may be far more reasonable. This is *probably* desired behavior, but it's worth a one-line comment confirming intent.

The audit explicitly mentions OREGANO QUEEN CREEK going from 64 to 162 lb to fix overflow — exactly the kind of correction the operator made manually. So the override is a real feature. But it needs at least:

**Suggested fix.**
```python
if new_ts.source == 'estimated' and cid in sheet_current:
    sheet_val = sheet_current[cid]
    if sheet_val is None:
        continue
    # Sanity clamp: reject obvious data errors (negative, or > 1.1x tank)
    tank = float(updated_tanks.get(cid, ts).__dict__.get('tank_capacity_lbs', 0))  # or pass capacity through
    if sheet_val < 0 or sheet_val > base.initial_tanks[cid].... :
        continue   # ignore bad sheet value, keep computed estimate
    if abs(sheet_val - new_ts.current_lbs) > 5:
        new_ts = replace(new_ts, current_lbs=float(sheet_val), ...)
```

At minimum: refuse sheet values < 0 or > tank_capacity (need to look the capacity up via `c.tank_capacity_lbs` from `base.clients`). Without this, a single bad cell in the operator's sheet quietly removes a client from the plan or causes overflow.

---

## High-priority issues (should fix soon, not blocking)

### H1. `_check_overrides_honored` DNS detection is a brittle substring match
**File:** `v2/invariants.py:200-205`

```python
dns_deferred = {
    cid for cid, reason in plan.deferred.items()
    if 'DO_NOT_SCHEDULE' in (reason or '').upper()
}
```

This works today because `extract.py:239` hard-codes the literal string `'DO_NOT_SCHEDULE'` (no extra punctuation). But the contract between extract and invariants is a magic string with no shared constant. If anyone ever changes extract to emit `'DNS'` or `'DoNotSchedule'`, the pin-vs-DNS conflict handling silently breaks and `OverrideHonorViolation` will fire for a perfectly correct plan.

**Fix.** Define a `DEFER_REASON_DNS = 'DO_NOT_SCHEDULE'` constant in `v2/domain/plan.py` and use it both places.

### H2. Median (50p) rate has no floor against zero-consumption clients
**File:** `final/sk_solver_final.py:153-224`

With `percentile=0.50`, a client whose last 60 days have one big delivery and many small ones now gets a substantially lower rate than 75p. This is fine for refill-volume accuracy (the docstring's rationale is sound). But:

- Combined with the new **no-late-scheduling constraint** in §5.11(e) (`max_cal_days_allowed = days_supply + 1`), a low rate means `days_supply` is LARGE → almost any horizon day is allowed → fine.
- BUT it also reduces the "needs to be served" signal: `compute_drop_penalty` walks the same `days_supply`. A 50p-rate client looks safer than a 75p-rate client and may fall to `DROP_PENALTY_LOW` ($5) → solver happily drops them → next horizon they really are dry.

The audit notes (line 142 commentary) that the rate is still the `max(60d_p50, all_time_p50)`. That's the safety net. But if the customer has been steadily consuming the whole time, both will be ~similar and the new value is still ~33% lower than 75p was. This is a **knowingly aggressive change**; just confirm the backtest already captured the stockout-count impact at 50p, not just at 75p. (The first sentence under §2 in the file claims backtest parity, so this may already be addressed — but it's worth a second-look the first week in production.)

### H3. At-risk block: `level = min(tank_cap or (level + qty), level + qty)`
**File:** `v2/reporting/excel.py:687`

```python
for dd, qty in client_deliveries:
    if dd == cursor:
        level = min(tank_cap or (level + qty), level + qty)
```

If `tank_cap` is `0` (lookup miss in the `for c in problem.clients` loop above — happens if the deferred client is somehow not in `problem.clients`), the expression `tank_cap or (level + qty)` falls through to the unclamped second operand, so `level` becomes `level + qty` with no cap. In practice every visited client comes from `pool` which came from `problem.clients`, so this is safe today — but defensively this should be:
```python
cap = tank_cap if tank_cap > 0 else float('inf')
level = min(cap, level + qty)
```

Also: the loop hits `tank_cap = ...; break` for each client, every day — that's O(N_clients²) for the at-risk block. Fine for ~200 clients, but build a `cap_lookup` dict above the loop.

### H4. Time-window range is set hard with no fallback
**File:** `final/sk_solver_final.py:670-700`

```python
open_off = max(0, int(open_off))
close_off = min(int(problem.shift_hard_max_min), int(close_off))
if close_off <= open_off:
    continue
node_idx = manager.NodeToIndex(i + 1)
try:
    time_dim.CumulVar(node_idx).SetRange(open_off, close_off)
```

Two concerns:

1. If a client's window is `[180, 240]` (9–10 AM) and the truck genuinely can't physically reach them by minute 240 from any depot start under any route, OR-Tools will return INFEASIBLE for the entire problem (or — more likely — the disjunction's huge `DROP_PENALTY_HARD` will be the cheaper alternative and the client gets dropped). The HARD penalty for a stockout-imminent client is $10,000 and the cost of dropping (no OT, no miles) is $0, so the solver will drop. **Net effect: a time-window-constrained urgent client whose window is physically impossible to meet will be silently dropped.** No log line is printed when a HARD-penalty disjunction is taken.

2. The clamp `close_off = min(shift_hard_max_min, close_off)` silently mutates the operator's intent. If a window is "11 AM – 5 PM" (300, 660) and `shift_hard_max_min = 615`, the window becomes (300, 615). The truck might leave the depot at 6 AM but the client is fine with arrival up to 5 PM — the clamp here is to the *cumulative shift minutes*, which is conceptually compatible (both measured from shift start), so it's correct on the math. Worth a code comment because re-reading this in 6 months will be confusing.

**Suggested fix.** Add a log line whenever a time-windowed client is dropped (urgency_tier was 'critical' or 'urgent') so operators see *why* their important client wasn't in the plan.

### H5. No-late-scheduling constraint can produce an empty allowed-vehicle set
**File:** `final/sk_solver_final.py:817-829`

```python
for i, c in enumerate(pool):
    ...
    days_supply = float(ts.current_lbs) / rate
    max_cal_days_allowed = days_supply + DRY_DAY_GRACE
    for v in range(n_vehicles):
        _, day_idx = v2td(v)
        if cal_days_to_d[day_idx] > max_cal_days_allowed:
            try: routing.VehicleVar(node_idx).RemoveValue(v)
            except Exception: pass
```

If `days_supply = 0.3` (already empty) and the horizon's first calendar-day offset is `2` (because today is Saturday and horizon starts Tuesday), then `max_cal_days_allowed = 1.3` and EVERY vehicle is removed → solver has no feasible day → the client gets dropped via disjunction. The `compute_drop_penalty` function would have classed them HARD (DTE < 2), so the cost of dropping is $10,000 — large but FINITE. With multiple such clients the solver may accept dropping a few rather than dispatching a Saturday Truck2, and you'd never see it. Combined with H4, this creates a silent stockout pathway.

**Suggested fix.** After the `RemoveValue` loop, if all vehicles were removed for a client, restore at least the EARLIEST day for that client (so a known-late delivery is still better than no delivery), and emit a console warning. Or simpler: only apply this constraint when `days_supply >= 1` so empty-today clients always go on day 0.

### H6. `pool` index lookup is O(n) per pin/forbid
**File:** `final/sk_solver_final.py:795-808, 902-905`

```python
i = next(idx for idx, c in enumerate(pool) if c.id == client_id)
```

Linear scan per override. Fine for small N. Build `pool_index_by_id = {c.id: i for i, c in enumerate(pool)}` once and reuse.

### H7. `prev_node` tracked in extract.py but never used
**File:** `v2/solver/extract.py:180`

```python
prev_node = next_node
```

Assigned, never read. Harmless but distracting — delete.

---

## Observations / code quality (nice-to-have)

- **sk_solver_final.py:565** — the `int(round(...))` of `tank - level_at_day` can yield a refill of 1 lb if rounding crosses, which is below `min_stop_lbs` but the §5.11(b/c) forbid will catch it. OK, just noting.
- **sk_solver_final.py:840** — `dte_today` for `is_zero_rate` clients is hard-coded to 999.0, then `continue`'s the loop, so it's dead code. Fine but confusing.
- **excel.py:706-714** — the nested `for (dd, tk), rte in plan.routes.items(): for s in rte.stops:` finds next/prev visit by iterating EVERY route every time. For a ~200-client at-risk block on a 5-day horizon, this is ~50k inner iterations per print sheet. Pre-compute a `{cid: sorted visits}` dict once.
- **excel.py:687** — clamp logic is needlessly opaque; `min(tank_cap, level + qty)` is clearer than `min(tank_cap or (level + qty), level + qty)`.
- **server.py:255** — `rows.sort(key=lambda r: r['dte'] if r['dte'] is not None else 999)` works but `999` is fragile. Use `float('inf')`.
- **app.js:475** — `Math.min(0.99, elapsed / budget)` — clear and well-commented, good.
- **server.py:284-299** — JSON unwrap logic for the archived plan's nested `route` key handles both list and dict shapes. Defensive and correct. Good.
- **app.js:347** — `displayName()` strips client ID prefixes for display; relies on `' - '` as a literal separator. Brittle to client renames; not blocking.
- **excel.py:1052** — `_build_summary` is called with `wb.active` (the auto-created sheet); subsequent sheets via `wb.create_sheet()`. Conventional and works.

---

## Things I checked and confirmed correct

- **Calendar-day fix is consistently applied.** `cal_days_to_d` is used at:
  - solver §5.3 (line 552) for `level_at_day` projection — correct
  - solver §5.11(e) (line 827) for the no-late-scheduling constraint — correct
  - extract.py:131 — `cal_days_to_arrival = max(0, (route_date - problem.today).days)` for `level_at_arrival` — correct, independently computed but consistent
  - I grepped for any remaining `d * rate` patterns; the only multiplications are `cal_days * rate` (line 557) and `cal_days_to_arrival * rate` (extract.py:132). Clean.
- **`current_lbs_today` field IS in the Stop dataclass** (`v2/domain/plan.py:25`), IS emitted by extract.py (line 153), and IS read by the dashboard renderer (`excel.py:335` via `getattr(stop, 'current_lbs_today', ...)`). No shape bug.
- **`do_not_schedule` field in Stop** (`v2/domain/plan.py:44`) defaults False and is checked in invariants — but Stop construction in extract.py NEVER sets it. The default of False means `_check_excluded_clients` is a no-op. That's actually fine because §5.1 already filters DNS out of `pool`, so no DNS can ever be in a Stop. But the invariant only protects against future regressions where someone adds DNS to pool — which is what invariants are for. Acceptable.
- **At-risk projection loop boundary**: `while cursor <= d:` with `if cursor < d: drain`. So on the final iteration (`cursor == d`) deliveries are applied but drain is skipped → level shown is post-delivery, post-arrival. Correct semantics: "the level on day d AFTER any day-d delivery."
- **At-risk block filters `deferred_unactionable`** including `'DO_NOT_SCHEDULE'`, `'EXCLUDED'`, `'NOT_IN_MATRIX'`, `'INSUFFICIENT_CONSUMPTION_DATA'`. Substring match (`r in (reason or '')`) is loose enough to handle minor reason wording drift. Good.
- **"NEEDS 2ND VISIT" status** at excel.py:732 is reachable only when `next_visit is None and prev_visit is not None`, which is the actual semantic. Correct.
- **Pin invariant + DNS exception** correctly skips pins where the client landed in `dns_deferred`. The substring match concern is in H1 but the behavior is right.
- **`SetCumulVarSoftUpperBound` for OT** is correctly applied at `routing.End(v)` (line 665), measuring total shift minutes. Correct.
- **Saturday rule** via `truck_available` dict (line 731-742) cleanly disables each vehicle's domain; no more $10⁹ hack. Clean.
- **Dispatch cost = 0** is guarded by `if dispatch_cost_units > 0:` so no degenerate `SetFixedCostOfVehicle(0, v)` calls. Fine.
- **Tiny-stop urgency check parity:** solver forbids tiny refills when `dte_today > 3.0` (line 856). Invariant accepts tiny stops when `urgency_tier in ('stockout', 'critical', 'urgent')` (invariants.py:178). The urgency tiers are defined in extract.py:318-325 as `urgent ≤ 3.0`. So `dte_today > 3.0` in solver ↔ `urgency_tier == 'normal'` in invariant ↔ rejection in both. **Parity is exact at the 3.0 boundary.** Good.
- **Drop penalty piecewise function** in §4 matches the RC-1 specification verbatim. The `if max_refill_per_client[i + 1] <= 0: penalty = 0` short-circuit prevents wasting disjunction slots on already-full tanks. Clean.
- **server.py `/api/last-plan`** correctly handles the JSON archive's nested `route` shape (lines 284-299) — the comment on lines 280-283 captures the prior bug history accurately.
- **Pin enforcement** (§5.13, line 901-917) correctly removes vehicles whose day_idx != pin_day_idx. The "pin date not in horizon → silently relax" branch (line 910) is defensible but worth logging — currently silent.

---

## What I couldn't verify without running code

- Whether the dashboard's `truck_unavailable.json` is actually written/read with the exact `(date, str)` key shape that `base.truck_available` uses. `availability_store.load_unavailability` wasn't in scope and the merge logic at lines 320-329 assumes returning a set/iterable of `(date, str)` tuples. If `load_unavailability` returns ISO strings instead of `date` objects, the `if (d, tid) in new_avail` check silently misses every entry. I can't tell which from the file contents alone.
- Whether the `prev_visit is None` test on line 704-714 in excel.py correctly handles the case where the at-risk client appears in `plan.deferred` (would have no entries in `plan.routes`). Looks correct on inspection but unverified.
- Whether `extract.py`'s `arrival = solution.Value(time_dim.CumulVar(next_idx))` (line 115) returns minutes-from-shift-start (as expected) when slack was inserted by the solver to wait for a time window. The OR-Tools docs say yes; the comment on lines 109-113 also says yes. I trust it but did not run.
- Whether `compute_drop_penalty` correctly tiers a pinned client into HARD×10 before max_refill check: pin path is `if c.id in pins_by_client: penalty_dollars = DROP_PENALTY_HARD * 10`, immediately followed by `if max_refill_per_client[i + 1] <= 0: penalty_dollars = 0.0`. **A pinned client whose tank is already full will have penalty=0.** That's probably correct (don't waste a slot on a full tank just because the operator pinned it last week and forgot to clear) — but it means a pin CAN be silently dropped if the tank is already full. Worth confirming intent with the operator.

---

**Final recommendation:** Fix **C1** and **C2** before push (both are ~5-line changes). The rest can land in a follow-up. The architecture, cost model, and constraint structure all look defensible and well-documented. The calendar-day fix is correctly propagated. The dashboard contract is clean.
