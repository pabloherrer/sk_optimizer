"""
Stress-test scenarios for sk_solver_final.

Each scenario is a no-arg function returning (ProblemInstance, expected_dict).
The Executor runs each via final/stress/runner.py and produces EXECUTION_LOG.md.

Methodology: every scenario was designed by reading sk_solver_final.py end-to-end
and ROOT_CAUSES.md. Each scenario has:
  - hypothesis: the specific behavior we expect to verify
  - expected:   bounds on the plan that PASS means the hypothesis held
  - what would CONFIRM the bug if the assertion fails

Date convention: 2026-05-22 is Friday. Working_days default = Tue–Sat.
Horizon dates skip Sun/Mon — be careful when interpreting day indices.
"""
from __future__ import annotations

from datetime import date
from final.stress.scenario_lib import build, client, truck, depot
from v2.domain.overrides import Pin, Forbid, Overrides


# ════════════════════════════════════════════════════════════════════════════
# CATEGORY A: DROP-PENALTY TIER BOUNDARIES (RC-1)
# ════════════════════════════════════════════════════════════════════════════

def scenario_a01_urgent_must_serve():
    """RC-1 / HARD tier: client with days_supply<2 MUST be served on day 0.

    Setup: 3 clients. URGENT has 1.5d supply (HARD). MID has 4d supply (HIGH).
    SAFE has 50d supply (LOW). One truck, 3-day horizon.

    Confirms: HARD-tier client (DTE<2) lands on day 0.
    Refutes: if URGENT is deferred or scheduled on day 1+.
    """
    p = build(
        today='2026-05-22',
        horizon_days=3,
        depot=depot(33.515, -112.167),
        trucks=[truck('Truck2')],
        clients=[
            client('URGENT', tank=1000, rate=200, current=300, lat=33.520, lon=-112.170),  # 1.5d
            client('MID',    tank=1000, rate=100, current=400, lat=33.525, lon=-112.175),  # 4d
            client('SAFE',   tank=1000, rate=20,  current=1000, lat=33.530, lon=-112.180), # 50d
        ],
    )
    expected = {
        'description': 'HARD-tier (DTE<2) must be served in commit window',
        'hypothesis': 'URGENT scheduled day 0; SAFE deferred (LOW); MID served somewhere',
        'must_serve': ['URGENT'],
        'must_serve_by_day': {'2026-05-22': ['URGENT']},
        'no_stockouts': True,
        'no_overflow': True,
    }
    return p, expected


def scenario_a02_safe_client_deferred():
    """RC-1 / LOW tier: full-tank client whose days_to_target > horizon should defer.

    Setup: 2 clients. URGENT (DTE 1d). VERY_SAFE has 1000 lbs tank, rate 5 lpd
    → 200 days supply → LOW. Should be deferred.

    Confirms: LOW-tier client deferred.
    Refutes: if VERY_SAFE shows up in the plan (drop penalty miscalibrated).
    """
    p = build(
        today='2026-05-22',
        horizon_days=2,
        depot=depot(33.515, -112.167),
        trucks=[truck('Truck2')],
        clients=[
            client('URGENT',    tank=1000, rate=200, current=200, lat=33.520, lon=-112.170),
            client('VERY_SAFE', tank=1000, rate=5,   current=1000, lat=33.530, lon=-112.180),
        ],
    )
    expected = {
        'description': 'LOW-tier (days_to_target >> horizon) should be deferred',
        'hypothesis': 'VERY_SAFE deferred (drop penalty $5 cheaper than route cost)',
        'must_serve': ['URGENT'],
        'must_defer': ['VERY_SAFE'],
        'no_overflow': True,
    }
    return p, expected


def scenario_a03_horizon_edge_med_tier():
    """RC-1 boundary: client whose days_to_target ≈ horizon_days = MED tier.

    Setup: client A has 8 days of supply, target_empty_fraction=0.30
    Tank 1000, current 800, rate 100 → days_to_target = (800-300)/100 = 5 days
    Horizon = 5 working days. days_to_target == horizon = MED boundary.

    Confirms: MED-tier deferable client IS served (drop penalty $35 > avg route cost).
    Refutes: if A is deferred (MED penalty miscalibrated).
    """
    p = build(
        today='2026-05-22',
        horizon_days=5,
        depot=depot(33.515, -112.167),
        trucks=[truck('Truck2')],
        clients=[
            client('A', tank=1000, rate=100, current=800, lat=33.520, lon=-112.170),
        ],
    )
    expected = {
        'description': 'MED-tier boundary: days_to_target == horizon should be served',
        'hypothesis': 'A served (MED $35 drop penalty > marginal route cost of ~$3)',
        'must_serve': ['A'],
        'no_overflow': True,
    }
    return p, expected


