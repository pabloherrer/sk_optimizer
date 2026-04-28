#!/usr/bin/env python3
"""
sim_rolling.py — Rolling day-by-day simulation
===============================================
Simulates real operations over N weeks:

  1. Run the unified solver → get a weekly plan
  2. "Execute" Day 0 deliveries: refill those clients' tanks
  3. Deplete ALL client tanks by one day of consumption
  4. Advance the calendar by one delivery day
  5. Repeat

This reveals steady-state behavior: does mileage stabilize? Do some
clients get chronically under-served? Does day distribution balance out?

Usage:
    python tests/sim_rolling.py                     # 3 weeks from 2026-04-14
    python tests/sim_rolling.py --start 2026-04-20  # custom start
    python tests/sim_rolling.py --weeks 5           # 5 weeks
"""

import sys
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
import numpy as np
from config import (
    INPUT_FILE, MATRIX_FILE, DAYS, NUM_DAYS,
    EXCLUDED_CLIENT_IDS, SHIFT_MIN,
)
from load_data import load_all
from forecast_consumption import estimate_consumption_rates
from inventory import enrich_snapshot
from router import load_matrix
from state import initialise_state_from_snapshot
from schema_loaders import load_time_windows, load_closures, load_depot_config
from unified_solver import solve_week
from run_unified import compute_plan_dates


_WEEKDAY_SHORT = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']


