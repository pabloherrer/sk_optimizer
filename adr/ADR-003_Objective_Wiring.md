# ADR-003: Remove Half-Built `drop_penalties` Wiring; Keep Legacy Magic Constants

**Status:** Accepted
**Date:** 2026-05-07
**Deciders:** Pablo (owner)
**Supersedes:** Implicit "TODO" in `run.py` ("an eventual surgical patch")

---

## Context

ADR-002 left one open question: the IRP layer in `solver/objective.py` provides a function `build_per_client_disjunction_penalties` that computes $-calibrated drop penalties (the economic cost of NOT serving each client). The intent was to replace the legacy hand-tuned magic constants (1B for must-visit, 100M for critical, 5M for urgent) with principled $-denominated values.

**The wiring was never finished.** The penalties are computed in `run.py` and printed, but never passed to `solve_horizon`. The solver continues to use the legacy constants internally.

This is the worst of both worlds:
- The computation runs every solve (wasted CPU)
- The legacy constants are still in force (no behavior change)
- Future readers see the call and wonder if it's load-bearing — it isn't

## Decision

**Remove the `build_per_client_disjunction_penalties` call from `run.py` and rely on the legacy magic constants inside the solver.**

The function itself stays in `solver/objective.py` (so future work can wire it properly) but the dead call site is removed.

## Options Considered

### Option A: Wire up the $-calibrated penalties properly (rejected)

| Dimension | Assessment |
|-----------|------------|
| Complexity | High — solve_horizon signature change, internal solver changes |
| Risk | High — penalty values would change for every client; could destabilize routing |
| Validation needed | Backtest comparison against current production |
| Time to ship | Days to weeks |

**Pros:** Principled $-economics replace magic constants.
**Cons:** Big change with no urgent need. Current routes work; problems we're seeing today are about INPUT DATA (stale Anova, stale state), not OBJECTIVE TUNING. Tuning the objective without first fixing inputs would mask the real bugs.

### Option B: Remove the dead call, keep the function for future use (CHOSEN)

| Dimension | Assessment |
|-----------|------------|
| Complexity | Trivial — delete a few lines |
| Risk | Zero — output is identical (the call had no effect) |
| Time to ship | Minutes |

**Pros:** Honest representation of the architecture. No more confusion. Function preserved if/when we choose to wire it up.
**Cons:** The IRP "economics layer" is now visibly half-realized — only `cost.late_dollars_per_day()` and other helpers in `solver/economics.py` are used (in `calibrate_legacy_knobs`). This is fine: those computations DO flow into solver knobs via `LATE_PENALTY_PER_DAY` etc.

### Option C: Delete `solver/objective.py` and `solver/economics.py` entirely (rejected)

**Cons:** `calibrate_legacy_knobs` IS used (when `--dollar-objective` flag is passed), and `safety_stock.py` consumes `economics.CostModel`. Removing these breaks real code paths.

## Consequences

### What becomes easier
- The boot log is cleaner: no misleading "Per-client drop penalty (avg): $X.XX" line that suggests the value is in use.
- New contributors see a clean separation: `solver/objective.calibrate_legacy_knobs` (used) vs `solver/objective.build_per_client_disjunction_penalties` (preserved for future work, not currently called).

### What becomes harder
- If we later DO want $-calibrated drop penalties, we'll need to:
  1. Modify `solve_horizon` signature to accept a per-client penalty dict
  2. Replace the magic constants in the disjunction-penalty assignment loop
  3. Backtest against current production to confirm no regression
- This work is tracked in **ADR-004** (proposed, not yet written).

### What we'll need to revisit
- ADR-004 should run the comparison: do $-calibrated penalties produce materially different routes than the legacy 1B/100M/5M tiers? If yes, wire it up. If no, delete `build_per_client_disjunction_penalties` entirely.

## Action Items

- [x] Remove the `build_per_client_disjunction_penalties` call from `run.py`
- [x] Remove its import line in `run.py`
- [x] Document the decision in this ADR
- [ ] Optional follow-up (ADR-004): backtest $-calibrated penalties vs legacy tiers
