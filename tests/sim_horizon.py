#!/usr/bin/env python3
"""
sim_horizon.py — Rolling Horizon Simulation
============================================
Simulates the daily rolling-horizon planning cycle over N weeks.

Each delivery day:
  1. Solve a rolling horizon (HORIZON_DAYS lookahead, commit Day 0).
  2. Execute committed routes: refill those clients' tanks.
  3. Deplete ALL client tanks by one day of consumption.
  4. Advance the calendar by one day.
  5. Repeat.

Compares against the old weekly-batch approach (sim_rolling.py) to show
improvements: fewer stockouts, better efficiency, no Friday dip.

Usage:
    python tests/sim_horizon.py                         # 3 weeks from 2026-04-14
    python tests/sim_horizon.py --weeks 5 --horizon 5   # 5 weeks, 5-day horizon
    python tests/sim_horizon.py --compare               # Side-by-side with weekly
"""

import sys
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
import numpy as np
from config import (
    INPUT_FILE, MATRIX_FILE, DAYS, NUM_DAYS,
    EXCLUDED_CLIENT_IDS, HORIZON_DAYS, COMMIT_DAYS,
)
from load_data import load_all
from forecast_consumption import estimate_consumption_rates
from inventory import enrich_snapshot
from router import load_matrix
from state import initialise_state_from_snapshot
from schema_loaders import load_time_windows, load_closures, load_depot_config
from unified_solver import solve_horizon, solve_week, compute_horizon_dates

_WEEKDAY_SHORT = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']


def _init_simulation(start_date):
    """Load data and initialise inventory — shared by both modes."""
    clients_raw, deliveries = load_all(INPUT_FILE)
    dm, tm, nim = load_matrix(MATRIX_FILE)
    tw = load_time_windows(INPUT_FILE)
    cl = load_closures(INPUT_FILE)
    dc = load_depot_config(INPUT_FILE)

    today = pd.Timestamp(start_date).normalize()
    clients_df = estimate_consumption_rates(deliveries, clients_raw, today=today)
    state = initialise_state_from_snapshot(clients_df)
    snapshot = enrich_snapshot(clients_df, state)

    # Build mutable inventory tracker
    inventory = {}
    rates = {}
    tanks = {}
    for _, row in snapshot.iterrows():
        cid = row['ID']
        inventory[cid] = float(row.get('Current_lbs', 0))
        rates[cid] = float(row.get('Avg_LbsPerDay', 0))
        tanks[cid] = float(row.get('Tank_lbs', 0))

    return {
        'snapshot': snapshot, 'dm': dm, 'tm': tm, 'nim': nim,
        'tw': tw, 'cl': cl, 'dc': dc,
        'inventory': inventory, 'rates': rates, 'tanks': tanks,
        'today': today,
    }


def _update_snapshot(data, inventory, rates, tanks):
    """Overlay current inventory onto snapshot for solver input."""
    snap = data['snapshot'].copy()
    for i, row in snap.iterrows():
        cid = row['ID']
        if cid in inventory:
            cur = max(0, inventory[cid])
            snap.at[i, 'Current_lbs'] = cur
            tank = tanks.get(cid, 1)
            rate = rates.get(cid, 0)
            snap.at[i, 'Refill_lbs'] = max(0, tank - cur)
            snap.at[i, 'Days_Until_Stockout'] = cur / rate if rate > 0 else 999
            from inventory import urgency_tier
            snap.at[i, 'Urgency'] = urgency_tier(cur / rate if rate > 0 else 999)
    return snap


def _execute_day0(routes, inventory, tanks, client_visits, current_date):
    """Execute Day 0 committed routes: refill tanks, track visits."""
    day0 = routes.get(0, pd.DataFrame())
    stops = 0
    miles = 0.0
    lbs = 0
    ot = 0
    clients_served = []

    if not day0.empty:
        stops = len(day0)
        for truck in day0['Truck'].unique():
            sub = day0[day0['Truck'] == truck]
            miles += sub['Route_Dist_mi'].iloc[0]
            ot += sub['OT_Min'].iloc[0] if 'OT_Min' in sub.columns else 0

        for _, stop in day0.iterrows():
            cid = stop['ID']
            refill = int(stop['Refill_lbs'])
            lbs += refill
            clients_served.append(cid)
            tank = tanks.get(cid, 10000)
            inventory[cid] = min(tank, inventory.get(cid, 0) + refill)
            if cid not in client_visits:
                client_visits[cid] = []
            client_visits[cid].append(current_date)

    return stops, miles, lbs, ot, clients_served


