#!/usr/bin/env python3
"""
run_unified.py — Run the unified week solver on SK data

Usage:
    python run_unified.py                         # Real SK data, default solve
    python run_unified.py --validate-only         # Validate inputs only
    python run_unified.py --skip-validation       # Skip validation (debug)
"""

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
from unified_solver import solve_week
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
    snapshot = enrich_snapshot(clients_df, state)

    # ── Compute plan dates (next N delivery days after today) ────────────
    plan_dates = compute_plan_dates(today)
    print(f'\n  Plan window ({len(plan_dates)} delivery days, starting from tomorrow):')
    for i, dt in enumerate(plan_dates):
        print(f'    Day {i + 1}: {dt.strftime("%a %b %d, %Y")}')

    # ── Solve ────────────────────────────────────────────────────────────
    print(f'\n[6/6] Running unified solver ({solve_sec}s time limit)...')
    routes, deferred = solve_week(
        snapshot, dm, tm, node_index_map,
        start_day=args.start_day,
        solve_seconds=solve_sec,
        time_windows_df=time_windows_df,
        closures_df=closures_df,
        today=today,
        depot_config=depot_config,
        plan_dates=plan_dates,
    )

    # ── Output ───────────────────────────────────────────────────────────
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    has_routes = any(not routes[d].empty for d in routes)
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

    # Save Excel
    excel_name = f'{prefix}_schedule.xlsx'
    save_excel_schedule(
        routes, deferred, filename=excel_name, output_dir=OUTPUT_DIR,
        plan_dates=plan_dates, today=today,
    )
    print(f'\n  Excel → {OUTPUT_DIR / excel_name}')

    # Save map
    map_name = f'{prefix}_map.html'
    save_route_map(routes, filename=map_name, output_dir=OUTPUT_DIR)
    print(f'  Map   → {OUTPUT_DIR / map_name}')

    print(f'\n✓ Done.\n')


if __name__ == '__main__':
    main()
