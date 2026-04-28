#!/usr/bin/env python3
"""
run_daily.py — Afternoon rolling-horizon planner
=================================================
Run each afternoon to produce tomorrow's committed routes + a multi-day
lookahead. Designed for the daily planning cycle:

  1. After today's deliveries: update inventory state (actual levels).
  2. Run this script → committed routes for tomorrow + tentative preview.
  3. Dispatch committed routes to Smart Service / iFleet.
  4. Next afternoon: repeat.

Rolling Horizon (Campbell & Savelsbergh 2004, Jaillet et al. 2002):
  - Plan HORIZON_DAYS working days ahead (default 5).
  - Commit only COMMIT_DAYS (default 1 = tomorrow).
  - Re-plan every afternoon with updated inventory.
  - End-of-horizon penalties prevent the "cliff effect" (deferring
    clients who become critical the day after the plan ends).

Usage:
    python run_daily.py                          # Plan from today
    python run_daily.py --today 2026-04-21       # Specific date
    python run_daily.py --horizon 7 --commit 2   # 7-day horizon, commit 2
    python run_daily.py --update-state           # Apply today's deliveries first
"""

import sys
import json
import argparse
from pathlib import Path
from typing import List

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

from config import (
    INPUT_FILE, MATRIX_FILE, STATE_FILE, OUTPUT_DIR,
    HORIZON_DAYS, COMMIT_DAYS, SOLVE_SEC_WEEK,
    DAYS, TRUCKS, TRUCK_NAMES,
)
from load_data import load_all
from forecast_consumption import estimate_consumption_rates
from inventory import enrich_snapshot
from router import load_matrix
from state import load_state, save_state, update_state, initialise_state_from_snapshot
from schema_loaders import load_time_windows, load_closures, load_depot_config
from unified_solver import solve_horizon


_WEEKDAY_SHORT = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']


def run_daily(
    today_str:    str = None,
    horizon_days: int = None,
    commit_days:  int = None,
    solve_sec:    int = None,
    do_update_state: bool = False,
    delivered_ids:   list = None,
):
    """
    Afternoon planning routine.

    Parameters
    ----------
    today_str      : Date string (YYYY-MM-DD). Default: actual today.
    horizon_days   : Override config HORIZON_DAYS.
    commit_days    : Override config COMMIT_DAYS.
    solve_sec      : Override solver time limit.
    do_update_state: If True, apply `delivered_ids` to state before planning.
    delivered_ids  : Client IDs that were actually delivered today.
    """
    if horizon_days is None:
        horizon_days = HORIZON_DAYS
    if commit_days is None:
        commit_days = COMMIT_DAYS
    if solve_sec is None:
        solve_sec = SOLVE_SEC_WEEK

    today = pd.Timestamp(today_str).normalize() if today_str else pd.Timestamp.today().normalize()

    print(f"\n{'━' * 80}")
    print(f"  S&K Daily Planner — {today.strftime('%A %B %d, %Y')}")
    print(f"{'━' * 80}")

    # ── Load static data ────────────────────────────────────────────────
    clients_raw, deliveries = load_all(INPUT_FILE)
    dm, tm, nim = load_matrix(MATRIX_FILE)
    tw = load_time_windows(INPUT_FILE)
    cl = load_closures(INPUT_FILE)
    dc = load_depot_config(INPUT_FILE)

    # ── Load or initialise inventory state ──────────────────────────────
    state = load_state(STATE_FILE)

    # Estimate consumption rates from delivery history
    clients_df = estimate_consumption_rates(deliveries, clients_raw, today=today)

    if not state:
        print("  No saved state — initialising from delivery log estimates.")
        state = initialise_state_from_snapshot(clients_df)
    else:
        print(f"  Loaded inventory state for {len(state)} clients.")

    # ── Optional: apply today's deliveries to state ─────────────────────
    if do_update_state and delivered_ids:
        print(f"  Applying {len(delivered_ids)} deliveries to state...")
        state = update_state(state, clients_df, delivered_ids, n_days_elapsed=1)
        save_state(state, STATE_FILE)
        print(f"  State updated and saved.")
    elif do_update_state:
        # Just age by one day (no deliveries)
        print(f"  Aging state by 1 day (no deliveries provided).")
        state = update_state(state, clients_df, [], n_days_elapsed=1)
        save_state(state, STATE_FILE)

    # ── Enrich snapshot with current state ──────────────────────────────
    # Overlay persisted state onto the delivery-log estimates
    for i, row in clients_df.iterrows():
        cid = row['ID']
        if cid in state:
            clients_df.at[i, 'Est_Current_lbs'] = state[cid]

    snapshot = enrich_snapshot(clients_df, state)

    # ── Solve the rolling horizon ───────────────────────────────────────
    committed, tentative, deferred = solve_horizon(
        clients_df=snapshot,
        dist_matrix=dm,
        time_matrix_min=tm,
        node_index_map=nim,
        today=today,
        horizon_days=horizon_days,
        commit_days=commit_days,
        solve_seconds=solve_sec,
        time_windows_df=tw,
        closures_df=cl,
        depot_config=dc,
    )

    # ── Output committed routes ─────────────────────────────────────────
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    for d, df in committed.items():
        if df.empty:
            continue
        date_str = df['Date'].iloc[0] if 'Date' in df.columns else f'day{d}'
        fname = OUTPUT_DIR / f'committed_{date_str}.csv'
        df.to_csv(fname, index=False)
        print(f"  Committed routes → {fname}")

        # Per-truck summary for dispatching
        for truck in TRUCK_NAMES:
            sub = df[df['Truck'] == truck]
            if sub.empty:
                continue
            n = len(sub)
            load = sub['Refill_lbs'].sum()
            dist = sub['Route_Dist_mi'].iloc[0]
            time_m = sub['Route_Time_min'].iloc[0]
            print(f"    {truck}: {n} stops, {load:,} lbs, "
                  f"{dist:.0f} mi, {time_m} min")

    # ── Output tentative preview ────────────────────────────────────────
    for d, df in tentative.items():
        if df.empty:
            continue
        date_str = df['Date'].iloc[0] if 'Date' in df.columns else f'day{d}'
        fname = OUTPUT_DIR / f'tentative_{date_str}.csv'
        df.to_csv(fname, index=False)

    # ── Save planned deliveries for next-day state update ───────────────
    committed_ids = []
    for d, df in committed.items():
        if not df.empty:
            committed_ids.extend(df['ID'].tolist())

    plan_file = OUTPUT_DIR / 'planned_deliveries.json'
    with open(plan_file, 'w') as f:
        json.dump({
            'plan_date': today.strftime('%Y-%m-%d'),
            'committed_ids': list(set(committed_ids)),
            'commit_days': commit_days,
            'horizon_days': horizon_days,
        }, f, indent=2)
    print(f"\n  Plan saved → {plan_file}")

    # ── At-risk report ──────────────────────────────────────────────────
    _print_risk_report(snapshot, committed, tentative, deferred, today)

    print(f"\n{'━' * 80}")
    print(f"  Done. Dispatch committed routes to Smart Service.")
    print(f"  Re-run tomorrow afternoon with --update-state for next cycle.")
    print(f"{'━' * 80}\n")

    return committed, tentative, deferred


