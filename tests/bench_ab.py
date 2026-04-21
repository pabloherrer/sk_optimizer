"""
bench_ab.py — A/B benchmark across paper-driven feature flags.

Not a pass/fail test — a report generator. For each cross of flag settings,
runs the unified solver on a reproducible synthetic scenario and (optionally)
on the real SK_Delivery_System.xlsx data. Writes a comparison markdown to
output/ab_bench_<timestamp>.md.

Axes (each ON/OFF unless otherwise noted):
  MAX_SERVICE_INTERVAL_DAYS: {365 (disabled), 14}
  ENFORCE_TIME_WINDOWS:      {False, True}
  EFFICIENCY_WEIGHT:         {0.0, 1.5}
  USE_FORWARD_REFILLS:       {False (snapshot), True (end-of-week)}
  OT_MULTIPLIER:             {1.0 (legacy no-OT-cost), 1.5 (paid OT)}

Full cross = 32 runs. In --fast mode, a 4-axis half-cross is used (16 runs,
MAX_SERVICE_INTERVAL_DAYS fixed at 14). In --minimal mode only 8 runs.

Usage:
    python tests/bench_ab.py               # synthetic, 32-run full cross
    python tests/bench_ab.py --fast        # synthetic, 16-run reduced cross
    python tests/bench_ab.py --minimal     # synthetic, 8-run smallest sweep
    python tests/bench_ab.py --real        # use real SK_Delivery_System.xlsx
    python tests/bench_ab.py --solve-sec 6 # override per-run solve seconds

Output columns per run:
    cfg_tag | stops | miles | lbs | lbs/mile | deferred | deferred_critical
    ot_min_total | ot_cost_$ | ot_routes_count | labor_cost_$ | elapsed_s
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
from config import (
    TRUCKS, DAYS, NUM_DAYS, PRODUCTS, LABOR_COST_PER_MIN,
    OUTPUT_DIR, DATA_DIR, SHIFT_MIN, INPUT_FILE, MATRIX_FILE,
)
from unified_solver import solve_week


# ─── Synthetic scenario (50 clients, reproducible) ──────────────────────────

def _synthetic_scenario(n_clients=50, seed=42):
    rng = np.random.default_rng(seed)
    clients = []
    for i in range(n_clients):
        lat = 33.4 + rng.uniform(0, 0.4)
        lon = -112.3 + rng.uniform(0, 0.4)
        clients.append({
            'ID': f'C{i:04d}', 'Customer': f'Client {i}',
            'Lat': lat, 'Lon': lon,
            'Tank_lbs': int(rng.choice([5000, 8000, 10000])),
            'Product': PRODUCTS[int(rng.integers(0, 2))],
            'Avg_LbsPerDay': int(rng.integers(50, 400)),
            'Days_Since_Last': int(rng.integers(3, 12)),
        })
    df = pd.DataFrame(clients)
    df['Current_lbs']         = (df['Tank_lbs'] * rng.uniform(0.1, 0.7, size=n_clients)).astype(int)
    df['Est_Current_lbs']     = df['Current_lbs']
    df['Refill_lbs']          = (df['Tank_lbs'] - df['Current_lbs']).clip(lower=0)
    df['Days_Until_Stockout'] = (df['Current_lbs'] / df['Avg_LbsPerDay'].clip(lower=1)).clip(lower=0.5)
    df['Urgency']             = 'normal'
    df['Refill_Today_lbs']    = df['Refill_lbs']
    df['Fill_Pct_Today']      = df['Refill_lbs'] / df['Tank_lbs']

    n_nodes = n_clients + 1
    dist = np.zeros((n_nodes, n_nodes), dtype=int)
    tm   = np.zeros((n_nodes, n_nodes), dtype=int)
    coords = [(33.5, -112.1)] + list(zip(df['Lat'], df['Lon']))
    for i in range(n_nodes):
        for j in range(n_nodes):
            if i != j:
                dx = (coords[i][0] - coords[j][0]) * 69
                dy = (coords[i][1] - coords[j][1]) * 60
                d_mi = (dx*dx + dy*dy) ** 0.5
                dist[i, j] = int(d_mi * 1609)
                tm[i, j]   = int(d_mi * 2 + 1)

    node_index_map = {'DEPOT': 0}
    for idx, cid in enumerate(df['ID'].tolist(), 1):
        node_index_map[cid] = idx

    # Add a pinch of time-window rules so ENFORCE_TIME_WINDOWS actually changes something.
    time_windows_df = pd.DataFrame([
        {'Client_ID': 'C0001', 'Day_of_Week': 'Tue', 'Open_Min': 420, 'Close_Min': 720},
        {'Client_ID': 'C0005', 'Day_of_Week': 'Wed', 'Open_Min': 540, 'Close_Min': 780},
        {'Client_ID': 'C0010', 'Day_of_Week': 'Thu', 'Open_Min': 600, 'Close_Min': 840},
    ])

    return {
        'label': f'synthetic-{n_clients}',
        'clients_df': df,
        'time_windows_df': time_windows_df,
        'closures_df': pd.DataFrame(columns=['Client_ID', 'Start_Date', 'End_Date', 'Reason']),
        'dist_matrix': dist, 'time_matrix': tm,
        'node_index_map': node_index_map,
        'depot_config': {
            'depot_lat': 33.5, 'depot_lon': -112.1,
            'shift_start_min': 360, 'shift_end_min': 1080,
            'morning_load_min': 30, 'evening_unload_min': 15,
            'work_days': DAYS,
        },
    }


# ─── Real-data scenario (SK_Delivery_System.xlsx) ───────────────────────────

def _real_scenario():
    from load_data import load_all
    from forecast_consumption import estimate_consumption_rates
    from inventory import enrich_snapshot
    from router import load_matrix
    from schema_loaders import load_time_windows, load_closures, load_depot_config
    from state import load_state, initialise_state_from_snapshot

    today = pd.Timestamp('2026-04-14')
    clients_raw, deliveries = load_all(INPUT_FILE)
    clients_df = estimate_consumption_rates(deliveries, clients_raw, today=today)
    dm, tm, node_index_map = load_matrix(MATRIX_FILE)
    tw = load_time_windows(INPUT_FILE)
    cl = load_closures(INPUT_FILE)
    depot = load_depot_config(INPUT_FILE)

    state = load_state(None) if hasattr(load_state, '__call__') else None
    try:
        state = load_state()
    except Exception:
        state = None
    if not state:
        state = initialise_state_from_snapshot(clients_df)
    snapshot = enrich_snapshot(clients_df, state)

    return {
        'label': 'real-SK',
        'clients_df': snapshot,
        'time_windows_df': tw,
        'closures_df': cl,
        'dist_matrix': dm, 'time_matrix': tm,
        'node_index_map': node_index_map,
        'depot_config': depot,
        'today': today,
    }


# ─── One solve ──────────────────────────────────────────────────────────────

def _run_once(scenario, solve_sec):
    t0 = time.time()
    today = scenario.get('today', pd.Timestamp('2026-04-14'))
    routes, deferred = solve_week(
        scenario['clients_df'], scenario['dist_matrix'], scenario['time_matrix'],
        scenario['node_index_map'],
        start_day=0, solve_seconds=solve_sec,
        time_windows_df=scenario['time_windows_df'],
        closures_df=scenario['closures_df'],
        today=today,
        depot_config=scenario['depot_config'],
    )
    elapsed = time.time() - t0

    parts = [r for r in routes.values() if not r.empty]
    if parts:
        df = pd.concat(parts)
        stops = len(df)
        miles = float(df.get('Route_Dist_mi', pd.Series(dtype=float)).drop_duplicates().sum())
        if miles == 0 and 'Cum_Dist_mi' in df.columns:
            # fall back to cumulative max per route
            miles = float(df.groupby(['Truck', 'Day'])['Cum_Dist_mi'].max().sum())
        lbs = float(df['Refill_lbs'].sum())
        ot_min_total = float(df.get('OT_Min', pd.Series(dtype=float)).sum())
        # OT_Min is per stop; per-route OT minutes are identical across stops in a route
        # so take the per-route max to avoid double-counting
        if 'OT_Min' in df.columns:
            ot_per_route = df.groupby(['Truck', 'Day'])['OT_Min'].max()
            ot_min_total = float(ot_per_route.sum())
            ot_routes_count = int((ot_per_route > 0).sum())
        else:
            ot_min_total, ot_routes_count = 0.0, 0
        if 'Labor_Cost' in df.columns:
            labor_cost = float(df.groupby(['Truck', 'Day'])['Labor_Cost'].max().sum())
        else:
            labor_cost = 0.0
        ot_cost = ot_min_total * cfg.LABOR_COST_PER_MIN * (cfg.OT_MULTIPLIER - 1.0)
    else:
        stops = 0
        miles = 0.0
        lbs = 0.0
        ot_min_total = 0.0
        ot_routes_count = 0
        labor_cost = 0.0
        ot_cost = 0.0

    if deferred.empty:
        deferred_ct = 0
        deferred_crit = 0
    else:
        deferred_ct = len(deferred)
        deferred_crit = int((deferred.get('Urgency', '') == 'critical').sum() +
                             (deferred.get('Urgency', '') == 'stockout').sum())

    lbs_per_mile = lbs / miles if miles > 0 else 0.0

    return {
        'stops': stops,
        'miles': round(miles, 1),
        'lbs': int(lbs),
        'lbs_per_mile': round(lbs_per_mile, 1),
        'deferred': deferred_ct,
        'deferred_crit': deferred_crit,
        'ot_min_total': int(ot_min_total),
        'ot_cost_usd': round(ot_cost, 0),
        'ot_routes': ot_routes_count,
        'labor_cost_usd': round(labor_cost, 0),
        'elapsed_s': round(elapsed, 1),
    }


# ─── Flag-sweep enumeration ─────────────────────────────────────────────────

AXES = {
    'MAX_SERVICE_INTERVAL_DAYS': [365, 14],
    'ENFORCE_TIME_WINDOWS':      [False, True],
    'EFFICIENCY_WEIGHT':         [0.0, 1.5],
    'USE_FORWARD_REFILLS':       [False, True],
    'OT_MULTIPLIER':             [1.0, 1.5],
}


def _enum(mode):
    """Return list of config dicts per run."""
    if mode == 'full':
        keys = list(AXES.keys())
        vals = [AXES[k] for k in keys]
        return [dict(zip(keys, combo)) for combo in itertools.product(*vals)]
    elif mode == 'fast':
        # Fix MAX_SERVICE_INTERVAL_DAYS at 14 (production value), sweep the other 4
        keys = ['ENFORCE_TIME_WINDOWS', 'EFFICIENCY_WEIGHT',
                'USE_FORWARD_REFILLS', 'OT_MULTIPLIER']
        vals = [AXES[k] for k in keys]
        out = []
        for combo in itertools.product(*vals):
            d = dict(zip(keys, combo))
            d['MAX_SERVICE_INTERVAL_DAYS'] = 14
            out.append(d)
        return out
    elif mode == 'minimal':
        # Sweep only 3 axes: EFFICIENCY_WEIGHT, USE_FORWARD_REFILLS, OT_MULTIPLIER
        keys = ['EFFICIENCY_WEIGHT', 'USE_FORWARD_REFILLS', 'OT_MULTIPLIER']
        vals = [AXES[k] for k in keys]
        out = []
        for combo in itertools.product(*vals):
            d = dict(zip(keys, combo))
            d['MAX_SERVICE_INTERVAL_DAYS'] = 14
            d['ENFORCE_TIME_WINDOWS'] = True
            out.append(d)
        return out
    else:
        raise ValueError(f'unknown mode {mode}')


def _tag(cfg_d):
    """Short tag for the configuration."""
    return (
        f"max{cfg_d['MAX_SERVICE_INTERVAL_DAYS']}"
        f"_tw{int(cfg_d['ENFORCE_TIME_WINDOWS'])}"
        f"_eff{cfg_d['EFFICIENCY_WEIGHT']}"
        f"_fwd{int(cfg_d['USE_FORWARD_REFILLS'])}"
        f"_ot{cfg_d['OT_MULTIPLIER']}"
    )


# ─── Driver ─────────────────────────────────────────────────────────────────

def _apply(cfg_d):
    for k, v in cfg_d.items():
        setattr(cfg, k, v)


def _snapshot_cfg():
    return {k: getattr(cfg, k) for k in AXES.keys()}


def run_benchmark(mode='fast', solve_sec=8, use_real=False, out_dir=None):
    if use_real:
        print(f'Loading real SK dataset...')
        scenarios = [_real_scenario()]
    else:
        scenarios = [_synthetic_scenario(50)]

    combos = _enum(mode)
    print(f'\nA/B benchmark: {len(combos)} runs × {len(scenarios)} scenario(s) '
          f'× {solve_sec}s each ≈ {len(combos) * len(scenarios) * solve_sec}s total')
    print('━' * 78)

    original = _snapshot_cfg()
    results = []
    try:
        for scen in scenarios:
            print(f'\nScenario: {scen["label"]} '
                  f'({len(scen["clients_df"])} clients)')
            for i, c in enumerate(combos, 1):
                _apply(c)
                tag = _tag(c)
                # Suppress the solver's verbose per-call output by redirecting stdout
                with open(os.devnull, 'w') as devnull:
                    _old = sys.stdout
                    sys.stdout = devnull
                    try:
                        try:
                            r = _run_once(scen, solve_sec)
                        except Exception as e:
                            sys.stdout = _old
                            print(f'  [{i:2d}/{len(combos)}] {tag}  CRASH: {type(e).__name__}')
                            continue
                    finally:
                        sys.stdout = _old
                r.update({'scenario': scen['label'], 'tag': tag, **c})
                results.append(r)
                print(f'  [{i:2d}/{len(combos)}] {tag:<50s}  '
                      f'stops={r["stops"]:3d}  mi={r["miles"]:6.1f}  '
                      f'lbs={r["lbs"]:6d}  ot={r["ot_min_total"]:3d}m  '
                      f'def={r["deferred"]:3d}  t={r["elapsed_s"]}s')
    finally:
        _apply(original)

    if not results:
        print('\nNo results — nothing to write.')
        return None

    # Write markdown report
    out_dir = Path(out_dir) if out_dir else OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    out_path = out_dir / f'ab_bench_{ts}.md'
    _write_report(results, combos, scenarios, mode, solve_sec, out_path)
    print(f'\n✓ Benchmark report → {out_path}')
    return out_path


def _write_report(results, combos, scenarios, mode, solve_sec, out_path):
    lines = []
    lines.append(f'# A/B Benchmark — {datetime.now():%Y-%m-%d %H:%M}')
    lines.append(f'')
    lines.append(f'**Mode:** `{mode}` ({len(combos)} configs × {len(scenarios)} scenarios '
                 f'× {solve_sec}s solve budget)')
    lines.append(f'**Scenarios:** {", ".join(s["label"] for s in scenarios)}')
    lines.append(f'')
    lines.append(f'## Axes')
    lines.append(f'')
    lines.append('| Flag | Values |')
    lines.append('|------|--------|')
    for k, vs in AXES.items():
        lines.append(f'| `{k}` | {", ".join(str(v) for v in vs)} |')
    lines.append(f'')

    df = pd.DataFrame(results)

    # Full results table
    lines.append(f'## Results (all runs)')
    lines.append(f'')
    cols = ['scenario', 'tag', 'stops', 'miles', 'lbs', 'lbs_per_mile',
            'deferred', 'ot_min_total', 'ot_cost_usd', 'ot_routes',
            'labor_cost_usd', 'elapsed_s']
    hdr = '| ' + ' | '.join(cols) + ' |'
    sep = '|' + '---|' * len(cols)
    lines.append(hdr)
    lines.append(sep)
    for _, r in df.iterrows():
        row = ['{:.1f}'.format(r[c]) if isinstance(r[c], float) else str(r[c])
               for c in cols]
        lines.append('| ' + ' | '.join(row) + ' |')
    lines.append('')

    # Per-axis pivot summaries (each axis averaged over other dims)
    lines.append(f'## Per-axis averages')
    lines.append(f'')
    for axis in AXES.keys():
        if axis not in df.columns:
            continue
        if df[axis].nunique() < 2:
            continue
        lines.append(f'### {axis}')
        lines.append('')
        lines.append(f'| {axis} | avg stops | avg mi | avg lbs | avg lbs/mi | avg OT_min | avg deferred | avg labor $ |')
        lines.append('|---|---|---|---|---|---|---|---|')
        grp = df.groupby(axis).agg({
            'stops': 'mean', 'miles': 'mean', 'lbs': 'mean',
            'lbs_per_mile': 'mean', 'ot_min_total': 'mean',
            'deferred': 'mean', 'labor_cost_usd': 'mean',
        })
        for idx, r in grp.iterrows():
            lines.append(f'| {idx} | {r["stops"]:.1f} | {r["miles"]:.1f} | '
                         f'{r["lbs"]:.0f} | {r["lbs_per_mile"]:.1f} | '
                         f'{r["ot_min_total"]:.1f} | {r["deferred"]:.1f} | '
                         f'{r["labor_cost_usd"]:.0f} |')
        lines.append('')

    # Best / worst
    lines.append(f'## Extremes')
    lines.append(f'')
    best_lbs_per_mile = df.loc[df['lbs_per_mile'].idxmax()]
    worst_lbs_per_mile = df.loc[df['lbs_per_mile'].idxmin()]
    lines.append(f'- **Highest lbs/mile:** `{best_lbs_per_mile["tag"]}`  '
                 f'({best_lbs_per_mile["lbs_per_mile"]:.1f} lbs/mi, '
                 f'{best_lbs_per_mile["stops"]} stops)')
    lines.append(f'- **Lowest lbs/mile:**  `{worst_lbs_per_mile["tag"]}`  '
                 f'({worst_lbs_per_mile["lbs_per_mile"]:.1f} lbs/mi, '
                 f'{worst_lbs_per_mile["stops"]} stops)')
    best_stops = df.loc[df['stops'].idxmax()]
    lines.append(f'- **Most stops:**      `{best_stops["tag"]}`  '
                 f'({best_stops["stops"]} stops)')
    lowest_labor = df.loc[df['labor_cost_usd'].idxmin()]
    lines.append(f'- **Lowest labor $:**  `{lowest_labor["tag"]}`  '
                 f'(${lowest_labor["labor_cost_usd"]:.0f})')
    lines.append('')

    out_path.write_text('\n'.join(lines))


def main():
    p = argparse.ArgumentParser(description='A/B benchmark across solver flags.')
    p.add_argument('--mode', choices=['full', 'fast', 'minimal'], default='fast')
    p.add_argument('--solve-sec', type=int, default=8)
    p.add_argument('--real', action='store_true', help='Use real SK_Delivery_System.xlsx')
    p.add_argument('--out-dir', default=None)
    args = p.parse_args()

    run_benchmark(mode=args.mode, solve_sec=args.solve_sec,
                  use_real=args.real, out_dir=args.out_dir)


if __name__ == '__main__':
    main()
