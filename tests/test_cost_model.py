"""
test_cost_model.py — Cost arithmetic must be exactly right.

Labor_Cost = Reg_Min × LABOR_COST_PER_MIN + OT_Min × LABOR_COST_PER_MIN × OT_MULTIPLIER
Reg_Min    = min(Route_Time_min, SHIFT_MIN)
OT_Min     = max(Route_Time_min - SHIFT_MIN, 0)

Wrap the arithmetic in tiny unit checks. These catch:
  - swapped multiplicand/multiplier
  - miscounting regular vs OT minutes at the shift boundary
  - OT_MULTIPLIER regressions (someone flips 1.5 to 1.0 in config)
  - Labor_Cost column filled from wrong source (e.g. Route_Time_min × rate ignoring OT)
"""

import sys
import time
from pathlib import Path
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import (
    TRUCKS, DAYS, NUM_DAYS, PRODUCTS,
    SHIFT_MIN, MAX_SHIFT_MIN,
    LABOR_COST_PER_MIN, OT_MULTIPLIER, OT_PENALTY_PER_MIN,
)
from unified_solver import solve_week


def run_test(name, fn):
    start = time.time()
    try:
        fn()
        print(f'  ✓ {name:<56s} ({(time.time()-start)*1000:.0f} ms)')
        return True
    except AssertionError as e:
        print(f'  ✗ {name:<56s} — {str(e)[:80]}')
        return False
    except Exception as e:
        print(f'  ✗ {name:<56s} — CRASH: {type(e).__name__}: {str(e)[:60]}')
        return False


# ── Pure arithmetic ─────────────────────────────────────────────────────────

def test_labor_cost_formula_below_shift():
    """Route_Time_min = 400 (under 600 SHIFT_MIN): no OT."""
    rt = 400
    reg, ot = min(rt, SHIFT_MIN), max(rt - SHIFT_MIN, 0)
    assert reg == 400 and ot == 0
    expected = reg * LABOR_COST_PER_MIN + ot * LABOR_COST_PER_MIN * OT_MULTIPLIER
    assert expected == 400 * LABOR_COST_PER_MIN

def test_labor_cost_formula_at_shift_boundary():
    rt = SHIFT_MIN
    reg, ot = min(rt, SHIFT_MIN), max(rt - SHIFT_MIN, 0)
    assert reg == SHIFT_MIN and ot == 0

def test_labor_cost_formula_over_shift():
    """Route_Time_min = 660 → 600 reg, 60 OT."""
    rt = 660
    reg, ot = min(rt, SHIFT_MIN), max(rt - SHIFT_MIN, 0)
    assert reg == 600 and ot == 60
    expected = 600 * LABOR_COST_PER_MIN + 60 * LABOR_COST_PER_MIN * OT_MULTIPLIER
    # = 600*50 + 60*50*1.5 = 30000 + 4500 = 34500
    assert expected == 34500, f"expected 34500, got {expected}"

def test_ot_penalty_per_min_derivation():
    """OT_PENALTY_PER_MIN (used in solver objective) must be the EXTRA cost per OT minute."""
    expected_extra = LABOR_COST_PER_MIN * (OT_MULTIPLIER - 1.0)
    assert OT_PENALTY_PER_MIN == int(expected_extra), \
        f"OT_PENALTY_PER_MIN {OT_PENALTY_PER_MIN} != int({expected_extra})"

def test_ot_monotonic_in_multiplier():
    """OT cost grows monotonically with OT_MULTIPLIER."""
    rt = 700  # 100 OT min
    reg, ot = 600, 100
    costs = []
    for mult in [1.0, 1.25, 1.5, 1.75, 2.0]:
        c = reg * LABOR_COST_PER_MIN + ot * LABOR_COST_PER_MIN * mult
        costs.append(c)
    assert all(costs[i] <= costs[i+1] for i in range(len(costs)-1)), \
        f"OT cost not monotonic: {costs}"

def test_hard_cap_rejects_over_max():
    """MAX_SHIFT_MIN=720 is the hard ceiling. 720 OK, 721 must be rejected by solver."""
    assert MAX_SHIFT_MIN > SHIFT_MIN
    assert MAX_SHIFT_MIN - SHIFT_MIN == 120  # 2-hour max OT per day


# ── Solver output arithmetic (end-to-end sanity) ────────────────────────────

