"""
Stress-test runner — executes scenarios written by the Designer agent.

Each scenario is a no-arg function that returns:
    (ProblemInstance, expected_dict)

where expected_dict is a dict with optional keys:

    must_serve         : list[client_id]    — these clients MUST appear in the plan
    must_defer         : list[client_id]    — these clients MUST NOT appear
    must_serve_by_day  : {date_str: list[client_id]}   — must appear on/before given day
    must_use_trucks    : list[truck_id]                — these trucks must dispatch at least once
    must_not_use_trucks: list[truck_id]                — these trucks must not dispatch
    no_route_on        : list[(date_str, truck_id)]    — no route may exist for these pairs
    must_serve_specific : list[(date_str, truck_id, client_id)]
                                              — client must be on exactly (date, truck)
    must_defer_with_reason : {client_id: reason_str}   — deferred with this exact reason
    min_total_stops    : int                — plan must have at least this many stops
    max_total_stops    : int                — plan must have at most this many stops
    min_total_lbs      : int                — plan must deliver at least this many lbs
    max_total_lbs      : int                — plan must deliver at most this many lbs
    no_stockouts       : bool               — no client's tank may go ≤ 0 within horizon if served as scheduled
    no_overflow        : bool               — no delivery may exceed tank capacity
    max_truck_days     : int                — total dispatch days ≤ this
    description        : str                — what the scenario tests (free-form)
    hypothesis         : str                — what bug it would expose

The runner produces a structured VERDICT per scenario:
    PASS      — all assertions passed
    FAIL      — at least one assertion failed (lists specifically what)
    SUSPICIOUS — plan passed but optimum-cost is surprising / suggests a bug

Usage (from sk_optimizer/):
    .venv/bin/python -m final.stress.runner               # all scenarios in scenarios.py
    .venv/bin/python -m final.stress.runner --filter pin  # only scenarios containing 'pin'
    .venv/bin/python -m final.stress.runner --module final.stress.scenarios_round2
"""
from __future__ import annotations

import argparse
import importlib
import json
import sys
import time
import traceback
from dataclasses import asdict
from datetime import date, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Tuple

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(REPO))

from v2.domain.plan import Plan
from final.sk_solver_final import solve_final


VERDICT_PASS = 'PASS'
VERDICT_FAIL = 'FAIL'
VERDICT_SUSPICIOUS = 'SUSPICIOUS'


def _all_served(plan: Plan) -> set:
    return {s.client_id for route in plan.routes.values() for s in route.stops}


def _served_by_day(plan: Plan) -> Dict[str, set]:
    out: Dict[str, set] = {}
    for (d, _), route in plan.routes.items():
        out.setdefault(str(d), set()).update(s.client_id for s in route.stops)
    return out


def _trucks_dispatched(plan: Plan) -> set:
    return {tid for (_, tid) in plan.routes.keys() if plan.routes[(_, tid)].stops}


def _no_stockouts(problem, plan: Plan) -> Tuple[bool, str]:
    """For each client, project the tank forward day-by-day, accounting for
    deliveries from the plan. Flag any time the tank crosses zero before
    end of horizon."""
    # Build deliveries-by-(client, day) from plan
    deliveries: Dict[Tuple[str, str], int] = {}
    for (d, _), route in plan.routes.items():
        for stop in route.stops:
            deliveries[(stop.client_id, str(d))] = int(stop.delivery_lbs)

    n_days = len(problem.horizon_dates)
    horizon_dates = [str(d) for d in problem.horizon_dates]
    for client_id, ts in problem.initial_tanks.items():
        level = float(ts.current_lbs)
        rate = float(ts.rate_lbs_per_day or 0.0)
        if rate <= 0:
            continue
        for d_idx, d_str in enumerate(horizon_dates):
            level += deliveries.get((client_id, d_str), 0)
            if level > 0 and (level - rate) < 0:
                return False, f"{client_id} stocks out on day {d_idx} ({d_str})"
            level -= rate
    return True, ''


def _no_overflow(plan: Plan) -> Tuple[bool, str]:
    for (d, t), route in plan.routes.items():
        for stop in route.stops:
            if stop.level_at_arrival_lbs + stop.delivery_lbs > stop.tank_capacity_lbs + 1:
                return False, (f"{stop.client_id} on {d} {t}: "
                                f"arrival {stop.level_at_arrival_lbs:.0f} + "
                                f"delivery {stop.delivery_lbs:.0f} > "
                                f"tank {stop.tank_capacity_lbs}")
    return True, ''


