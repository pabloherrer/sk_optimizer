#!/usr/bin/env python3
"""
run_unified.py — Run the unified week solver on SK data

Usage:
    python run_unified.py                         # Real SK data, default solve
    python run_unified.py --validate-only         # Validate inputs only
    python run_unified.py --skip-validation       # Skip validation (debug)
"""

import os
import sys
import argparse
from pathlib import Path
from typing import List
import pandas as pd

# Ensure the package is importable
sys.path.insert(0, str(Path(__file__).parent))

from config import (
    INPUT_FILE, MATRIX_FILE, STATE_FILE, OUTPUT_DIR, DATA_DIR,
    SOLVE_SEC, SOLVE_SEC_WEEK, TRUCKS, DAYS,
)


# ── Plan-date utility ────────────────────────────────────────────────────────
_WEEKDAY_SHORT = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']


def _print_stockout_risk(snapshot: pd.DataFrame, routes: dict, today: pd.Timestamp) -> None:
    """Print the top at-risk clients and whether each is scheduled this week."""
    if snapshot is None or snapshot.empty or 'Days_Until_Stockout' not in snapshot.columns:
        return

    # Build a set of client IDs that appear in the generated routes
    scheduled_ids = set()
    sched_day: dict = {}
    for d, df in routes.items():
        if df is None or df.empty or 'ID' not in df.columns:
            continue
        for _, r in df.iterrows():
            cid = str(r.get('ID', ''))
            scheduled_ids.add(cid)
            if cid not in sched_day:
                sched_day[cid] = str(r.get('Date', r.get('Day', '?')))

    df = snapshot.copy()
    df = df.sort_values('Days_Until_Stockout', na_position='last')
    topN = df.head(15)

    print('\n  At-risk clients (top 15, sorted by days-until-stockout):')
    print(f'    {"ID":<7s} {"Customer":<26s} {"Days":>5s} {"Stockout date":<16s} {"Urg":<9s} {"Scheduled":<14s}')
    for _, r in topN.iterrows():
        cid = str(r.get('ID', ''))
        cust = str(r.get('Customer', ''))[:26]
        days = r.get('Days_Until_Stockout', float('nan'))
        try:
            days_s = f'{float(days):>5.1f}'
            stk_date = (today + pd.Timedelta(days=max(float(days), 0))).strftime('%a %b %d')
        except Exception:
            days_s = '  n/a'
            stk_date = '—'
        urg = str(r.get('Urgency', ''))[:9]
        if cid in scheduled_ids:
            sched = f'Yes ({sched_day.get(cid, "?")})'[:14]
        else:
            sched = 'DEFERRED'
        print(f'    {cid:<7s} {cust:<26s} {days_s} {stk_date:<16s} {urg:<9s} {sched:<14s}')


def compute_plan_dates(today: pd.Timestamp, n_days: int = 5) -> List[pd.Timestamp]:
    """
    Return the next `n_days` delivery dates strictly AFTER `today`.

    A "delivery date" is a date whose weekday is in the global DAYS list
    (default Tue-Sat). We always start from today+1, so that running the
    optimizer on e.g. Wednesday produces a plan that starts Thursday
    (Wednesday's deliveries are treated as already committed).

    Example (today = Thu Apr 16 2026):
        → [Fri Apr 17, Sat Apr 18, Tue Apr 21, Wed Apr 22, Thu Apr 23]
    """
    workday_set = set(DAYS)
    dates: List[pd.Timestamp] = []
    cur = pd.Timestamp(today).normalize() + pd.Timedelta(days=1)
    # Hard upper bound of 30 iterations in case of a misconfigured DAYS.
    for _ in range(30):
        if len(dates) >= n_days:
            break
        if _WEEKDAY_SHORT[cur.weekday()] in workday_set:
            dates.append(cur)
        cur = cur + pd.Timedelta(days=1)
    return dates
from load_data import load_all
from forecast_consumption import estimate_consumption_rates
from inventory import enrich_snapshot
from router import load_matrix
from state import load_state, initialise_state_from_snapshot
from schema_loaders import (
    load_time_windows, load_closures, load_depot_config, load_trucks
)
from unified_solver import solve_week, solve_horizon
from output import save_excel_schedule, save_route_map
from validator import validate_inputs


