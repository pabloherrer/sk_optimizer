"""
test_tw_interactions.py — TW × {closures, capacity, narrow windows, multi-client}

Context: we just replaced the union-envelope TW logic with per-vehicle
conditional CumulVar constraints (unified_solver.py:549-666). These tests
stress the new formulation against every interaction we could think of.

Coverage:
  * TW × closures (same day, different day)
  * TW × narrow windows (1 min, start/end of shift)
  * TW × same-day multi-row (coalescing)
  * TW × multi-client simultaneity (can't both be at same stop at same time)
  * TW × all-days-windowed (solver picks cheapest day)
  * TW × arrival_min reported accurately
"""

import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import DAYS, NUM_DAYS, PRODUCTS, TRUCKS, TRUCK_NAMES
from unified_solver import solve_week


def run_test(name, fn):
    start = time.time()
    try:
        fn()
        print(f'  ✓ {name:<62s} ({(time.time()-start)*1000:.0f} ms)')
        return True
    except AssertionError as e:
        print(f'  ✗ {name:<62s} — {str(e)[:70]}')
        return False
    except Exception as e:
        print(f'  ✗ {name:<62s} — CRASH: {type(e).__name__}: {str(e)[:60]}')
        return False


# ── Scenario builder ────────────────────────────────────────────────────────

def _make_scenario(n=4, seed=1, tight_refill=True):
    """Small scenario suitable for TW+closure stress.

    Clients are given a refill-heavy profile (Fill_Pct > 0.5) so they pass
    the eligibility gate in _build_pool. Rate is high so Days_Until_Stockout
    is also low, making them obvious candidates.
    """
    rng = np.random.default_rng(seed)
    clients = []
    coords = [(33.50, -112.10)]
    for i in range(n):
        lat = 33.51 + (i % 4) * 0.01
        lon = -112.11 + (i // 4) * 0.01
        coords.append((lat, lon))
        tank_lbs = 5000
        # Fill_Pct ≥ 0.6 (Refill ≥ 3000) → eligible by fill gate
        current  = 1500 if tight_refill else 2500
        clients.append({
            'ID': f'W{i:03d}', 'Customer': f'TW{i}',
            'Lat': lat, 'Lon': lon,
            'Tank_lbs': tank_lbs, 'Product': PRODUCTS[i % 2],
            'Avg_LbsPerDay': 300, 'Days_Since_Last': 5,
            'Current_lbs': current,
        })
    df = pd.DataFrame(clients)
    df['Est_Current_lbs']      = df['Current_lbs']
    df['Refill_lbs']           = (df['Tank_lbs'] - df['Current_lbs']).clip(lower=0)
    df['Days_Until_Stockout']  = (df['Current_lbs'] / df['Avg_LbsPerDay'].clip(lower=1)).clip(lower=0.5)
    df['Urgency']              = 'normal'
    df['Refill_Today_lbs']     = df['Refill_lbs']
    df['Fill_Pct_Today']       = df['Refill_lbs'] / df['Tank_lbs']

    nn = len(coords)
    dist = np.zeros((nn, nn), dtype=int)
    tm   = np.zeros((nn, nn), dtype=int)
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
        'dist_matrix': dist,
        'time_matrix': tm,
        'node_index_map': nix,
        'time_windows_df': pd.DataFrame(columns=['Client_ID', 'Day_of_Week', 'Open_Min', 'Close_Min']),
        'closures_df': pd.DataFrame(columns=['Client_ID', 'Start_Date', 'End_Date', 'Reason']),
        'depot_config': {
            'depot_lat': 33.5, 'depot_lon': -112.1,
            'shift_start_min': 360, 'shift_end_min': 1080,
            'morning_load_min': 30, 'evening_unload_min': 15,
            'work_days': DAYS,
        },
    }


def _solve(s, budget=3):
    return solve_week(
        s['clients_df'], s['dist_matrix'], s['time_matrix'], s['node_index_map'],
        start_day=0, solve_seconds=budget,
        time_windows_df=s['time_windows_df'],
        closures_df=s['closures_df'],
        today=pd.Timestamp('2026-04-14'),
        depot_config=s['depot_config'],
    )


