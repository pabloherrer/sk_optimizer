# ADR-003: Consolidate to a Single Entry Point and Remove Dead Code

**Status:** Accepted — Stages 1-4 executed 2026-05-07
**Date:** 2026-05-07
**Deciders:** Pablo (owner)

---

## Context

The codebase appears to have "two solvers" (`run_irp.py` and `run_unified.py` / `run_daily.py`) but a forensic audit shows this is misleading.

**Actual finding**: There is exactly ONE OR-Tools solver — `unified_solver.solve_horizon` — and three entry-point scripts that all call it. The duplication is in pre-processing (state loading, demand model, Anova ingest, urgency tiers) and post-processing (output, persistence), not in the optimization core itself.

The audit also identified ~6,500 lines of dead code (entire files never imported), plus 18 one-shot Excel-recovery scripts dated May 6 2026 that were never archived.

### Why this matters now

Every layer of duplicated logic is a place where the system can drift out of sync. We are already seeing the cost:
- The Anova ingest is implemented three different ways across `run_irp.py`, `run_unified.py`, and `run_daily.py`
- The `state.py` (v1 flat) and `irp_core/state_manager.py` (v2 atomic) co-exist; legacy entry points still write the v1 format
- IRP-calibrated drop penalties are computed but never passed into the solver — the layer is half-built
- 90% of `router.py` (531 lines) is kept alive for one 30-line `load_matrix` helper

The "Generate Routes" button in `app.py` already invokes only `run_irp.py`. The other entry points are CLI fallbacks nobody uses.

---

## Decision

**Consolidate to a single entry point (`run_irp.py`), delete all confirmed dead modules, and finish the half-built IRP plumbing.**

The architectural truth is:

```
       Excel + Anova Query sheet + delivery log
                       │
                       ▼
              [pre-processor pipeline]
                       │
                       ▼
       unified_solver.solve_horizon  ← the ONLY solver
                       │
                       ▼
              [post-processor pipeline]
                       │
                       ▼
       Excel schedule + map + SmartService CSV + state.json + plan.json
```

Everything else — `run_unified.py`, `run_daily.py`, `unified_solver_new.py`, `main.py`, `rolling_optimizer.py`, `scheduler.py`, `territory.py` — is either an alternate front door or remnants of an earlier two-phase architecture that the unified solver superseded.

---

## Options Considered

### Option A: Status quo (do nothing)

| Dimension | Assessment |
|-----------|------------|
| Complexity | High — three entry points, dual state formats, three Anova ingest paths |
| Cost | Ongoing — every change must be made in 2-3 places |
| Risk | High — drift between paths is inevitable |
| Team familiarity | Low — newcomers can't tell which path is canonical |

**Pros:** No change risk; existing CLI fallbacks remain.
**Cons:** Half-built IRP plumbing, dead code accumulates, real bugs (the Anova ingest divergence we saw today).

### Option B: Single entry point — `run_irp.py` (CHOSEN)

| Dimension | Assessment |
|-----------|------------|
| Complexity | Lower — one path, one state format, one Anova ingest |
| Cost | One-time refactor, then sustained savings |
| Risk | Medium — must verify nothing was relying on the legacy paths |
| Team familiarity | High — the canonical path is obvious |

**Pros:**
- Removes 6,500+ lines of dead code
- Eliminates triple-implementation of Anova ingest
- Forces the IRP layer to be either fully wired or removed
- Matches what production already uses

**Cons:**
- CLI users (if any) lose `run_unified.py` — must re-learn `run_irp.py` flags
- Some risk that hidden dependencies exist on the dead modules

### Option C: New unified entry point — replace both with a clean rewrite

| Dimension | Assessment |
|-----------|------------|
| Complexity | Highest short-term, lowest long-term |
| Cost | Weeks |
| Risk | High — production would freeze during the rewrite |

**Pros:** Cleanest possible result.
**Cons:** Out of proportion to the actual problem. `run_irp.py` is already pretty clean; the dead code is the real issue.

---

## Trade-off Analysis

The choice is really between **Option A (do nothing)** and **Option B (consolidate)**. Option C is over-engineering.

The audit demonstrated the cost of Option A is real: today's bugs (stale Anova readings rejected, Oregano Pima scheduled 4 days late, cross-metro zigzags) trace partly to the duplicated/half-built logic. Option B doesn't fix routing logic directly, but it removes the conditions that let those bugs hide.