# ════════════════════════════════════════════════════════════════════════════
# CATEGORY B: BOTH-TRUCKS DAILY (RC-2, RC-3)
# ════════════════════════════════════════════════════════════════════════════

def scenario_b01_split_for_ot_savings():
    """RC-3 corrected hypothesis: splitting saves money ONLY when single-truck
    would trigger OT premium. Build a workload that overflows target on one
    truck and verify the spread.

    Setup: 8 clients in a tight grid. Single truck: ~140 min/stop × 8 ≈ 320
    min > target 304 min → ~16 min OT. Both trucks: 4 each = 70 min × 4 =
    280 min/truck < target → no OT. Splitting saves ~16 min × $0.42 = $6.70.
    Per-extra-mile cost of splitting (~5 mi) = $2.75. Net: split wins.

    Original hypothesis (2 far clients should split) was wrong: for those,
    miles dominate and single-truck minimizes total mi.

    Confirms: both trucks dispatched.
    Refutes: single truck (OT premium too small to overcome route doubling).
    """
    cs = []
    base_lat, base_lon = 33.50, -112.20
    for i in range(8):
        cs.append(client(f'C{i}', tank=1000, rate=150, current=300,
                          lat=base_lat + 0.01 * i, lon=base_lon + 0.01 * i))
    p = build(
        today='2026-05-22',
        horizon_days=1,
        depot=depot(33.515, -112.167),
        trucks=[truck('Truck2'), truck('Truck9')],
        clients=cs,
    )
    expected = {
        'description': '8-stop day should split to avoid OT premium',
        'hypothesis': 'Spreading 8 stops across both trucks beats single-truck-OT',
        'must_use_trucks': ['Truck2', 'Truck9'],
        'min_total_stops': 8,
        'max_total_stops': 8,
        'no_overflow': True,
    }
    return p, expected


def scenario_b02_consolidate_when_cheaper():
    """RC-3 inverse: two near-neighbor clients should consolidate on ONE truck.

    Setup: 2 clients 0.5 mi apart. 2 trucks. Single day.
    Sending both trucks adds dispatch overhead with no benefit.
    Note: dispatch=$0 means the solver is indifferent — could go either way.
    Verify it doesn't WASTEFULLY split.

    Confirms: only one truck dispatched (single short route).
    Refutes: both trucks dispatched on 1 stop each (weird with dispatch=$0).
    """
    p = build(
        today='2026-05-22',
        horizon_days=1,
        depot=depot(33.515, -112.167),
        trucks=[truck('Truck2'), truck('Truck9')],
        clients=[
            client('A', tank=1000, rate=100, current=200, lat=33.520, lon=-112.170),
            client('B', tank=1000, rate=100, current=200, lat=33.521, lon=-112.171),
        ],
    )
    expected = {
        'description': 'Two near-neighbor clients should consolidate on one truck',
        'hypothesis': 'Truck route is total 1 (with both A and B) — no wasteful split',
        'must_serve': ['A', 'B'],
        'max_total_stops': 2,
        'no_overflow': True,
    }
    return p, expected


# ════════════════════════════════════════════════════════════════════════════
# CATEGORY C: CAPACITY DIMENSION (RC-4)
# ════════════════════════════════════════════════════════════════════════════

def scenario_c01_no_overflow_on_huge_refill_day():
    """RC-4: A client whose refill GROWS over horizon should still not be
    over-served on its biggest-refill day.

    Setup: Client BIG has tank 2000, current 0, rate 200. Refills:
    Day 0: 2000, Day 1: 2000 (capped at tank), Day 4: 2000.
    Truck has only 1000 capacity. So BIG can only be partially served — but
    the model's _check_no_tank_overflow should still ensure we don't deliver
    more than the actual tank room.

    Confirms: BIG is served with delivery ≤ 2000 lbs (no overflow).
    Refutes: if delivery exceeds tank capacity.
    """
    p = build(
        today='2026-05-22',
        horizon_days=3,
        depot=depot(33.515, -112.167),
        trucks=[truck('Truck2', cap=2500, compartment_cap=2500)],  # generous truck
        clients=[
            client('BIG', tank=2000, rate=200, current=0, lat=33.52, lon=-112.17),
        ],
    )
    expected = {
        'description': 'Empty 2000-lb tank should be filled, never exceeded',
        'hypothesis': 'BIG served, delivery exactly equals refill ≤ 2000 lbs',
        'must_serve': ['BIG'],
        'no_overflow': True,
        'min_total_stops': 1,
        'max_total_stops': 1,
    }
    return p, expected