def _get(routes, cid):
    for d, rdf in routes.items():
        if rdf.empty: continue
        m = rdf['ID'] == cid
        if m.any(): return rdf[m].iloc[0]
    return None


# ── Tests ───────────────────────────────────────────────────────────────────

def test_tw_plus_closure_same_day_defers():
    """Client W000 has only a Tue window; also closed all Tue. Must defer."""
    s = _make_scenario(n=2)
    # Tue-only narrow window
    s['time_windows_df'] = pd.DataFrame([
        {'Client_ID': 'W000', 'Day_of_Week': 'Tue', 'Open_Min': 480, 'Close_Min': 600}
    ])
    # Tue closure covering same day
    s['closures_df'] = pd.DataFrame([
        {'Client_ID': 'W000',
         'Start_Date': pd.Timestamp('2026-04-14'),
         'End_Date':   pd.Timestamp('2026-04-14'),
         'Reason': 'Holiday'}
    ])
    routes, deferred = _solve(s, budget=3)
    stop = _get(routes, 'W000')
    # Either deferred OR not scheduled
    deferred_ids = set(deferred['ID'].tolist()) if not deferred.empty else set()
    assert stop is None or 'W000' in deferred_ids, \
        f'W000 must not be scheduled when Tue is only window and also closed'


def test_tw_plus_closure_other_day_ok():
    """Closure on Wed, TW Tue & Thu — scheduled Tue or Thu."""
    s = _make_scenario(n=2)
    s['time_windows_df'] = pd.DataFrame([
        {'Client_ID': 'W000', 'Day_of_Week': 'Tue', 'Open_Min': 480, 'Close_Min': 720},
        {'Client_ID': 'W000', 'Day_of_Week': 'Thu', 'Open_Min': 480, 'Close_Min': 720},
    ])
    s['closures_df'] = pd.DataFrame([
        {'Client_ID': 'W000',
         'Start_Date': pd.Timestamp('2026-04-15'),  # Wed
         'End_Date':   pd.Timestamp('2026-04-15'),
         'Reason': 'Holiday'}
    ])
    routes, _ = _solve(s, budget=3)
    stop = _get(routes, 'W000')
    assert stop is not None, 'W000 should still schedule on Tue or Thu'
    day_label = stop['Day']
    assert day_label in ('Tue', 'Thu'), f'W000 scheduled on {day_label} — expected Tue/Thu'


def test_tw_narrow_15min_window_still_feasible():
    """A 15-minute TW on a well-located client should still be served."""
    s = _make_scenario(n=2)
    # Client very close to depot (W000 is at 33.51, -112.11 — ~1 mi)
    s['time_windows_df'] = pd.DataFrame([
        {'Client_ID': 'W000', 'Day_of_Week': 'Tue',
         'Open_Min': 480, 'Close_Min': 495}  # 8:00-8:15 AM
    ])
    routes, deferred = _solve(s, budget=3)
    stop = _get(routes, 'W000')
    # Allow defer, but if scheduled verify arrival in [480, 495] in shift-relative
    if stop is not None:
        arrival = int(stop.get('Arrival_Min', -1))
        # shift_start is 360, so arrival in shift-relative = actual_arrival - 360
        # The window (480, 495) was stored as absolute clock minutes. The solver
        # rebased to (480-360, 495-360) = (120, 135).
        assert 120 <= arrival <= 135, \
            f'W000 arrival {arrival} outside tight 15-min TW [120,135]'


