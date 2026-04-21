# S&K Route Optimizer — Comprehensive Test Plan

**Last revised:** 2026-04-20
**Goal:** catch regressions, verify paper-driven additions behave as designed, and
produce repeatable A/B evidence so every subsequent change can be measured.

## Test pyramid

```
                /    Real-data integration    \     3 tests   — minutes
               / ──────────────────────────── \
              /     Scenario / invariant       \   ~20 tests  — seconds
             / ──────────────────────────────── \
            /       Feature (paper-gated)        \ ~25 tests  — seconds
           / ──────────────────────────────────── \
          /            Unit math / helpers         \~60 tests  — milliseconds
```

Fast tests run on every commit. Real-data integration runs on demand or nightly.

## Suites

### 1. Pure unit tests (`test_unit_inventory.py`, `test_unit_helpers.py`)

No solver, no DataFrames of consequence. Fast, deterministic, isolate the math.

**Inventory math (`inventory.py`)**
- `project_level`: current + future + clamp to [floor, tank]; zero-rate case; negative rate rejected; large days_forward floor clamp.
- `compute_refill`: fills-to-tank invariant; never negative; zero-rate client.
- `days_until_stockout`: zero current_lbs → 0; zero rate → 0; at-floor client → 0.
- `urgency_tier`: boundaries at 0, CRITICAL_DAYS (1.5), URGENT_DAYS (4.0).
- `fill_efficiency`: zero tank → 0; matches refill / tank.
- `service_time_min`: Truck2 vs Truck9 speed difference; scales linearly.
- `enrich_snapshot`: adds all columns; respects `inventory_state` overrides; bounded clip.
- `build_refill_matrix`: shape n×n_days, non-negative.

**Helpers (`unified_solver.py`)**
- `_haversine_mi`: same point = 0; known-point pair matches published distance; symmetric.
- `_compute_geo_clusters_single`: depot → METRO_*; far client → FAR_{city}; missing city → spatial bucket.
- `_compute_geo_clusters`: corridor detection reassigns on-the-way metro; centroids computed correctly.
- `vehicle_to_truck_day_config` ⇄ `truck_day_config_to_vehicle` round-trip bijection.
- `truck_day_to_vehicles`: returns 3 vehicles per (truck, day).
- `_assign_compartments_by_config`: SPLIT/A_ONLY/B_ONLY totals match `product_lbs`; overflow split across the two compartments.

### 2. Solver invariants (`test_solver_invariants.py`)

Any legal solution *must* satisfy these. We generate small synthetic scenarios and
check the invariants on the output, regardless of what the objective chose.

