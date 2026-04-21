"""
bench_rollout.py — Multi-week rolling-horizon simulator.

A single good week is necessary but NOT sufficient. The real test is whether
the solver produces a stable schedule when you chain weeks together, carrying
state forward. Failure modes we can only catch this way:

  1. Starvation: some clients are deferred every week, never scheduled.
  2. Oscillation: same clients bounce between weeks (no stable cadence).
  3. Contract drift: clients exceed MAX_SERVICE_INTERVAL_DAYS because each
     week individually looked fine but the gap accumulated.
  4. OT creep: labor cost trends up week-over-week as deferrals compound.
  5. Coverage collapse: week 4 has 50% fewer stops than week 1 because
     state got lumpy.

Simulation loop
---------------
  for week in 1..N:
    solve(current_snapshot)
    record metrics
    advance_state(delivered_ids, days=7)
    mark Days_Since_Last_Delivery appropriately
    re-derive Current_lbs, Urgency, etc.
    snapshot = enriched(new state)

Scenarios
---------
  synthetic-50    : 50-client, seed 42, tuned to produce ~3 stops/truck/day
  real            : SK_Delivery_System.xlsx, if present

Usage
-----
    python tests/bench_rollout.py --weeks 4
    python tests/bench_rollout.py --weeks 8 --scenario real
    python tests/bench_rollout.py --weeks 4 --solve-sec 3   # quick smoke
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
    MAX_SERVICE_INTERVAL_DAYS, MIN_OIL_PCT,
    INPUT_FILE, MATRIX_FILE,
)
from unified_solver import solve_week
from inventory import enrich_snapshot


# ─── Synthetic scenario (self-contained) ───────────────────────────────────

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

    # Distance / time matrices (straight-line euclidean, miles → meters/seconds)
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
        'label': f'synthetic-{n_clients}-rolling',
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
        'label': 'real-SK-rolling',
        'clients_df': snapshot,
        'time_windows_df': tw, 'closures_df': cl,
        'dist_matrix': dm, 'time_matrix': tm,
        'node_index_map': node_index_map,
        'depot_config': depot, 'today': today,
    }


# ─── State advance  ─────────────────────────────────────────────────────────

def _advance_week(snapshot_df, delivered_ids, days=7):
    """
    Return a fresh snapshot reflecting `days` of consumption since the last solve.

    Delivered clients:  Current_lbs → Tank_lbs (filled), Days_Since_Last → 0
    Unvisited clients:  Current_lbs -= days × Avg_LbsPerDay (clamped to floor),
                        Days_Since_Last += days
    """
    new = snapshot_df.copy()
    delivered = set(delivered_ids)

    # Update Current_lbs
    for idx, row in new.iterrows():
        tank = float(row['Tank_lbs'])
        rate = float(row['Avg_LbsPerDay'])
        floor = tank * MIN_OIL_PCT
        if row['ID'] in delivered:
            new.at[idx, 'Current_lbs'] = tank
            new.at[idx, 'Days_Since_Last'] = 0
        else:
            curr = float(row['Current_lbs'])
            new.at[idx, 'Current_lbs'] = max(curr - days * rate, floor)
            new.at[idx, 'Days_Since_Last'] = int(row.get('Days_Since_Last', 0)) + days

    # Refresh derived columns via the same helper production uses
    new['Est_Current_lbs'] = new['Current_lbs']
    enriched = enrich_snapshot(new, inventory_state=dict(zip(new['ID'], new['Current_lbs'])))
    return enriched


# ─── Metrics ────────────────────────────────────────────────────────────────

def _metrics(routes, deferred, snapshot_df, week, elapsed):
    parts = [r for r in routes.values() if not r.empty]
    if parts:
        df = pd.concat(parts)
        stops = len(df)
        miles = float(df.groupby(['Truck', 'Day'])['Cum_Dist_mi'].max().sum())
        lbs   = float(df['Refill_lbs'].sum())
        scheduled_ids = set(df['ID'])
        route_times = df.groupby(['Truck', 'Day'])['Route_Time_min'].max() \
            if 'Route_Time_min' in df.columns else pd.Series(dtype=float)
        ot_per_route = df.groupby(['Truck', 'Day'])['OT_Min'].max() \
            if 'OT_Min' in df.columns else pd.Series(dtype=float)
        ot_min = int(ot_per_route.sum()) if len(ot_per_route) else 0
        ot_routes = int((ot_per_route > 0).sum()) if len(ot_per_route) else 0
        labor = float(df.groupby(['Truck', 'Day'])['Labor_Cost'].max().sum()) \
            if 'Labor_Cost' in df.columns else 0.0
        n_routes = len(route_times)
        utilization = (route_times.sum() / (n_routes * SHIFT_MIN) * 100) if n_routes else 0.0
    else:
        stops, miles, lbs = 0, 0.0, 0.0
        scheduled_ids = set()
        ot_min, ot_routes, labor, n_routes, utilization = 0, 0, 0.0, 0, 0.0

    deferred_ids = set() if deferred.empty else set(deferred['ID'])
    # Contract-breach: any client whose Days_Since_Last > MAX_SERVICE_INTERVAL_DAYS
    breaches = int((snapshot_df['Days_Since_Last'] > MAX_SERVICE_INTERVAL_DAYS).sum())

    return {
        'week':               week,
        'stops':              stops,
        'miles':              round(miles, 1),
        'lbs':                int(lbs),
        'lbs_per_mile':       round(lbs / miles, 1) if miles > 0 else 0.0,
        'deferred':           len(deferred_ids),
        'n_routes':           n_routes,
        'utilization_pct':    round(utilization, 1),
        'ot_min':             ot_min,
        'ot_routes':          ot_routes,
        'labor_cost_usd':     round(labor, 0),
        'contract_breaches':  breaches,
        'scheduled_ids':      scheduled_ids,
        'deferred_ids':       deferred_ids,
        'elapsed_s':          round(elapsed, 1),
    }


# ─── Main rollout loop ──────────────────────────────────────────────────────

def rollout(scen, weeks=4, solve_sec=6):
    print(f'\nRolling {weeks}-week simulation on {scen["label"]} '
          f'({len(scen["clients_df"])} clients)')
    print('━' * 78)

    # Initial enrichment so downstream has the derived columns
    snap_state = dict(zip(scen['clients_df']['ID'], scen['clients_df']['Current_lbs']))
    snapshot = enrich_snapshot(scen['clients_df'], inventory_state=snap_state)
    today = scen.get('today', pd.Timestamp('2026-04-14'))

    weekly_metrics = []
    per_client_counts = {}   # client_id -> n_visits_across_weeks
    for cid in snapshot['ID']:
        per_client_counts[cid] = 0

    for wk in range(1, weeks + 1):
        print(f'\n─── Week {wk} ({today:%Y-%m-%d}) '
              f'└─ {len(snapshot)} clients,  '
              f'{int((snapshot["Current_lbs"] / snapshot["Tank_lbs"] < 0.3).sum())} below 30% fill,  '
              f'{int((snapshot["Days_Since_Last"] > 10).sum())} with >10 day gap')

        t0 = time.time()
        with open(os.devnull, 'w') as devnull:
            _old = sys.stdout
            sys.stdout = devnull
            try:
                routes, deferred = solve_week(
                    snapshot, scen['dist_matrix'], scen['time_matrix'],
                    scen['node_index_map'],
                    start_day=0, solve_seconds=solve_sec,
                    time_windows_df=scen['time_windows_df'],
                    closures_df=scen['closures_df'],
                    today=today,
                    depot_config=scen['depot_config'],
                )
            except Exception as e:
                sys.stdout = _old
                print(f'  week {wk} SOLVE CRASH: {type(e).__name__}: {e}')
                raise
            finally:
                sys.stdout = _old
        elapsed = time.time() - t0
        m = _metrics(routes, deferred, snapshot, wk, elapsed)
        weekly_metrics.append(m)
        print(f'  stops={m["stops"]:3d}  miles={m["miles"]:6.1f}  '
              f'lbs={m["lbs"]:6d}  def={m["deferred"]:3d}  '
              f'breach={m["contract_breaches"]:2d}  '
              f'util={m["utilization_pct"]:5.1f}%  '
              f'labor=${m["labor_cost_usd"]:.0f}  ({elapsed:.1f}s)')

        for cid in m['scheduled_ids']:
            per_client_counts[cid] = per_client_counts.get(cid, 0) + 1

        # Advance state and time
        snapshot = _advance_week(snapshot, list(m['scheduled_ids']), days=7)
        today = today + pd.Timedelta(days=7)

    return weekly_metrics, per_client_counts, snapshot


# ─── Reporting ──────────────────────────────────────────────────────────────

def write_report(weekly, per_client, final_snapshot, out_path, scenario_label, solve_sec):
    n_weeks = len(weekly)
    total_client_weeks = sum(m['stops'] for m in weekly)
    avg_stops = np.mean([m['stops'] for m in weekly])
    std_stops = np.std([m['stops'] for m in weekly])
    total_labor = sum(m['labor_cost_usd'] for m in weekly)
    total_miles = sum(m['miles'] for m in weekly)
    total_lbs   = sum(m['lbs'] for m in weekly)
    total_breaches = sum(m['contract_breaches'] for m in weekly)

    # Starvation: clients never scheduled
    starved = [cid for cid, n in per_client.items() if n == 0]
    # Oscillators: clients scheduled every week
    loyal = [cid for cid, n in per_client.items() if n == n_weeks]
    # Mid-frequency
    hist = {}
    for cid, n in per_client.items():
        hist[n] = hist.get(n, 0) + 1

    lines = [
        f'# Rolling-Horizon Rollout — {datetime.now():%Y-%m-%d %H:%M}',
        '',
        f'**Scenario:** `{scenario_label}`  |  **Weeks:** {n_weeks}  |  '
        f'**Solve budget:** {solve_sec}s/week',
        '',
        '## Aggregate',
        '',
        f'- Total client-weeks scheduled: **{total_client_weeks}**',
        f'- Avg stops/week: **{avg_stops:.1f}** (σ = {std_stops:.1f})',
        f'- Total miles: {total_miles:.0f}',
        f'- Total lbs delivered: {total_lbs:,}',
        f'- Total labor $: ${total_labor:,.0f}',
        f'- Total contract breaches observed: **{total_breaches}**',
        f'- Starved clients (never scheduled): **{len(starved)}** / {len(per_client)}',
        f'- Every-week clients: **{len(loyal)}** / {len(per_client)}',
        '',
        '## Per-week metrics',
        '',
        '| Week | Stops | Miles | lbs | lbs/mi | Routes | Util % | OT min | Def | Breach | Labor $ | Solve s |',
        '|---|---|---|---|---|---|---|---|---|---|---|---|',
    ]
    for m in weekly:
        lines.append(
            f'| {m["week"]} | {m["stops"]} | {m["miles"]:.1f} | {m["lbs"]:,} | '
            f'{m["lbs_per_mile"]:.1f} | {m["n_routes"]} | {m["utilization_pct"]:.1f} | '
            f'{m["ot_min"]} | {m["deferred"]} | {m["contract_breaches"]} | '
            f'${m["labor_cost_usd"]:.0f} | {m["elapsed_s"]} |'
        )
    lines.append('')

    # Week-over-week deltas
    if n_weeks >= 2:
        lines.append('## Week-over-week deltas')
        lines.append('')
        lines.append('| Transition | Δ stops | Δ miles | Δ labor $ | Δ deferred |')
        lines.append('|---|---|---|---|---|')
        for i in range(1, n_weeks):
            prev, curr = weekly[i-1], weekly[i]
            lines.append(
                f'| W{prev["week"]}→W{curr["week"]} | '
                f'{curr["stops"] - prev["stops"]:+d} | '
                f'{curr["miles"] - prev["miles"]:+.1f} | '
                f'${curr["labor_cost_usd"] - prev["labor_cost_usd"]:+.0f} | '
                f'{curr["deferred"] - prev["deferred"]:+d} |'
            )
        lines.append('')

    # Visit-frequency distribution
    lines.append('## Visit-frequency distribution')
    lines.append('')
    lines.append('| Times scheduled | Client count |')
    lines.append('|---|---|')
    for n in sorted(hist.keys()):
        lines.append(f'| {n} | {hist[n]} |')
    lines.append('')

    # Stability verdict
    lines.append('## Stability verdict')
    lines.append('')
    oscillation = std_stops / avg_stops if avg_stops > 0 else 0
    if oscillation < 0.10:
        verdict = 'STABLE — coverage varies <10% week-over-week.'
    elif oscillation < 0.25:
        verdict = 'ACCEPTABLE — some drift but no runaway collapse.'
    else:
        verdict = 'UNSTABLE — >25% coverage swing; steady-state not reached.'
    lines.append(f'- Coverage coefficient of variation: **{oscillation*100:.1f}%** → {verdict}')
    if total_breaches == 0:
        lines.append(f'- Contract compliance: **No 14-day-gap breaches observed.**')
    else:
        lines.append(f'- Contract compliance: **{total_breaches} breach-weeks observed** (investigate)')
    if starved:
        lines.append(f'- ⚠ Starvation: {len(starved)} clients never scheduled. '
                     f'Sample IDs: {starved[:5]}')

    Path(out_path).write_text('\n'.join(lines))
    print(f'\n✓ Report → {out_path}')


def main():
    p = argparse.ArgumentParser(description='Multi-week rolling simulator.')
    p.add_argument('--weeks', type=int, default=4)
    p.add_argument('--solve-sec', type=int, default=5)
    p.add_argument('--scenario', default='synthetic-50',
                   choices=['synthetic-50', 'real'])
    p.add_argument('--out', default=None)
    args = p.parse_args()

    scen = _make_real() if args.scenario == 'real' else _make_synthetic()
    weekly, per_client, final = rollout(scen, weeks=args.weeks, solve_sec=args.solve_sec)

    out = args.out or (OUTPUT_DIR / f'rollout_{scen["label"]}_{datetime.now():%Y%m%d_%H%M%S}.md')
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    write_report(weekly, per_client, final, out, scen['label'], args.solve_sec)


if __name__ == '__main__':
    main()
