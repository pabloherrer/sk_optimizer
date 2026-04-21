"""
bench_sensitivity.py — Fine-grained parameter sensitivity (one-at-a-time).

Where bench_ab.py does a boolean-cross of 5 flags, this sweeps each flag
over a wider range while holding the others at their production defaults.
This is the cleanest way to measure marginal effect and find inflection
points. Output: per-flag response curves.

Flags swept
-----------
  EFFICIENCY_WEIGHT          : 0.0, 0.5, 1.0, 1.5, 2.0, 3.0
  OT_MULTIPLIER              : 1.0, 1.25, 1.5, 1.75, 2.0, 2.5
  MAX_SERVICE_INTERVAL_DAYS  : 7, 10, 14, 18, 21, 365
  SOLVE_SEC                  : 3, 6, 12, 20, 40
  USE_FORWARD_REFILLS        : False, True           (binary — for baseline)
  BALANCE_WEIGHT             : 0.0, 0.25, 0.5, 0.75, 1.0   (phase-1 greedy)
  OPPORTUNISTIC_FILL_PCT     : 0.3, 0.4, 0.5, 0.6, 0.7

The solver time sweep deserves special attention: it is the one "knob" that
costs real wallclock, and we want to know the diminishing-returns knee.

Scenario options
----------------
  synthetic-50    standard 50-client reproducible (default)
  synthetic-150   dense scenario that actually exercises OT (seed=7, tighter)
  real            SK_Delivery_System.xlsx

Usage
-----
    python tests/bench_sensitivity.py --axis EFFICIENCY_WEIGHT
    python tests/bench_sensitivity.py --axis OT_MULTIPLIER --scenario synthetic-150
    python tests/bench_sensitivity.py --axis ALL --scenario synthetic-50

Per-run solve budget defaults to 5s (keep total wallclock reasonable).
"""

import argparse
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
    OUTPUT_DIR, DATA_DIR, SHIFT_MIN, MAX_SHIFT_MIN,
    INPUT_FILE, MATRIX_FILE,
)
from unified_solver import solve_week


# ─── Sweep axes ─────────────────────────────────────────────────────────────

SWEEPS = {
    'EFFICIENCY_WEIGHT':         [0.0, 0.5, 1.0, 1.5, 2.0, 3.0],
    'OT_MULTIPLIER':             [1.0, 1.25, 1.5, 1.75, 2.0, 2.5],
    'MAX_SERVICE_INTERVAL_DAYS': [7, 10, 14, 18, 21, 365],
    'SOLVE_SEC':                 [3, 6, 12],  # special: wallclock knob (extended via separate calls)
    'USE_FORWARD_REFILLS':       [False, True],
    'BALANCE_WEIGHT':            [0.0, 0.25, 0.5, 0.75, 1.0],
    'OPPORTUNISTIC_FILL_PCT':    [0.30, 0.40, 0.50, 0.60, 0.70],
}

# Production defaults (everything we're NOT sweeping is held at these values)
DEFAULTS = {
    'EFFICIENCY_WEIGHT':         1.5,
    'OT_MULTIPLIER':             1.5,
    'MAX_SERVICE_INTERVAL_DAYS': 14,
    'USE_FORWARD_REFILLS':       True,
    'BALANCE_WEIGHT':            0.5,
    'OPPORTUNISTIC_FILL_PCT':    0.50,
    'ENFORCE_TIME_WINDOWS':      True,
}


# ─── Synthetic scenarios ────────────────────────────────────────────────────