def run_horizon_simulation(start_date, n_weeks=3, horizon_days=None, solve_sec=12):
    """Run rolling-horizon simulation."""
    if horizon_days is None:
        horizon_days = HORIZON_DAYS

    data = _init_simulation(start_date)
    inventory = data['inventory']
    rates = data['rates']
    tanks = data['tanks']
    workday_set = set(DAYS)
    total_delivery_days = n_weeks * NUM_DAYS

    day_results = []
    client_visits = {}
    weekly_summaries = []

    print(f"\n{'═' * 80}")
    print(f"  Rolling Horizon Simulation: {start_date} → {n_weeks} weeks")
    print(f"  Mode: {horizon_days}-day horizon, commit 1 day")
    print(f"{'═' * 80}")

    delivery_day_count = 0
    current_date = pd.Timestamp(start_date).normalize() + pd.Timedelta(days=1)
    week_stops = week_miles = week_lbs = week_ot = 0
    week_num = 1
    days_in_week = 0

    while delivery_day_count < total_delivery_days:
        day_name = _WEEKDAY_SHORT[current_date.weekday()]
        if day_name not in workday_set:
            for cid in inventory:
                inventory[cid] = max(0, inventory[cid] - rates.get(cid, 0))
            current_date += pd.Timedelta(days=1)
            continue

        delivery_day_count += 1
        days_in_week += 1

        snap = _update_snapshot(data, inventory, rates, tanks)

        try:
            committed, tentative, deferred = solve_horizon(
                snap, data['dm'], data['tm'], data['nim'],
                today=current_date - pd.Timedelta(days=1),
                horizon_days=horizon_days,
                commit_days=1,
                solve_seconds=solve_sec,
                time_windows_df=data['tw'],
                closures_df=data['cl'],
                depot_config=data['dc'],
            )
        except Exception as e:
            print(f"  ERROR on {current_date.date()}: {e}")
            for cid in inventory:
                inventory[cid] = max(0, inventory[cid] - rates.get(cid, 0))
            current_date += pd.Timedelta(days=1)
            continue

        day_stops, day_miles, day_lbs, day_ot, day_clients = _execute_day0(
            committed, inventory, tanks, client_visits, current_date)

        # Deplete all tanks
        for cid in inventory:
            inventory[cid] = max(0, inventory[cid] - rates.get(cid, 0))

        n_stockout = sum(1 for cid, lvl in inventory.items()
                         if lvl <= 0 and rates.get(cid, 0) > 0
                         and cid not in EXCLUDED_CLIENT_IDS)

        day_results.append({
            'date': current_date, 'day_name': day_name,
            'stops': day_stops, 'miles': day_miles,
            'lbs': day_lbs, 'ot_min': day_ot, 'stockouts': n_stockout,
        })

        week_stops += day_stops
        week_miles += day_miles
        week_lbs += day_lbs
        week_ot += day_ot

        mi_ps = day_miles / day_stops if day_stops else 0
        print(f"  {current_date.date()} {day_name}: {day_stops:>3} stops  "
              f"{day_miles:>6.0f} mi  {day_lbs:>6,} lbs  "
              f"{mi_ps:>5.1f} mi/stp  OT={day_ot:>3}m  "
              f"stockouts={n_stockout}")

        if days_in_week >= NUM_DAYS:
            mi_ps_w = week_miles / week_stops if week_stops else 0
            weekly_summaries.append({
                'week': week_num, 'stops': week_stops, 'miles': week_miles,
                'lbs': week_lbs, 'ot_min': week_ot, 'mi_per_stop': mi_ps_w,
            })
            print(f"  ── Week {week_num}: {week_stops} stops, {week_miles:.0f} mi, "
                  f"{week_lbs:,} lbs, {mi_ps_w:.1f} mi/stop, OT={week_ot}m ──")
            week_stops = week_miles = week_lbs = week_ot = 0
            week_num += 1
            days_in_week = 0

        current_date += pd.Timedelta(days=1)

    _print_summary(n_weeks, weekly_summaries, day_results, client_visits,
                   inventory, rates, tanks, "Rolling Horizon")
    return day_results, weekly_summaries, client_visits


