"""
diagnostics.py
==============
Post-solve KPI computation, warning flags, and deferred-client reporting.

Call generate_report() after plan_week() to get a printable summary and a
diagnostics DataFrame that can be included as an Excel sheet.
"""

import pandas as pd
import numpy as np
from typing import Dict, Optional
from config import (
    DAYS, NUM_DAYS, TRUCKS, TRUCK_NAMES, SHIFT_MIN, MIN_FILL_PCT, CRITICAL_DAYS,
)


def generate_report(
    all_routes:  Dict[int, pd.DataFrame],
    assignment:  Optional[pd.DataFrame] = None,
    clients_df:  Optional[pd.DataFrame] = None,
) -> pd.DataFrame:
    """
    Compute and print a full diagnostics report.

    Returns a DataFrame suitable for writing to an Excel 'Diagnostics' sheet.
    """
    # Combine all route records
    all_data = [r for r in all_routes.values() if r is not None and not r.empty]
    if not all_data:
        print("  No routes to diagnose.")
        return pd.DataFrame()

    routes = pd.concat(all_data, ignore_index=True)

    print("\n" + "═" * 60)
    print("  WEEKLY DIAGNOSTICS")
    print("═" * 60)

    kpis = _compute_kpis(routes, assignment, clients_df)
    _print_kpis(kpis)

    warnings = _generate_warnings(routes, assignment)
    if warnings:
        print(f"\n  ⚠  {len(warnings)} Warning(s):")
        for w in warnings:
            print(f"     • {w}")

    deferred_report = _deferred_summary(assignment, clients_df)
    if not deferred_report.empty:
        print(f"\n  Deferred clients: {len(deferred_report)}")
        print(deferred_report[
            ['ID', 'Customer', 'Zone', 'Days_Until_Stockout', 'ProjectedRefill_lbs', 'AssignedDay']
        ].to_string(index=False))

    # Pack everything into a single diagnostics DataFrame
    diag_rows = []
    for k, v in kpis.items():
        diag_rows.append({'Metric': k, 'Value': v})
    for w in warnings:
        diag_rows.append({'Metric': 'WARNING', 'Value': w})

    return pd.DataFrame(diag_rows)


def get_deferred(assignment: Optional[pd.DataFrame]) -> pd.DataFrame:
    """Return clients that were not assigned to any day."""
    if assignment is None:
        return pd.DataFrame()
    deferred = assignment[
        assignment['AssignedDay'].isin(['Deferred', 'OVERFLOW'])
    ].copy()
    # Sort by urgency (most urgent first)
    return deferred.sort_values('Days_Until_Stockout').reset_index(drop=True)


# ── KPIs ──────────────────────────────────────────────────────────────────────

def _compute_kpis(
    routes:     pd.DataFrame,
    assignment: Optional[pd.DataFrame],
    clients_df: Optional[pd.DataFrame],
) -> dict:
    kpis = {}

    # Volume
    kpis['Total_Lbs_Delivered']  = int(routes['Refill_lbs'].sum())
    kpis['Total_Stops']          = int(len(routes))
    kpis['Avg_Fill_Pct']         = round(routes['Fill_Pct'].mean(), 1)
    kpis['Min_Fill_Pct']         = round(routes['Fill_Pct'].min(), 1)
    kpis['Stops_Below_85pct']    = int((routes['Fill_Pct'] < MIN_FILL_PCT * 100).sum())

    # Fleet utilisation
    for truck in TRUCK_NAMES:
        sub = routes[routes['Truck'] == truck]
        if sub.empty:
            continue
        days_active = sub['Day'].nunique()
        avg_shift   = sub.groupby('Day')['Route_Time_min'].first().mean()
        avg_cap     = sub.groupby('Day')['Refill_lbs'].sum().mean()
        kpis[f'{truck}_Days_Active']   = int(days_active)
        kpis[f'{truck}_Avg_Shift_Pct'] = round(avg_shift / SHIFT_MIN * 100, 1)
        kpis[f'{truck}_Avg_Cap_Pct']   = round(avg_cap / TRUCKS[truck]['capacity_lbs'] * 100, 1)

    # Urgency coverage
    critical_visited = int(
        routes[routes['Urgency'].isin(['critical', 'stockout'])].shape[0]
    )
    kpis['Critical_Stops_Served'] = critical_visited

    # Deferred
    if assignment is not None:
        deferred = get_deferred(assignment)
        kpis['Clients_Deferred'] = int(len(deferred))
        crit_deferred = int(
            deferred[deferred['Days_Until_Stockout'] <= CRITICAL_DAYS].shape[0]
        )
        kpis['Critical_Clients_Deferred'] = crit_deferred

    return kpis


