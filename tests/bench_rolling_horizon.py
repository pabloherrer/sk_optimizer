"""
bench_rolling_horizon.py — 8-week consecutive-solve stability.

Why: A single-week solve can look great in isolation but cause a growing
deferral backlog or a "squeaky wheel" pattern (same hard clients deferred
every week). This simulates 8 consecutive weeks with a simple inventory
advance between weeks:

  * Scheduled clients are fully refilled at end of week.
  * Unscheduled clients consume 5 days of Avg_LbsPerDay.
  * Days_Since_Last advances by 5 for unvisited clients, resets to 0 otherwise.

Per-week metrics captured:
  stops, miles, lbs, deferred, solve_time

Cross-week metrics:
  * Jaccard(week_N, week_N-1) — schedule churn
  * Cumulative-coverage — fraction of clients visited ≥ once
  * Starvation count — clients visited 0 times in 8 weeks
  * Max-gap — longest span any client went without a visit
  * Deferral trend — is deferred growing, shrinking, stable?

Default: mixed-60, urban-50, peak-60 at 2s/week × 8 weeks ≈ 16s per run.
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

import tests.scenario_lib as scn
from config import OUTPUT_DIR
from unified_solver import solve_week


N_WEEKS_DEFAULT = 8
BUDGET_DEFAULT = 2
RESULTS_TMP_DIR = Path('/tmp/sk_rolling_horizon')


def _advance_state(clients_df, scheduled_ids):
    df = clients_df.copy()
    for idx in df.index:
        cid = df.at[idx, 'ID']
        rate = df.at[idx, 'Avg_LbsPerDay']
        tank = df.at[idx, 'Tank_lbs']
        if cid in scheduled_ids:
            df.at[idx, 'Current_lbs'] = tank
            df.at[idx, 'Days_Since_Last'] = 0
        else:
            df.at[idx, 'Current_lbs'] = max(0, df.at[idx, 'Current_lbs'] - rate * 5)
            df.at[idx, 'Days_Since_Last'] = df.at[idx, 'Days_Since_Last'] + 5
    df['Est_Current_lbs']     = df['Current_lbs']
    df['Refill_lbs']          = (df['Tank_lbs'] - df['Current_lbs']).clip(lower=0)
    df['Days_Until_Stockout'] = (df['Current_lbs'] / df['Avg_LbsPerDay'].clip(lower=1)).clip(lower=0.5)
    df['Refill_Today_lbs']    = df['Refill_lbs']
    df['Fill_Pct_Today']      = df['Refill_lbs'] / df['Tank_lbs']
    return df


def _solve_and_collect(df, s, budget):
    t0 = time.time()
    with open(os.devnull, 'w') as devnull:
        _so = sys.stdout
        sys.stdout = devnull
        try:
            routes, deferred = solve_week(
                df, s['dist_matrix'], s['time_matrix'], s['node_index_map'],
                start_day=0, solve_seconds=budget,
                time_windows_df=s['time_windows_df'],
                closures_df=s['closures_df'],
                today=pd.Timestamp('2026-04-14'),
                depot_config=s['depot_config'],
            )
        finally:
            sys.stdout = _so
    elapsed = time.time() - t0

    parts = [r for r in routes.values() if not r.empty]
    if parts:
        flat = pd.concat(parts, ignore_index=True)
        miles = float(flat.groupby(['Truck', 'Day'])['Route_Dist_mi'].first().sum())
        labor = float(flat.groupby(['Truck', 'Day'])['Labor_Cost'].first().sum())
        summary = {
            'stops': len(flat),
            'miles': round(miles, 1),
            'lbs': int(flat['Refill_lbs'].sum()),
            'labor_cost_usd': round(labor, 0),
            'deferred': len(deferred),
            'sched_ids': set(flat['ID'].tolist()),
            'elapsed_s': round(elapsed, 1),
        }
    else:
        summary = {'stops': 0, 'miles': 0.0, 'lbs': 0, 'labor_cost_usd': 0.0,
                   'deferred': len(deferred), 'sched_ids': set(),
                   'elapsed_s': round(elapsed, 1)}
    return summary


def _jaccard(a, b):
    if not a and not b:
        return 1.0
    return len(a & b) / max(1, len(a | b))


def run_scenario(scenario_name, n_weeks=N_WEEKS_DEFAULT, budget=BUDGET_DEFAULT):
    RESULTS_TMP_DIR.mkdir(parents=True, exist_ok=True)
    s = scn.get_scenario(scenario_name)
    df = s['clients_df']
    all_ids = list(df['ID'])

    visits = {cid: 0 for cid in all_ids}
    last_visit_week = {cid: -1 for cid in all_ids}
    max_gap = {cid: 0 for cid in all_ids}
    weekly = []
    prev_sched = None

    print(f'\nRolling-horizon on {scenario_name} — {n_weeks} weeks × {budget}s')
    print('━' * 78)
    print(f'  week | stops | miles |  lbs  | $labor | def | new | '
          f'jaccard | cum_cov | elapsed')
    print(f'  -----|-------|-------|-------|--------|-----|-----|'
          f'---------|---------|--------')

    total_visited = set()
    for week in range(n_weeks):
        res = _solve_and_collect(df, s, budget)
        sched = res['sched_ids']
        new_this_week = len(sched - total_visited)
        total_visited |= sched
        for cid in all_ids:
            if cid in sched:
                if last_visit_week[cid] >= 0:
                    gap = week - last_visit_week[cid]
                    max_gap[cid] = max(max_gap[cid], gap)
                visits[cid] += 1
                last_visit_week[cid] = week

        j = _jaccard(prev_sched, sched) if prev_sched is not None else float('nan')
        cum_cov = len(total_visited) / len(all_ids)
        weekly.append({
            'week': week, **{k: v for k, v in res.items() if k != 'sched_ids'},
            'jaccard_prev': round(j, 3) if not np.isnan(j) else None,
            'new_clients': new_this_week,
            'cum_cov': round(cum_cov, 3),
        })
        jac_s = f'{j:.2f}' if not np.isnan(j) else ' —  '
        print(f'   {week:2d}  |  {res["stops"]:3d}  | {res["miles"]:5.1f} | '
              f'{res["lbs"]:5d} | {res["labor_cost_usd"]:6.0f} | '
              f'{res["deferred"]:3d} | {new_this_week:3d} | '
              f'  {jac_s}  |  {cum_cov:.2f}  | {res["elapsed_s"]}s')

        prev_sched = sched
        df = _advance_state(df, sched)

    starved = [c for c, v in visits.items() if v == 0]
    report = {
        'scenario': scenario_name,
        'n_weeks': n_weeks,
        'budget_s': budget,
        'n_clients': len(all_ids),
        'starvation_count': len(starved),
        'starvation_frac': round(len(starved) / len(all_ids), 3),
        'cum_coverage': round(len(total_visited) / len(all_ids), 3),
        'max_observed_gap_weeks': max(max_gap.values()) if max_gap else 0,
        'weekly': weekly,
        'visits_histogram': _histogram([visits[c] for c in all_ids]),
        'max_gap_histogram': _histogram([max_gap[c] for c in all_ids]),
    }

    out = RESULTS_TMP_DIR / f'{scenario_name}.json'
    out.write_text(json.dumps(report, indent=2))

    print()
    print(f'  Starved (0 visits in {n_weeks} wk): {len(starved)}/'
          f'{len(all_ids)} ({report["starvation_frac"]:.0%})')
    print(f'  Cumulative coverage: {report["cum_coverage"]:.0%}')
    print(f'  Max gap observed: {report["max_observed_gap_weeks"]} weeks')
    print(f'  → {out}')
    return report


def _histogram(values):
    h = {}
    for v in values:
        h[v] = h.get(v, 0) + 1
    return {str(k): v for k, v in sorted(h.items())}


def combine(out_dir=None):
    files = sorted(RESULTS_TMP_DIR.glob('*.json'))
    if not files:
        print('No results to combine.')
        return None
    reports = [json.loads(f.read_text()) for f in files]

    out_dir = Path(out_dir) if out_dir else OUTPUT_DIR
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    out_path = out_dir / f'bench_rolling_horizon_{ts}.md'

    lines = [f'# Rolling-Horizon Stability — {datetime.now():%Y-%m-%d %H:%M}',
             '',
             f'Simulates N consecutive weeks of solving with state advance '
             '(scheduled → refilled; unscheduled → consume × 5).',
             '',
             '## Summary per scenario',
             '',
             '| scenario | n_weeks | clients | cum_cov | starved | starv% | '
             'max_gap_wk | avg_jaccard |',
             '|---|---|---|---|---|---|---|---|',
             ]
    for rep in reports:
        js = [w['jaccard_prev'] for w in rep['weekly']
              if w['jaccard_prev'] is not None]
        avgj = sum(js) / len(js) if js else 0.0
        lines.append(
            f'| {rep["scenario"]} | {rep["n_weeks"]} | {rep["n_clients"]} | '
            f'{rep["cum_coverage"]:.0%} | {rep["starvation_count"]} | '
            f'{rep["starvation_frac"]:.0%} | '
            f'{rep["max_observed_gap_weeks"]} | {avgj:.2f} |'
        )
    lines.append('')

    for rep in reports:
        lines.append(f'## {rep["scenario"]} — weekly')
        lines.append('')
        lines.append('| wk | stops | miles | lbs | $labor | def | new | '
                     'jaccard | cum_cov |')
        lines.append('|---|---|---|---|---|---|---|---|---|')
        for w in rep['weekly']:
            jac = (f'{w["jaccard_prev"]:.2f}' if w['jaccard_prev'] is not None
                   else '—')
            lines.append(
                f'| {w["week"]} | {w["stops"]} | {w["miles"]} | {w["lbs"]} | '
                f'{w["labor_cost_usd"]:.0f} | {w["deferred"]} | '
                f'{w["new_clients"]} | {jac} | {w["cum_cov"]:.2f} |'
            )
        lines.append('')
        lines.append(f'**Visits histogram** (clients with k visits): '
                     f'{rep["visits_histogram"]}')
        lines.append('')
        lines.append(f'**Max-gap histogram** (weeks): '
                     f'{rep["max_gap_histogram"]}')
        lines.append('')

    # Deferral trend analysis
    lines.append('## Deferral trend')
    lines.append('')
    lines.append('| scenario | wk0 def | wk-last def | Δ | direction |')
    lines.append('|---|---|---|---|---|')
    for rep in reports:
        d0 = rep['weekly'][0]['deferred']
        dl = rep['weekly'][-1]['deferred']
        delta = dl - d0
        direction = ('growing' if delta > 0 else
                     'shrinking' if delta < 0 else 'stable')
        lines.append(f'| {rep["scenario"]} | {d0} | {dl} | {delta:+d} | '
                     f'{direction} |')
    lines.append('')

    out_path.write_text('\n'.join(lines))
    print(f'✓ Combined → {out_path}')
    return out_path


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--scenario', default=None)
    p.add_argument('--weeks', type=int, default=N_WEEKS_DEFAULT)
    p.add_argument('--budget', type=int, default=BUDGET_DEFAULT)
    p.add_argument('--combine', action='store_true')
    args = p.parse_args()
    if args.combine:
        combine()
    elif args.scenario:
        run_scenario(args.scenario, args.weeks, args.budget)
    else:
        print('Specify --scenario NAME or --combine')


if __name__ == '__main__':
    main()
