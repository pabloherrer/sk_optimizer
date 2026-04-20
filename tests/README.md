# S&K Optimizer Test Suite

Comprehensive test coverage for the route optimization validator and solver.

## Quick Start

Run all tests:
```bash
cd /path/to/sk_optimizer
python3 tests/run_tests.py
# or
python3 -m tests.test_scenarios
```

## Test Scenarios (15 total)

### Validator Tests

#### Test 15: Validator Happy Path
- **What it tests:** Pre-solve validator accepts clean inputs.
- **Setup:** Single client with GPS, tank, and delivery history.
- **Expected:** `ValidationReport.ok = True`, no errors.
- **Pass criteria:** Validator reports no errors.

---

### Solver Tests

#### Test 01: Trivial One-Stop
- **What it tests:** Single client, single truck, baseline scenario.
- **Setup:** 1 client needing 500 lbs CANOLA.
- **Expected:** 1 route with 1 stop, 500 lbs delivered.
- **Pass criteria:** Route contains C001, >=1 stop in output.

#### Test 02: Capacity Cap
- **What it tests:** Truck capacity limits scheduling.
- **Setup:** 3 clients × 5000 lbs each, 1 truck with 10k capacity.
- **Expected:** 2 clients scheduled (10k max), 1 deferred with reason `NO_CAPACITY`.
- **Pass criteria:** >=2 scheduled, >=1 deferred.

#### Test 03: Product Split
- **What it tests:** Multiple products use separate compartments (SPLIT config).
- **Setup:** 2 clients: one CANOLA, one FRYERS CHOICE.
- **Expected:** Both scheduled on same truck-day with different compartments.
- **Pass criteria:** Both C001 and C002 in routes.

#### Test 04: Same-Product Double
- **What it tests:** Multiple clients using same product on same route.
- **Setup:** 2 clients × 5000 lbs CANOLA, 1 truck 10k cap.
- **Expected:** Both scheduled on same truck-day (A_ONLY config).
- **Pass criteria:** Both C001 and C002 scheduled.

#### Test 05: Urgency Wins Slot
- **What it tests:** Critical clients (1 day to stockout) take priority over normal.
- **Setup:** Critical client (5000 lbs/day consumption) vs. normal (500 lbs/day).
- **Expected:** Critical scheduled, normal deferred (capacity-limited to 1 slot per truck-day).
- **Pass criteria:** C_CRITICAL in scheduled routes.

#### Test 06: Hard Time Window Honored
- **What it tests:** Clients with time windows are scheduled within the window or deferred.
- **Setup:** 1 client with Tue 09:00-11:00 window.
- **Expected:** Arrives within window or deferred (no infeasible scheduling).
- **Pass criteria:** No crash; client either scheduled in-window or deferred.

#### Test 07: Closure Blocks Tuesday
- **What it tests:** Closures prevent delivery on specific days.
- **Setup:** 1 client closed Tue only (Tue-Sat work week).
- **Expected:** Scheduled Wed-Sat, not Tue.
- **Pass criteria:** Not deferred with CLOSED_ALL_WEEK reason.

#### Test 08: All-Week Closure Plus Urgent
- **What it tests:** Even urgent clients cannot be served if closed all week.
- **Setup:** 1 urgent client (5000 lbs/day) closed Tue-Sat.
- **Expected:** Deferred with reason `CLOSED_ALL_WEEK`.
- **Pass criteria:** Deferred row exists with Reason='CLOSED_ALL_WEEK'.

#### Test 09: Missing GPS
- **What it tests:** Clients without coordinates are deferred before solve.
- **Setup:** 1 client with Lat=None.
- **Expected:** Deferred with reason `NO_GPS`.
- **Pass criteria:** Deferred row exists with Reason='NO_GPS'.

#### Test 10: No Consumption History
- **What it tests:** Clients with no delivery history are either deferred or scheduled with fallback rate.
- **Setup:** 1 client with Avg_LbsPerDay=0.
- **Expected:** Deferred with NO_CONSUMPTION_DATA or scheduled using fallback.
- **Pass criteria:** No crash; handled gracefully.

