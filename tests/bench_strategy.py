"""
bench_strategy.py — OR-Tools first-solution × local-search ablation.

We've been running PATH_CHEAPEST_ARC + GUIDED_LOCAL_SEARCH because that's
what OR-Tools examples default to. But S&K's problem shape (dense urban,
capacity-pressured, time-windowed with closures) may reward a different
combination. This bench sweeps a compact grid and reports per-scenario
winners.

Grid (default — small to keep wallclock reasonable):
  first_solution_strategy ∈ {PATH_CHEAPEST_ARC, SAVINGS, CHRISTOFIDES,
                              PARALLEL_CHEAPEST_INSERTION}
  local_search_metaheuristic ∈ {GUIDED_LOCAL_SEARCH, SIMULATED_ANNEALING,
                                  TABU_SEARCH, GREEDY_DESCENT}

Scenarios: urban-50, mixed-60 (change via --scenario).
Budget: 3s/run (32 runs ≈ 100s per scenario).
"""

import argparse
import itertools
import os
import sys
import time
from datetime import datetime
from pathlib import Path
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

import config as cfg
from config import OUTPUT_DIR, DAYS
from unified_solver import solve_week
import tests.scenario_lib as scn


FIRST_SOLUTIONS = [
    'PATH_CHEAPEST_ARC',
    'SAVINGS',
    'CHRISTOFIDES',
    'PARALLEL_CHEAPEST_INSERTION',
]
METAHEURISTICS = [
    'GUIDED_LOCAL_SEARCH',
    'SIMULATED_ANNEALING',
    'TABU_SEARCH',
    'GREEDY_DESCENT',
]


def _run_once(scen, budget):
    t0 = time.time()
    routes, deferred = solve_week(
        scen['clients_df'], scen['dist_matrix'], scen['time_matrix'],
        scen['node_index_map'],
        start_day=0, solve_seconds=budget,
        time_windows_df=scen['time_windows_df'],
        closures_df=scen['closures_df'],
        today=pd.Timestamp('2026-04-14'),
        depot_config=scen['depot_config'],
    )
    elapsed = time.time() - t0
    parts = [r for r in routes.values() if not r.empty]
    if not parts:
        return {'stops': 0, 'miles': 0.0, 'lbs': 0, 'lbs_per_mile': 0.0,
                'deferred': len(deferred), 'labor_cost_usd': 0.0,
                'elapsed_s': round(elapsed, 1)}
    df = pd.concat(parts, ignore_index=True)
    miles = float(df.groupby(['Truck','Day'])['Route_Dist_mi'].first().sum())
    lbs = int(df['Refill_lbs'].sum())
    labor = float(df.groupby(['Truck','Day'])['Labor_Cost'].first().sum())
    return {
        'stops': len(df),
        'miles': round(miles, 1),
        'lbs': lbs,
        'lbs_per_mile': round(lbs / miles, 1) if miles > 0 else 0.0,
        'deferred': len(deferred),
        'labor_cost_usd': round(labor, 0),
        'elapsed_s': round(elapsed, 1),
    }