**Key risk in Option B**: are there CLI users running `python run_unified.py` outside the web UI? The header in `app.py:410` calls it "the legacy run_unified.py still exists as a CLI fallback — call it directly from the terminal if you ever need it." So someone, at some point, anticipated CLI use. Mitigation: keep the file in `archive/` (not deleted from history) for one release cycle.

---

## Consequences

### What becomes easier
- One place to fix bugs (Anova ingest, state handling, urgency tiers)
- One state format (atomic v2) — no more "which version of state.json is on disk?"
- The IRP layer's value becomes visible: state persistence, P95 urgency, warm-start, plan stability — all preserved
- Newcomers / agents have one canonical path to read

### What becomes harder
- CLI users (probably none) lose `run_unified.py` and `run_daily.py`
- Backtest harness (`backtest_irp.py`) must be verified to still work after dead-code removal

### What we'll need to revisit
- The IRP `objective.py` / `economics.py` plumbing: either finish wiring `drop_penalties` into the solver, or delete the unused calculation. ADR-004 should make this call.
- The `unified_solver.py` god object (2,032 LoC) — splitting geo-clustering into its own module is a separate decision (ADR-005).

---

## Action Items (executed in stages)

### Stage 1: Delete confirmed dead code (zero-risk — these files are not imported by anything)
- [x] `unified_solver_new.py` (1,563 LoC) — earlier 10-vehicle prototype
- [x] `main.py`, `rolling_optimizer.py`, `scheduler.py` — abandoned two-phase architecture
- [x] `territory.py` (4,236 LoC) — never imported
- [x] `diagnostics.py` (top-level, 206 LoC) — only imported by dead `main.py`
- [x] One-shot Excel-recovery scripts (18 files) → `archive/oneshot_2026-05-06/`

### Stage 2: Retire legacy entry points (DONE)
- [x] Move `run_unified.py` and `run_daily.py` to `archive/legacy_entrypoints/`
- [x] `app.py` already only invokes `run_irp.py` (verified at line 423-431)

### Stage 3: Consolidate Anova ingest (DONE)
- [x] Added `load_readings_from_query_sheet` and `apply_query_sheet_to_state` to `irp_core/anova_integration.py` — single function with stale-reading projection
- [x] `run_irp.py` now calls only `apply_query_sheet_to_state` (one line replaces 80 lines of inline logic)
- [x] Inline Anova blocks removed (legacy entry points archived)

### Stage 4: Move `router.load_matrix` and shrink `router.py` (DONE)
- [x] Created `matrix_loader.py` with the 28-line `load_matrix` helper
- [x] Updated imports in `run_irp.py` and `backtest_irp.py`
- [x] Archived `router.py` (503 lines of dead Phase-2 code)

### Stage 5: Address half-built IRP layer (separate ADR)
- [ ] Decide: finish wiring `drop_penalties` into solver, or remove the calibration. Track in ADR-004.

### Stage 6: God-file cleanup (separate ADRs)
- [ ] Extract HTML template from `app.py` into `templates/` (ADR-005)
- [ ] Extract geo-clustering from `unified_solver.py` into `geo_cluster.py` (ADR-006)

---

## Notes on the IRP layer's value

The audit confirmed the IRP layer is not "extra solver" — it is genuinely useful pre/post-processing:

| Capability | Provided by | Status |
|---|---|---|
| Atomic v2 state + delivery log | `irp_core/state_manager.py` | Active, valuable |
| Empirical-Bayes DOW demand model | `irp_core/forecasting.py` | Active, used for σ̂ |
| P95 chance-constrained urgency | `irp_core/safety_stock.py` | Active, valuable |
| Warm-start from prior plan | `irp_core/warm_start.py` | Active, valuable |
| SmartService CSV export | `irp_core/smartservice_export.py` | Active, valuable |
| Plan-quality diagnostics + stability | `irp_core/diagnostics.py` | Active, valuable |
| σ tightening for sensor-monitored | `irp_core/anova_integration.py` | Active, valuable |
| $-calibrated drop penalties | `irp_core/objective.py`, `economics.py` | **Computed but unused — half-built** |

Eight modules. Seven are pulling weight. One (`objective.py`/`economics.py` per-client penalty calibration) is decorative. ADR-004 will resolve that.