def scenario_c02_truck_capacity_enforces():
    """RC-4: Total truck capacity (sum of refills) should constrain. With a
    truck holding 1000 lbs and 4 clients each needing ~600 lbs, the solver
    must defer or split.

    Setup: 4 needy clients, 1 truck cap 1000 lbs. Single day.
    Total need ≈ 2400 lbs. Truck can do AT MOST 2 stops in one day.

    Confirms: ≤2 stops AND total deliveries ≤ 1000 lbs.
    Refutes: 3+ stops or overflow.
    """
    p = build(
        today='2026-05-22',
        horizon_days=1,
        depot=depot(33.515, -112.167),
        trucks=[truck('Truck2', cap=1000, compartment_cap=1000)],
        clients=[
            client('A', tank=700, rate=100, current=100, lat=33.520, lon=-112.170),  # need ~600
            client('B', tank=700, rate=100, current=100, lat=33.522, lon=-112.172),
            client('C', tank=700, rate=100, current=100, lat=33.524, lon=-112.174),
            client('D', tank=700, rate=100, current=100, lat=33.526, lon=-112.176),
        ],
    )
    expected = {
        'description': 'Truck capacity 1000 lbs caps single-day deliveries',
        'hypothesis': 'Solver respects truck cap — at most 2 stops, ≤ 1000 lbs total',
        'max_total_stops': 2,
        'max_total_lbs': 1100,  # small slack for rounding
        'no_overflow': True,
    }
    return p, expected


# ════════════════════════════════════════════════════════════════════════════
# CATEGORY D: COMMIT WINDOW (RC-7)
# ════════════════════════════════════════════════════════════════════════════

def scenario_d01_lock_to_commit_window():
    """RC-7: DTE ≤ commit_days+0.5 forces day 0 visit.

    Setup: commit_days=1, buffer=0.5. SOON has DTE=1.0 (must lock to day 0).
    LATER has DTE=3.0 (not locked). 2-day horizon.

    Confirms: SOON served on day 0; LATER may be on day 0 or 1.
    Refutes: if SOON is on day 1.
    """
    p = build(
        today='2026-05-22',
        horizon_days=2,
        depot=depot(33.515, -112.167),
        trucks=[truck('Truck2')],
        clients=[
            client('SOON',  tank=1000, rate=200, current=200, lat=33.520, lon=-112.170),
            client('LATER', tank=1000, rate=200, current=600, lat=33.522, lon=-112.172),
        ],
        commit_days=1,
    )
    expected = {
        'description': 'SOON (DTE=1.0) must be locked to day 0 (commit+buffer)',
        'hypothesis': 'SOON served on 2026-05-22; LATER not locked',
        'must_serve_by_day': {'2026-05-22': ['SOON']},
        'must_serve': ['SOON', 'LATER'],
        'no_stockouts': True,
    }
    return p, expected


def scenario_d02_commit_window_no_capacity():
    """RC-7 edge: what if commit-window has too many locks for one truck?
    Solver must still serve all locked clients (they have HARD penalty).

    Setup: 5 urgent clients, 1 truck, 2-day horizon. All 5 have DTE=1.0.
    All must serve in commit window (day 0). Single truck day 0 must do 5.

    Confirms: all 5 on day 0.
    Refutes: if any urgent client is deferred or pushed to day 1.
    """
    p = build(
        today='2026-05-22',
        horizon_days=2,
        depot=depot(33.515, -112.167),
        trucks=[truck('Truck2')],
        clients=[
            client('U1', tank=400, rate=200, current=200, lat=33.520, lon=-112.170),
            client('U2', tank=400, rate=200, current=200, lat=33.522, lon=-112.172),
            client('U3', tank=400, rate=200, current=200, lat=33.524, lon=-112.174),
            client('U4', tank=400, rate=200, current=200, lat=33.526, lon=-112.176),
            client('U5', tank=400, rate=200, current=200, lat=33.528, lon=-112.178),
        ],
        commit_days=1,
    )
    expected = {
        'description': 'All 5 urgent clients must hit day 0 (commit window)',
        'hypothesis': 'All 5 on Fri May 22; none deferred or pushed',
        'must_serve_by_day': {'2026-05-22': ['U1', 'U2', 'U3', 'U4', 'U5']},
        'no_stockouts': True,
        'no_overflow': True,
    }
    return p, expected


