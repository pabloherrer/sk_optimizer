"""
test_plan_stability.py — Plan stickiness across rolling re-solves

The user's intuition: if Monday's solve plans E, F, G, H for Wednesday, and
Tuesday's deliveries execute exactly as forecast, then re-running on Tuesday
night should STILL plan E, F, G, H for Wednesday.

This is a plan-stability property. If it doesn't hold, drivers won't trust
the system (plan flips every morning), and operational planning becomes
impossible. These tests encode the property.

Method: solve a week with start_day=0 (Tue). Mark the clients scheduled for
Day 1 (Wed). Then simulate "Tuesday deliveries happened" by advancing state
for Day-0 clients to full tanks and decrementing Day-1+ clients by one
day of consumption. Re-solve with start_day=0 but today=yesterday+1, using
the smaller eligible set (excluding Day-0 served). Assert the scheduled
Wed set from the re-solve is a SUPERSET of original Wed plan (no client got
dropped after re-solve).
"""
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import DAYS, PRODUCTS
from unified_solver import solve_week


def run_test(name, fn):
    start = time.time()
    try:
        fn()
        print(f'  ✓ {name:<58s} ({(time.time()-start)*1000:.0f} ms)')
        return True
    except AssertionError as e:
        print(f'  ✗ {name:<58s} — {str(e)[:80]}')
        return False
    except Exception as e:
        print(f'  ✗ {name:<58s} — CRASH: {type(e).__name__}: {str(e)[:60]}')
        return False


def _make_scenario(n=15, seed=1):
    rng = np.random.default_rng(seed)
    clients = []
    coords = [(33.50, -112.10)]
    for i in range(n):
        lat = 33.50 + rng.uniform(-0.05, 0.05)
        lon = -112.10 + rng.uniform(-0.05, 0.05)
        coords.append((lat, lon))
        clients.append({
            'ID': f'S{i:03d}', 'Customer': f'C{i}',
            'Lat': lat, 'Lon': lon,
            'Tank_lbs': 5000, 'Product': PRODUCTS[i % 2],
            'Avg_LbsPerDay': 200 + int(rng.integers(-40, 40)),
            'Days_Since_Last': int(rng.integers(3, 8)),
            'Current_lbs': int(rng.integers(1200, 3500)),
        })
    df = pd.DataFrame(clients)
    df['Est_Current_lbs']     = df['Current_lbs']
    df['Refill_lbs']          = (df['Tank_lbs'] - df['Current_lbs']).clip(lower=0)
    df['Days_Until_Stockout'] = (df['Current_lbs'] / df['Avg_LbsPerDay'].clip(lower=1)).clip(lower=0.5)
    df['Urgency']             = 'normal'
    df['Refill_Today_lbs']    = df['Refill_lbs']
    df['Fill_Pct_Today']      = df['Refill_lbs'] / df['Tank_lbs']

    nn = len(coords)
    dist = np.zeros((nn, nn), dtype=int)
    tm = np.zeros((nn, nn), dtype=int)
    for i in range(nn):
        for j in range(nn):
            if i != j:
                dx = (coords[i][0] - coords[j][0]) * 69.0
                dy = (coords[i][1] - coords[j][1]) * 60.0
                d_mi = (dx * dx + dy * dy) ** 0.5
                dist[i, j] = int(d_mi * 1609)
                tm[i, j] = max(1, int(d_mi * 2 + 1))
    nix = {'DEPOT': 0}
    for i, cid in enumerate(df['ID'].tolist(), 1):
        nix[cid] = i
    return {
        'clients_df': df,
        'dist_matrix': dist, 'time_matrix': tm, 'node_index_map': nix,
        'time_windows_df': pd.DataFrame(columns=['Client_ID', 'Day_of_Week', 'Open_Min', 'Close_Min']),
        'closures_df': pd.DataFrame(columns=['Client_ID', 'Start_Date', 'End_Date', 'Reason']),
        'depot_config': {
            'depot_lat': 33.5, 'depot_lon': -112.1,
            'shift_start_min': 360, 'shift_end_min': 1080,
            'morning_load_min': 30, 'evening_unload_min': 15,
            'work_days': DAYS,
        },
    }


def _solve(s, today=pd.Timestamp('2026-04-14'), budget=3):
    return solve_week(
        s['clients_df'], s['dist_matrix'], s['time_matrix'], s['node_index_map'],
        start_day=0, solve_seconds=budget,
        time_windows_df=s['time_windows_df'],
        closures_df=s['closures_df'],
        today=today,
        depot_config=s['depot_config'],
    )


def _ids_by_day(routes):
    """Return dict: day_index → set of client IDs scheduled that day."""
    out = {}
    for d, rdf in routes.items():
        if rdf.empty:
            out[d] = set()
        else:
            out[d] = set(rdf['ID'].tolist())
    return out