def run_rolling_simulation(start_date: str, n_weeks: int = 3, solve_sec: int = 12):
    """Run a rolling day-by-day simulation."""

    # ── Load static data (once) ──────────────────────────────────────────
    clients_raw, deliveries = load_all(INPUT_FILE)
    dm, tm, nim = load_matrix(MATRIX_FILE)
    tw = load_time_windows(INPUT_FILE)
    cl = load_closures(INPUT_FILE)
    dc = load_depot_config(INPUT_FILE)

    today = pd.Timestamp(start_date).normalize()

    # Initial consumption estimation
    clients_df = estimate_consumption_rates(deliveries, clients_raw, today=today)
    state = initialise_state_from_snapshot(clients_df)
    snapshot = enrich_snapshot(clients_df, state)

    # Build a mutable inventory tracker: {client_id: current_lbs}
    inventory = {}
    rates = {}       # {client_id: avg_lbs_per_day}
    tanks = {}       # {client_id: tank_lbs}
    for _, row in snapshot.iterrows():
        cid = row['ID']
        inventory[cid] = float(row.get('Current_lbs', 0))
        rates[cid] = float(row.get('Avg_LbsPerDay', 0))
        tanks[cid] = float(row.get('Tank_lbs', 0))

    workday_set = set(DAYS)
    total_delivery_days = n_weeks * NUM_DAYS

    # ── Results tracking ─────────────────────────────────────────────────
    day_results = []       # per-delivery-day summary
    client_visits = {}     # {client_id: [list of dates visited]}
    weekly_summaries = []

    print(f"\n{'═' * 80}")
    print(f"  Rolling Simulation: {start_date} → {n_weeks} weeks ({total_delivery_days} delivery days)")
    print(f"{'═' * 80}")

    delivery_day_count = 0
    current_date = today + pd.Timedelta(days=1)  # start from tomorrow

    # Track weekly accumulators
    week_stops = 0
    week_miles = 0.0
    week_lbs = 0
    week_ot = 0
    week_num = 1
    days_in_week = 0

    while delivery_day_count < total_delivery_days:
        # Skip non-delivery days (Mon, Sun)
        day_name = _WEEKDAY_SHORT[current_date.weekday()]
        if day_name not in workday_set:
            # Still deplete tanks on non-delivery days
            for cid in inventory:
                inventory[cid] = max(0, inventory[cid] - rates.get(cid, 0))
            current_date += pd.Timedelta(days=1)
            continue

        delivery_day_count += 1
        days_in_week += 1

        # ── Update snapshot with current inventory ──���────────────────
        snap = snapshot.copy()
        for i, row in snap.iterrows():
            cid = row['ID']
            if cid in inventory:
                snap.at[i, 'Current_lbs'] = max(0, inventory[cid])
                tank = tanks.get(cid, 1)
                cur = max(0, inventory[cid])
                rate = rates.get(cid, 0)
                snap.at[i, 'Refill_lbs'] = max(0, tank - cur)
                snap.at[i, 'Days_Until_Stockout'] = cur / rate if rate > 0 else 999
                from inventory import urgency_tier
                snap.at[i, 'Urgency'] = urgency_tier(cur / rate if rate > 0 else 999)

        # ── Run solver for the week starting from this day ───────────
        plan_dates = compute_plan_dates(current_date - pd.Timedelta(days=1))

        try:
            routes, deferred = solve_week(
                snap, dm, tm, nim, solve_seconds=solve_sec,
                time_windows_df=tw, closures_df=cl,
                today=current_date - pd.Timedelta(days=1),
                depot_config=dc, plan_dates=plan_dates,
            )
        except Exception as e:
            print(f"  ERROR on {current_date.date()}: {e}")
            # Deplete and advance
            for cid in inventory:
                inventory[cid] = max(0, inventory[cid] - rates.get(cid, 0))
            current_date += pd.Timedelta(days=1)
            continue

        # ── Extract Day 0 deliveries and "execute" them ──────────────
        day0_route = routes.get(0, pd.DataFrame())
        day_stops = 0
        day_miles = 0.0
        day_lbs = 0
        day_ot = 0
        day_clients_served = []

        if not day0_route.empty:
            day_stops = len(day0_route)
            # Miles per truck-route on this day
            for truck in day0_route['Truck'].unique():
                sub = day0_route[day0_route['Truck'] == truck]
                day_miles += sub['Route_Dist_mi'].iloc[0]
                day_ot += sub['OT_Min'].iloc[0] if 'OT_Min' in sub.columns else 0

            # Execute deliveries: refill tanks
            for _, stop in day0_route.iterrows():
                cid = stop['ID']
                refill = int(stop['Refill_lbs'])
                day_lbs += refill
                day_clients_served.append(cid)

                # Update inventory: add refill (cap at tank size)
                tank = tanks.get(cid, 10000)
                inventory[cid] = min(tank, inventory.get(cid, 0) + refill)

                # Track visits
                if cid not in client_visits:
                    client_visits[cid] = []
                client_visits[cid].append(current_date)

        # ── Deplete ALL tanks by one day of consumption ─���────────────
        for cid in inventory:
            inventory[cid] = max(0, inventory[cid] - rates.get(cid, 0))

        # ── Count stockouts ──────────────────────────────────────────
        n_stockout = sum(1 for cid, lvl in inventory.items()
                        if lvl <= 0 and rates.get(cid, 0) > 0
                        and cid not in EXCLUDED_CLIENT_IDS)

        day_results.append({
            'date': current_date,
            'day_name': day_name,
            'stops': day_stops,
            'miles': day_miles,
            'lbs': day_lbs,
            'ot_min': day_ot,
            'stockouts': n_stockout,
            'clients_served': day_clients_served,
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

        # ── End of week? ─────────────────────────────────────────────
        if days_in_week >= NUM_DAYS:
            mi_ps_w = week_miles / week_stops if week_stops else 0
            weekly_summaries.append({
                'week': week_num,
                'stops': week_stops,
                'miles': week_miles,
                'lbs': week_lbs,
                'ot_min': week_ot,
                'mi_per_stop': mi_ps_w,
            })
            print(f"  ── Week {week_num} total: {week_stops} stops, {week_miles:.0f} mi, "
                  f"{week_lbs:,} lbs, {mi_ps_w:.1f} mi/stop, OT={week_ot}m ──")
            week_stops = 0
            week_miles = 0.0
            week_lbs = 0
            week_ot = 0
            week_num += 1
            days_in_week = 0

        current_date += pd.Timedelta(days=1)

    # ── Final summary ────────────────────────────────────────────────────
    print(f"\n{'═' * 80}")
    print(f"  SIMULATION SUMMARY ({n_weeks} weeks)")
    print(f"{'═' * 80}")

    print(f"\n  Weekly totals:")
    print(f"  {'Week':>4} {'Stops':>6} {'Miles':>7} {'Lbs':>9} {'mi/stop':>8} {'OT min':>7}")
    print(f"  {'-'*45}")
    for ws in weekly_summaries:
        print(f"  {ws['week']:>4} {ws['stops']:>6} {ws['miles']:>7.0f} "
              f"{ws['lbs']:>9,} {ws['mi_per_stop']:>8.1f} {ws['ot_min']:>7}")

    avg_stops = np.mean([w['stops'] for w in weekly_summaries]) if weekly_summaries else 0
    avg_miles = np.mean([w['miles'] for w in weekly_summaries]) if weekly_summaries else 0
    avg_mps = np.mean([w['mi_per_stop'] for w in weekly_summaries]) if weekly_summaries else 0
    print(f"  {'avg':>4} {avg_stops:>6.0f} {avg_miles:>7.0f} "
          f"{'':>9} {avg_mps:>8.1f}")

    # ── Client visit frequency analysis ──────────────────────────────
    all_active = [cid for cid, rate in rates.items()
                  if rate > 0 and cid not in EXCLUDED_CLIENT_IDS
                  and tanks.get(cid, 0) > 0]
    visit_counts = {cid: len(client_visits.get(cid, [])) for cid in all_active}

    n_never = sum(1 for c in visit_counts.values() if c == 0)
    n_once = sum(1 for c in visit_counts.values() if c == 1)
    n_multi = sum(1 for c in visit_counts.values() if c >= 2)
    avg_visits = np.mean(list(visit_counts.values())) if visit_counts else 0

    print(f"\n  Client coverage ({len(all_active)} active clients, {n_weeks} weeks):")
    print(f"    Never visited:  {n_never}")
    print(f"    Visited once:   {n_once}")
    print(f"    Visited 2+:     {n_multi}")
    print(f"    Avg visits:     {avg_visits:.1f}")

    # ── Stockout analysis ────────────────────────────────────────────
    stockout_days = [d['stockouts'] for d in day_results]
    print(f"\n  Stockout exposure:")
    print(f"    Max simultaneous: {max(stockout_days)}")
    print(f"    Avg per day:      {np.mean(stockout_days):.1f}")
    print(f"    Days with 0:      {sum(1 for s in stockout_days if s == 0)}/{len(stockout_days)}")

    # ── Visit interval analysis (for clients with 2+ visits) ────────
    intervals = []
    for cid, dates_list in client_visits.items():
        if len(dates_list) >= 2:
            sorted_d = sorted(dates_list)
            for i in range(1, len(sorted_d)):
                gap = (sorted_d[i] - sorted_d[i-1]).days
                intervals.append(gap)

    if intervals:
        print(f"\n  Visit intervals (clients with 2+ visits):")
        print(f"    Min gap:  {min(intervals)} days")
        print(f"    Avg gap:  {np.mean(intervals):.1f} days")
        print(f"    Max gap:  {max(intervals)} days")
        print(f"    Median:   {np.median(intervals):.0f} days")

    # ��─ Final inventory state ────────────────────────────────────────
    final_stockouts = sum(1 for cid in all_active if inventory.get(cid, 0) <= 0)
    avg_fill_final = np.mean([
        inventory.get(cid, 0) / tanks[cid] if tanks.get(cid, 0) > 0 else 0
        for cid in all_active
    ])
    print(f"\n  Final inventory state:")
    print(f"    Clients at stockout: {final_stockouts}")
    print(f"    Avg tank fill:       {avg_fill_final*100:.1f}%")

    print()
    return day_results, weekly_summaries, client_visits


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--start', default='2026-04-14')
    parser.add_argument('--weeks', type=int, default=3)
    parser.add_argument('--solve-sec', type=int, default=12)
    args = parser.parse_args()
    run_rolling_simulation(args.start, args.weeks, args.solve_sec)