def run(scenario_name, budget=3, out_dir=None):
    scen = scn.get_scenario(scenario_name)
    print(f'\nStrategy ablation on {scenario_name} — '
          f'{len(FIRST_SOLUTIONS)}×{len(METAHEURISTICS)} = '
          f'{len(FIRST_SOLUTIONS)*len(METAHEURISTICS)} runs × {budget}s ≈ '
          f'{len(FIRST_SOLUTIONS)*len(METAHEURISTICS)*budget}s')
    print('━' * 78)

    results = []
    for fs, lsm in itertools.product(FIRST_SOLUTIONS, METAHEURISTICS):
        cfg.FIRST_SOLUTION_STRATEGY = fs
        cfg.LOCAL_SEARCH_METAHEURISTIC = lsm
        # Suppress solver stdout
        with open(os.devnull, 'w') as devnull:
            _stdout = sys.stdout
            sys.stdout = devnull
            try:
                r = _run_once(scen, budget)
            except Exception as e:
                sys.stdout = _stdout
                print(f'  [{fs:<28s} + {lsm:<22s}]  CRASH {type(e).__name__}')
                continue
            finally:
                sys.stdout = _stdout
        r['first_solution'] = fs
        r['metaheuristic']  = lsm
        results.append(r)
        print(f'  [{fs:<28s} + {lsm:<22s}]  '
              f'stops={r["stops"]:3d}  mi={r["miles"]:5.1f}  '
              f'lbs={r["lbs"]:6d}  $={r["labor_cost_usd"]:.0f}  '
              f't={r["elapsed_s"]}s')

    # Reset
    cfg.FIRST_SOLUTION_STRATEGY = 'PATH_CHEAPEST_ARC'
    cfg.LOCAL_SEARCH_METAHEURISTIC = 'GUIDED_LOCAL_SEARCH'

    if not results:
        return None

    out_dir = Path(out_dir) if out_dir else OUTPUT_DIR
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    out_path = out_dir / f'bench_strategy_{scenario_name}_{ts}.md'
    _write(results, scenario_name, budget, out_path)
    print(f'\n✓ Report → {out_path}')
    return out_path


def _write(results, scenario, budget, path):
    df = pd.DataFrame(results)
    lines = [f'# Strategy Ablation — {scenario} ({datetime.now():%Y-%m-%d %H:%M})',
             '',
             f'Per-run solve budget: {budget}s',
             '',
             '## All runs',
             '',
             '| first_solution | metaheuristic | stops | miles | lbs | lbs/mi | deferred | $labor | t (s) |',
             '|---|---|---|---|---|---|---|---|---|',
             ]
    for _, r in df.iterrows():
        lines.append(f'| `{r["first_solution"]}` | `{r["metaheuristic"]}` | '
                     f'{r["stops"]} | {r["miles"]} | {r["lbs"]} | {r["lbs_per_mile"]} | '
                     f'{r["deferred"]} | {r["labor_cost_usd"]:.0f} | {r["elapsed_s"]} |')
    lines.append('')

    # Best per metric
    lines.append('## Winners per metric')
    lines.append('')
    lines.append('| Metric | first_solution | metaheuristic | value |')
    lines.append('|---|---|---|---|')
    best_stops = df.loc[df['stops'].idxmax()]
    best_eff = df.loc[df['lbs_per_mile'].idxmax()]
    best_cost = df.loc[df['labor_cost_usd'].idxmin()]
    fewest_def = df.loc[df['deferred'].idxmin()]
    lines.append(f'| Most stops | `{best_stops["first_solution"]}` | `{best_stops["metaheuristic"]}` | {best_stops["stops"]} |')
    lines.append(f'| Best lbs/mi | `{best_eff["first_solution"]}` | `{best_eff["metaheuristic"]}` | {best_eff["lbs_per_mile"]} |')
    lines.append(f'| Lowest $labor | `{best_cost["first_solution"]}` | `{best_cost["metaheuristic"]}` | ${best_cost["labor_cost_usd"]:.0f} |')
    lines.append(f'| Fewest deferred | `{fewest_def["first_solution"]}` | `{fewest_def["metaheuristic"]}` | {fewest_def["deferred"]} |')
    lines.append('')

    # Per-axis pivot
    for axis in ('first_solution', 'metaheuristic'):
        lines.append(f'## Average by {axis}')
        lines.append('')
        lines.append(f'| {axis} | avg stops | avg mi | avg lbs/mi | avg $labor |')
        lines.append('|---|---|---|---|---|')
        for val, g in df.groupby(axis):
            lines.append(f'| `{val}` | {g["stops"].mean():.1f} | '
                         f'{g["miles"].mean():.1f} | {g["lbs_per_mile"].mean():.1f} | '
                         f'{g["labor_cost_usd"].mean():.0f} |')
        lines.append('')

    path.write_text('\n'.join(lines))


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--scenario', default='urban-50')
    p.add_argument('--budget', type=int, default=3)
    args = p.parse_args()
    run(args.scenario, args.budget)


if __name__ == '__main__':
    main()
