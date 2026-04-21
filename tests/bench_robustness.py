"""
bench_robustness.py — Robustness under noisy or degraded inputs.

The solver is only as good as its inputs. Real-world inputs are NEVER clean:
forecasts are off, trucks break, clients churn, time windows tighten last
minute. This benchmark perturbs the inputs and re-solves, measuring how much
coverage and stability we lose vs. a clean baseline.

Perturbations
-------------
  baseline             : no perturbation (control)
  noise_low            : Avg_LbsPerDay × Uniform[0.9, 1.1]   (10% rate noise)
  noise_high           : Avg_LbsPerDay × Uniform[0.8, 1.2]   (20% rate noise)
  noise_extreme        : Avg_LbsPerDay × Uniform[0.7, 1.3]   (30% rate noise)
  truck_down           : drop one truck (3-truck → 2-truck setup)
  tight_windows        : 30% of clients get a 2h time-window
  client_churn_high    : 20% of clients dropped, 20% new clients added

Each perturbation is run 3 times with different seeds for variance.
Results compared to the baseline run (same scenario, no perturbation).

Usage
-----
    python tests/bench_robustness.py
    python tests/bench_robustness.py --scenario real --replicates 5
    python tests/bench_robustness.py --perturbation noise_high
"""

import argparse
import os
import sys
import time
import copy
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
from inventory import enrich_snapshot


# ─── Synthetic baseline ─────────────────────────────────────────────────────