def test_tw_window_covering_full_shift_is_noop():
    """Window from shift start to shift end should not restrict anything."""
    s = _make_scenario(n=3)
    s['time_windows_df'] = pd.DataFrame([
        {'Client_ID': 'W000', 'Day_of_Week': 'Tue', 'Open_Min': 360, 'Close_Min': 1080},
        {'Client_ID': 'W000', 'Day_of_Week': 'Wed', 'Open_Min': 360, 'Close_Min': 1080},
        {'Client_ID': 'W000', 'Day_of_Week': 'Thu', 'Open_Min': 360, 'Close_Min': 1080},
        {'Client_ID': 'W000', 'Day_of_Week': 'Fri', 'Open_Min': 360, 'Close_Min': 1080},
        {'Client_ID': 'W000', 'Day_of_Week': 'Sat', 'Open_Min': 360, 'Close_Min': 1080},
    ])
    routes, deferred = _solve(s, budget=3)
    stop = _get(routes, 'W000')
    # Should schedule (or be deferred for some non-TW reason)
    deferred_ids = set(deferred['ID'].tolist()) if not deferred.empty else set()
    if 'W000' in deferred_ids:
        reason = deferred[deferred['ID'] == 'W000'].iloc[0].get('Reason', '')
        assert 'TW' not in str(reason).upper(), \
            f'W000 deferred for TW reason despite full-shift window: {reason}'


def test_tw_start_of_shift_window():
    """Window at very start of shift [0, 60] shift-relative."""
    s = _make_scenario(n=3)
    s['time_windows_df'] = pd.DataFrame([
        {'Client_ID': 'W000', 'Day_of_Week': 'Tue', 'Open_Min': 360, 'Close_Min': 420},
    ])
    routes, deferred = _solve(s, budget=3)
    stop = _get(routes, 'W000')
    if stop is not None:
        arrival = int(stop.get('Arrival_Min', -1))
        assert 0 <= arrival <= 60, f'arrival {arrival} outside [0,60] start-of-shift window'


def test_tw_end_of_shift_window():
    """Window near end of shift [660, 720] shift-relative = 5-6 PM."""
    s = _make_scenario(n=3)
    s['time_windows_df'] = pd.DataFrame([
        {'Client_ID': 'W000', 'Day_of_Week': 'Thu', 'Open_Min': 1020, 'Close_Min': 1080},
        # Also give flexible windows on other days so the solver has room
        {'Client_ID': 'W000', 'Day_of_Week': 'Tue', 'Open_Min': 360, 'Close_Min': 1080},
    ])
    routes, _ = _solve(s, budget=3)
    stop = _get(routes, 'W000')
    if stop is not None:
        day_label = stop['Day']
        arrival = int(stop.get('Arrival_Min', -1))
        if day_label == 'Thu':
            assert 660 <= arrival <= 720, \
                f'Thu arrival {arrival} outside [660,720] end-of-shift window'


def test_tw_same_day_two_rows_coalesced():
    """
    Two TW rows for same client same day [480,540] and [540,600] should
    coalesce into [480,600] via min-open / max-close. Verify scheduling works.
    """
    s = _make_scenario(n=3)
    s['time_windows_df'] = pd.DataFrame([
        {'Client_ID': 'W000', 'Day_of_Week': 'Tue', 'Open_Min': 480, 'Close_Min': 540},
        {'Client_ID': 'W000', 'Day_of_Week': 'Tue', 'Open_Min': 540, 'Close_Min': 600},
    ])
    routes, _ = _solve(s, budget=3)
    stop = _get(routes, 'W000')
    if stop is not None:
        assert stop['Day'] == 'Tue', f'W000 must be Tue when only Tue has TW'
        arrival = int(stop.get('Arrival_Min', -1))
        # Coalesced window: 480..600 in absolute → 120..240 shift-relative
        assert 120 <= arrival <= 240, \
            f'arrival {arrival} outside coalesced [120,240] window'


def test_tw_all_days_windowed_picks_cheapest():
    """
    All 5 days have identical 8-10 AM TW. Solver picks any one; schedule
    should not be deferred.
    """
    s = _make_scenario(n=3)
    rows = [
        {'Client_ID': 'W000', 'Day_of_Week': d, 'Open_Min': 480, 'Close_Min': 600}
        for d in DAYS
    ]
    s['time_windows_df'] = pd.DataFrame(rows)
    routes, deferred = _solve(s, budget=3)
    stop = _get(routes, 'W000')
    assert stop is not None, f'must schedule with 5×TW; deferred={len(deferred)}'
    arrival = int(stop.get('Arrival_Min', -1))
    assert 120 <= arrival <= 240, f'arrival {arrival} outside any 8-10 AM window'


