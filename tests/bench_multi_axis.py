"""
bench_multi_axis.py — 3-way factorial A/B across EFFICIENCY_WEIGHT,
USE_FORWARD_REFILLS, and ENFORCE_TIME_WINDOWS.

Why: Single-axis sweeps measure main effects but miss interactions.
Forward refills may look mediocre at EFFICIENCY_WEIGHT=0 but shine at =3,
because the solver only "buys" an expensive far-away route if it can
amortize multiple fills on it. This script runs the full 2×2×3 grid on
three scenarios and writes a results CSV + markdown with main-effect and
interaction decompositions.

Grid (12 runs per scenario):
  EFFICIENCY_WEIGHT     ∈ {0.0, 1.5, 3.0}   — reward for visiting near-full tanks
  USE_FORWARD_REFILLS   ∈ {False, True}     — forward-looking inventory in refill math
  ENFORCE_TIME_WINDOWS  ∈ {False, True}     — TW as hard constraint
  SOLVE_BUDGET = 2s (we observed convergence at 1-2s on these scenarios)

Runs per scenario: 3×2×2 = 12 → ~24s/scenario

Usage:
  python bench_multi_axis.py --scenario mixed-60
  python bench_multi_axis.py --scenario urban-50
  python bench_multi_axis.py --scenario peak-60
  python bench_multi_axis.py --combine  # after running all three
"""

import argparse
import itertools
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

import config as cfg
from config import OUTPUT_DIR
from unified_solver import solve_week
import tests.scenario_lib as scn


BUDGET_DEFAULT = 2

GRID = {
    'EFFICIENCY_WEIGHT':   [0.0, 1.5, 3.0],
    'USE_FORWARD_REFILLS': [False, True],
    'ENFORCE_TIME_WINDOWS': [False, True],
}

RESULTS_TMP_DIR = Path('/tmp/sk_multi_axis')


def _one(scen, budget):
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
    miles = float(df.groupby(['Truck', 'Day'])['Route_Dist_mi'].first().sum())
    lbs = int(df['Refill_lbs'].sum())
    labor = float(df.groupby(['Truck', 'Day'])['Labor_Cost'].first().sum())
    return {
        'stops': len(df),
        'miles': round(miles, 1),
        'lbs': lbs,
        'lbs_per_mile': round(lbs / miles, 1) if miles > 0 else 0.0,
        'deferred': len(deferred),
        'labor_cost_usd': round(labor, 0),
        'elapsed_s': round(elapsed, 1),
    }


def run_scenario(scenario_name, budget=BUDGET_DEFAULT):
    RESULTS_TMP_DIR.mkdir(parents=True, exist_ok=True)
    print(f'\nMulti-axis A/B on {scenario_name} — '
          f'{len(GRID["EFFICIENCY_WEIGHT"])}×{len(GRID["USE_FORWARD_REFILLS"])}×'
          f'{len(GRID["ENFORCE_TIME_WINDOWS"])} = 12 runs × {budget}s')
    print('━' * 78)

    # Save default
    defaults = {k: getattr(cfg, k) for k in GRID}

    results = []
    combos = list(itertools.product(
        GRID['EFFICIENCY_WEIGHT'],
        GRID['USE_FORWARD_REFILLS'],
        GRID['ENFORCE_TIME_WINDOWS'],
    ))
    for ew, fwd, tw in combos:
        # Build fresh scenario so no state leaks
        scen = scn.get_scenario(scenario_name)
        cfg.EFFICIENCY_WEIGHT = ew
        cfg.USE_FORWARD_REFILLS = fwd
        cfg.ENFORCE_TIME_WINDOWS = tw
        with open(os.devnull, 'w') as devnull:
            _so = sys.stdout
            sys.stdout = devnull
            try:
                r = _one(scen, budget)
            except Exception as e:
                sys.stdout = _so
                print(f'  EW={ew} FWD={int(fwd)} TW={int(tw)}  CRASH '
                      f'{type(e).__name__}: {str(e)[:40]}')
                continue
            finally:
                sys.stdout = _so
        r.update({'EFFICIENCY_WEIGHT': ew, 'USE_FORWARD_REFILLS': fwd,
                  'ENFORCE_TIME_WINDOWS': tw, 'scenario': scenario_name})
        results.append(r)
        print(f'  EW={ew:<3} FWD={int(fwd)} TW={int(tw)}   '
              f'stops={r["stops"]:3d}  mi={r["miles"]:5.1f}  '
              f'lbs={r["lbs"]:6d}  $={r["labor_cost_usd"]:.0f}  '
              f'def={r["deferred"]:3d}  t={r["elapsed_s"]}s')

    # Restore
    for k, v in defaults.items():
        setattr(cfg, k, v)

    # Persist
    out = RESULTS_TMP_DIR / f'{scenario_name}.json'
    out.write_text(json.dumps(results, indent=2))
    print(f'\n  → {out}')
    return results


