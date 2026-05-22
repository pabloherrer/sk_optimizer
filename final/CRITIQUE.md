# Stress-Test Critique — Rounds 1 & 2

Critic role: identify gaps, weak assertions, and possible hidden bugs in the
22-scenario suite. Verdict at end.

---

## 1. Coverage gaps (per RC item)

### RC-1 — Drop-penalty tier boundaries
**What's tested:** HARD (a01), LOW (a02), MED boundary (a03), HIGH-via-stockout (l02).
**Gaps:**
- The **HARD→HIGH boundary** at `days_supply == 2.0` is not probed. a01 has DTE=1.5
  (clearly HARD) and l02 has DTE=1.0. There's no scenario at DTE=2.0±epsilon, which
  is exactly where an off-by-one (`<` vs `<=`) would hide.
- The **HIGH→MED boundary** at `days_supply == horizon_days` is not probed cleanly.
  a03 sits at MED with `days_to_target == horizon`, but the HIGH side of that same
  boundary is missing — i.e., a client where `days_supply` equals `horizon_days`
  exactly (HIGH per the spec) is never tested.
- The fix description in RC-1 says `next_target_visit` uses **target_empty_fraction
  (30% of tank)** rather than zero. **No scenario varies `target_empty_fraction`**
  — every client uses the default. If the solver silently ignores per-client
  `target_empty_fraction`, no test would catch it.
- **MED-vs-HIGH ordering under contention**: no scenario forces the solver to
  choose between two MED clients and two HIGH clients with insufficient truck
  time. Tier priority is asserted only in absence of contention.

### RC-2 — Labor double-counting
**What's tested:** Indirectly via b01 (split saves OT) and b02 (consolidate when
no OT).
**Gaps:**
- **No direct OT-premium probe.** A scenario like "exactly target+1 min, single
  truck" would confirm the OT shoulder activates at the right threshold. Without
  this, b01 only confirms the *sign* of the cost differential, not the magnitude.
- **No baseline assertion** that `objective` for an all-regular-hours day does
  NOT scale linearly with route minutes. RC-2's bug was "salary charged to every
  minute" — the test for the fix is "objective for 280-min day should be roughly
  equal to objective for 100-min day, ignoring fuel." Nothing tests this.

### RC-3 — Truck dispatch cost = $0
**What's tested:** b01 (must split), b02 (may consolidate).
**Gaps:**
- b02 explicitly notes the solver is "indifferent" with dispatch=$0 — so the
  scenario passing doesn't prove the dispatch cost is zero, only that it's
  ≤ the routing cost differential. **A scenario with dispatch_cost knob > 0
  would re-confirm the inverse**, but isn't included.
- **No multi-day test of "both trucks every regular day."** The RC-3 expected
  impact was "splits even single-stop days" — no scenario verifies this on a
  multi-day horizon with small daily workloads.

### RC-4 — Capacity dimension (75p quantile)
**What's tested:** c01 (single-client cap), c02 (multi-client cap).
**Gaps:**
- The fix moved from `max_refill` to `75p quantile across horizon`. **No
  scenario constructs a refill distribution with high variance** (e.g., day 0
  refill=100 lbs, day 4 refill=2000 lbs — 75p ≈ 1525, max = 2000). If the code
  uses the wrong percentile or reverted to max, c01/c02 wouldn't catch it
  because their refills are roughly stationary.
- **Per-vehicle capacity** (option b in the fix) isn't probed — k01 is the
  only compartment test and it has only one compartment-relevant client.