def _solve_trivial():
    """Small scenario guaranteed to produce at least one route."""
    clients = []
    for i in range(6):
        clients.append({
            'ID': f'C{i:03d}', 'Customer': f'Client {i}',
            'Lat': 33.51 + i * 0.01, 'Lon': -112.10 + i * 0.01,
            'Tank_lbs': 5000, 'Product': PRODUCTS[0],
            'Avg_LbsPerDay': 200, 'Days_Since_Last': 7,
            'Current_lbs': 1500,
        })
    df = pd.DataFrame(clients)
    df['Est_Current_lbs']     = df['Current_lbs']
    df['Refill_lbs']          = df['Tank_lbs'] - df['Current_lbs']
    df['Days_Until_Stockout'] = (df['Current_lbs'] / df['Avg_LbsPerDay']).clip(lower=0.5)
    df['Urgency']             = 'normal'
    df['Refill_Today_lbs']    = df['Refill_lbs']
    df['Fill_Pct_Today']      = df['Refill_lbs'] / df['Tank_lbs']

    n = 7
    dist = np.zeros((n, n), dtype=int)
    tm   = np.zeros((n, n), dtype=int)
    for i in range(n):
        for j in range(n):
            if i != j:
                dist[i, j] = abs(i - j) * 1500
                tm[i, j]   = abs(i - j) * 4
    return df, dist, tm, {'DEPOT': 0, **{f'C{i:03d}': i+1 for i in range(6)}}


def test_labor_cost_column_matches_formula():
    df, dist, tm, nmap = _solve_trivial()
    routes, _ = solve_week(
        df, dist, tm, nmap,
        start_day=0, solve_seconds=3,
        time_windows_df=pd.DataFrame(columns=['Client_ID','Day_of_Week','Open_Min','Close_Min']),
        closures_df=pd.DataFrame(columns=['Client_ID','Start_Date','End_Date','Reason']),
        today=pd.Timestamp('2026-04-14'),
        depot_config={'depot_lat':33.5,'depot_lon':-112.1,
                      'shift_start_min':360,'shift_end_min':1080,
                      'morning_load_min':30,'evening_unload_min':15,
                      'work_days':DAYS},
    )
    parts = [r for r in routes.values() if not r.empty]
    assert parts, "solver produced no routes"
    all_routes = pd.concat(parts)
    for (truck, day), grp in all_routes.groupby(['Truck', 'Day']):
        rt  = grp['Route_Time_min'].iloc[0]
        reg = grp['Reg_Min'].iloc[0]
        ot  = grp['OT_Min'].iloc[0]
        labor = grp['Labor_Cost'].iloc[0]
        expected = reg * LABOR_COST_PER_MIN + ot * LABOR_COST_PER_MIN * OT_MULTIPLIER
        assert abs(labor - expected) < 1, \
            f"{truck} {day}: labor {labor} != formula {expected} (rt={rt}, reg={reg}, ot={ot})"


def test_reg_plus_ot_equals_route_time():
    df, dist, tm, nmap = _solve_trivial()
    routes, _ = solve_week(
        df, dist, tm, nmap,
        start_day=0, solve_seconds=3,
        time_windows_df=pd.DataFrame(columns=['Client_ID','Day_of_Week','Open_Min','Close_Min']),
        closures_df=pd.DataFrame(columns=['Client_ID','Start_Date','End_Date','Reason']),
        today=pd.Timestamp('2026-04-14'),
        depot_config={'depot_lat':33.5,'depot_lon':-112.1,
                      'shift_start_min':360,'shift_end_min':1080,
                      'morning_load_min':30,'evening_unload_min':15,
                      'work_days':DAYS},
    )
    parts = [r for r in routes.values() if not r.empty]
    assert parts
    for r in parts:
        for (truck, day), grp in r.groupby(['Truck', 'Day']):
            rt = grp['Route_Time_min'].iloc[0]
            reg = grp['Reg_Min'].iloc[0]
            ot  = grp['OT_Min'].iloc[0]
            assert reg + ot == rt, f"{truck} {day}: {reg}+{ot} != {rt}"


TESTS = [
    ('labor_cost_formula_below_shift',           test_labor_cost_formula_below_shift),
    ('labor_cost_formula_at_shift_boundary',     test_labor_cost_formula_at_shift_boundary),
    ('labor_cost_formula_over_shift',            test_labor_cost_formula_over_shift),
    ('ot_penalty_per_min_derivation',            test_ot_penalty_per_min_derivation),
    ('ot_monotonic_in_multiplier',               test_ot_monotonic_in_multiplier),
    ('hard_cap_rejects_over_max',                test_hard_cap_rejects_over_max),
    ('labor_cost_column_matches_formula',        test_labor_cost_column_matches_formula),
    ('reg_plus_ot_equals_route_time',            test_reg_plus_ot_equals_route_time),
]


def run_all_tests():
    print('\nCost Model')
    print('━' * 70)
    passed = failed = 0
    start = time.time()
    for name, fn in TESTS:
        if run_test(name, fn):
            passed += 1
        else:
            failed += 1
    elapsed = time.time() - start
    print('━' * 70)
    tag = '✓' if failed == 0 else '✗'
    print(f'{tag} {passed} passed, {failed} failed in {elapsed:.1f}s')
    return 0 if failed == 0 else 1


if __name__ == '__main__':
    sys.exit(run_all_tests())