def combine(out_dir=None):
    """Combine all per-scenario JSONs into a single markdown with analysis."""
    files = sorted(RESULTS_TMP_DIR.glob('*.json'))
    if not files:
        print('No results to combine. Run per-scenario first.')
        return None
    all_rows = []
    for f in files:
        all_rows.extend(json.loads(f.read_text()))
    df = pd.DataFrame(all_rows)

    out_dir = Path(out_dir) if out_dir else OUTPUT_DIR
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    out_path = out_dir / f'bench_multi_axis_{ts}.md'

    lines = [f'# Multi-Axis A/B Benchmark — {datetime.now():%Y-%m-%d %H:%M}',
             '',
             '3-way factorial across EFFICIENCY_WEIGHT × USE_FORWARD_REFILLS × '
             'ENFORCE_TIME_WINDOWS',
             '',
             '## All runs',
             '',
             '| scenario | EW | FWD | TW | stops | miles | lbs | lbs/mi | '
             'deferred | $labor | t |',
             '|---|---|---|---|---|---|---|---|---|---|---|',
             ]
    for _, r in df.iterrows():
        lines.append(
            f'| {r["scenario"]} | {r["EFFICIENCY_WEIGHT"]} | '
            f'{int(r["USE_FORWARD_REFILLS"])} | '
            f'{int(r["ENFORCE_TIME_WINDOWS"])} | '
            f'{r["stops"]} | {r["miles"]} | {r["lbs"]} | {r["lbs_per_mile"]} | '
            f'{r["deferred"]} | {r["labor_cost_usd"]:.0f} | {r["elapsed_s"]} |'
        )
    lines.append('')

    # Main effects per scenario
    for metric in ['lbs', 'miles', 'labor_cost_usd', 'stops', 'deferred']:
        lines.append(f'## Main effects on `{metric}` (per scenario)')
        lines.append('')
        lines.append('| scenario | EW=0 | EW=1.5 | EW=3.0 | '
                     'FWD=F | FWD=T | TW=F | TW=T |')
        lines.append('|---|---|---|---|---|---|---|---|')
        for scen_name, g in df.groupby('scenario'):
            row = [f'| {scen_name}']
            for ew in GRID['EFFICIENCY_WEIGHT']:
                v = g[g['EFFICIENCY_WEIGHT'] == ew][metric].mean()
                row.append(f'{v:.1f}' if pd.notna(v) else '—')
            for fwd in [False, True]:
                v = g[g['USE_FORWARD_REFILLS'] == fwd][metric].mean()
                row.append(f'{v:.1f}' if pd.notna(v) else '—')
            for tw in [False, True]:
                v = g[g['ENFORCE_TIME_WINDOWS'] == tw][metric].mean()
                row.append(f'{v:.1f}' if pd.notna(v) else '—')
            row.append('|')
            lines.append(' | '.join(row))
        lines.append('')

    # Top / bottom 5 overall
    lines.append('## Top 5 configs by lbs/mi (efficiency)')
    lines.append('')
    lines.append('| scenario | EW | FWD | TW | lbs/mi | stops | $labor | deferred |')
    lines.append('|---|---|---|---|---|---|---|---|')
    top = df.nlargest(5, 'lbs_per_mile')
    for _, r in top.iterrows():
        lines.append(
            f'| {r["scenario"]} | {r["EFFICIENCY_WEIGHT"]} | '
            f'{int(r["USE_FORWARD_REFILLS"])} | {int(r["ENFORCE_TIME_WINDOWS"])} | '
            f'{r["lbs_per_mile"]} | {r["stops"]} | '
            f'{r["labor_cost_usd"]:.0f} | {r["deferred"]} |'
        )
    lines.append('')

    lines.append('## Lowest $labor overall (per scenario)')
    lines.append('')
    lines.append('| scenario | EW | FWD | TW | $labor | stops | miles | deferred |')
    lines.append('|---|---|---|---|---|---|---|---|')
    for scen_name, g in df.groupby('scenario'):
        r = g.loc[g['labor_cost_usd'].idxmin()]
        lines.append(
            f'| {scen_name} | {r["EFFICIENCY_WEIGHT"]} | '
            f'{int(r["USE_FORWARD_REFILLS"])} | {int(r["ENFORCE_TIME_WINDOWS"])} | '
            f'{r["labor_cost_usd"]:.0f} | {r["stops"]} | {r["miles"]} | '
            f'{r["deferred"]} |'
        )
    lines.append('')

    # 2-way interactions: EW × FWD
    lines.append('## Interaction: EW × FWD on lbs (summed over TW)')
    lines.append('')
    for scen_name, g in df.groupby('scenario'):
        lines.append(f'### {scen_name}')
        lines.append('')
        lines.append('| EW \\ FWD | FWD=F | FWD=T | Δ (T−F) |')
        lines.append('|---|---|---|---|')
        for ew in GRID['EFFICIENCY_WEIGHT']:
            v_f = g[(g['EFFICIENCY_WEIGHT'] == ew) &
                    (~g['USE_FORWARD_REFILLS'])]['lbs'].mean()
            v_t = g[(g['EFFICIENCY_WEIGHT'] == ew) &
                    (g['USE_FORWARD_REFILLS'])]['lbs'].mean()
            delta = (v_t - v_f) if pd.notna(v_t) and pd.notna(v_f) else float('nan')
            lines.append(f'| {ew} | {v_f:.0f} | {v_t:.0f} | {delta:+.0f} |')
        lines.append('')

    # Rank stability: does "best config" flip across scenarios?
    lines.append('## Rank stability: best config per metric × scenario')
    lines.append('')
    lines.append('| metric | mixed-60 | urban-50 | peak-60 |')
    lines.append('|---|---|---|---|')
    for metric, higher_better in [('lbs_per_mile', True), ('lbs', True),
                                   ('labor_cost_usd', False)]:
        row = [f'| `{metric}`']
        for scen_name in ['mixed-60', 'urban-50', 'peak-60']:
            g = df[df['scenario'] == scen_name]
            if len(g) == 0:
                row.append('—')
                continue
            best = (g.loc[g[metric].idxmax()] if higher_better
                    else g.loc[g[metric].idxmin()])
            row.append(f'EW={best["EFFICIENCY_WEIGHT"]}/'
                       f'FWD={int(best["USE_FORWARD_REFILLS"])}/'
                       f'TW={int(best["ENFORCE_TIME_WINDOWS"])}')
        row.append('|')
        lines.append(' | '.join(row))
    lines.append('')

    out_path.write_text('\n'.join(lines))
    print(f'✓ Combined report → {out_path}')
    return out_path


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--scenario', default=None)
    p.add_argument('--budget', type=int, default=BUDGET_DEFAULT)
    p.add_argument('--combine', action='store_true')
    args = p.parse_args()
    if args.combine:
        combine()
    elif args.scenario:
        run_scenario(args.scenario, args.budget)
    else:
        print('Specify --scenario NAME or --combine')


if __name__ == '__main__':
    main()