### RC-5 — Consumption rate estimator
**Not tested at all.** The stress suite builds `ProblemInstance` objects with
rates supplied as scalars. The recency-weighted estimator in
`forecast/consumption.py` is bypassed entirely. **HAROLDS-style accelerating
clients (RC-5's smoking gun) cannot be reproduced by this suite** — it
tests the solver, not the forecaster. Real regression risk here is invisible.

### RC-6 — Small-stop fee as soft penalty
**What's tested:** h01.
**Gaps:**
- h01 has no `must_serve` / `must_defer` assertion on TOPOFF — the scenario
  *observes* but doesn't assert. The execution log shows TOPOFF *was* taken
  (700 lbs total, both clients served), which contradicts the hypothesis text
  ("Solver should NOT take it"). **This is a passive PASS** — the assertion
  is so loose that either outcome passes. Real test would assert one branch.
- No scenario verifies the **urgent override** ("Urgent clients (DTE ≤ 3): no
  penalty"). A DTE-2 client needing only 30 lbs should still be served if
  near a route. Not tested.

### RC-7 — Commit window enforcement
**What's tested:** d01 (single client), d02 (5 urgent clients).
**Gaps:**
- d02 passes with all 5 on day 0, but the truck-time was 108 min — far below
  target. **No scenario where commit-window load EXCEEDS one day's capacity.**
  The fix description says "must be visited within the commit_days window"
  — what happens with 10 urgent clients on a 1-truck, 1-day commit window?
  Does the invariant catch overflow, or does the solver silently drop one?
- No scenario varies `commit_days` to 0, 3, or >horizon.

### RC-8 — Saturday rule (Truck9 unavailable)
**What's tested:** e01, j02.
**Gaps:**
- e01's assertion explicitly admits "we can't directly assert no truck9 on Sat
  with current schema" — it falls back on `must_serve` only. The execution
  log shows all 4 stops landed on Friday, so the Saturday slot wasn't even
  exercised. **The test passed without ever needing Saturday.** To actually
  exercise the rule, force Saturday work via Friday-only-forbids or capacity
  overflow.
- **No scenario** has a client whose ONLY feasible day is Saturday AND a
  conflicting need for Truck9 capacity. j02 is the closest but uses a single
  500-lb stop — well within Truck2.

---

## 2. Weak assertions (specific scenarios)

| Scenario | Weakness |
|---|---|
| **a01** | `must_serve_by_day` correctly tests day 0, but no `must_defer` on SAFE. Plan shows MID and URGENT served — but if SAFE had been served (50-day supply, should be LOW), would the test have failed? Only via `min/max_total_stops`, which aren't set. **Add `must_defer: ['SAFE']`.** |
| **a02** | Correctly asserts `must_defer: ['VERY_SAFE']`. Strong. |
| **a03** | Only asserts `must_serve: ['A']`. No bound on stops. A solver that served A twice (impossible but...) would pass. **Add `max_total_stops: 1`.** |
| **b01** | `min/max_total_stops: 8` good, `must_use_trucks` good. But **no assertion on OT** — if the solver split but BOTH trucks went into OT (e.g., because of a routing bug), it would still pass. Add a `max_objective` upper bound or `max_route_minutes_per_truck`. |
| **b02** | `max_total_stops: 2` is fine but doesn't bind: solver couldn't do more than 2. The real assertion needed is `max_truck_days: 1` (no wasteful split). |
| **c01** | Asserts `min/max_total_stops: 1` and `no_overflow`. But no assertion that **delivery equals tank room (2000 lbs)**. Plan shows 2000 lbs — could a delivery of 500 lbs (under-serve) also pass? Yes. **Add `min_total_lbs: 2000`.** |
| **c02** | `max_total_lbs: 1100` (1000 + slack). But execution shows only 600 lbs delivered to 1 client — way below truck cap. The test passed by under-using the truck, not by hitting the cap. **The truck-cap constraint was never *binding*** in this scenario. To force binding, you need workload that's tight on capacity, not loose. |
| **d01** | `must_serve_by_day: {'2026-05-22': ['SOON']}` is correct. But no constraint on LATER's day. Solver could schedule LATER on day 0 ALSO (which it did), failing to exercise the "LATER not locked" half of the hypothesis. |
| **d02** | Solid. |
| **e01** | Already flagged — assertion punts ("manual check"). The actual test (no Sat-Truck9 row) is not enforced. |
| **f01** | Good. |
| **f02** | `must_serve_by_day: {'2026-05-23': ['URGENT_FORBID']}` — good. |
| **g01** | `must_defer: ['DNS_CLIENT']` good. |
| **h01** | Already flagged — informational only. |
| **i01** | `must_defer: ['NORATE']` good, but **doesn't assert defer reason** is `INSUFFICIENT_CONSUMPTION_DATA`. Could be silently misclassified. |
| **j01** | Designed to record outcome — but assertion is empty (`{}` of meaningful checks). Crash was the observed outcome; that's not necessarily "correct" — should the invariant catch this BEFORE solving and degrade gracefully (drop the pin or drop the client)? **The crash IS a bug** masked as expected behavior. |
| **j02** | Strong. |
| **k01** | Hypothesis says "DEFERRED or partial". Plan shows full defer of CAN_BIG. But the objective was $10000.52 — that's HARD-tier penalty fired. **Is full deferral the right answer or should it be partial across days?** The test doesn't pin this down. A bug where the compartment constraint *always* defers (even when 2-day partial fill would work) would pass. |
| **l01** | Good. |
| **l02** | `must_serve_by_day: {'2026-05-22': ['DRY']}` is strong, but the hypothesis text says "day 0 or 1" — assertion is stricter than the hypothesis. If the solver legitimately serves on day 1 (still HIGH-tier OK), the test fails. **Spec disagreement between hypothesis and assertion.** |
| **m01** | `must_serve: <urgent_ids>` and `min_total_stops: 9`. Plan delivered all 50, so the assertion was loose. **No assertion on plan structure** (e.g., urgent clients on early days). |
| **n01** | Only asserts NOW_URGENT served on day 0. Plan shows FUTURE *also* served day 1 with only 200 lbs. **Should a 2000-lb tank with 200 lbs free even be served?** That's a refill of 200 lbs — possibly below `min_stop_lbs`, possibly an opportunistic top-off, possibly a bug. Test doesn't say. |

---

## 3. PASS that could hide bugs

1. **scenario_c02** — Truck cap "enforced" but never binding. Solver could be
   ignoring truck cap entirely; the per-stop time/route constraints alone
   would still cap stops. **Hidden bug risk: HIGH.**

2. **scenario_e01** — Saturday rule "tested" but Saturday wasn't used. If
   Truck9 was happily running Saturdays, this scenario would still pass
   because the entire 4-client load fit on Friday. **Hidden bug risk: MEDIUM.**

3. **scenario_h01** — Hypothesis says "shouldn't take TOPOFF (fee > detour)";
   plan took it. Either the hypothesis is wrong or the small-stop fee isn't
   firing. Test passed because assertion was vacuous. **Hidden bug risk: MEDIUM.**

4. **scenario_k01** — CAN_BIG fully deferred with $10K HARD penalty. If
   the correct behavior is "partial fill 5000 lbs day 0, top up day 1", the
   solver is over-conservative. Test can't distinguish. **Hidden bug risk: MEDIUM.**

5. **scenario_b01** — Split happened, but no assertion that **load is
   balanced** or that **neither truck went into OT**. Truck9 did 137 min,
   Truck2 did 80 min — that's a 57-min imbalance with no explanation.
   Possibly an artifact of greedy assignment. **Hidden bug risk: LOW.**

6. **scenario_a01** — URGENT and MID both served day 0, total 1300 lbs.
   But MID is HIGH-tier (4d supply on 3d horizon) — should it have been
   served? Hypothesis said "MID served somewhere" — vague. The test passes
   regardless of MID placement. **Hidden bug risk: LOW.**

7. **scenario_d01** — LATER served on day 0 along with SOON, even though
   hypothesis says "LATER not locked." Solver chose to bundle them. Is this
   because the marginal cost of adding LATER is < its day-1 cost? Could be
   correct, but the scenario doesn't verify. **Hidden bug risk: LOW.**

8. **scenario_n01** — FUTURE served day 1 with 200 lbs to a 2000-lb tank.
   This looks like a small refill that may or may not be intended. The "no
   peeking into future" hypothesis is vacuous since FUTURE was served anyway.
   **Hidden bug risk: MEDIUM.**

---

## 4. Round-3 scenarios I'd add

### R3-01: Commit-window capacity overflow
**Hypothesis:** When commit-window has more locked clients than one truck-day
can serve, what happens? Should split across both trucks in commit window
(not push to day 2).

**Setup:** 12 urgent clients (DTE=1.0), 2 trucks, commit_days=1. Each stop
~25 min → 300 min total. One truck can't do it; both trucks can.

**Expected:** All 12 on day 0, split across Truck2 and Truck9.
Assertion: `must_serve_by_day: {day0: <all 12>}`, `must_use_trucks: ['Truck2','Truck9']`.

### R3-02: HARD-tier boundary at DTE=2.0
**Hypothesis:** A client with exactly `days_supply=2.0` should be HARD (per
the spec `<2` is HARD). Test the off-by-one.

**Setup:** Client tank=1000, current=400, rate=200 → DTE=2.0 exactly. Compare
to second client with DTE=1.99. Both should be HARD-tier.

**Expected:** Both on day 0. Assertion: `must_serve_by_day: {day0: ['A','B']}`.

### R3-03: Variable refill distribution (75p capacity probe)
**Hypothesis:** A client whose refill on day 0 is 100 lbs but day 4 is
1800 lbs should be capacity-budgeted at the 75p, not max.

**Setup:** Single truck cap 2000. Client A with current=900, rate=200,
tank=2000 (day 0 refill = 1100). Plus 3 other small clients (refill 300
each on day 0). Total day-0 demand = 1100 + 900 = 2000. If capacity
callback uses max-across-horizon for A (say 1800 on day 4), it would
forbid combining → only 1 small client fits. If it uses 75p (~1100), all fit.

**Expected:** All 4 on day 0. Assertion: `min_total_stops: 4` on day 0.

### R3-04: DNS + pin conflict
**Hypothesis:** Operator pins a DNS client. Which wins? (Per business
rule DNS should win, but invariant might just crash like j01.)

**Setup:** 1 client, do_not_schedule=True, also pinned to day 0.

**Expected:** Either graceful deferral with `OVERRIDE_IGNORED_DNS` reason
OR an invariant error explaining the conflict. NOT a crash that looks
identical to j01's crash.

### R3-05: Saturday-forced service (e01 done right)
**Hypothesis:** When 6 urgent clients can't fit on Friday and 2 must spill
to Saturday, Truck9 must NOT take any of them.

**Setup:** 6 clients, each ~80-min service, total 480 min > Friday target
of 304 min. Single truck horizon Fri+Sat. Truck9 available Fri only.

**Expected:** Friday: ~4 clients on Truck2 + Truck9 split. Saturday: 2
clients on Truck2 ONLY. Custom check: `no_truck9_on_saturday`.

---

## 5. Designer rationalization risk

Round 2 was designed *after* the round-1 fixes landed. The Designer should
have written tests that **specifically attack the fixes**. Instead:

- **Round 1 found 4 bugs**, fixed. **Round 2 contains zero scenarios that
  directly verify those 4 fixes.** Round 2 is almost entirely new attack
  surface: pin×forbid (j01-2), multi-product (k01), already-empty (l01-2),
  scale (m01), future-leakage (n01).
- **The closest thing to a fix-verification** is l01/l02 (HARD-tier on
  already-empty tanks), which is really just a stricter version of a01.
  Even there, the assertion is weaker than a01's (no `must_defer` on
  comparison clients).
- **j01 is the only scenario that explicitly accepts "crash OR drop"** as
  PASS. After it crashed, the EXECUTION_LOG marks it FAIL — but the
  expected dict literally said "Solver either crashes... OR resolves to
  drop." This is a contract that the Designer set up to never lose.
  Either branch was OK. The FAIL is mostly a runner convention.
- **m01 (50 clients) shows 30,000 lbs delivered, 0 deferred** — that
  number is suspiciously round and the test passed without inspecting
  *which days* urgent clients landed on. A bug that delays all urgent
  clients to day 4 would still pass `must_serve` + `min_total_stops`.
- **The Designer never wrote a scenario where `target_empty_fraction`
  varies per client** — even though RC-1's fix explicitly uses it.

**Most rationalized assertion:** `scenario_e01`'s comment "We can't
directly assert no truck9 on Sat with current schema" — this is the
Designer admitting the suite *cannot* check the very thing the scenario
claims to test. That's not a schema limitation; it's a missing
custom-check feature that should be implemented before claiming RC-8 is
covered.

---

## 6. Verdict

**Not enough for production.** Two rounds with 22 scenarios provide
reassuring breadth but thin depth on the constraints that matter most:

- RC-5 (consumption rate forecaster) is **completely untested** by the
  stress suite — and it's responsible for the original HAROLDS stockout.
- RC-8 (Saturday rule) and RC-6 (small-stop fee) have **assertion-vacuous
  tests** that PASS regardless of behavior.
- The **four round-1 fixes never got dedicated regression tests** in
  round 2 — round 2 went looking for new bugs instead of nailing down
  the old ones.
- Multiple PASS scenarios have hidden-bug risk (c02, e01, h01, k01).

Recommend **a round 3 minimum** covering: (a) the 5 scenarios above, (b)
explicit regression tests for each round-1 fix, and (c) a custom
assertion API for "no truck X on day Y" so RC-8 can be properly tested.
Additionally, the forecaster (RC-5) needs its own unit-test layer — the
stress suite cannot substitute for that.

Until then: deploy to a non-critical fleet or shadow-run against Tammy's
manual plan for ≥2 weeks before cutover.