# ════════════════════════════════════════════════════════════════════════════
# CATEGORY E: SATURDAY RULE (RC-8)
# ════════════════════════════════════════════════════════════════════════════

def scenario_e01_saturday_no_truck9():
    """RC-8: Saturday is Truck2-only. Truck9 must not run on Saturday even
    if there are clients to serve.

    Setup: 4 clients needing service, today is Friday → Sat in horizon.
    2 trucks. Truck9 MUST NOT dispatch on Sat May 23.

    Confirms: no (Sat, Truck9) route.
    Refutes: if Truck9 has stops on Saturday.
    """
    p = build(
        today='2026-05-22',
        horizon_days=2,        # Fri + Sat
        depot=depot(33.515, -112.167),
        trucks=[truck('Truck2'), truck('Truck9')],
        clients=[
            client('A', tank=400, rate=200, current=300, lat=33.520, lon=-112.170),
            client('B', tank=400, rate=200, current=300, lat=33.522, lon=-112.172),
            client('C', tank=400, rate=200, current=300, lat=33.524, lon=-112.174),
            client('D', tank=400, rate=200, current=300, lat=33.526, lon=-112.176),
        ],
    )
    expected = {
        'description': 'Truck9 may not run on Saturday',
        'hypothesis': 'No (Sat May 23, Truck9) route exists',
        'must_serve': ['A', 'B', 'C', 'D'],
        # Note: must_not_use_trucks rejects truck use across ALL days.
        # We can't directly assert "no truck9 on Sat" with current schema.
        # Approach: inspect routes manually in expected_dict via custom check
        # (executor records the day×truck routes — we can verify post-hoc).
    }
    return p, expected


# ════════════════════════════════════════════════════════════════════════════
# CATEGORY F: PIN / FORBID OVERRIDES
# ════════════════════════════════════════════════════════════════════════════

def scenario_f01_pin_forces_service():
    """Pin must override drop-penalty tier AND force the pinned date.

    Setup: 1 client (SAFE_PINNED), 60% full tank (so refill is feasible),
    very slow consumption — would normally be LOW-tier (200 days supply).
    Pinned to day 0 (2026-05-22). Must be served on that exact day.

    Confirms: SAFE_PINNED served on day 0.
    Refutes: SAFE_PINNED deferred OR served on day 1+ (Pin precedence/date broken).
    """
    pin = Pin(client_id='SAFE_PINNED', date=date(2026, 5, 22),
               reason='operator test')
    p = build(
        today='2026-05-22',
        horizon_days=2,
        depot=depot(33.515, -112.167),
        trucks=[truck('Truck2')],
        clients=[
            # current=600, tank=1000 → day-0 refill=400 lbs (feasible).
            # rate=5 → days_supply=120 → would be LOW-tier without pin.
            client('SAFE_PINNED', tank=1000, rate=5, current=600,
                    lat=33.520, lon=-112.170),
        ],
        overrides=Overrides(pins=(pin,)),
    )
    expected = {
        'description': 'Pin overrides LOW-tier deferral AND locks to date',
        'hypothesis': 'SAFE_PINNED served on 2026-05-22 (Fri) despite 120d supply',
        'must_serve': ['SAFE_PINNED'],
        'must_serve_by_day': {'2026-05-22': ['SAFE_PINNED']},
    }
    return p, expected


def scenario_f02_forbid_blocks_service():
    """Forbid: client must NOT be served on the forbidden dates.

    Setup: 2 clients. URGENT_FORBID has DTE=1 BUT is forbidden on day 0.
    Solver must defer to day 1.

    Confirms: URGENT_FORBID served on day 1, not day 0.
    Refutes: if URGENT_FORBID is on day 0 (Forbid not honored).
    """
    forbid = Forbid(client_id='URGENT_FORBID',
                     dates=(date(2026, 5, 22),),
                     reason='client closed')
    p = build(
        today='2026-05-22',
        horizon_days=2,
        depot=depot(33.515, -112.167),
        trucks=[truck('Truck2')],
        clients=[
            client('URGENT_FORBID', tank=1000, rate=200, current=200,
                    lat=33.520, lon=-112.170),
        ],
        overrides=Overrides(forbids=(forbid,)),
    )
    expected = {
        'description': 'Forbid must prevent service on listed date',
        'hypothesis': 'URGENT_FORBID NOT on day 0 (Fri May 22); served on day 1 (Sat May 23)',
        'must_serve_by_day': {'2026-05-23': ['URGENT_FORBID']},
        # Note: we can't directly assert "must NOT be served on day X" with
        # current schema. The runner reports day×stop routes — manual check.
    }
    return p, expected


