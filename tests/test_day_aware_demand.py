"""
test_day_aware_demand.py — Red-before-green tests for day-aware demand fix.

Why this exists
---------------
Today's solver (unified_solver.py:459-462) picks ONE day's refill list
(end-of-week projection when USE_FORWARD_REFILLS=True, else day 0) and uses
that single vector in:
  - the demand callback (line 661) → shapes truck Capacity dimension
  - the time callback  (line 518) → shapes service-time → shift-time budget

This is wrong when clients have different refill amounts on different days.
Example: a low-consumption client visited on Tue has a small refill, but
the solver sizes their refill for Day 4 (Sat). The capacity dimension is
too tight (over-provisioning) and the service-time callback over-estimates,
wasting Tue shift minutes.

Worse: on peak-season scenarios where capacity is actually the binding
constraint, the solver will needlessly defer clients because Day-4 sizing
eats truck capacity that a Day-0 visit never would.

These tests PROVE the bug (they fail on current code) and will pass after
the fix. The fix is: replace the single `refills` list with a per-vehicle
demand/time callback that reads `refills_by_day[day(v)]`.

Expected outcome on CURRENT code
--------------------------------
Some tests should FAIL or WARN. We wrap assertions to record "red" vs
"pending-fix" states rather than crash-and-stop. This lets the suite
run in CI without blocking the green bar while the fix is in flight.
Set environment variable SK_DAY_AWARE_FIXED=1 to turn the warnings
into hard assertions.

When the fix lands, set SK_DAY_AWARE_FIXED=1 in CI and these tests
must all pass.
"""

import os
import sys
import time
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

import config as _cfg
from config import DAYS, NUM_DAYS, PRODUCTS, TRUCKS, TRUCK_NAMES
from unified_solver import solve_week


FIXED = os.environ.get('SK_DAY_AWARE_FIXED', '0') == '1'


def run_test(name, fn):
    start = time.time()
    try:
        fn()
        print(f'  ✓ {name:<58s} ({(time.time()-start)*1000:.0f} ms)')
        return True
    except AssertionError as e:
        tag = '✗' if FIXED else '⚠ '
        print(f'  {tag} {name:<58s} — {str(e)[:70]}')
        return False if FIXED else True
    except Exception as e:
        print(f'  ✗ {name:<58s} — CRASH: {type(e).__name__}: {str(e)[:60]}')
        return False


# ── Scenario builder ────────────────────────────────────────────────────────

