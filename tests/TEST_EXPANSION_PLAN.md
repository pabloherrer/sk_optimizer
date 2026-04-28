# S&K Optimizer — Deep Test Expansion Plan (2026-04-21)

## Context

We just fixed the **P0 TW union-envelope bug** (per-vehicle conditional
CumulVar reification). Regression coverage now passes 117/117. But there is a
large surface area still under-tested that gates further accuracy and
efficiency work.

This plan adds **9 new test files** and **one big benchmark** (walk-forward),
taking us from ~117 → ~190 tests. Goal: red-light the bugs we haven't hit yet
and make future model improvements observable through metrics, not vibes.

## Gap analysis

### Gaps in accuracy testing

| Layer | Current coverage | Missing |
|---|---|---|
| `inventory.project_level` | unit tests | edge cases (negative time, zero tank, fractional floor) |
| `inventory.days_until_stockout` | some | ties, floor=current, negative inputs |
| `forecast_consumption.estimate_consumption_rates` | **none** (pure integration via real_data) | per-client, IQR outlier math, fallback hierarchy |
| `state.update_state` | **none** | delivered-vs-unvisited math, multi-day decay, floor clamp |
| Day-indexed demand in solver | **none** — bug lives here | capacity-per-day, service-time-per-day |

### Gaps in efficiency testing

| Metric | Current | Missing |
|---|---|---|
| Cost-per-gallon | tracked in bench, not asserted | range check on standard scenarios |
| Miles-per-stop | tracked in bench, not asserted | sanity bounds |
| Truck utilization balance | **none** | solver should not load 1 truck & leave other idle |
| Budget-convergence | **none** | does solve quality improve with budget? (regret test) |
| Nearest-neighbor upper bound | **none** | solver must beat a greedy baseline on miles |

### Gaps in solver-correctness testing (from recent TW fix)

| Interaction | Tested | Missing |
|---|---|---|
| TW × closure | no | infeasible mixes, non-conflicting mixes |
| TW × capacity | no | when TW forces Day 4, is Day-4 capacity respected? |
| TW same-day overlap | partial | coalescing union within day |
| TW × narrow windows | no | 1-minute windows, start-of-shift, end-of-shift |
| TW × empty-windows (all days) | no | degenerate case |

### Gaps in multi-week / real-data testing

| Scenario | Current | Missing |
|---|---|---|
| Walk-forward replay with forecast drift | rolling_horizon bench exists, but forecasts perfectly (no noise) | noisy replay to measure forecast accuracy |
| Real-data differential: does the solver behave similarly on real vs synth of the same size? | no | distribution matching & solve-output parity |

## New test files

### 1. `test_day_aware_demand.py` — RED-BEFORE-GREEN (7 tests)

These tests FAIL on current code (proves the bug) and PASS once the solver
reads refills from `refills_by_day[day(v)]` instead of a single `refills` list.

- `test_tue_heavy_client_capacity_sizing_day_aware`  
- `test_sat_heavy_client_fills_truck_on_sat`  
- `test_service_time_reflects_day_of_visit`  
- `test_per_day_capacity_upper_bound_respected`  
- `test_low_day0_but_high_day4_still_schedulable`  
- `test_deferral_when_any_day_feasible_exists`  
- `test_day_aware_matches_forward_refill_upper_bound`

### 2. `test_tw_interactions.py` — TW × other features (11 tests)

- `test_tw_plus_closure_same_day_defers`  
- `test_tw_plus_closure_other_day_ok`  
- `test_tw_forces_specific_day`  
- `test_tw_narrow_1min_window_still_feasible`  
- `test_tw_window_covering_full_shift_is_noop`  
- `test_tw_start_of_shift_window`  
- `test_tw_end_of_shift_window`  
- `test_tw_same_day_two_rows_coalesced`  
- `test_tw_all_days_windowed_picks_cheapest`  
- `test_tw_multiple_clients_simultaneous_not_double_booked`  
- `test_tw_arrival_min_reported_accurately`

