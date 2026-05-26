"""
Compare two plan archives side-by-side and produce a quality score.

Use cases:
  - "His machine vs my machine ran the same date — what's different?"
  - "Did changing min_fill_pct help or hurt?"
  - "Is the new build better than the old one on identical inputs?"

Usage:
    python -m final.compare <plan_A.json> <plan_B.json>
    python -m final.compare ~/Downloads/their_run.json ./final/output/archive/plan_2026-05-22.json

The "quality score" is a single number, LOWER IS BETTER:

    score = objective_$ + 100*deferred_count + 10*at_risk_count + 0.50*overtime_min

  · objective_$        — solver's own dollar cost
  · deferred_count×100 — each unscheduled client costs $100 in the score
                         (way more than money; an unserved client is a future
                         stockout risk, the model just doesn't see horizon end)
  · at_risk_count×10   — each client projected to be dry on some plan day
                         costs $10 in the score (less than deferred, more than
                         a few extra miles)
  · overtime_min×0.50  — each OT minute costs $0.50 in the score (the same
                         premium the solver itself uses)

These weights match the model's own preference order: never stockout >
minimize unserved > minimize OT > minimize cost.

The lower-scored plan wins. If they're within 1% of each other, declared a tie.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Tuple


# ── Weights for the aggregate score ────────────────────────────────────────
W_DEFERRED  = 100.0   # $ per deferred client
W_AT_RISK   = 10.0    # $ per at-risk (projected-empty-on-plan-day) client
W_OT_MIN    = 0.50    # $ per overtime minute

TIE_TOLERANCE_PCT = 1.0   # % difference under which we call it a tie


def load_plan(path: Path) -> Dict:
    return json.loads(Path(path).read_text(encoding='utf-8'))


def _routes(plan: Dict) -> List[Dict]:
    """Return list of routes as flat dicts (handles the nested-archive format)."""
    raw = plan.get('routes') or []
    flat: List[Dict] = []
    if isinstance(raw, list):
        for r in raw:
            route = r.get('route') if isinstance(r, dict) and 'route' in r else r
            flat.append({**(route or {}),
                          'date': r.get('date'),
                          'truck_id': r.get('truck_id')})
    elif isinstance(raw, dict):
        for _, v in raw.items():
            route = v.get('route') if isinstance(v, dict) and 'route' in v else v
            flat.append({**(route or {}),
                          'date': v.get('date'),
                          'truck_id': v.get('truck_id')})
    return flat


def summarize(plan: Dict) -> Dict:
    """Pull out the metrics we care about from a plan dict."""
    routes = _routes(plan)
    truck_days = sum(1 for r in routes if r.get('stops'))

    overtime_min = sum(int(r.get('overtime_minutes') or 0) for r in routes)
    total_min = sum(int(r.get('total_minutes') or 0) for r in routes)

    # Count "at-risk" stops: those whose level-at-arrival is at/below 0
    # (the solver scheduled them just-in-time or after dry-out).
    at_risk_stops = 0
    for r in routes:
        for stop in r.get('stops') or []:
            level = float(stop.get('level_at_arrival_lbs') or 0)
            if level <= 0:
                at_risk_stops += 1

    # Per-truck breakdown
    per_truck: Dict[str, int] = defaultdict(int)
    for r in routes:
        if r.get('stops'):
            per_truck[str(r.get('truck_id') or '?')] += len(r['stops'])

    # Per-day breakdown
    per_day: Dict[str, int] = defaultdict(int)
    for r in routes:
        per_day[str(r.get('date'))] += len(r.get('stops') or [])

    served_clients = {
        (r.get('truck_id'), r.get('date'), s.get('client_id'))
        for r in routes for s in (r.get('stops') or [])
    }
    served_ids = {triple[2] for triple in served_clients}

    return {
        'today': plan.get('today'),
        'horizon_days': len(plan.get('horizon_dates') or []),
        'commit_days': plan.get('commit_days'),
        'total_stops': int(plan.get('total_stops') or 0),
        'total_lbs': float(plan.get('total_lbs_delivered') or 0),
        'total_miles': float(plan.get('total_miles') or 0),
        'avg_fill': float(plan.get('avg_fill_pct') or 0),
        'pct_under_target_fill': float(plan.get('pct_stops_under_target_fill') or 0),
        'objective_dollars': float(plan.get('objective_cost_dollars') or 0),
        'solve_seconds': float(plan.get('solve_seconds') or 0),
        'solver_status': plan.get('solver_status'),
        'truck_days': truck_days,
        'overtime_min': overtime_min,
        'total_min': total_min,
        'at_risk_stops': at_risk_stops,
        'deferred_count': len(plan.get('deferred') or {}),
        'per_truck_stops': dict(per_truck),
        'per_day_stops': dict(per_day),
        'served_ids': served_ids,
        'deferred_ids': set((plan.get('deferred') or {}).keys()),
    }


def quality_score(summary: Dict) -> float:
    """Compute the single aggregate score. Lower is better."""
    return (
        summary['objective_dollars']
        + W_DEFERRED  * summary['deferred_count']
        + W_AT_RISK   * summary['at_risk_stops']
        + W_OT_MIN    * summary['overtime_min']
    )


# ── Pretty-print helpers ───────────────────────────────────────────────────

GREEN  = '\033[92m'
RED    = '\033[91m'
YELLOW = '\033[93m'
BOLD   = '\033[1m'
DIM    = '\033[2m'
RESET  = '\033[0m'


def _delta_str(a: float, b: float, lower_is_better: bool = True, fmt: str = '{:+.0f}') -> str:
    """Format the b-vs-a delta with color (green = B better, red = B worse).
    The format string is expected to include `:+` so the sign is emitted
    once (positive → '+', negative → '-', no double-prefix from us)."""
    diff = b - a
    if diff == 0:
        return f'{DIM}—{RESET}'
    color = GREEN if ((diff < 0) == lower_is_better) else RED
    return f'{color}{fmt.format(diff)}{RESET}'


def render_comparison(name_a: str, name_b: str, sa: Dict, sb: Dict) -> str:
    """Build the printable side-by-side report."""
    lines = []
    lines.append(f'\n{BOLD}══════ PLAN COMPARISON ══════{RESET}')
    lines.append(f'  A  = {name_a}')
    lines.append(f'  B  = {name_b}')
    lines.append('')

    if sa.get('today') != sb.get('today'):
        lines.append(f'{YELLOW}⚠ Different planning dates — comparison is suggestive, not apples-to-apples.{RESET}')
        lines.append(f'  A.today = {sa.get("today")}    B.today = {sb.get("today")}')
        lines.append('')

    def row(label, a_val, b_val, lower_better=True, fmt='{:>8}', val_fmt=str):
        a_disp = val_fmt(a_val)
        b_disp = val_fmt(b_val)
        try:
            delta = _delta_str(float(a_val), float(b_val), lower_better,
                                fmt='{:+.0f}' if isinstance(a_val, (int, float)) and abs(float(a_val)) >= 10 else '{:+.1f}')
        except (ValueError, TypeError):
            delta = ''
        lines.append(f'  {label:<32} {fmt.format(a_disp)}   {fmt.format(b_disp)}   {delta}')

    lines.append(f'  {"":>32} {"A":>8}   {"B":>8}   {BOLD}B−A{RESET}')
    lines.append('  ' + '─' * 64)
    row('Solver status',          sa['solver_status'],            sb['solver_status'],            lower_better=False, val_fmt=str)
    row('Total stops',            sa['total_stops'],               sb['total_stops'],               lower_better=False)
    row('Total lbs delivered',    f'{sa["total_lbs"]:.0f}',         f'{sb["total_lbs"]:.0f}',         lower_better=False)
    row('Total miles',            f'{sa["total_miles"]:.0f}',       f'{sb["total_miles"]:.0f}',       lower_better=True)
    row('Avg fill %',             f'{sa["avg_fill"]:.0f}%',          f'{sb["avg_fill"]:.0f}%',          lower_better=False)
    row('Truck-days dispatched',  sa['truck_days'],                sb['truck_days'],                lower_better=True)
    row('Total minutes',          sa['total_min'],                 sb['total_min'],                 lower_better=True)
    row('Overtime minutes',       sa['overtime_min'],              sb['overtime_min'],              lower_better=True)
    row('Deferred clients',       sa['deferred_count'],            sb['deferred_count'],            lower_better=True)
    row('At-risk stops (DTE≤0)',  sa['at_risk_stops'],             sb['at_risk_stops'],             lower_better=True)
    row('Objective ($)',          f'{sa["objective_dollars"]:.0f}', f'{sb["objective_dollars"]:.0f}', lower_better=True)
    row('Solve seconds',          f'{sa["solve_seconds"]:.0f}',     f'{sb["solve_seconds"]:.0f}',     lower_better=True)

    # Per-truck
    lines.append('')
    lines.append(f'{BOLD}  Stops by truck{RESET}')
    trucks = sorted(set(sa['per_truck_stops']) | set(sb['per_truck_stops']))
    for t in trucks:
        a = sa['per_truck_stops'].get(t, 0)
        b = sb['per_truck_stops'].get(t, 0)
        lines.append(f'    {t:<8}  A={a:>4}    B={b:>4}    delta={b-a:+d}')

    # Roster differences
    only_a = sa['served_ids'] - sb['served_ids']
    only_b = sb['served_ids'] - sa['served_ids']
    lines.append('')
    lines.append(f'{BOLD}  Roster differences{RESET}')
    lines.append(f'    Served by A only: {len(only_a)}')
    if only_a:
        lines.append(f'      {sorted(only_a)[:10]}{" ..." if len(only_a)>10 else ""}')
    lines.append(f'    Served by B only: {len(only_b)}')
    if only_b:
        lines.append(f'      {sorted(only_b)[:10]}{" ..." if len(only_b)>10 else ""}')

    # Score + verdict
    score_a = quality_score(sa)
    score_b = quality_score(sb)
    lines.append('')
    lines.append('  ' + '═' * 64)
    lines.append(f'{BOLD}  QUALITY SCORE (lower is better){RESET}')
    lines.append(f'    A: {score_a:>10.0f}       B: {score_b:>10.0f}')
    lines.append('')
    lines.append(f'    formula: obj_$ + 100×deferred + 10×at_risk + 0.5×OT_min')

    diff = score_b - score_a
    pct = (abs(diff) / max(score_a, 1.0)) * 100.0
    lines.append('')
    if pct < TIE_TOLERANCE_PCT:
        lines.append(f'{BOLD}{YELLOW}    VERDICT: TIE (within {TIE_TOLERANCE_PCT:.0f}%, scores essentially equal){RESET}')
    elif diff < 0:
        lines.append(f'{BOLD}{GREEN}    VERDICT: B wins by {abs(diff):.0f} pts ({pct:.1f}%){RESET}')
    else:
        lines.append(f'{BOLD}{GREEN}    VERDICT: A wins by {diff:.0f} pts ({pct:.1f}%){RESET}')

    return '\n'.join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description='Compare two plan archives side-by-side.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument('plan_a', type=Path, help='First plan archive JSON')
    parser.add_argument('plan_b', type=Path, help='Second plan archive JSON')
    parser.add_argument('--json', action='store_true',
                        help='Emit JSON instead of pretty text')
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    for p in (args.plan_a, args.plan_b):
        if not p.exists():
            print(f'ERROR: not found: {p}', file=sys.stderr)
            return 2

    plan_a = load_plan(args.plan_a)
    plan_b = load_plan(args.plan_b)
    sa = summarize(plan_a)
    sb = summarize(plan_b)

    if args.json:
        out = {
            'a': {**{k: v for k, v in sa.items() if not isinstance(v, set)},
                  'name': str(args.plan_a),
                  'score': quality_score(sa)},
            'b': {**{k: v for k, v in sb.items() if not isinstance(v, set)},
                  'name': str(args.plan_b),
                  'score': quality_score(sb)},
            'roster_only_a': sorted(sa['served_ids'] - sb['served_ids']),
            'roster_only_b': sorted(sb['served_ids'] - sa['served_ids']),
        }
        print(json.dumps(out, indent=2, default=str))
    else:
        print(render_comparison(args.plan_a.name, args.plan_b.name, sa, sb))

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