#### Test 11: Shift Overflow
- **What it tests:** Large client base with limited capacity causes graceful deferrals.
- **Setup:** 20 clients × 10000 lbs each, 2 trucks × 5 days = 100k weekly capacity.
- **Expected:** Some scheduled, many deferred (200k demand > 100k capacity).
- **Pass criteria:** Solver completes without crash; deferred list populated.

#### Test 12: Depot Invariant
- **What it tests:** Every route starts and ends at depot (not in middle).
- **Setup:** 2 clients on 1 truck, 1 day.
- **Expected:** Routes have depot as first and last stop (logical, checked in output).
- **Pass criteria:** Route structure consistent.

#### Test 13: No Double-Visit
- **What it tests:** Each client is visited at most once per week.
- **Setup:** 30 clients across 5 days, 2 trucks (enough capacity for one visit each).
- **Expected:** No client_id appears twice in combined routes across all days.
- **Pass criteria:** All unique client_ids have visit count = 1.

#### Test 14: Compartment Math
- **What it tests:** Compartment loads sum to total refill (CompA_lbs + CompB_lbs = Refill_lbs sum).
- **Setup:** Mixed products on one truck.
- **Expected:** Manifest totals match refill quantities within rounding.
- **Pass criteria:** |CompA + CompB - SumRefill| < 1 lbs per route.

---

## Running Individual Tests

You can import and run a specific test:

```python
from tests.test_scenarios import test_01_trivial_one_stop

test_01_trivial_one_stop()
print("Test passed!")
```

## Interpreting Test Failures

### Common Failure Messages

**"Expected >=1 route stops, got 0"**
- Solver deferred all clients instead of scheduling.
- Check: Are clients marked as deferred? Run solver with `--today` override to check date.

**"Expected CLOSED_ALL_WEEK, got NO_CAPACITY"**
- Closure check ran but capacity was hit first (order of checks in solver).
- This is a potential bug — closures should be checked before capacity.

**"Duplicate client visits: C001"**
- Client appeared on multiple routes in a single week.
- Indicates multi-visit bug in solver (should visit each client at most once).

**"Compartment math failed: 5000 + 5000 != 9999"**
- Floating-point rounding or missing refill value.
- Check columns Comp_A_lbs, Comp_B_lbs, Refill_lbs in output routes.

## Test Data Design

All tests use a **synthetic distance matrix** where:
- `dist(i, j) = abs(i - j) * 1000 meters`
- `time(i, j) = abs(i - j) * 2 minutes`
- Nodes: 0 = depot, 1..N = clients (in ID order)

This is **deterministic and fast**: you can hand-compute expected drive times and verify the solver's routing.

## Adding a New Test

1. Create a function `test_NN_<name>()` in `test_scenarios.py`.
2. Use `_make_scenario()` to build inputs:
   ```python
   def test_16_new_scenario():
       scenario = _make_scenario(
           clients=[...],
           deliveries=[...],
           ...
       )
       routes, deferred = solve_week(...)
       assert <condition>, f"Failure message: {actual}"
   ```
3. Assertions must use plain `.assert()` with a descriptive message.
4. Add the test tuple to `tests` list in `run_all_tests()`.
5. Run `python tests/run_tests.py` to verify it appears and passes.

## Dependencies

- `pandas` — DataFrames
- `numpy` — Matrix operations
- `ortools` — Route optimization
- No external test framework required (uses plain `assert`).

## CI Integration

The test suite is designed for CI pipelines:

- **Exit code 0** if all tests pass.
- **Exit code 1** if any test fails.
- Output is plain text (parseable by log scrapers).
- Total runtime: <60 seconds (usually <30s).

Example CI usage:
```bash
python tests/run_tests.py || echo "Tests failed"
```

## Troubleshooting

**Tests hang or timeout**
- Set `solve_seconds=5` in `solve_week()` call (default may be too high).
- Check for infinite loops in matrix-building or route reconstruction.

**Import errors**
- Ensure Python 3.10+ is available: `python3 --version`.
- Check that `config.py`, `unified_solver.py`, etc. are importable from test file.

**Flaky tests**
- Tests use fixed scenarios (no random seed needed).
- If randomness is added to solver, seed it for reproducibility.

---

**Last updated:** 2026-04-15  
**Test count:** 15  
**Avg runtime:** ~2-3 seconds