def _print_risk_report(snapshot, committed, tentative, deferred, today):
    """Print clients at risk of stockout — are they committed, tentative, or deferred?"""
    if snapshot is None or snapshot.empty or 'Days_Until_Stockout' not in snapshot.columns:
        return

    committed_ids = set()
    tentative_ids = set()
    for d, df in committed.items():
        if not df.empty:
            committed_ids.update(df['ID'].tolist())
    for d, df in tentative.items():
        if not df.empty:
            tentative_ids.update(df['ID'].tolist())

    at_risk = snapshot[snapshot['Days_Until_Stockout'] <= 7].sort_values('Days_Until_Stockout')

    if at_risk.empty:
        print("\n  No clients at risk of stockout within 7 days.")
        return

    print(f"\n  At-risk clients (stockout ≤ 7 days):")
    print(f"  {'ID':<7} {'Customer':<28} {'DTE':>5} {'Status':<12}")
    print(f"  {'─' * 56}")
    for _, row in at_risk.head(20).iterrows():
        cid = row['ID']
        dte = float(row['Days_Until_Stockout'])
        if cid in committed_ids:
            status = 'COMMITTED'
        elif cid in tentative_ids:
            status = 'tentative'
        else:
            status = 'DEFERRED'
        cust = str(row.get('Customer', ''))[:28]
        print(f"  {cid:<7} {cust:<28} {dte:>5.1f} {status:<12}")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='S&K Daily Rolling Horizon Planner')
    parser.add_argument('--today', default=None,
                        help='Planning date (YYYY-MM-DD). Default: actual today.')
    parser.add_argument('--horizon', type=int, default=None,
                        help=f'Horizon length in working days (default: {HORIZON_DAYS})')
    parser.add_argument('--commit', type=int, default=None,
                        help=f'Commit window in days (default: {COMMIT_DAYS})')
    parser.add_argument('--solve-sec', type=int, default=None,
                        help=f'Solver time limit (default: {SOLVE_SEC_WEEK}s)')
    parser.add_argument('--update-state', action='store_true',
                        help='Age inventory state by 1 day before planning')
    parser.add_argument('--delivered', nargs='*', default=None,
                        help='Client IDs delivered today (used with --update-state)')

    args = parser.parse_args()
    run_daily(
        today_str=args.today,
        horizon_days=args.horizon,
        commit_days=args.commit,
        solve_sec=args.solve_sec,
        do_update_state=args.update_state,
        delivered_ids=args.delivered,
    )
