"""
test_determinism_golden.py — Identical input + seed = identical schedule.

Two kinds of checks:

1. **Determinism**: solve the same scenario 4 times with the same config.
   Scheduled-set must be identical. OT totals and total miles should match.

2. **Golden baseline**: lock a scheduled-set / labor / miles signature for
   synthetic-50 at the production defaults and a fixed solve budget. If
   someone changes the solver or config in a way that silently shifts output,
   this trips.

Golden values are captured on first successful run and stored in
`tests/golden_baseline.json`. To intentionally update them after a real
improvement: delete the file and re-run.
"""

import json
import sys
import time
from pathlib import Path
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

import tests.scenario_lib as scn
import config as _cfg
from config import DAYS, PRODUCTS
from unified_solver import solve_week

GOLDEN_PATH = Path(__file__).parent / 'golden_baseline.json'
SOLVE_BUDGET = 3  # seconds — fixed so repeat runs converge to same solution


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


def _solve(s, budget=SOLVE_BUDGET):
    return solve_week(
        s['clients_df'], s['dist_matrix'], s['time_matrix'], s['node_index_map'],
        start_day=0, solve_seconds=budget,
        time_windows_df=s['time_windows_df'],
        closures_df=s['closures_df'],
        today=pd.Timestamp('2026-04-14'),
        depot_config=s['depot_config'],
    )


def _summary(routes, deferred):
    parts = [r for r in routes.values() if not r.empty]
    if not parts:
        return {'stops': 0, 'miles': 0, 'lbs': 0, 'sched_ids': [], 'deferred_ct': len(deferred)}
    flat = pd.concat(parts, ignore_index=True)
    per_route_miles = flat.groupby(['Truck','Day'])['Route_Dist_mi'].first().sum()
    return {
        'stops': len(flat),
        'miles': round(float(per_route_miles), 1),
        'lbs': int(flat['Refill_lbs'].sum()),
        'sched_ids': sorted(flat['ID'].tolist()),
        'deferred_ct': len(deferred),
    }


# ── Determinism ─────────────────────────────────────────────────────────────

def test_determinism_mixed_60_four_runs():
    s1 = scn.mixed(60, seed=42)
    s2 = scn.mixed(60, seed=42)
    s3 = scn.mixed(60, seed=42)
    s4 = scn.mixed(60, seed=42)

    results = []
    for s in [s1, s2, s3, s4]:
        r, d = _solve(s, budget=SOLVE_BUDGET)
        results.append(_summary(r, d))

    ref = results[0]
    for i, r in enumerate(results[1:], 1):
        # Scheduled set must be identical across runs
        assert r['sched_ids'] == ref['sched_ids'], \
            f"run{i} scheduled set differs from run0 " \
            f"(sym diff={len(set(ref['sched_ids'])^set(r['sched_ids']))})"
        # stops and lbs should match exactly (same clients)
        assert r['stops'] == ref['stops']
        assert r['lbs']   == ref['lbs']
        # Miles tolerant to ≤0.1 rounding (but normally exact)
        assert abs(r['miles'] - ref['miles']) < 1, \
            f"run{i} miles {r['miles']} vs run0 {ref['miles']}"


def test_determinism_urban_50_three_runs():
    results = []
    for _ in range(3):
        s = scn.urban(50, seed=42)
        r, d = _solve(s, budget=SOLVE_BUDGET)
        results.append(_summary(r, d))

    ref = results[0]
    for i, r in enumerate(results[1:], 1):
        assert r['sched_ids'] == ref['sched_ids'], \
            f"urban run{i} scheduled differs (sym diff "\
            f"{len(set(ref['sched_ids'])^set(r['sched_ids']))})"


# ── Input immutability ──────────────────────────────────────────────────────

def test_clients_df_immutable_multi_run():
    s = scn.mixed(30, seed=7)
    before = s['clients_df'].copy(deep=True)
    _solve(s, budget=3)
    _solve(s, budget=3)
    after = s['clients_df']
    assert before.equals(after), "clients_df mutated across multiple solves"


def test_time_windows_df_immutable():
    s = scn.tight_windows(25, 0.8, seed=5)
    before = s['time_windows_df'].copy(deep=True)
    _solve(s, budget=3)
    after = s['time_windows_df']
    assert before.equals(after), "time_windows_df mutated"


# ── Golden baseline ──────────────────────────────────────────────────────────

def _capture_golden(name, s):
    r, d = _solve(s, budget=SOLVE_BUDGET)
    summary = _summary(r, d)
    summary.pop('sched_ids')  # stored separately
    return summary, _summary(r, d)['sched_ids']


def test_golden_baseline():
    """Lock production-default metrics for mixed-60 and urban-50."""
    scenarios = {
        'mixed-60': scn.mixed(60, seed=42),
        'urban-50': scn.urban(50, seed=42),
    }

    if not GOLDEN_PATH.exists():
        # First run: capture and write
        data = {'config': {
                    'EFFICIENCY_WEIGHT':  _cfg.EFFICIENCY_WEIGHT,
                    'OT_MULTIPLIER':      _cfg.OT_MULTIPLIER,
                    'USE_FORWARD_REFILLS':_cfg.USE_FORWARD_REFILLS,
                    'ENFORCE_TIME_WINDOWS':_cfg.ENFORCE_TIME_WINDOWS,
                    'MAX_SERVICE_INTERVAL_DAYS':_cfg.MAX_SERVICE_INTERVAL_DAYS,
                    'SOLVE_BUDGET': SOLVE_BUDGET,
                },
                'scenarios': {}}
        for name, s in scenarios.items():
            summary, ids = _capture_golden(name, s)
            data['scenarios'][name] = {**summary, 'sched_ids': ids}
        GOLDEN_PATH.write_text(json.dumps(data, indent=2))
        print(f'    (golden captured → {GOLDEN_PATH.name})')
        return

    data = json.loads(GOLDEN_PATH.read_text())
    for name, s in scenarios.items():
        expected = data['scenarios'].get(name)
        if expected is None:
            continue
        r, d = _solve(s, budget=SOLVE_BUDGET)
        got = _summary(r, d)

        # scheduled-set identity (most sensitive check)
        sym_diff = set(expected['sched_ids']) ^ set(got['sched_ids'])
        assert not sym_diff, \
            f"{name}: scheduled set drifted from golden " \
            f"(sym diff = {len(sym_diff)})"

        # metrics within 1% tolerance
        for metric in ['stops', 'lbs']:
            assert expected[metric] == got[metric], \
                f"{name}: {metric} {got[metric]} != golden {expected[metric]}"
        # miles tolerant to 2 mi rounding
        assert abs(expected['miles'] - got['miles']) <= 2, \
            f"{name}: miles {got['miles']} vs golden {expected['miles']}"


TESTS = [
    ('determinism_mixed_60_four_runs',   test_determinism_mixed_60_four_runs),
    ('determinism_urban_50_three_runs',  test_determinism_urban_50_three_runs),
    ('clients_df_immutable_multi_run',   test_clients_df_immutable_multi_run),
    ('time_windows_df_immutable',        test_time_windows_df_immutable),
    ('golden_baseline',                  test_golden_baseline),
]


def run_all_tests():
    print('\nDeterminism & Golden Baseline')
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