- **Capacity (total):** for every vehicle, sum of `Refill_lbs` ≤ truck's `capacity_lbs`.
- **Capacity (per-product):** for every vehicle × product, product-lbs ≤ config cap.
- **Shift time (soft):** `Route_Time_min` may exceed `SHIFT_MIN`; must stay ≤ `MAX_SHIFT_MIN` (hard driver-hours cap). Minutes beyond `SHIFT_MIN` are reported as `OT_min` and costed at 1.5× base labor.
- **Single visit:** each `ID` appears at most once across all days.
- **Depot delimiters:** route starts and ends at depot (stops don't include node 0).
- **At-most-one-config-per-truck-day:** a truck never runs SPLIT + A_ONLY on the same day.
- **Compartment totals:** `Comp_A_lbs + Comp_B_lbs == sum(Refill_lbs)` per route.
- **Deferred accounting:** every client in `clients_df` is in exactly one of {scheduled, deferred}.

### 3. Feature tests — one per paper-driven change

**14-day contract (Aksen 2012) — `test_feature_contract.py`**
- Client with `Days_Since_Last=13`, tank 90% full → forced into the plan.
- Client with `Days_Since_Last=3`, tank 80% full → solver may skip if demand dense.
- Boundary: `Days_Since_Last + NUM_DAYS == MAX_SERVICE_INTERVAL_DAYS` → escalation fires.
- Under very light demand, the contract-escalated client is still served.

**Time windows (Cornillier 2009) — `test_feature_windows.py`**
- Client with Tue-only 9–11 window arrives within [540, 660] abs min (→ [180, 300] rel).
- Client with Tue-only window NEVER assigned to Wed/Thu/Fri/Sat vehicles.
- Cross-day union: client with Tue 9–11 and Thu 13–15 → union envelope on CumulVar + only Tue/Thu vehicles eligible.
- `ENFORCE_TIME_WINDOWS=False` → time-window DataFrame is ignored (regression gate).
- Infeasible window (Open=50, Close=60 when arrival needs >100 min) → client deferred.

**Fill-efficiency weight (Cornillier/Archetti) — `test_feature_efficiency.py`**
- Two equidistant candidates with identical urgency, one at 90% fill and one at 10% fill; with `EFFICIENCY_WEIGHT=1.5` the high-fill one is preferred when only one slot exists.
- `EFFICIENCY_WEIGHT=0.0` → legacy parity: selection matches pre-weighting behavior.
- Penalty monotonicity: higher best-fill → higher computed drop-penalty.

**Forward-projected refills (Coelho 2014) — `test_feature_forward_refills.py`**
- Single client, large consumption rate: Day-4 refill > Day-0 refill; solver's load plan matches Day-4.
- Fleet at capacity on Day 0 snapshot but infeasible when planned for Day 4 → solver defers client.
- `refills_by_day` array consistent with `inventory.compute_refill` for each day.

**Overtime labor model — `test_feature_overtime.py`**
- Short route (total 400 min) → `OT_min = 0`, labor cost = 400 × base.
- Long route (total 660 min, SHIFT_MIN=600) → `OT_min = 60`, labor cost = 600 × base + 60 × 1.5 × base.
- At-threshold route (total 600 min) → boundary: `OT_min = 0`.
- Hard ceiling: route exceeding `MAX_SHIFT_MIN` (e.g., 720) is rejected / client deferred.
- Objective tradeoff: scenario where one extra stop adds 30 OT-min but gains revenue > OT cost → solver takes it; when OT cost > marginal revenue → solver drops stop.

### 4. Determinism (`test_determinism.py`)

- Solver called twice with identical inputs and identical `solve_seconds` yields identical scheduled-set (order-independent).
- Input `clients_df` not mutated by `solve_week` (checksum before/after).
- `time_windows_df` not mutated.

### 5. Scale / performance (`test_scale.py`)

- 50-client synthetic dataset solves in ≤ 30 s and returns non-empty routes.
- 150-client synthetic dataset solves in ≤ 60 s.
- Solver status is `ROUTING_SUCCESS` (ortools code 1) or `ROUTING_PARTIAL_SUCCESS` (3), never `ROUTING_FAIL` (4).

### 6. A/B benchmark (`bench_ab.py`)

Not a pass/fail test — a report generator. Runs the solver on each configured
dataset with every combination of the paper flags and prints a comparison table:

| Scenario | Config | Stops | Miles | Lbs | Lbs/Mile | Deferred-Critical |
|---|---|---|---|---|---|---|

**Flag axes (each ON/OFF unless otherwise):**
- `MAX_SERVICE_INTERVAL_DAYS`: {365 (disabled), 14}
- `ENFORCE_TIME_WINDOWS`: {False, True}
- `EFFICIENCY_WEIGHT`: {0.0, 1.5}
- `USE_FORWARD_REFILLS`: {False (snapshot), True (end-of-week)} — new flag to add
- `OT_MULTIPLIER`: {1.0 (no OT cost — legacy), 1.5 (new)}

Full cross = 32 runs per dataset. Runs on `SK_Delivery_System.xlsx` and, if sheets
can be normalized, on `SK_Fictional_Demo.xlsx`. Results written to
`output/ab_bench_<timestamp>.md`.

Benchmark reports additional columns for overtime: `OT_min_total`, `OT_cost_$`,
`OT_routes_count` (how many of the 10 truck-days crossed the 600-min line).

### 7. Real-data integration (`test_real_data.py`)

Uses actual `SK_Delivery_System.xlsx`. Slow (~30 s per run), so gated behind an
`--integration` flag so regular runs stay fast.

- End-to-end load → solve with 20s limit completes without crash.
- At least 1 stop scheduled, at least 1 route produced.
- All invariants (suite 2) hold on the real output.
- Contractual-cadence escalation count is 0 ≤ N ≤ `len(clients_df)`.

### 8. Master runner (`run_all.py`)

Orchestrates suites 1–5, times each, prints pass/fail with elapsed. Writes
`output/test_report.md` so CI can attach it to a build artifact.

## Coverage targets

| Module | Line coverage target | Priority |
|---|---|---|
| `inventory.py` | 95% | critical |
| `unified_solver.py` (helpers) | 90% | critical |
| `unified_solver.solve_week` | 75% (many code paths are CP-solver-internal) | high |
| `schema_loaders.py` | 85% | high |
| `validator.py` | 90% | medium |
| `forecast_consumption.py` | 80% | medium |
| `output.py` | 60% | low |

## What we're deliberately *not* testing

- OR-Tools internals (assumed correct).
- OSRM network calls (stubbed in synthetic fixtures).
- Flask UI (out of scope here).
- Map HTML rendering (visual-only).

## Philosophy

Each paper-driven feature has its own `test_feature_*.py` so adding or removing a
paper is a local edit: same file for the feature code, same file for its tests,
and no cross-contamination. If a future audit wants to know "is the Aksen 2012
behavior still in?" the answer is "run `test_feature_contract.py`."

The A/B benchmark is separate from pass/fail tests because "fewer miles" is a
judgment call the user makes from the numbers, not a hard assertion the test
suite should fail on.