def run_scenario(name: str, fn: Callable, solve_seconds: int = 30) -> Dict:
    """Execute one scenario and report verdict."""
    t0 = time.time()
    try:
        problem, expected = fn()
    except Exception:
        return {
            'name': name,
            'verdict': VERDICT_FAIL,
            'failures': [f'Scenario builder crashed: {traceback.format_exc()}'],
            'expected': {},
            'plan_summary': {},
            'duration_s': 0.0,
        }

    description = expected.get('description', '')
    hypothesis = expected.get('hypothesis', '')

    try:
        plan = solve_final(problem, solve_seconds=solve_seconds)
    except Exception as e:
        return {
            'name': name,
            'description': description,
            'hypothesis': hypothesis,
            'verdict': VERDICT_FAIL,
            'failures': [f'Solver crashed: {type(e).__name__}: {e}'],
            'expected': expected,
            'plan_summary': {},
            'duration_s': time.time() - t0,
        }

    failures: List[str] = []
    notes: List[str] = []

    served = _all_served(plan)
    deferred = set(plan.deferred.keys())
    by_day = _served_by_day(plan)
    trucks = _trucks_dispatched(plan)

    # must_serve
    for cid in expected.get('must_serve', []):
        if cid not in served:
            failures.append(f"must_serve: {cid} was NOT in plan (deferred? {cid in deferred})")

    # must_defer
    for cid in expected.get('must_defer', []):
        if cid in served:
            failures.append(f"must_defer: {cid} WAS in plan (expected deferral)")

    # must_serve_by_day
    for d_str, cids in expected.get('must_serve_by_day', {}).items():
        served_by = set()
        for d, s in by_day.items():
            if d <= d_str:
                served_by |= s
        for cid in cids:
            if cid not in served_by:
                failures.append(f"must_serve_by_day[{d_str}]: {cid} NOT served by then")

    # truck dispatch expectations
    for tid in expected.get('must_use_trucks', []):
        if tid not in trucks:
            failures.append(f"must_use_trucks: {tid} not dispatched")
    for tid in expected.get('must_not_use_trucks', []):
        if tid in trucks:
            failures.append(f"must_not_use_trucks: {tid} was dispatched")

    # stops bounds
    if 'min_total_stops' in expected:
        if plan.total_stops < expected['min_total_stops']:
            failures.append(f"min_total_stops: got {plan.total_stops}, expected ≥ {expected['min_total_stops']}")
    if 'max_total_stops' in expected:
        if plan.total_stops > expected['max_total_stops']:
            failures.append(f"max_total_stops: got {plan.total_stops}, expected ≤ {expected['max_total_stops']}")

    # lbs bounds
    if 'min_total_lbs' in expected:
        if plan.total_lbs_delivered < expected['min_total_lbs']:
            failures.append(f"min_total_lbs: got {plan.total_lbs_delivered:.0f}, expected ≥ {expected['min_total_lbs']}")
    if 'max_total_lbs' in expected:
        if plan.total_lbs_delivered > expected['max_total_lbs']:
            failures.append(f"max_total_lbs: got {plan.total_lbs_delivered:.0f}, expected ≤ {expected['max_total_lbs']}")

    # no_route_on: hard assertion that (date, truck) has no stops
    for d_str, tid in expected.get('no_route_on', []):
        for (d, t), route in plan.routes.items():
            if str(d) == d_str and t == tid and route.stops:
                failures.append(f"no_route_on: ({d_str}, {tid}) has {len(route.stops)} stops "
                                 f"({[s.client_id for s in route.stops]})")

    # must_serve_specific: client on exactly (date, truck)
    for d_str, tid, cid in expected.get('must_serve_specific', []):
        found = False
        for (d, t), route in plan.routes.items():
            if str(d) == d_str and t == tid:
                for s in route.stops:
                    if s.client_id == cid:
                        found = True
                        break
        if not found:
            failures.append(f"must_serve_specific: {cid} not found on ({d_str}, {tid})")

    # must_defer_with_reason
    for cid, expected_reason in expected.get('must_defer_with_reason', {}).items():
        actual_reason = plan.deferred.get(cid)
        if actual_reason is None:
            failures.append(f"must_defer_with_reason: {cid} was served (expected deferral with reason {expected_reason})")
        elif expected_reason not in actual_reason:
            failures.append(f"must_defer_with_reason: {cid} deferred but reason is {actual_reason!r}, expected to contain {expected_reason!r}")

    # max_truck_days
    if 'max_truck_days' in expected:
        active_routes = sum(1 for r in plan.routes.values() if r.stops)
        if active_routes > expected['max_truck_days']:
            failures.append(f"max_truck_days: got {active_routes}, expected ≤ {expected['max_truck_days']}")

    # invariants — solver already checks these; this is belt-and-suspenders
    if expected.get('no_stockouts', False):
        ok, msg = _no_stockouts(problem, plan)
        if not ok:
            failures.append(f"no_stockouts violated: {msg}")

    if expected.get('no_overflow', False):
        ok, msg = _no_overflow(plan)
        if not ok:
            failures.append(f"no_overflow violated: {msg}")

    verdict = VERDICT_FAIL if failures else VERDICT_PASS

    plan_summary = {
        'total_stops': plan.total_stops,
        'total_lbs_delivered': float(plan.total_lbs_delivered),
        'total_miles': float(plan.total_miles),
        'truck_days': len(plan.routes),
        'deferred': len(plan.deferred),
        'objective_dollars': float(plan.objective_cost_dollars),
        'served': sorted(served),
        'deferred_clients': sorted(deferred),
        'routes': {
            f"{d} {t}": {
                'stops': [s.client_id for s in r.stops],
                'lbs': float(r.total_load_lbs),
                'minutes': int(r.total_minutes),
            }
            for (d, t), r in sorted(plan.routes.items())
        },
    }

    return {
        'name': name,
        'description': description,
        'hypothesis': hypothesis,
        'verdict': verdict,
        'failures': failures,
        'notes': notes,
        'expected': {k: v for k, v in expected.items()
                     if k not in ('description', 'hypothesis')},
        'plan_summary': plan_summary,
        'duration_s': time.time() - t0,
    }