# ════════════════════════════════════════════════════════════════════════════
# CATEGORY G: DNS / EXCLUDED CLIENTS
# ════════════════════════════════════════════════════════════════════════════

def scenario_g01_dns_never_scheduled():
    """DNS client must never be served regardless of urgency.

    Setup: DNS client at STOCKOUT (DTE=0). Plus a normal urgent client.
    Solver must serve URGENT, never DNS_CLIENT.

    Confirms: DNS_CLIENT in deferred list.
    Refutes: DNS_CLIENT in plan (violates business rule).
    """
    p = build(
        today='2026-05-22',
        horizon_days=2,
        depot=depot(33.515, -112.167),
        trucks=[truck('Truck2')],
        clients=[
            client('DNS_CLIENT', tank=1000, rate=200, current=0,
                    lat=33.520, lon=-112.170, do_not_schedule=True),
            client('URGENT', tank=1000, rate=200, current=200,
                    lat=33.522, lon=-112.172),
        ],
    )
    expected = {
        'description': 'DNS clients must never be scheduled even when stocked-out',
        'hypothesis': 'DNS_CLIENT deferred; URGENT served',
        'must_serve': ['URGENT'],
        'must_defer': ['DNS_CLIENT'],
    }
    return p, expected


# ════════════════════════════════════════════════════════════════════════════
# CATEGORY H: SOFT SMALL-STOP FEE (RC-6)
# ════════════════════════════════════════════════════════════════════════════

def scenario_h01_opportunistic_topoff_taken():
    """Opportunistic 100-lb top-off near a 600-lb stop should be taken.

    Setup: MAIN needs 600 lbs. TOPOFF is right next door, needs only 100 lbs.
    Total detour for TOPOFF: <0.5 mi → cost < $0.30. Small-stop fee = $1.50.
    Even with fee, taking it is cheap if detour cost is small.

    NB: actually small_stop_fee ($1.50) > detour ($0.30), so this may NOT
    be taken. The hypothesis is: solver picks up TOPOFF only if detour
    cost < fee. With clients 0.5 mi apart, detour ≈ $0.30, fee = $1.50.
    Solver should NOT take it.

    Confirms: TOPOFF deferred OR served (we observe).
    """
    p = build(
        today='2026-05-22',
        horizon_days=1,
        depot=depot(33.515, -112.167),
        trucks=[truck('Truck2')],
        clients=[
            client('MAIN',   tank=1000, rate=200, current=400, lat=33.520, lon=-112.170),
            client('TOPOFF', tank=1000, rate=20,  current=900, lat=33.521, lon=-112.171),
        ],
    )
    expected = {
        'description': 'Opportunistic small top-off near a real stop',
        'hypothesis': 'MAIN served; TOPOFF may or may not be taken (observe)',
        'must_serve': ['MAIN'],
        # No must_serve/must_defer on TOPOFF — informational
    }
    return p, expected


# ════════════════════════════════════════════════════════════════════════════
# CATEGORY I: FILTERED INPUTS (DNS, missing data)
# ════════════════════════════════════════════════════════════════════════════

def scenario_i01_no_rate_client_deferred():
    """A client with zero consumption rate (no historical data) must be
    deferred with reason INSUFFICIENT_CONSUMPTION_DATA — never silently
    crashed nor mis-scheduled.

    Setup: 1 client with rate=0.

    Confirms: NORATE deferred (insufficient data).
    Refutes: NORATE served (model would have to imagine consumption).
    """
    p = build(
        today='2026-05-22',
        horizon_days=2,
        depot=depot(33.515, -112.167),
        trucks=[truck('Truck2')],
        clients=[
            client('NORATE', tank=1000, rate=0.0, current=500, lat=33.520, lon=-112.170),
        ],
    )
    expected = {
        'description': 'Zero-rate client is gracefully deferred',
        'hypothesis': 'NORATE deferred (drop penalty LOW + refill forbids day 0)',
        'must_defer': ['NORATE'],
    }
    return p, expected