def _make_synthetic(n_clients=50, seed=42):
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
    df['Current_lbs']     = (df['Tank_lbs'] * rng.uniform(0.3, 0.9, size=n_clients)).astype(int)
    df['Est_Current_lbs'] = df['Current_lbs']

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

    snap_state = dict(zip(df['ID'], df['Current_lbs']))
    df = enrich_snapshot(df, inventory_state=snap_state)

    return {
        'label': f'synthetic-{n_clients}',
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


def _make_real():
    from load_data import load_all
    from forecast_consumption import estimate_consumption_rates
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


# ─── Perturbations ──────────────────────────────────────────────────────────

def perturb_noise(scen, frac, seed):
    """Multiply Avg_LbsPerDay by Uniform[1-frac, 1+frac] per-client."""
    rng = np.random.default_rng(seed)
    new = copy.deepcopy(scen)
    df = new['clients_df'].copy()
    factors = rng.uniform(1 - frac, 1 + frac, size=len(df))
    df['Avg_LbsPerDay'] = (df['Avg_LbsPerDay'] * factors).astype(int).clip(lower=10)
    # Re-enrich (Days_Until_Stockout etc. depend on rate)
    snap_state = dict(zip(df['ID'], df['Current_lbs']))
    df = enrich_snapshot(df, inventory_state=snap_state)
    new['clients_df'] = df
    return new


def perturb_truck_down(scen, seed=None):
    """
    Simulate losing a truck. Because unified_solver imports TRUCKS at module
    load (not via _cfg), we cannot mutate truck count at runtime — we'd have
    to reload the module. Instead we APPROXIMATE truck loss by halving both
    trucks' capacity, which reduces weekly throughput by ~50% same as dropping
    one truck would.
    """
    return scen   # the capacity halving happens in run_perturbation


def perturb_capacity_halved(scen, seed=None):
    """Both trucks' capacity_lbs halved (approximates single-truck week)."""
    return scen   # capacity halving happens in run_perturbation


def perturb_tight_windows(scen, frac=0.30, seed=42):
    """Add a tight 2h time-window to `frac` of clients."""
    rng = np.random.default_rng(seed)
    new = copy.deepcopy(scen)
    ids = new['clients_df']['ID'].tolist()
    n_pick = max(1, int(len(ids) * frac))
    picked = rng.choice(ids, size=n_pick, replace=False)
    rows = []
    for cid in picked:
        # 2h window starting at a random "morning" or "afternoon" slot
        if rng.random() < 0.5:
            open_min, close_min = 480, 600     # 8 AM – 10 AM (relative to midnight)
        else:
            open_min, close_min = 780, 900     # 1 PM – 3 PM
        # Apply on a random day
        d = DAYS[int(rng.integers(0, len(DAYS)))]
        rows.append({'Client_ID': cid, 'Day_of_Week': d,
                     'Open_Min': open_min, 'Close_Min': close_min})
    new['time_windows_df'] = pd.concat(
        [new['time_windows_df'], pd.DataFrame(rows)], ignore_index=True)
    return new


def perturb_churn(scen, frac=0.20, seed=42):
    """Drop `frac` of clients, add same number of new clients reusing matrix slots."""
    rng = np.random.default_rng(seed)
    new = copy.deepcopy(scen)
    df = new['clients_df'].reset_index(drop=True)
    n = len(df)

    # Restrict to clients whose ID exists in node_index_map — real datasets
    # have client_list rows that aren't in the matrix (lat/lon missing etc.).
    valid_mask = df['ID'].isin(scen['node_index_map'].keys())
    valid_df = df[valid_mask].reset_index(drop=True)
    if len(valid_df) == 0:
        return new

    n_drop = max(1, int(len(valid_df) * frac))
    drop_idxs = rng.choice(valid_df.index, size=n_drop, replace=False)
    drop_old_ids = [valid_df.loc[idx]['ID'] for idx in drop_idxs]

    keep = df[~df['ID'].isin(drop_old_ids)].reset_index(drop=True)
    replacements = []
    for j, idx in enumerate(drop_idxs):
        old_row = valid_df.loc[idx]
        new_id = f'NEW{j:03d}_{int(rng.integers(0, 9999))}'
        replacements.append({
            'ID': new_id, 'Customer': f'New {j}',
            'Lat': old_row['Lat'], 'Lon': old_row['Lon'],
            'Tank_lbs': int(rng.choice([5000, 8000, 10000])),
            'Product': PRODUCTS[int(rng.integers(0, 2))],
            'Avg_LbsPerDay': int(rng.integers(50, 400)),
            'Days_Since_Last': int(rng.integers(3, 14)),
            'Current_lbs':     int(old_row['Tank_lbs'] * rng.uniform(0.2, 0.8)),
        })
    rep_df = pd.DataFrame(replacements)
    rep_df['Est_Current_lbs'] = rep_df['Current_lbs']

    combined = pd.concat([keep, rep_df], ignore_index=True)
    snap_state = dict(zip(combined['ID'], combined['Current_lbs']))
    combined = enrich_snapshot(combined, inventory_state=snap_state)
    new['clients_df'] = combined

    # Rebuild node_index_map: kept clients keep their slot, new clients reuse
    # the slot of the dropped client they replaced.
    new_map = {'DEPOT': 0}
    old_map = scen['node_index_map']
    for cid in keep['ID']:
        if cid in old_map:
            new_map[cid] = old_map[cid]
    for j, new_id in enumerate(rep_df['ID']):
        new_map[new_id] = old_map[drop_old_ids[j]]
    new['node_index_map'] = new_map
    return new


PERTURBATIONS = {
    'baseline':         lambda s, seed: s,
    'noise_low':        lambda s, seed: perturb_noise(s, 0.10, seed),
    'noise_high':       lambda s, seed: perturb_noise(s, 0.20, seed),
    'noise_extreme':    lambda s, seed: perturb_noise(s, 0.30, seed),
    'capacity_halved':  perturb_capacity_halved,
    'tight_windows':    lambda s, seed: perturb_tight_windows(s, 0.30, seed),
    'client_churn':     lambda s, seed: perturb_churn(s, 0.20, seed),
}


# ─── Solve & metrics ────────────────────────────────────────────────────────

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


def _metrics(routes, deferred, elapsed):
    parts = [r for r in routes.values() if not r.empty]
    if not parts:
        return {'stops': 0, 'miles': 0.0, 'lbs': 0, 'lbs_per_mile': 0.0,
                'deferred': len(deferred) if not deferred.empty else 0,
                'ot_min': 0, 'labor_cost_usd': 0.0,
                'scheduled_ids': set(), 'elapsed_s': round(elapsed, 1)}
    df = pd.concat(parts)
    miles = float(df.groupby(['Truck', 'Day'])['Cum_Dist_mi'].max().sum())
    lbs   = float(df['Refill_lbs'].sum())
    ot_per_route = df.groupby(['Truck', 'Day'])['OT_Min'].max() \
        if 'OT_Min' in df.columns else pd.Series([0])
    labor = float(df.groupby(['Truck', 'Day'])['Labor_Cost'].max().sum()) \
        if 'Labor_Cost' in df.columns else 0.0
    return {
        'stops': len(df),
        'miles': round(miles, 1),
        'lbs': int(lbs),
        'lbs_per_mile': round(lbs / miles, 1) if miles > 0 else 0.0,
        'deferred': 0 if deferred.empty else len(deferred),
        'ot_min': int(ot_per_route.sum()),
        'labor_cost_usd': round(labor, 0),
        'scheduled_ids': set(df['ID']),
        'elapsed_s': round(elapsed, 1),
    }


# ─── Driver ─────────────────────────────────────────────────────────────────

def run(scen, perturbations, solve_sec=5, replicates=3):
    """Returns list of result rows."""
    results = []

    for pert_name in perturbations:
        if pert_name not in PERTURBATIONS:
            print(f'  ! unknown perturbation: {pert_name}')
            continue
        print(f'\n─── {pert_name} ───')
        for rep in range(replicates):
            seed = 1000 + rep * 31
            # Special: capacity_halved mutates cfg.TRUCKS in place to halve capacities
            original_trucks = None
            if pert_name == 'capacity_halved':
                original_trucks = {k: dict(v) for k, v in cfg.TRUCKS.items()}
                for tn in cfg.TRUCKS:
                    cfg.TRUCKS[tn]['capacity_lbs'] = original_trucks[tn]['capacity_lbs'] // 2
                p_scen = scen
            else:
                p_scen = PERTURBATIONS[pert_name](scen, seed)

            try:
                routes, deferred, elapsed = _solve(p_scen, solve_sec)
                m = _metrics(routes, deferred, elapsed)
            except Exception as e:
                print(f'  rep{rep} CRASH: {type(e).__name__}: {e}')
                continue
            finally:
                if original_trucks is not None:
                    for tn, vals in original_trucks.items():
                        cfg.TRUCKS[tn].update(vals)

            row = {'perturbation': pert_name, 'rep': rep, 'seed': seed,
                   'sched_ids': m.pop('scheduled_ids'), **m}
            results.append(row)
            print(f'  rep{rep}  stops={m["stops"]:3d}  '
                  f'miles={m["miles"]:6.1f}  '
                  f'lbs={m["lbs"]:6d}  def={m["deferred"]:3d}  '
                  f'labor=${m["labor_cost_usd"]:.0f}  ({m["elapsed_s"]}s)')
    return results


# ─── Reporting ──────────────────────────────────────────────────────────────

def write_report(rows, scenario_label, out_path, solve_sec, replicates):
    df = pd.DataFrame([{k: v for k, v in r.items() if k != 'sched_ids'} for r in rows])

    lines = [
        f'# Robustness Benchmark — {datetime.now():%Y-%m-%d %H:%M}',
        '',
        f'**Scenario:** `{scenario_label}`  |  '
        f'**Replicates per perturbation:** {replicates}  |  '
        f'**Solve budget:** {solve_sec}s',
        '',
        '## Perturbation results (averaged across replicates)',
        '',
        '| Perturbation | Stops avg | Stops σ | Miles avg | Lbs avg | Lbs/mi | Deferred avg | OT min | Labor $ avg |',
        '|---|---|---|---|---|---|---|---|---|',
    ]
    grp = df.groupby('perturbation', sort=False).agg({
        'stops':          ['mean', 'std'],
        'miles':          'mean',
        'lbs':            'mean',
        'lbs_per_mile':   'mean',
        'deferred':       'mean',
        'ot_min':         'mean',
        'labor_cost_usd': 'mean',
    })
    for pert, r in grp.iterrows():
        lines.append(
            f'| {pert} | {r[("stops","mean")]:.1f} | {r[("stops","std")]:.2f} | '
            f'{r[("miles","mean")]:.1f} | {r[("lbs","mean")]:.0f} | '
            f'{r[("lbs_per_mile","mean")]:.1f} | {r[("deferred","mean")]:.1f} | '
            f'{r[("ot_min","mean")]:.0f} | ${r[("labor_cost_usd","mean")]:.0f} |'
        )
    lines.append('')

    # Degradation vs. baseline
    if 'baseline' in df['perturbation'].values:
        baseline = df[df['perturbation'] == 'baseline'].iloc[0]
        lines.append('## Degradation vs. baseline')
        lines.append('')
        lines.append('| Perturbation | Δ stops | Δ miles | Δ deferred | Δ labor $ | Solution drift (Jaccard) |')
        lines.append('|---|---|---|---|---|---|')
        baseline_ids = next(r['sched_ids'] for r in rows if r['perturbation'] == 'baseline')
        for pert in df['perturbation'].unique():
            if pert == 'baseline':
                continue
            sub = df[df['perturbation'] == pert]
            avg = sub.iloc[0]
            d_stops = avg['stops'] - baseline['stops']
            d_miles = avg['miles'] - baseline['miles']
            d_def   = avg['deferred'] - baseline['deferred']
            d_lab   = avg['labor_cost_usd'] - baseline['labor_cost_usd']
            # Jaccard similarity of scheduled IDs (averaged across replicates)
            jaccards = []
            for r in rows:
                if r['perturbation'] == pert and r['sched_ids']:
                    inter = len(baseline_ids & r['sched_ids'])
                    union = len(baseline_ids | r['sched_ids'])
                    jaccards.append(inter / union if union else 0)
            avg_jac = np.mean(jaccards) if jaccards else 0
            lines.append(
                f'| {pert} | {d_stops:+.1f} | {d_miles:+.1f} | '
                f'{d_def:+.1f} | ${d_lab:+.0f} | {avg_jac:.2f} |'
            )
        lines.append('')
        lines.append('Jaccard 1.0 = same clients scheduled; <0.5 = solution unstable under perturbation.')
        lines.append('')

    # Robustness verdict
    lines.append('## Robustness verdict')
    lines.append('')
    if 'baseline' in df['perturbation'].values:
        baseline = df[df['perturbation'] == 'baseline'].iloc[0]
        for pert in df['perturbation'].unique():
            if pert == 'baseline':
                continue
            sub = df[df['perturbation'] == pert].iloc[0]
            stops_loss = (baseline['stops'] - sub['stops']) / baseline['stops'] * 100 if baseline['stops'] > 0 else 0
            if abs(stops_loss) < 5:
                verdict = 'ROBUST'
            elif abs(stops_loss) < 15:
                verdict = 'TOLERABLE'
            else:
                verdict = 'BRITTLE'
            lines.append(f'- `{pert}`: {stops_loss:+.1f}% coverage change → **{verdict}**')

    Path(out_path).write_text('\n'.join(lines))
    print(f'\n✓ Report → {out_path}')


def main():
    p = argparse.ArgumentParser(description='Robustness benchmark.')
    p.add_argument('--scenario', default='synthetic-50',
                   choices=['synthetic-50', 'real'])
    p.add_argument('--perturbation', default='ALL',
                   help='Single perturbation name or ALL')
    p.add_argument('--replicates', type=int, default=3)
    p.add_argument('--solve-sec', type=int, default=5)
    p.add_argument('--out', default=None)
    args = p.parse_args()

    scen = _make_real() if args.scenario == 'real' else _make_synthetic()
    perts = list(PERTURBATIONS.keys()) if args.perturbation == 'ALL' else [args.perturbation]
    print(f'Robustness on {scen["label"]} ({len(scen["clients_df"])} clients)')
    print(f'Perturbations: {perts}  |  replicates: {args.replicates}  |  '
          f'solve_sec: {args.solve_sec}')
    print('━' * 78)

    rows = run(scen, perts, solve_sec=args.solve_sec, replicates=args.replicates)

    out = args.out or (OUTPUT_DIR / f'robustness_{scen["label"]}_{datetime.now():%Y%m%d_%H%M%S}.md')
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    write_report(rows, scen['label'], out, args.solve_sec, args.replicates)


if __name__ == '__main__':
    main()