def _print_kpis(kpis: dict) -> None:
    print(f"\n  Volume:")
    print(f"    Total lbs delivered : {kpis.get('Total_Lbs_Delivered', 0):>10,}")
    print(f"    Total stops         : {kpis.get('Total_Stops', 0):>10}")
    print(f"    Avg fill %          : {kpis.get('Avg_Fill_Pct', 0):>9.1f}%")
    print(f"    Min fill %          : {kpis.get('Min_Fill_Pct', 0):>9.1f}%")
    stops_below = kpis.get('Stops_Below_85pct', 0)
    flag = '  ← review' if stops_below else ''
    print(f"    Stops < 85% fill    : {stops_below:>10}{flag}")

    print(f"\n  Fleet utilisation:")
    for truck in TRUCK_NAMES:
        days   = kpis.get(f'{truck}_Days_Active', 0)
        shift  = kpis.get(f'{truck}_Avg_Shift_Pct', 0)
        cap    = kpis.get(f'{truck}_Avg_Cap_Pct', 0)
        print(f"    {truck}: {days} day(s) active | "
              f"avg shift {shift:.0f}% | avg cap {cap:.0f}%")

    print(f"\n  Coverage:")
    print(f"    Critical stops served : {kpis.get('Critical_Stops_Served', 0)}")
    print(f"    Clients deferred      : {kpis.get('Clients_Deferred', 0)}")
    crit_def = kpis.get('Critical_Clients_Deferred', 0)
    if crit_def:
        print(f"    ⚠  Critical clients deferred : {crit_def}  ← MANUAL ACTION NEEDED")


def _generate_warnings(
    routes:     pd.DataFrame,
    assignment: Optional[pd.DataFrame],
) -> list:
    warnings = []

    # Shift overruns
    for truck in TRUCK_NAMES:
        for day in DAYS:
            sub = routes[(routes['Truck'] == truck) & (routes['Day'] == day)]
            if sub.empty:
                continue
            t = sub['Route_Time_min'].iloc[0]
            if t > SHIFT_MIN:
                overtime = t - SHIFT_MIN
                warnings.append(
                    f"{truck} {day}: route time {t} min exceeds "
                    f"shift cap by {overtime} min"
                )

    # Top-off stops (fill < threshold)
    topoffs = routes[routes['Fill_Pct'] < MIN_FILL_PCT * 100]
    for _, r in topoffs.iterrows():
        warnings.append(
            f"Top-off detected: {r['Customer'][:35]} on {r['Truck']} "
            f"{r['Day']} — only {r['Fill_Pct']:.0f}% fill"
        )

    # Deferred critical clients
    if assignment is not None:
        deferred = get_deferred(assignment)
        crit = deferred[deferred['Days_Until_Stockout'] <= CRITICAL_DAYS]
        for _, r in crit.iterrows():
            warnings.append(
                f"CRITICAL client deferred — {r['ID']} {r['Customer'][:35]}: "
                f"stockout in {r['Days_Until_Stockout']:.1f}d"
            )

    return warnings


def _deferred_summary(
    assignment: Optional[pd.DataFrame],
    clients_df: Optional[pd.DataFrame],
) -> pd.DataFrame:
    if assignment is None:
        return pd.DataFrame()
    deferred = get_deferred(assignment)
    if deferred.empty:
        return pd.DataFrame()

    cols = [c for c in [
        'ID', 'Customer', 'Zone', 'Days_Until_Stockout',
        'Fill_Pct_Today', 'ProjectedRefill_lbs', 'Tank_lbs',
        'Avg_LbsPerDay', 'AssignedDay',
    ] if c in deferred.columns]

    return deferred[cols].sort_values('Days_Until_Stockout').reset_index(drop=True)