def _synthetic_scenario(n_clients=50, seed=42, density='normal'):
    """
    density='normal': standard scatter (matches bench_ab)
    density='dense':  tighter geo + higher avg consumption → pushes OT envelope
    """
    rng = np.random.default_rng(seed)
    spread = 0.2 if density == 'dense' else 0.4
    rate_low, rate_high = (150, 500) if density == 'dense' else (50, 400)

    clients = []
    for i in range(n_clients):
        lat = 33.4 + rng.uniform(0, spread)
        lon = -112.3 + rng.uniform(0, spread)
        clients.append({
            'ID': f'C{i:04d}', 'Customer': f'Client {i}',
            'Lat': lat, 'Lon': lon,
            'Tank_lbs': int(rng.choice([5000, 8000, 10000])),
            'Product': PRODUCTS[int(rng.integers(0, 2))],
            'Avg_LbsPerDay': int(rng.integers(rate_low, rate_high)),
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

    return {
        'label': f'synthetic-{n_clients}-{density}',
        'clients_df': df,
        'time_windows_df': pd.DataFrame(columns=['Client_ID', 'Day_of_Week', 'Open_Min', 'Close_Min']),
        'closures_df':      pd.DataFrame(columns=['Client_ID', 'Start_Date', 'End_Date', 'Reason']),
        'dist_matrix': dist, 'time_matrix': tm,
        'node_index_map': node_index_map,
        'depot_config': {
            'depot_lat': 33.5, 'depot_lon': -112.1,
            'shift_start_min': 360, 'shift_end_min': 1080,
            'morning_load_min': 30, 'evening_unload_min': 15,
            'work_days': DAYS,
        },
    }


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
    try:
        state = load_state(cfg.STATE_FILE)
    except Exception:
        state = {}
    if not state:
        state = initialise_state_from_snapshot(clients_df)
    snapshot = enrich_snapshot(clients_df, state)
    return {
        'label': 'real-SK',
        'clients_df': snapshot,
        'time_windows_df': tw, 'closures_df': cl,
        'dist_matrix': dm, 'time_matrix': tm,
        'node_index_map': node_index_map,
        'depot_config': depot, 'today': today,
    }


# ─── Solve helpers (shared with bench_ab pattern) ───────────────────────────

def _extract_metrics(routes, deferred, elapsed):
    parts = [r for r in routes.values() if not r.empty]
    if not parts:
        return {'stops': 0, 'miles': 0.0, 'lbs': 0, 'lbs_per_mile': 0.0,
                'deferred': len(deferred) if not deferred.empty else 0,
                'deferred_crit': 0, 'ot_min_total': 0, 'ot_routes': 0,
                'labor_cost_usd': 0.0, 'ot_cost_usd': 0.0,
                'n_routes': 0, 'utilization_pct': 0.0, 'elapsed_s': round(elapsed, 1)}

    df = pd.concat(parts)
    stops = len(df)
    miles = float(df.groupby(['Truck', 'Day'])['Cum_Dist_mi'].max().sum())
    lbs   = float(df['Refill_lbs'].sum())

    ot_per_route = df.groupby(['Truck', 'Day'])['OT_Min'].max() if 'OT_Min' in df.columns else None
    ot_min_total = int(ot_per_route.sum()) if ot_per_route is not None else 0
    ot_routes_ct = int((ot_per_route > 0).sum()) if ot_per_route is not None else 0

    if 'Labor_Cost' in df.columns:
        labor_cost = float(df.groupby(['Truck', 'Day'])['Labor_Cost'].max().sum())
    else:
        labor_cost = 0.0
    ot_cost = ot_min_total * cfg.LABOR_COST_PER_MIN * (cfg.OT_MULTIPLIER - 1.0)

    # Utilization = total route time / (n_routes × SHIFT_MIN)
    if 'Route_Time_min' in df.columns:
        route_times = df.groupby(['Truck', 'Day'])['Route_Time_min'].max()
        n_routes = len(route_times)
        utilization = float(route_times.sum()) / (n_routes * SHIFT_MIN) * 100 if n_routes else 0
    else:
        n_routes, utilization = 0, 0.0

    deferred_ct = 0 if deferred.empty else len(deferred)
    deferred_crit = 0
    if not deferred.empty and 'Urgency' in deferred.columns:
        deferred_crit = int((deferred['Urgency'].isin(['critical', 'stockout'])).sum())

    return {
        'stops': stops,
        'miles': round(miles, 1),
        'lbs': int(lbs),
        'lbs_per_mile': round(lbs / miles, 1) if miles > 0 else 0.0,
        'deferred': deferred_ct,
        'deferred_crit': deferred_crit,
        'ot_min_total': ot_min_total,
        'ot_routes': ot_routes_ct,
        'labor_cost_usd': round(labor_cost, 0),
        'ot_cost_usd': round(ot_cost, 0),
        'n_routes': n_routes,
        'utilization_pct': round(utilization, 1),
        'elapsed_s': round(elapsed, 1),
    }


def _solve(scenario, solve_sec):
    today = scenario.get('today', pd.Timestamp('2026-04-14'))
    t0 = time.time()
    with open(os.devnull, 'w') as devnull:
        _old = sys.stdout
        sys.stdout = devnull
        try:
            routes, deferred = solve_week(
                scenario['clients_df'], scenario['dist_matrix'], scenario['time_matrix'],
                scenario['node_index_map'],
                start_day=0, solve_seconds=solve_sec,
                time_windows_df=scenario['time_windows_df'],
                closures_df=scenario['closures_df'],
                today=today,
                depot_config=scenario['depot_config'],
            )
        finally:
            sys.stdout = _old
    return routes, deferred, time.time() - t0


# ─── Sensitivity driver ─────────────────────────────────────────────────────

def _apply_defaults():
    for k, v in DEFAULTS.items():
        setattr(cfg, k, v)


def _snapshot(keys):
    return {k: getattr(cfg, k) for k in keys}


def sweep_axis(axis, scenario, solve_sec=5, n_replicates=1):
    """Sweep a single axis, hold everything else at production defaults.

    n_replicates: re-run each point with a fresh solver seed (OR-Tools picks a
                  different local optimum on solver restart). Useful for measuring
                  variance; 1 is fine for a fast pass.
    """
    assert axis in SWEEPS, f'unknown axis {axis}'
    values = SWEEPS[axis]

    _apply_defaults()
    originals = _snapshot(list(DEFAULTS.keys()) + [axis])

    rows = []
    try:
        for val in values:
            # Special case: SOLVE_SEC is the solve budget, not a cfg attribute on the solver
            if axis == 'SOLVE_SEC':
                _solve_sec = int(val)
                effective_axis_str = f'{val}s'
            else:
                setattr(cfg, axis, val)
                _solve_sec = solve_sec
                effective_axis_str = str(val)

            for rep in range(n_replicates):
                routes, deferred, elapsed = _solve(scenario, _solve_sec)
                m = _extract_metrics(routes, deferred, elapsed)
                m.update({'axis': axis, 'value': effective_axis_str, 'rep': rep})
                rows.append(m)
                print(f'  {axis}={effective_axis_str:<8s} rep{rep}  '
                      f'stops={m["stops"]:3d}  mi={m["miles"]:6.1f}  '
                      f'util={m["utilization_pct"]:5.1f}%  '
                      f'ot={m["ot_min_total"]:4d}m  '
                      f'def={m["deferred"]:3d}  labor=${m["labor_cost_usd"]:.0f}  '
                      f'({m["elapsed_s"]}s)')
    finally:
        for k, v in originals.items():
            setattr(cfg, k, v)

    return rows


def sweep_all(scenario, solve_sec=5, n_replicates=1, axes=None):
    axes = axes or list(SWEEPS.keys())
    all_rows = []
    for axis in axes:
        print(f'\n─── {axis} ' + '─' * max(0, 60 - len(axis)))
        rows = sweep_axis(axis, scenario, solve_sec=solve_sec, n_replicates=n_replicates)
        all_rows.extend(rows)
    return all_rows


# ─── Reporting ──────────────────────────────────────────────────────────────

def write_report(rows, scenario_label, out_path, solve_sec):
    df = pd.DataFrame(rows)
    lines = [
        f'# Sensitivity Sweep — {datetime.now():%Y-%m-%d %H:%M}',
        '',
        f'**Scenario:** `{scenario_label}`  ',
        f'**Per-run solve budget:** {solve_sec}s  ',
        f'**Total runs:** {len(rows)}  ',
        '',
        '## Production defaults held constant',
        '',
        '| Parameter | Default |',
        '|---|---|',
    ]
    for k, v in DEFAULTS.items():
        lines.append(f'| `{k}` | `{v}` |')
    lines.append('')

    # One table per axis
    for axis in df['axis'].unique():
        sub = df[df['axis'] == axis]
        lines.append(f'## {axis}')
        lines.append('')
        lines.append('| value | stops | miles | lbs | lbs/mi | OT min | OT routes | deferred | util % | labor $ | solve s |')
        lines.append('|---|---|---|---|---|---|---|---|---|---|---|')
        # Aggregate by value (if multiple replicates, average)
        grp = sub.groupby('value', sort=False).agg({
            'stops': 'mean', 'miles': 'mean', 'lbs': 'mean',
            'lbs_per_mile': 'mean', 'ot_min_total': 'mean',
            'ot_routes': 'mean', 'deferred': 'mean',
            'utilization_pct': 'mean', 'labor_cost_usd': 'mean',
            'elapsed_s': 'mean',
        })
        for idx, r in grp.iterrows():
            lines.append(
                f'| {idx} | {r["stops"]:.1f} | {r["miles"]:.1f} | {r["lbs"]:.0f} | '
                f'{r["lbs_per_mile"]:.1f} | {r["ot_min_total"]:.0f} | '
                f'{r["ot_routes"]:.1f} | {r["deferred"]:.1f} | '
                f'{r["utilization_pct"]:.1f} | ${r["labor_cost_usd"]:.0f} | '
                f'{r["elapsed_s"]:.1f} |'
            )
        lines.append('')

        # Inflection analysis
        if len(grp) >= 3:
            stops_range = grp['stops'].max() - grp['stops'].min()
            miles_range = grp['miles'].max() - grp['miles'].min()
            labor_range = grp['labor_cost_usd'].max() - grp['labor_cost_usd'].min()
            lines.append(f'**Range across sweep:** stops Δ={stops_range:.1f}, '
                         f'miles Δ={miles_range:.1f}, labor Δ=${labor_range:.0f}')
            lines.append('')

    # Overall best-by-metric table
    lines.append('## Best configs per metric')
    lines.append('')
    lines.append('| Metric | Axis | Value | Score |')
    lines.append('|---|---|---|---|')
    best_coverage = df.loc[df['stops'].idxmax()]
    lines.append(f'| Most stops | `{best_coverage["axis"]}` | {best_coverage["value"]} | {best_coverage["stops"]} |')
    if (df['miles'] > 0).any():
        best_lbs_per_mile = df[df['miles'] > 0].loc[df[df['miles'] > 0]['lbs_per_mile'].idxmax()]
        lines.append(f'| Best lbs/mile | `{best_lbs_per_mile["axis"]}` | {best_lbs_per_mile["value"]} | {best_lbs_per_mile["lbs_per_mile"]:.1f} |')
    min_labor = df[df['labor_cost_usd'] > 0].loc[df[df['labor_cost_usd'] > 0]['labor_cost_usd'].idxmin()]
    lines.append(f'| Lowest labor $ | `{min_labor["axis"]}` | {min_labor["value"]} | ${min_labor["labor_cost_usd"]:.0f} |')
    lowest_deferred = df.loc[df['deferred'].idxmin()]
    lines.append(f'| Fewest deferred | `{lowest_deferred["axis"]}` | {lowest_deferred["value"]} | {lowest_deferred["deferred"]} |')

    Path(out_path).write_text('\n'.join(lines))
    print(f'\n✓ Report → {out_path}')


def main():
    p = argparse.ArgumentParser(description='Fine-grained sensitivity sweeps.')
    p.add_argument('--axis', default='ALL',
                   help=f'One of {list(SWEEPS.keys())} or ALL')
    p.add_argument('--scenario', default='synthetic-50',
                   choices=['synthetic-50', 'synthetic-150-dense', 'real'])
    p.add_argument('--solve-sec', type=int, default=5)
    p.add_argument('--replicates', type=int, default=1)
    p.add_argument('--out', default=None)
    args = p.parse_args()

    if args.scenario == 'synthetic-50':
        scen = _synthetic_scenario(n_clients=50, seed=42)
    elif args.scenario == 'synthetic-150-dense':
        scen = _synthetic_scenario(n_clients=150, seed=7, density='dense')
    else:
        scen = _real_scenario()

    print(f'Sensitivity sweep on {scen["label"]} ({len(scen["clients_df"])} clients)')
    print(f'Per-run solve budget: {args.solve_sec}s  |  replicates: {args.replicates}')
    print('━' * 78)

    if args.axis.upper() == 'ALL':
        rows = sweep_all(scen, solve_sec=args.solve_sec, n_replicates=args.replicates)
    else:
        rows = sweep_axis(args.axis, scen, solve_sec=args.solve_sec, n_replicates=args.replicates)

    out = args.out or (OUTPUT_DIR / f'sensitivity_{scen["label"]}_{datetime.now():%Y%m%d_%H%M%S}.md')
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    write_report(rows, scen['label'], out, args.solve_sec)


if __name__ == '__main__':
    main()