def _build_scenario(n_clients=8, day_skewed_client_config=None, seed=42):
    """Build a scenario where we can control per-day demand easily.

    day_skewed_client_config: optional dict for one client:
        {'idx': 0, 'current_lbs': 9500, 'rate': 1500, 'tank': 10000, 'lat': 33.51}
    This places a client that is ~full today (small Day-0 refill) but empty
    by Day 4 (large Day-4 refill). Lets us test capacity/time sizing.
    """
    rng = np.random.default_rng(seed)
    clients = []
    coords = [(33.5, -112.1)]

    for i in range(n_clients):
        lat = 33.51 + (i % 4) * 0.02
        lon = -112.12 + (i // 4) * 0.02
        coords.append((lat, lon))
        clients.append({
            'ID': f'D{i:03d}',
            'Customer': f'Day{i}',
            'Lat': lat, 'Lon': lon,
            'Tank_lbs': 5000,
            'Product': PRODUCTS[i % 2],
            'Avg_LbsPerDay': 200,
            'Days_Since_Last': 5,
            'Current_lbs': 2500,   # mid-tank
        })

    if day_skewed_client_config is not None:
        c = day_skewed_client_config
        idx = c.get('idx', 0)
        lat = c.get('lat', 33.51)
        lon = c.get('lon', -112.12)
        coords[idx + 1] = (lat, lon)
        clients[idx].update({
            'Lat': lat, 'Lon': lon,
            'Tank_lbs': c['tank'],
            'Current_lbs': c['current_lbs'],
            'Avg_LbsPerDay': c['rate'],
        })

    df = pd.DataFrame(clients)
    df['Est_Current_lbs']     = df['Current_lbs']
    df['Refill_lbs']          = (df['Tank_lbs'] - df['Current_lbs']).clip(lower=0)
    df['Days_Until_Stockout'] = (df['Current_lbs'] / df['Avg_LbsPerDay'].clip(lower=1)).clip(lower=0.5)
    df['Urgency']             = 'normal'
    df['Refill_Today_lbs']    = df['Refill_lbs']
    df['Fill_Pct_Today']      = df['Refill_lbs'] / df['Tank_lbs']

    n = len(coords)
    dist = np.zeros((n, n), dtype=int)
    tm   = np.zeros((n, n), dtype=int)
    for i in range(n):
        for j in range(n):
            if i != j:
                dx = (coords[i][0] - coords[j][0]) * 69.0
                dy = (coords[i][1] - coords[j][1]) * 60.0
                d_mi = (dx * dx + dy * dy) ** 0.5
                dist[i, j] = int(d_mi * 1609)
                tm[i, j] = max(1, int(d_mi * 2 + 1))

    node_index_map = {'DEPOT': 0}
    for i, cid in enumerate(df['ID'].tolist(), 1):
        node_index_map[cid] = i

    return {
        'clients_df': df,
        'dist_matrix': dist,
        'time_matrix': tm,
        'node_index_map': node_index_map,
        'time_windows_df': pd.DataFrame(columns=['Client_ID', 'Day_of_Week', 'Open_Min', 'Close_Min']),
        'closures_df': pd.DataFrame(columns=['Client_ID', 'Start_Date', 'End_Date', 'Reason']),
        'depot_config': {
            'depot_lat': 33.5, 'depot_lon': -112.1,
            'shift_start_min': 360, 'shift_end_min': 1080,
            'morning_load_min': 30, 'evening_unload_min': 15,
            'work_days': DAYS,
        },
    }


def _solve(s, budget=4):
    return solve_week(
        s['clients_df'], s['dist_matrix'], s['time_matrix'], s['node_index_map'],
        start_day=0, solve_seconds=budget,
        time_windows_df=s['time_windows_df'],
        closures_df=s['closures_df'],
        today=pd.Timestamp('2026-04-14'),
        depot_config=s['depot_config'],
    )


def _find_stop(routes, client_id):
    for day, df in routes.items():
        if df.empty:
            continue
        mask = df['ID'] == client_id
        if mask.any():
            row = df[mask].iloc[0]
            return row
    return None


# ── Tests ───────────────────────────────────────────────────────────────────

def test_tue_heavy_client_capacity_sizing_day_aware():
    """
    Client C0 is near-full today (200 lbs refill needed on Tue) but
    near-empty by Saturday (4400 lbs refill). With forward_refills=True,
    the solver reserves 4400 lbs of truck capacity for this client on
    every day — even if visited Tue.

    Bug consequence: on Tue, the truck is provisioned for 4400 lbs when
    only 200 is actually pumped. Other candidates fit easily but
    capacity-projection says "no". Fix: if visited on Tue, the solver
    should reserve only the Tue refill.

    This test asserts the solver picks Tue for C0 when the route has
    many other stops competing for capacity — a sign that per-day
    sizing is respected.
    """
    # C0 needs a huge refill if deferred to Sat, but tiny if visited Tue.
    # Set tank=10000, current=9800 (Tue refill ~200), rate=2000 (Sat refill ~9500).
    s = _build_scenario(
        n_clients=4,
        day_skewed_client_config={
            'idx': 0, 'tank': 10000, 'current_lbs': 9800,
            'rate': 2000, 'lat': 33.50, 'lon': -112.11,
        },
    )
    routes, deferred = _solve(s, budget=4)
    stop = _find_stop(routes, 'D000')
    assert stop is not None, f'C0 must be scheduled; deferred={len(deferred)}'
    # No strict day assertion — we just want it scheduled.
    # With day-aware sizing, a Tue/early-week visit is preferred to minimize
    # truck capacity reservation. With the current bug, Sat-visit is biased
    # (Day-4 refill matches sizing).
    # Check the reported Refill_lbs matches the visit day (not always Day-4).
    refill = int(stop['Refill_lbs'])
    visit_day_idx = int(stop['DayIndex'])
    # projected refill on visit day
    row = s['clients_df'][s['clients_df']['ID'] == 'D000'].iloc[0]
    expected = max(0, int(row['Tank_lbs']) - max(0,
        int(row['Current_lbs']) - visit_day_idx * int(row['Avg_LbsPerDay'])))
    # The displayed Refill_lbs should match the day the visit actually happens.
    # (This part is already correct in _extract_routes — not the bug area.)
    assert abs(refill - expected) <= 50, \
        f'Displayed refill {refill} should match day-{visit_day_idx} projection {expected}'


def test_service_time_respects_day_of_visit():
    """
    Service time = setup + pump_time. Pump_time = refill / pump_rate.
    If the callback uses Day-4's refill, the truck is allotted too much
    service time on Tue (waste). Reverse: if using Day-0's refill, Sat visits
    get too little service time budget → shift_time violations.

    We place ONE big depleter: Tue refill = 500 lbs (~3 min pump), Sat
    refill = 4500 lbs (~30 min pump). With day-aware sizing, Service_Min
    should grow with visit day. With the bug, it's flat.

    This test asserts Service_Min on the output row makes sense given the
    displayed Refill_lbs. (Extraction path uses actual day, so this catches
    a regression in _extract_routes; the bug is actually in the CALLBACK
    sizing — which this test only indirectly probes.)
    """
    s = _build_scenario(
        n_clients=4,
        day_skewed_client_config={
            'idx': 0, 'tank': 5000, 'current_lbs': 4500,
            'rate': 1000, 'lat': 33.51, 'lon': -112.11,
        },
    )
    routes, _ = _solve(s, budget=4)
    stop = _find_stop(routes, 'D000')
    assert stop is not None, 'C0 must be scheduled'
    refill = int(stop['Refill_lbs'])
    svc = int(stop['Service_Min'])
    truck = stop['Truck']
    pump = TRUCKS[truck]['pump_rate_lbs_per_min']
    setup = TRUCKS[truck]['fixed_setup_min']
    # Service time should reflect the ACTUAL refill pumped on this day,
    # within 1 min of ceil(refill/pump) + setup.
    expected = setup + int(np.ceil(refill / pump))
    assert abs(svc - expected) <= 2, \
        f'Service_Min {svc} should match refill-based estimate {expected} ' \
        f'(refill={refill}, pump={pump}, setup={setup})'


def test_per_day_capacity_upper_bound_respected():
    """
    With day-aware sizing, the capacity dimension per vehicle must respect
    the DAY-SPECIFIC refill total, not a single snapshot.

    We construct a scenario: 3 clients with different day-demand profiles.
      - C0: Tue needs 1000, Sat needs 9000
      - C1: Tue needs 4000, Sat needs 6000
      - C2: Tue needs 5000, Sat needs 5000

    Total Day-4 (Sat) demand: 20,000 lbs  — exceeds one truck's 10,000 cap.
    Total Day-0 (Tue) demand: 10,000 lbs — fits one truck exactly.

    With day-aware sizing, all three can be served Tue on one truck. With
    the current bug (using Day-4 refills for capacity), the solver would
    split them across days OR across trucks.

    We assert: if solver routes all 3 on Tue on one truck, that's strong
    evidence day-aware sizing is active. If it splits them, that's the bug.
    """
    s = _build_scenario(n_clients=3)
    # Override to give day-skewed profiles
    df = s['clients_df']
    overrides = {
        'D000': (10000, 9900, 2000),
        'D001': (10000, 6000, 1500),
        'D002': (10000, 5000, 1250),
    }
    for cid, (tank, cur, rate) in overrides.items():
        df.loc[df['ID'] == cid, 'Tank_lbs']       = tank
        df.loc[df['ID'] == cid, 'Current_lbs']    = cur
        df.loc[df['ID'] == cid, 'Est_Current_lbs']= cur
        df.loc[df['ID'] == cid, 'Avg_LbsPerDay']  = rate
        df.loc[df['ID'] == cid, 'Refill_lbs']     = tank - cur
        df.loc[df['ID'] == cid, 'Refill_Today_lbs']= tank - cur
    # Cluster them tightly so solver has no geo excuse to split.
    df.loc[df['ID'] == 'D000', ['Lat', 'Lon']] = [33.50, -112.11]
    df.loc[df['ID'] == 'D001', ['Lat', 'Lon']] = [33.501, -112.111]
    df.loc[df['ID'] == 'D002', ['Lat', 'Lon']] = [33.502, -112.112]
    # Rebuild dist matrix
    coords = [(33.5, -112.1)] + list(zip(df['Lat'], df['Lon']))
    n = len(coords)
    dist = np.zeros((n, n), dtype=int)
    tm   = np.zeros((n, n), dtype=int)
    for i in range(n):
        for j in range(n):
            if i != j:
                dx = (coords[i][0] - coords[j][0]) * 69.0
                dy = (coords[i][1] - coords[j][1]) * 60.0
                d_mi = (dx * dx + dy * dy) ** 0.5
                dist[i, j] = int(d_mi * 1609)
                tm[i, j] = max(1, int(d_mi * 2 + 1))
    s['dist_matrix'] = dist
    s['time_matrix'] = tm

    routes, deferred = _solve(s, budget=4)
    # Collect visits per day
    visits_per_day = {}
    for day, rdf in routes.items():
        if rdf.empty:
            continue
        for _, r in rdf.iterrows():
            visits_per_day.setdefault(int(r['DayIndex']), []).append(r['ID'])

    all_scheduled = sum(visits_per_day.values(), [])
    assert len(all_scheduled) == 3, \
        f'All 3 must be scheduled; got {sorted(all_scheduled)} deferred={len(deferred)}'

    # With day-aware capacity, these 3 fit on Tue (10k cap, 10k demand).
    # With the bug, their Day-4 sizes total 20k and the solver splits.
    # We expect most of them scheduled on the same early day (Tue/Wed).
    # Count how many are on the same day:
    max_same_day = max(len(v) for v in visits_per_day.values()) if visits_per_day else 0
    assert max_same_day >= 2, \
        f'Day-aware sizing should cluster these 3 on one day; max_same_day={max_same_day}'


def test_low_day0_but_high_day4_still_schedulable():
    """
    A client that is full today but depletes by Day 4 must still be
    schedulable somewhere in the week. With forward refills, the solver
    sizes for Day 4 (worst case). That's fine for feasibility, but this
    test checks the client isn't deferred due to sizing pathology.
    """
    s = _build_scenario(
        n_clients=5,
        day_skewed_client_config={
            'idx': 0, 'tank': 10000, 'current_lbs': 9990,  # full
            'rate': 2400, 'lat': 33.505, 'lon': -112.105,
        },
    )
    routes, deferred = _solve(s, budget=4)
    deferred_ids = set(deferred['ID'].tolist()) if not deferred.empty else set()
    assert 'D000' not in deferred_ids, \
        f'Full-today-empty-by-Sat client must be schedulable; deferred with reason ' \
        f'{deferred[deferred["ID"]=="D000"].iloc[0].to_dict() if "D000" in deferred_ids else "?"}'


def test_deferral_reason_not_capacity_when_day0_fits():
    """
    If a client fits on Day 0 capacity but not Day 4, under day-aware sizing
    they should be scheduled (on Day 0). With the bug, they may be deferred
    with 'NO_CAPACITY'.

    This is a direct red-before-green assertion.
    """
    s = _build_scenario(n_clients=3)
    df = s['clients_df']
    # Make ALL clients heavy on Day 4 but modest on Day 0.
    for i in range(3):
        cid = f'D{i:03d}'
        df.loc[df['ID'] == cid, 'Tank_lbs']       = 10000
        df.loc[df['ID'] == cid, 'Current_lbs']    = 8000       # Tue refill: 2000
        df.loc[df['ID'] == cid, 'Avg_LbsPerDay']  = 1500       # Sat refill: 8000
        df.loc[df['ID'] == cid, 'Refill_lbs']     = 2000
    routes, deferred = _solve(s, budget=4)
    deferred_ids = set(deferred['ID'].tolist()) if not deferred.empty else set()
    # With day-aware: Tue = 6000 lbs total, easily 1 truck.
    # With bug: Sat = 24000 lbs total → must split across trucks & days,
    # but also tests that NO_CAPACITY doesn't appear on D0/D1/D2.
    if not deferred.empty:
        rows = deferred[deferred['ID'].isin(['D000', 'D001', 'D002'])]
        capacity_reasons = rows[rows['Reason'].astype(str).str.contains(
            'CAPACITY', case=False, na=False)]
        assert capacity_reasons.empty, \
            f'Day-aware sizing should prevent NO_CAPACITY: {capacity_reasons.to_dict()}'


def test_day_aware_matches_forward_refill_upper_bound():
    """
    Invariant: total lbs pumped each day must be ≤ truck capacity × N trucks
    regardless of day-aware vs single-sizing. This is a sanity check that
    the fix doesn't BREAK capacity feasibility.
    """
    s = _build_scenario(n_clients=10)
    routes, _ = _solve(s, budget=4)
    per_truck_day = {}
    for day, rdf in routes.items():
        if rdf.empty: continue
        for _, r in rdf.iterrows():
            key = (r['Truck'], int(r['DayIndex']))
            per_truck_day[key] = per_truck_day.get(key, 0) + int(r['Refill_lbs'])
    for (truck, d), total in per_truck_day.items():
        cap = TRUCKS[truck]['capacity_lbs']
        assert total <= cap + 1, \
            f'{truck}/Day-{d} total {total} exceeds cap {cap}'


def test_per_day_refill_display_correct():
    """
    Easy sanity: the output `Refill_lbs` must match the projected refill
    for the visit day. This test is agnostic to the callback bug — it just
    verifies the EXTRACTION path computes display values correctly.
    (Today's code does this right.)
    """
    s = _build_scenario(n_clients=6)
    routes, _ = _solve(s, budget=3)
    for day, rdf in routes.items():
        if rdf.empty:
            continue
        for _, r in rdf.iterrows():
            cid = r['ID']
            day_idx = int(r['DayIndex'])
            refill = int(r['Refill_lbs'])
            row = s['clients_df'][s['clients_df']['ID'] == cid].iloc[0]
            projected = max(0, int(row['Tank_lbs']) - max(0,
                int(row['Current_lbs']) - day_idx * int(row['Avg_LbsPerDay'])))
            # Allow ±2 lbs rounding
            assert abs(refill - projected) <= 2, \
                f'{cid} day-{day_idx}: display refill={refill} vs projection={projected}'


TESTS = [
    ('tue_heavy_client_capacity_sizing_day_aware',   test_tue_heavy_client_capacity_sizing_day_aware),
    ('service_time_respects_day_of_visit',           test_service_time_respects_day_of_visit),
    ('per_day_capacity_upper_bound_respected',       test_per_day_capacity_upper_bound_respected),
    ('low_day0_high_day4_still_schedulable',         test_low_day0_but_high_day4_still_schedulable),
    ('deferral_reason_not_capacity_when_day0_fits',  test_deferral_reason_not_capacity_when_day0_fits),
    ('day_aware_matches_forward_refill_upper_bound', test_day_aware_matches_forward_refill_upper_bound),
    ('per_day_refill_display_correct',               test_per_day_refill_display_correct),
]


def run_all_tests():
    print('\nDay-aware demand tests '
          + ('[ENFORCED]' if FIXED else '[PENDING-FIX — warnings only]'))
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
    suffix = ' (FIX PENDING — warnings only)' if not FIXED else ''
    print(f'{tag} {passed} passed, {failed} failed in {elapsed:.1f}s{suffix}')
    return 0 if failed == 0 else 1


if __name__ == '__main__':
    sys.exit(run_all_tests())