def run_weekly_simulation(start_date, n_weeks=3, solve_sec=12):
    """Run weekly-batch simulation (baseline for comparison)."""
    from run_unified import compute_plan_dates

    data = _init_simulation(start_date)
    inventory = data['inventory']
    rates = data['rates']
    tanks = data['tanks']
    workday_set = set(DAYS)
    total_delivery_days = n_weeks * NUM_DAYS

    day_results = []
    client_visits = {}
    weekly_summaries = []

    print(f"\n{'═' * 80}")
    print(f"  Weekly Batch Simulation: {start_date} → {n_weeks} weeks")
    print(f"  Mode: full 5-day plan, re-solve each day")
    print(f"{'═' * 80}")

    delivery_day_count = 0
    current_date = pd.Timestamp(start_date).normalize() + pd.Timedelta(days=1)
    week_stops = week_miles = week_lbs = week_ot = 0
    week_num = 1
    days_in_week = 0

    while delivery_day_count < total_delivery_days:
        day_name = _WEEKDAY_SHORT[current_date.weekday()]
        if day_name not in workday_set:
            for cid in inventory:
                inventory[cid] = max(0, inventory[cid] - rates.get(cid, 0))
            current_date += pd.Timedelta(days=1)
            continue

        delivery_day_count += 1
        days_in_week += 1

        snap = _update_snapshot(data, inventory, rates, tanks)
        plan_dates = compute_plan_dates(current_date - pd.Timedelta(days=1))

        try:
            routes, deferred = solve_week(
                snap, data['dm'], data['tm'], data['nim'],
                solve_seconds=solve_sec,
                time_windows_df=data['tw'], closures_df=data['cl'],
                today=current_date - pd.Timedelta(days=1),
                depot_config=data['dc'], plan_dates=plan_dates,
            )
        except Exception as e:
            print(f"  ERROR on {current_date.date()}: {e}")
            for cid in inventory:
                inventory[cid] = max(0, inventory[cid] - rates.get(cid, 0))
            current_date += pd.Timedelta(days=1)
            continue

        day_stops, day_miles, day_lbs, day_ot, day_clients = _execute_day0(
            routes, inventory, tanks, client_visits, current_date)

        for cid in inventory:
            inventory[cid] = max(0, inventory[cid] - rates.get(cid, 0))

        n_stockout = sum(1 for cid, lvl in inventory.items()
                         if lvl <= 0 and rates.get(cid, 0) > 0
                         and cid not in EXCLUDED_CLIENT_IDS)

        day_results.append({
            'date': current_date, 'day_name': day_name,
            'stops': day_stops, 'miles': day_miles,
            'lbs': day_lbs, 'ot_min': day_ot, 'stockouts': n_stockout,
        })

        week_stops += day_stops
        week_miles += day_miles
        week_lbs += day_lbs
        week_ot += day_ot

        mi_ps = day_miles / day_stops if day_stops else 0
        print(f"  {current_date.date()} {day_name}: {day_stops:>3} stops  "
              f"{day_miles:>6.0f} mi  {day_lbs:>6,} lbs  "
              f"{mi_ps:>5.1f} mi/stp  OT={day_ot:>3}m  "
              f"stockouts={n_stockout}")

        if days_in_week >= NUM_DAYS:
            mi_ps_w = week_miles / week_stops if week_stops else 0
            weekly_summaries.append({
                'week': week_num, 'stops': week_stops, 'miles': week_miles,
                'lbs': week_lbs, 'ot_min': week_ot, 'mi_per_stop': mi_ps_w,
            })
            print(f"  ── Week {week_num}: {week_stops} stops, {week_miles:.0f} mi, "
                  f"{week_lbs:,} lbs, {mi_ps_w:.1f} mi/stop, OT={week_ot}m ──")
            week_stops = week_miles = week_lbs = week_ot = 0
            week_num += 1
            days_in_week = 0

        current_date += pd.Timedelta(days=1)

    _print_summary(n_weeks, weekly_summaries, day_results, client_visits,
                   inventory, rates, tanks, "Weekly Batch")
    return day_results, weekly_summaries, client_visits