def main(argv: List[str] = None) -> int:
    parser = argparse.ArgumentParser(description="Run stress-test scenarios")
    parser.add_argument('--module', type=str, default='final.stress.scenarios',
                        help='Python module containing scenario functions')
    parser.add_argument('--filter', type=str, default=None,
                        help='Only run scenarios whose name contains this string')
    parser.add_argument('--solve-seconds', type=int, default=30,
                        help='Time limit per scenario solve')
    parser.add_argument('--output', type=Path, default=HERE / 'EXECUTION_LOG.md')
    parser.add_argument('--json', type=Path, default=HERE / 'execution_log.json')
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    # Import the scenarios module
    print(f"Loading scenarios from {args.module} ...")
    mod = importlib.import_module(args.module)
    fns = [(name, getattr(mod, name)) for name in dir(mod)
           if name.startswith('scenario_') and callable(getattr(mod, name))]
    fns.sort()

    if args.filter:
        fns = [(n, f) for n, f in fns if args.filter.lower() in n.lower()]

    print(f"Running {len(fns)} scenario(s) ...")
    results: List[Dict] = []
    for i, (name, fn) in enumerate(fns, start=1):
        print(f"\n[{i}/{len(fns)}] {name}")
        r = run_scenario(name, fn, solve_seconds=args.solve_seconds)
        results.append(r)
        emoji = '✓' if r['verdict'] == VERDICT_PASS else '✗'
        print(f"  {emoji} {r['verdict']}  ({r['duration_s']:.1f}s)")
        for f in r['failures']:
            print(f"    – {f}")

    # Save JSON
    args.json.write_text(json.dumps(results, default=str, indent=2))

    # Write markdown log
    md = ['# EXECUTION LOG', '']
    n_pass = sum(1 for r in results if r['verdict'] == VERDICT_PASS)
    md.append(f'**Result: {n_pass}/{len(results)} scenarios PASS**')
    md.append('')
    for r in results:
        md.append(f"## {r['name']}  —  {r['verdict']}")
        if r.get('description'):
            md.append(f"_{r['description']}_")
        if r.get('hypothesis'):
            md.append(f"**Hypothesis:** {r['hypothesis']}")
        md.append('')
        md.append(f"**Duration:** {r['duration_s']:.1f}s")
        md.append('')
        if r['failures']:
            md.append('### Failures')
            for f in r['failures']:
                md.append(f"- {f}")
            md.append('')
        md.append('### Plan summary')
        ps = r['plan_summary']
        if ps:
            md.append(f"- Total stops: {ps.get('total_stops', '?')}")
            md.append(f"- Total lbs:   {ps.get('total_lbs_delivered', '?'):.0f}")
            md.append(f"- Truck-days:  {ps.get('truck_days', '?')}")
            md.append(f"- Deferred:    {ps.get('deferred', '?')}")
            md.append(f"- Objective:   ${ps.get('objective_dollars', '?'):.2f}")
            md.append('')
            md.append('**Routes:**')
            for k, v in ps.get('routes', {}).items():
                md.append(f"  - `{k}`: {v['stops']}  ({v['lbs']:.0f} lbs, {v['minutes']} min)")
            if ps.get('deferred_clients'):
                md.append(f"\n**Deferred clients:** {ps['deferred_clients']}")
        md.append('')
        md.append('---')
        md.append('')

    args.output.write_text('\n'.join(md))
    print(f"\nLog → {args.output}")
    print(f"JSON → {args.json}")
    print(f"\nFinal: {n_pass}/{len(results)} PASS")
    return 0 if n_pass == len(results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
