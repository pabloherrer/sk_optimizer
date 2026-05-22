"""
v2.pipeline — end-to-end orchestration.

plan_day(today, config_dir, input_file, matrix_file, output_dir) -> Plan

This is the only module that knows about ALL the others. It composes:
  ingest → forecast → solver → reporting → state
"""
from __future__ import annotations
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from v2.schemas import load_app_config
from v2.ingest.build_problem import build_problem_instance
from v2.solver.solve import solve
from v2.reporting import write_all_outputs
from v2.state.store import StateStore


def plan_day(
    today: date,
    config_dir: Path,
    input_file: Path,
    matrix_file: Path,
    output_dir: Path,
    state_file: Optional[Path] = None,
    solve_seconds: Optional[int] = None,
    run_id: Optional[str] = None,
) -> dict:
    """
    Run the full pipeline for a single day's planning.

    Returns a dict with:
        'plan'      : the v2.domain.Plan
        'outputs'   : dict of artifact paths (excel, pdfs, csv, map, archive)
        'state_path': path to the persisted state.json
    """
    run_id = run_id or _make_run_id()
    print(f"\n{'═' * 78}")
    print(f"  SK Route Optimizer v2 — run {run_id}")
    print(f"  Planning as-of {today}")
    print(f"{'═' * 78}")

    # ── 1. Load + validate config ────────────────────────────────────────
    print(f"\n[1/5] Loading config from {config_dir}")
    config = load_app_config(config_dir)
    print(f"  Economics: ${config.economics.cost_per_mile}/mi, "
          f"${config.economics.cost_per_minute_labor}/min labor, "
          f"OT {config.economics.overtime_multiplier}×")
    print(f"  Fleet:     {len(config.fleet.trucks)} trucks, "
          f"target {config.fleet.shift.target_minutes} min/day")
    print(f"  Policy:    horizon {config.policy.horizon_days}d, "
          f"commit {config.policy.commit_days}d, "
          f"min stop {config.policy.min_stop_lbs} lbs")

    # ── 2. Build ProblemInstance from Excel + Anova + state ──────────────
    print(f"\n[2/5] Building problem instance")
    problem = build_problem_instance(
        config_dir=config_dir,
        input_file=input_file,
        matrix_file=matrix_file,
        today=today,
        run_id=run_id,
    )
    if solve_seconds is not None:
        # Override the solve_seconds in the immutable problem
        # (dataclass is frozen; reconstruct with the override)
        from dataclasses import replace
        problem = replace(problem, solve_seconds=solve_seconds)
    print(f"  Problem: {len(problem.clients)} clients, "
          f"{len(problem.trucks)} trucks × {len(problem.horizon_dates)} days, "
          f"{len(problem.overrides.pins)} pins, {len(problem.overrides.forbids)} forbids")

    # ── 3. Solve ────────────────────────────────────────────────────────
    print(f"\n[3/5] Solving...")
    plan = solve(problem)

    # ── 4. Write all outputs ────────────────────────────────────────────
    print(f"\n[4/5] Writing outputs to {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    outputs = write_all_outputs(plan, output_dir, problem=problem)
    for name, path in outputs.items():
        if isinstance(path, list):
            for p in path:
                print(f"  {name}: {p}")
        else:
            print(f"  {name}: {path}")

    # ── 5. Persist state ─────────────────────────────────────────────────
    print(f"\n[5/5] Persisting state")
    state_file = state_file or (input_file.parent / 'inventory_state_v2.json')
    store = StateStore(state_file)
    # Build state dict from initial_tanks (this is what we know now;
    # next-run reconciliation will update from actuals)
    state_dict = {
        cid: {
            'id': cid,
            'current_lbs': float(ts.current_lbs),
            'confidence': ts.source,
            'updated_at': ts.as_of.isoformat() if ts.as_of else '',
        }
        for cid, ts in problem.initial_tanks.items()
    }
    store.save(state_dict, plan)
    store.record_run(run_id, {
        'today': today.isoformat(),
        'horizon': len(problem.horizon_dates),
        'committed': sum(
            1 for (dt, _), r in plan.routes.items()
            if dt in problem.horizon_dates[:problem.commit_days]
        ),
        'objective_dollars': plan.objective_cost_dollars,
        'avg_fill_pct': plan.avg_fill_pct,
    })
    print(f"  State → {state_file}")

    print(f"\n{'═' * 78}")
    print(f"  ✓ Done. Plan ready for review.")
    print(f"{'═' * 78}\n")

    return {
        'plan': plan,
        'outputs': outputs,
        'state_path': state_file,
    }


def _make_run_id() -> str:
    return f"{datetime.now().strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:6]}"