def main():
    parser = argparse.ArgumentParser(description='S&K Unified Route Optimizer')
    parser.add_argument('--demo', action='store_true',
                        help='Use fictional demo data')
    parser.add_argument('--solve-sec', type=int, default=None,
                        help=f'Solver time limit in seconds (default: {SOLVE_SEC_WEEK})')
    parser.add_argument('--start-day', type=int, default=0,
                        help='Start day index (0=Tue)')
    parser.add_argument('--output-prefix', type=str, default=None,
                        help='Output file prefix')
    parser.add_argument('--today', default=None,
                        help="Override today's date (YYYY-MM-DD)")
    parser.add_argument('--validate-only', action='store_true',
                        help='Run validation and exit')
    parser.add_argument('--skip-validation', action='store_true',
                        help='Skip input validation (debug mode)')
    parser.add_argument('--input-file', type=str, default=None,
                        help='Override input Excel file path')
    args = parser.parse_args()

    solve_sec = args.solve_sec or SOLVE_SEC_WEEK

    # ── Select input file ────────────────────────────────────────────────
    if args.demo:
        input_file = DATA_DIR / 'SK_Fictional_Demo.xlsx'
        prefix = args.output_prefix or 'demo_unified'
    elif args.input_file:
        input_file = Path(args.input_file)
        prefix = args.output_prefix or 'sk_unified'
    else:
        input_file = INPUT_FILE
        prefix = args.output_prefix or 'sk_unified'

    print('═' * 65)
    print('  S&K Route Optimizer — Unified Solver')
    print('═' * 65)

    # ── Load data ────────────────────────────────────────────────────────
    print(f'\n[1/6] Loading data from {input_file.name}...')
    clients_raw, deliveries = load_all(input_file)

    print(f'\n[2/6] Estimating consumption rates...')
    if args.today:
        today = pd.Timestamp(args.today).normalize()
        print(f'  ▸ Solving as-of {today.date()} (override)')
    else:
        today = pd.Timestamp.today().normalize()
    clients_df = estimate_consumption_rates(deliveries, clients_raw, today=today)

    print(f'\n[3/6] Loading distance/time matrix...')
    dm, tm, node_index_map = load_matrix(MATRIX_FILE)

    print(f'\n[4/6] Loading constraints (time windows, closures, depot config)...')
    time_windows_df = load_time_windows(input_file)
    closures_df = load_closures(input_file)
    depot_config = load_depot_config(input_file)
    trucks_cfg = load_trucks(input_file)
    if not time_windows_df.empty:
        print(f'  ▸ Loaded {len(time_windows_df)} time window rules')
    if not closures_df.empty:
        print(f'  ▸ Loaded {len(closures_df)} closure periods')
    print(f'  ▸ Depot: {depot_config.get("depot_lat")}, {depot_config.get("depot_lon")} '
          f'| Shift {depot_config.get("shift_start_min", 360)//60:02d}:00 '
          f'– {depot_config.get("shift_end_min", 960)//60:02d}:00')

    # ── Validate inputs ──────────────────────────────────────────────────
    if not args.skip_validation:
        print(f'\n[5/6] Validating inputs...')
        report = validate_inputs(
            clients_df, deliveries,
            time_windows_df=time_windows_df,
            closures_df=closures_df,
            trucks_cfg=trucks_cfg,
            depot_config=depot_config,
            matrix_nodes=node_index_map,
        )
        report.pretty_print()

        if not report.ok:
            print('\n⛔ FIX INPUT ERRORS BEFORE SOLVING')
            sys.exit(1)

        if args.validate_only:
            print('\n✓ Validation passed.\n')
            return

        print(f'\n[5/6] Enriching inventory snapshot...')
    else:
        print(f'\n[5/6] Enriching inventory snapshot (validation skipped)...')

    state = load_state(STATE_FILE)
    if not state:
        print('  No prior state — initialising from delivery-log estimates.')
        state = initialise_state_from_snapshot(clients_df)

    # ── Anova sensor override ────────────────────────────────────────────
    # For clients with fresh (< 24h) sensor readings, overwrite the
    # estimated tank level with the observed level. Graceful fallback if
    # the file is missing or stale.
    try:
        from anova_fetch import load_anova_latest
        anova = load_anova_latest()
        n_overrides = 0
        for cid, reading in anova.get('readings', {}).items():
            if reading.get('age_hours', 999) <= 24.0:
                state[cid] = float(reading['level_lbs'])
                n_overrides += 1
        if n_overrides:
            print(f'  ▸ Anova: {n_overrides} client(s) using live sensor levels')
    except Exception as e:
        print(f'  ⚠ Anova override skipped: {e}')

    snapshot = enrich_snapshot(clients_df, state)

    # ── Read operator overrides from env vars (set by app.py) ──────────
    skip_ids_env = os.environ.get('SK_SKIP_IDS', '')
    must_ids_env = os.environ.get('SK_MUST_VISIT_IDS', '')
    active_trucks_env = os.environ.get('SK_ACTIVE_TRUCKS', '')

    skip_ids = set(s.strip() for s in skip_ids_env.split(',') if s.strip()) if skip_ids_env else set()
    must_visit_ids = set(s.strip() for s in must_ids_env.split(',') if s.strip()) if must_ids_env else set()
    active_trucks = [s.strip() for s in active_trucks_env.split(',') if s.strip()] if active_trucks_env else None

    if skip_ids:
        print(f'  Operator overrides: {len(skip_ids)} client(s) skipped')
    if must_visit_ids:
        print(f'  Operator overrides: {len(must_visit_ids)} client(s) must-visit')
    if active_trucks:
        print(f'  Operator overrides: active trucks = {active_trucks}')

    # ── Solve with rolling horizon ───────────────────────────────────────
    from config import HORIZON_DAYS as _cfg_horizon, COMMIT_DAYS as _cfg_commit
    horizon_days = _cfg_horizon
    commit_days = _cfg_commit

    print(f'\n[6/6] Running horizon solver ({solve_sec}s, {horizon_days}-day horizon, '
          f'commit {commit_days})...')
    committed, tentative, deferred = solve_horizon(
        clients_df=snapshot,
        dist_matrix=dm,
        time_matrix_min=tm,
        node_index_map=node_index_map,
        today=today,
        horizon_days=horizon_days,
        commit_days=commit_days,
        solve_seconds=solve_sec,
        time_windows_df=time_windows_df,
        closures_df=closures_df,
        depot_config=depot_config,
        skip_ids=skip_ids,
        must_visit_ids=must_visit_ids,
        active_trucks=active_trucks,
    )

    # ── Merge committed + tentative for output ──────────────────────────
    # Tag each route DataFrame so Excel/map can distinguish them
    routes = {}
    plan_dates = compute_plan_dates(today, n_days=horizon_days)
    for d, df in committed.items():
        if not df.empty:
            df = df.copy()
            df['Status'] = 'COMMITTED'
        routes[d] = df
    for d, df in tentative.items():
        if not df.empty:
            df = df.copy()
            df['Status'] = 'TENTATIVE'
        routes[d] = df

    # ── Output ───────────────────────────────────────────────────────────
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    has_routes = any(not routes.get(d, pd.DataFrame()).empty for d in routes)
    if not has_routes:
        print('\n  ⚠  No routes generated — nothing to save.')
        return

    # Add km columns for backward compat with output.py
    for d in routes:
        df = routes[d]
        if df.empty:
            continue
        if 'Dist_To_km' not in df.columns and 'Dist_To_mi' in df.columns:
            df['Dist_To_km'] = round(df['Dist_To_mi'] * 1.60934, 2)
        if 'Cum_Dist_km' not in df.columns and 'Cum_Dist_mi' in df.columns:
            df['Cum_Dist_km'] = round(df['Cum_Dist_mi'] * 1.60934, 2)
        if 'Route_Dist_km' not in df.columns and 'Route_Dist_mi' in df.columns:
            df['Route_Dist_km'] = round(df['Route_Dist_mi'] * 1.60934, 1)

    # Print stockout-risk preview (top 15) — use all routes
    _print_stockout_risk(snapshot, routes, today)

    # Save Excel
    excel_name = f'{prefix}_schedule.xlsx'
    save_excel_schedule(
        routes, deferred, filename=excel_name, output_dir=OUTPUT_DIR,
        plan_dates=plan_dates, today=today, snapshot=snapshot,
    )
    print(f'\n  Excel → {OUTPUT_DIR / excel_name}')

    # Save map (all routes — committed + tentative; map has day toggles)
    map_name = f'{prefix}_map.html'
    save_route_map(routes, filename=map_name, output_dir=OUTPUT_DIR)
    print(f'  Map   → {OUTPUT_DIR / map_name}')

    print(f'\n✓ Done.\n')


if __name__ == '__main__':
    main()