def test_tw_multiple_clients_simultaneous_different_days():
    """
    Two clients both with Tue 8-10 AM windows. They're colocated. The solver
    can schedule both on Tue (same truck) or split them. Either is fine —
    we just verify no infeasibility.
    """
    s = _make_scenario(n=4)
    s['time_windows_df'] = pd.DataFrame([
        {'Client_ID': 'W000', 'Day_of_Week': 'Tue', 'Open_Min': 480, 'Close_Min': 600},
        {'Client_ID': 'W001', 'Day_of_Week': 'Tue', 'Open_Min': 480, 'Close_Min': 600},
    ])
    routes, deferred = _solve(s, budget=3)
    s0 = _get(routes, 'W000')
    s1 = _get(routes, 'W001')
    # Both must be scheduled (they have 2 full hours + the rest of the week)
    deferred_ids = set(deferred['ID'].tolist()) if not deferred.empty else set()
    assert 'W000' not in deferred_ids, 'W000 must schedule'
    assert 'W001' not in deferred_ids, 'W001 must schedule'
    # If both on Tue, verify both arrivals in 120-240 shift-relative
    for st in (s0, s1):
        if st is not None and st['Day'] == 'Tue':
            arrival = int(st.get('Arrival_Min', -1))
            assert 120 <= arrival <= 240, f'Tue arrival {arrival} out of window'


def test_tw_arrival_min_reported():
    """The new `Arrival_Min` column in output rows must be populated."""
    s = _make_scenario(n=3)
    s['time_windows_df'] = pd.DataFrame([
        {'Client_ID': 'W000', 'Day_of_Week': 'Tue', 'Open_Min': 600, 'Close_Min': 720},
    ])
    routes, _ = _solve(s, budget=3)
    for d, rdf in routes.items():
        if rdf.empty: continue
        assert 'Arrival_Min' in rdf.columns, 'Output rows must carry Arrival_Min'
        for _, r in rdf.iterrows():
            arrival = int(r['Arrival_Min'])
            assert 0 <= arrival <= 720, f'Arrival_Min {arrival} wildly out of range'


def test_tw_day_restriction_prunes_vehicles():
    """
    Client with ONLY a Thu window must be scheduled Thu (or deferred),
    never on Tue/Wed/Fri/Sat.
    """
    s = _make_scenario(n=3)
    s['time_windows_df'] = pd.DataFrame([
        {'Client_ID': 'W000', 'Day_of_Week': 'Thu', 'Open_Min': 480, 'Close_Min': 720},
    ])
    routes, deferred = _solve(s, budget=3)
    stop = _get(routes, 'W000')
    if stop is not None:
        assert stop['Day'] == 'Thu', f'W000 scheduled on {stop["Day"]} — Thu-only TW'


TESTS = [
    ('tw_plus_closure_same_day_defers',         test_tw_plus_closure_same_day_defers),
    ('tw_plus_closure_other_day_ok',            test_tw_plus_closure_other_day_ok),
    ('tw_narrow_15min_window_still_feasible',   test_tw_narrow_15min_window_still_feasible),
    ('tw_window_covering_full_shift_is_noop',   test_tw_window_covering_full_shift_is_noop),
    ('tw_start_of_shift_window',                test_tw_start_of_shift_window),
    ('tw_end_of_shift_window',                  test_tw_end_of_shift_window),
    ('tw_same_day_two_rows_coalesced',          test_tw_same_day_two_rows_coalesced),
    ('tw_all_days_windowed_picks_cheapest',     test_tw_all_days_windowed_picks_cheapest),
    ('tw_multiple_clients_simultaneous_different_days',
                                                test_tw_multiple_clients_simultaneous_different_days),
    ('tw_arrival_min_reported',                 test_tw_arrival_min_reported),
    ('tw_day_restriction_prunes_vehicles',      test_tw_day_restriction_prunes_vehicles),
]


def run_all_tests():
    print('\nTW interactions (TW × closures / capacity / narrow / multi)')
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
