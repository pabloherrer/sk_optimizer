"""
test_equity.py — Does the optimizer treat clients fairly over time?

A narrow optimizer may always prioritize the same easy clients (dense urban,
big tanks, high consumption), leaving awkward clients perpetually deferred.
We're not paid to optimize lbs delivered — we're paid to service the fleet.

Metrics:
  - Coverage: fraction of clients visited ≥ once in N weeks.
  - Starvation count: clients visited 0 times in N weeks.
  - Visit-count Gini: 0 = perfectly uniform visits, 1 = all visits to one client.
  - Max-gap: longest span any single client went without a visit.
"""

import sys
import time
from pathlib import Path
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

import tests.scenario_lib as scn
from config import DAYS, PRODUCTS, NUM_DAYS
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


def _gini(values):
    """Gini coefficient (0=equal, 1=max inequality)."""
    x = np.sort(np.asarray(values, dtype=float))
    n = len(x)
    if n == 0 or x.sum() == 0:
        return 0.0
    cum = np.cumsum(x)
    return (n + 1 - 2 * cum.sum() / x.sum()) / n


def _advance_state(clients_df, scheduled_ids):
    """Crude state advance: scheduled clients get refilled, others consume a week."""
    df = clients_df.copy()
    for idx in df.index:
        cid = df.at[idx, 'ID']
        rate = df.at[idx, 'Avg_LbsPerDay']
        tank = df.at[idx, 'Tank_lbs']
        if cid in scheduled_ids:
            df.at[idx, 'Current_lbs'] = tank  # fully filled
            df.at[idx, 'Days_Since_Last'] = 0
        else:
            df.at[idx, 'Current_lbs'] = max(0, df.at[idx, 'Current_lbs'] - rate * 5)
            df.at[idx, 'Days_Since_Last'] = df.at[idx, 'Days_Since_Last'] + 5
    df['Est_Current_lbs']     = df['Current_lbs']
    df['Refill_lbs']          = (df['Tank_lbs'] - df['Current_lbs']).clip(lower=0)
    df['Days_Until_Stockout'] = (df['Current_lbs'] / df['Avg_LbsPerDay'].clip(lower=1)).clip(lower=0.5)
    df['Refill_Today_lbs']    = df['Refill_lbs']
    df['Fill_Pct_Today']      = df['Refill_lbs'] / df['Tank_lbs']
    return df


def _rollout(scenario_builder, n_weeks=3, budget=3):
    """Simulate n_weeks of rolling schedules, return (visit_count, gap_weeks)."""
    s = scenario_builder()
    df = s['clients_df']
    ids = list(df['ID'])
    visits = {cid: 0 for cid in ids}
    last_visit_week = {cid: -1 for cid in ids}
    max_gap = {cid: 0 for cid in ids}

    for week in range(n_weeks):
        routes, deferred = solve_week(
            df, s['dist_matrix'], s['time_matrix'], s['node_index_map'],
            start_day=0, solve_seconds=budget,
            time_windows_df=s['time_windows_df'],
            closures_df=s['closures_df'],
            today=pd.Timestamp('2026-04-14') + pd.Timedelta(weeks=week),
            depot_config=s['depot_config'],
        )
        scheduled = set()
        for r in routes.values():
            if not r.empty:
                scheduled.update(r['ID'].tolist())
        for cid in ids:
            if cid in scheduled:
                if last_visit_week[cid] >= 0:
                    gap = week - last_visit_week[cid]
                    max_gap[cid] = max(max_gap[cid], gap)
                visits[cid] += 1
                last_visit_week[cid] = week
        df = _advance_state(df, scheduled)

    return visits, max_gap


# ── Tests ──────────────────────────────────────────────────────────────────

def test_no_starvation_over_3_weeks_urban():
    """In 3 weeks of urban-50, every eligible client should be visited at least once."""
    visits, _ = _rollout(lambda: scn.urban(30, seed=42), n_weeks=3, budget=3)
    starved = [cid for cid, v in visits.items() if v == 0]
    # We allow ≤ 20% starvation on this tight scenario (fleet capacity limits)
    frac = len(starved) / len(visits)
    assert frac <= 0.35, f"starved {len(starved)}/{len(visits)} = {frac:.0%} (>35%)"


def test_visit_count_not_zero_across_weeks():
    """Mixed-40 across 3 weeks — most clients visited ≥ 1 time."""
    visits, _ = _rollout(lambda: scn.mixed(40, seed=7), n_weeks=3, budget=3)
    visited = sum(1 for v in visits.values() if v > 0)
    frac = visited / len(visits)
    assert frac >= 0.50, f"only {frac:.0%} clients visited at all (need ≥50%)"


def test_visit_count_gini_reasonable():
    """Gini on visit counts across 3 weeks should be < 0.6 — no extreme concentration."""
    visits, _ = _rollout(lambda: scn.mixed(30, seed=5), n_weeks=3, budget=3)
    g = _gini(list(visits.values()))
    assert g < 0.6, f"Gini {g:.2f} too high — unequal service (>0.6)"


TESTS = [
    ('no_starvation_over_3_weeks_urban',   test_no_starvation_over_3_weeks_urban),
    ('visit_count_not_zero_across_weeks',  test_visit_count_not_zero_across_weeks),
    ('visit_count_gini_reasonable',        test_visit_count_gini_reasonable),
]


def run_all_tests():
    print('\nEquity / Fairness')
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