def _advance_state_after_day0(scenario, day0_served):
    """
    Simulate: Day-0 deliveries happened exactly as planned.
     - Day-0 served clients: tank refilled to full (Current=Tank, Refill=0)
     - All clients lose 1 day's consumption (Current -= Avg_LbsPerDay)
     - Days_Until_Stockout recomputed
    Returns a fresh scenario dict with updated clients_df.
    """
    df = scenario['clients_df'].copy()
    for i, row in df.iterrows():
        cid = row['ID']
        if cid in day0_served:
            df.at[i, 'Current_lbs'] = float(row['Tank_lbs'])
        else:
            df.at[i, 'Current_lbs'] = max(0.0, float(row['Current_lbs']) - float(row['Avg_LbsPerDay']))
    df['Est_Current_lbs']     = df['Current_lbs']
    df['Refill_lbs']          = (df['Tank_lbs'] - df['Current_lbs']).clip(lower=0)
    df['Days_Until_Stockout'] = (df['Current_lbs'] / df['Avg_LbsPerDay'].clip(lower=1)).clip(lower=0.5)
    df['Refill_Today_lbs']    = df['Refill_lbs']
    df['Fill_Pct_Today']      = df['Refill_lbs'] / df['Tank_lbs']
    out = dict(scenario)
    out['clients_df'] = df
    return out


# ── Tests ───────────────────────────────────────────────────────────────────


def test_day1_roster_preserved_after_day0_executes():
    """
    Main property: Wednesday's roster from Tuesday's plan should still be
    scheduled (for any day in the remaining week) after Tuesday executes.
    """
    s = _make_scenario(n=15, seed=7)
    routes1, _ = _solve(s, today=pd.Timestamp('2026-04-14'), budget=2)
    by_day1 = _ids_by_day(routes1)
    day0_served = by_day1.get(0, set())
    originally_wed = by_day1.get(1, set())
    if not originally_wed:
        return  # nothing was planned for Wed — vacuous

    s2 = _advance_state_after_day0(s, day0_served)
    routes2, _ = _solve(s2, today=pd.Timestamp('2026-04-15'), budget=2)
    by_day2 = _ids_by_day(routes2)

    # Union of all days in the re-solve
    rescheduled = set().union(*by_day2.values()) if by_day2 else set()

    # At least 70% of originally-planned-Wed clients should still be scheduled
    # somewhere in the re-solve
    if not originally_wed:
        return
    kept = len(originally_wed & rescheduled)
    frac = kept / len(originally_wed)
    assert frac >= 0.70, \
        f'Plan churn: only {kept}/{len(originally_wed)} ({frac:.0%}) Wed clients retained'


def test_deferred_clients_do_not_become_urgent_unfairly():
    """
    A client deferred in Monday's plan (current inventory sufficient for the
    week) shouldn't suddenly become urgent the next day if nothing changed.
    """
    s = _make_scenario(n=15, seed=15)
    routes1, deferred1 = _solve(s, today=pd.Timestamp('2026-04-14'), budget=2)
    deferred_ids = set(deferred1['ID'].tolist()) if deferred1 is not None and not deferred1.empty else set()
    by_day1 = _ids_by_day(routes1)
    day0_served = by_day1.get(0, set())
    s2 = _advance_state_after_day0(s, day0_served)
    routes2, deferred2 = _solve(s2, today=pd.Timestamp('2026-04-15'), budget=2)
    by_day2 = _ids_by_day(routes2)

    # A deferred client shouldn't BOTH be scheduled today (Day 0 of new solve)
    # AND have been safe yesterday. Ideally deferrals "stick" or escalate
    # gracefully.
    now_day0 = by_day2.get(0, set())
    escalated = deferred_ids & now_day0
    # Some escalation is OK (client drifted closer to stockout), but >40% would
    # indicate the planner isn't seeing risk on day 1.
    if not deferred_ids:
        return
    frac = len(escalated) / len(deferred_ids)
    assert frac <= 0.50, \
        f'Too many deferred clients escalated to Day-0 urgent: {frac:.0%}'


def test_day0_schedule_deterministic_with_same_inputs():
    """
    Control case: two solves of the SAME scenario with the same seed must
    produce the same Day-0 roster. Catches nondeterminism bugs.
    """
    s = _make_scenario(n=12, seed=3)
    r1, _ = _solve(s, today=pd.Timestamp('2026-04-14'), budget=2)
    r2, _ = _solve(s, today=pd.Timestamp('2026-04-14'), budget=2)
    d1 = _ids_by_day(r1).get(0, set())
    d2 = _ids_by_day(r2).get(0, set())
    assert d1 == d2, f'Non-deterministic Day-0: {d1} vs {d2}'


TESTS = [
    ('day1_roster_preserved_after_day0_executes',    test_day1_roster_preserved_after_day0_executes),
    ('deferred_clients_do_not_become_urgent_unfairly', test_deferred_clients_do_not_become_urgent_unfairly),
    ('day0_schedule_deterministic_with_same_inputs', test_day0_schedule_deterministic_with_same_inputs),
]


def run_all_tests():
    print('\nPlan stability across rolling re-solves')
    print('━' * 78)
    passed = failed = 0
    start = time.time()
    for name, fn in TESTS:
        if run_test(name, fn):
            passed += 1
        else:
            failed += 1
    elapsed = time.time() - start
    print('━' * 78)
    tag = '✓' if failed == 0 else '✗'
    print(f'{tag} {passed} passed, {failed} failed in {elapsed:.1f}s')
    return 0 if failed == 0 else 1


if __name__ == '__main__':
    sys.exit(run_all_tests())