def _print_summary(n_weeks, weekly_summaries, day_results, client_visits,
                   inventory, rates, tanks, mode_label):
    """Print simulation summary."""
    print(f"\n{'═' * 80}")
    print(f"  {mode_label} SUMMARY ({n_weeks} weeks)")
    print(f"{'═' * 80}")

    print(f"\n  Weekly totals:")
    print(f"  {'Week':>4} {'Stops':>6} {'Miles':>7} {'Lbs':>9} {'mi/stop':>8} {'OT min':>7}")
    print(f"  {'-'*45}")
    for ws in weekly_summaries:
        print(f"  {ws['week']:>4} {ws['stops']:>6} {ws['miles']:>7.0f} "
              f"{ws['lbs']:>9,} {ws['mi_per_stop']:>8.1f} {ws['ot_min']:>7}")

    if weekly_summaries:
        avg_stops = np.mean([w['stops'] for w in weekly_summaries])
        avg_miles = np.mean([w['miles'] for w in weekly_summaries])
        avg_mps = np.mean([w['mi_per_stop'] for w in weekly_summaries])
        print(f"  {'avg':>4} {avg_stops:>6.0f} {avg_miles:>7.0f} "
              f"{'':>9} {avg_mps:>8.1f}")

    # Client coverage
    all_active = [cid for cid, rate in rates.items()
                  if rate > 0 and cid not in EXCLUDED_CLIENT_IDS
                  and tanks.get(cid, 0) > 0]
    visit_counts = {cid: len(client_visits.get(cid, [])) for cid in all_active}
    n_never = sum(1 for c in visit_counts.values() if c == 0)
    n_multi = sum(1 for c in visit_counts.values() if c >= 2)
    avg_visits = np.mean(list(visit_counts.values())) if visit_counts else 0
    print(f"\n  Coverage ({len(all_active)} active):")
    print(f"    Never visited: {n_never} | Visited 2+: {n_multi} | Avg: {avg_visits:.1f}")

    # Stockouts
    stockout_days = [d['stockouts'] for d in day_results]
    print(f"\n  Stockouts:")
    print(f"    Max: {max(stockout_days)} | Avg: {np.mean(stockout_days):.1f} | "
          f"Zero-days: {sum(1 for s in stockout_days if s == 0)}/{len(stockout_days)}")

    # Day balance
    day_stops_by_name = {}
    for d in day_results:
        day_stops_by_name.setdefault(d['day_name'], []).append(d['stops'])
    print(f"\n  Day balance (avg stops):")
    for dn in DAYS:
        vals = day_stops_by_name.get(dn, [])
        if vals:
            print(f"    {dn}: {np.mean(vals):.0f} stops (range {min(vals)}–{max(vals)})")

    print()


def compare_modes(start_date, n_weeks=3, solve_sec=12, horizon_days=None):
    """Run both modes and print comparison."""
    print(f"\n{'▓' * 80}")
    print(f"  A/B COMPARISON: Rolling Horizon vs Weekly Batch")
    print(f"{'▓' * 80}")

    # Run weekly batch
    w_results, w_weekly, w_visits = run_weekly_simulation(start_date, n_weeks, solve_sec)

    # Run rolling horizon
    h_results, h_weekly, h_visits = run_horizon_simulation(
        start_date, n_weeks, horizon_days, solve_sec)

    # Side-by-side comparison
    print(f"\n{'▓' * 80}")
    print(f"  HEAD-TO-HEAD COMPARISON")
    print(f"{'▓' * 80}")

    w_avg_miles = np.mean([w['miles'] for w in w_weekly]) if w_weekly else 0
    h_avg_miles = np.mean([w['miles'] for w in h_weekly]) if h_weekly else 0
    w_avg_mps = np.mean([w['mi_per_stop'] for w in w_weekly]) if w_weekly else 0
    h_avg_mps = np.mean([w['mi_per_stop'] for w in h_weekly]) if h_weekly else 0
    w_avg_stockouts = np.mean([d['stockouts'] for d in w_results])
    h_avg_stockouts = np.mean([d['stockouts'] for d in h_results])

    print(f"\n  {'Metric':<25} {'Weekly':>10} {'Horizon':>10} {'Delta':>10}")
    print(f"  {'─' * 58}")
    print(f"  {'Avg weekly miles':<25} {w_avg_miles:>10.0f} {h_avg_miles:>10.0f} "
          f"{h_avg_miles - w_avg_miles:>+10.0f}")
    print(f"  {'Avg mi/stop':<25} {w_avg_mps:>10.1f} {h_avg_mps:>10.1f} "
          f"{h_avg_mps - w_avg_mps:>+10.1f}")
    print(f"  {'Avg stockouts/day':<25} {w_avg_stockouts:>10.1f} {h_avg_stockouts:>10.1f} "
          f"{h_avg_stockouts - w_avg_stockouts:>+10.1f}")

    # Day balance comparison
    for mode_label, results in [('Weekly', w_results), ('Horizon', h_results)]:
        stops_by_day = {}
        for d in results:
            stops_by_day.setdefault(d['day_name'], []).append(d['stops'])
        stds = [np.std(v) for v in stops_by_day.values() if v]
        print(f"  {mode_label + ' day StdDev':<25} {np.mean(stds):>10.1f}")


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--start', default='2026-04-14')
    parser.add_argument('--weeks', type=int, default=3)
    parser.add_argument('--horizon', type=int, default=None)
    parser.add_argument('--solve-sec', type=int, default=12)
    parser.add_argument('--compare', action='store_true',
                        help='Run both modes and compare')
    args = parser.parse_args()

    if args.compare:
        compare_modes(args.start, args.weeks, args.solve_sec, args.horizon)
    else:
        run_horizon_simulation(args.start, args.weeks, args.horizon, args.solve_sec)