### 3. `test_forecasting.py` — Consumption & inventory forecast (14 tests)

- `test_project_level_negative_time_extrapolates_backward`  
- `test_project_level_zero_rate_constant`  
- `test_days_until_stockout_at_floor_is_zero`  
- `test_days_until_stockout_monotone_in_current`  
- `test_days_until_stockout_monotone_in_rate_inverse`  
- `test_compute_refill_monotone_in_day`  
- `test_fill_efficiency_bounded_01`  
- `test_service_time_min_linear_in_refill`  
- `test_enrich_snapshot_idempotent`  
- `test_enrich_snapshot_state_override_wins`  
- `test_rate_estimator_exact_known_history`  
- `test_rate_estimator_iqr_outlier_excluded`  
- `test_rate_estimator_zone_median_fallback`  
- `test_rate_estimator_global_median_fallback`

### 4. `test_inventory_state.py` — state.py round-trip (7 tests)

- `test_load_state_missing_file_empty_dict`  
- `test_save_load_roundtrip_exact`  
- `test_update_state_delivered_resets_to_full`  
- `test_update_state_unvisited_decrements`  
- `test_update_state_floor_clamp_5pct`  
- `test_update_state_multi_day_elapsed`  
- `test_initialise_state_from_snapshot_uses_est_current`

### 5. `test_optimality.py` — Solver quality bounds (7 tests)

- `test_beats_nearest_neighbor_on_miles`  
- `test_solution_improves_or_equal_with_longer_budget`  
- `test_truck_utilization_within_30pct_of_balanced`  
- `test_no_empty_trucks_when_pipeline_has_urgent`  
- `test_no_single_stop_routes_unless_far_cluster`  
- `test_distance_dominates_when_efficiency_weight_zero`  
- `test_weekly_distance_stable_across_seeds`

### 6. `test_efficiency_metrics.py` — Business KPIs (6 tests)

- `test_cost_per_gallon_under_threshold`  
- `test_miles_per_stop_reasonable`  
- `test_stops_per_driver_hour_reasonable`  
- `test_fill_pct_distribution_skewed_toward_full`  
- `test_avg_load_factor_above_50pct`  
- `test_deferred_fraction_below_30pct_on_normal_week`

### 7. `test_real_data_differential.py` — Real vs synth parity (5 tests)

- `test_real_data_loads_cleanly`  
- `test_real_data_solve_no_errors`  
- `test_real_data_no_constraint_violations`  
- `test_real_vs_synth_client_count_similar`  
- `test_real_vs_synth_solve_metrics_same_order_of_magnitude`

### 8. `test_rolling_horizon_unit.py` — State-advance math (5 tests)

- `test_advance_state_resets_delivered_to_full`  
- `test_advance_state_decays_unvisited_by_5_days`  
- `test_advance_state_floor_clamp`  
- `test_jaccard_identical_sets`  
- `test_jaccard_disjoint_sets`

### 9. `bench_walk_forward.py` — Noisy replay (1 big integration)

Simulates 12 weeks of rolling solving with:
- 10% noise injected into Avg_LbsPerDay estimates each week
- 5% of clients have a demand spike (3× their rate for 1 week)
- Forecast vs actual inventory gap tracked

Output: markdown report with:
- MAPE (mean absolute percent error) on inventory forecasts
- Deferral backlog over time
- Coverage curve (fraction of clients visited ≥ once by week N)

## Run strategy

All new fast tests land in `run_all.py`'s fast-tier. The walk-forward bench
is opt-in (`--with-walk-forward`). Expected total fast-tier runtime: 60-90s.

## Success criteria

- All new tests PASS on current code EXCEPT `test_day_aware_demand.py`
  which is allowed to FAIL (it's the red test for the next P0 fix).
- Walk-forward produces a report showing forecast MAPE < 25% on normal weeks.
- Master runner completes in < 2 minutes for fast tier.
