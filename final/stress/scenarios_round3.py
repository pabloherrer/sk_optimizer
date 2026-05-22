"""
Round-3 stress scenarios — derived from the Critic's findings (CRITIQUE.md).

This round addresses:
  - Boundary cases the prior rounds missed (DTE = 2.0 exactly, etc.)
  - Direct regression tests for the 4 round-1 fixes
  - Custom assertions (no_route_on, must_defer_with_reason) that round 1/2
    could not express
  - Variable refill distributions to probe the p75 capacity dimension
  - Coverage of per-client target_empty_fraction (not yet)
"""
from __future__ import annotations
from datetime import date
from final.stress.scenario_lib import build, client, truck, depot
from v2.domain.overrides import Pin, Forbid, Overrides


# ════════════════════════════════════════════════════════════════════════════
# CATEGORY P: BOUNDARY CASES (from Critic R3-02 + RC-1 boundary probe)
# ════════════════════════════════════════════════════════════════════════════

def scenario_p01_dte_exactly_2_is_hard():
    """RC-1 boundary: client at days_supply EXACTLY 2.0.

    Spec says `< 2` → HARD, `< horizon` → HIGH. At exactly 2.0 the client
    should fall into HIGH (not HARD). With a 5-day horizon, HIGH-tier
    means $500 drop penalty — must serve.

    Setup: 2 clients. A at DTE=2.0 (current=400, rate=200). B at DTE=1.99
    (current=398, rate=200). Both should be served; A is HIGH, B is HARD.

    Confirms: both served within horizon.
    Refutes: if A is deferred (off-by-one bug at HARD/HIGH boundary).
    """
    p = build(
        today='2026-05-22',
        horizon_days=5,
        depot=depot(33.515, -112.167),
        trucks=[truck('Truck2')],
        clients=[
            client('A_EQ2', tank=1000, rate=200, current=400, lat=33.520, lon=-112.170),
            client('B_LT2', tank=1000, rate=200, current=398, lat=33.521, lon=-112.171),
        ],
    )
    expected = {
        'description': 'DTE exactly 2.0 — HARD/HIGH boundary test',
        'hypothesis': 'Both clients served (A: HIGH $500, B: HARD $10k)',
        'must_serve': ['A_EQ2', 'B_LT2'],
        'no_overflow': True,
    }
    return p, expected


def scenario_p02_dte_exactly_horizon_is_high():
    """RC-1 boundary: client where days_supply = horizon_days exactly.

    Spec says `< horizon` → HIGH, else MED. At exactly horizon, falls
    into MED. With $35 MED penalty, should still be served (penalty
    exceeds marginal route cost).
    """
    p = build(
        today='2026-05-22',
        horizon_days=5,
        depot=depot(33.515, -112.167),
        trucks=[truck('Truck2')],
        clients=[
            # rate=100, current=500 → DTE=5.0 = horizon
            client('EQ_HORIZON', tank=1000, rate=100, current=500,
                    lat=33.520, lon=-112.170),
        ],
    )
    expected = {
        'description': 'days_supply == horizon exactly — MED boundary',
        'hypothesis': 'EQ_HORIZON served (MED penalty $35 exceeds route cost)',
        'must_serve': ['EQ_HORIZON'],
    }
    return p, expected


# ════════════════════════════════════════════════════════════════════════════
# CATEGORY Q: REGRESSION FOR ROUND-1 FIXES
# ════════════════════════════════════════════════════════════════════════════

def scenario_q01_fix1_hard_floor_blocks_5lb_visit():
    """Regression for Bug #1 (hard-floor refill in model).

    Slow-consumption client where ALL horizon days have refill < 50 lbs.
    With rate=4 lpd, current=995, tank=1000, 10-day horizon:
      day 0: refill=5, day 1: refill=9, ..., day 9: refill=41 — all <50.

    Pre-fix: solver schedules on day 9 (49 lbs), invariant rejects.
    Post-fix: all days forbidden → must defer.
    """
    p = build(
        today='2026-05-22',
        horizon_days=10,
        depot=depot(33.515, -112.167),
        trucks=[truck('Truck2')],
        clients=[
            client('SLOW', tank=1000, rate=4, current=995,
                    lat=33.520, lon=-112.170),
        ],
    )
    expected = {
        'description': 'Regression #1: ALL horizon days have refill < 50 hard floor',
        'hypothesis': 'SLOW deferred (all horizon refills < 50)',
        'must_defer': ['SLOW'],
    }
    return p, expected


def scenario_q02_fix2_pin_locks_to_date():
    """Regression for Bug #2 (pin must lock to date).

    Client pinned to Saturday. Plenty of capacity on Friday. Solver MUST
    serve on the pinned date, not Friday.

    Setup: 1 client pinned to Sat 2026-05-23. Horizon = Fri + Sat.
    """
    pin = Pin(client_id='P', date=date(2026, 5, 23), reason='reg')
    p = build(
        today='2026-05-22',
        horizon_days=2,
        depot=depot(33.515, -112.167),
        trucks=[truck('Truck2')],
        clients=[client('P', tank=1000, rate=100, current=500,
                         lat=33.520, lon=-112.170)],
        overrides=Overrides(pins=(pin,)),
    )
    expected = {
        'description': 'Regression #2: pin locks to date even when other day is cheaper',
        'hypothesis': 'P served on Sat May 23 (Truck2), NOT on Fri',
        'must_serve_specific': [('2026-05-23', 'Truck2', 'P')],
        'no_route_on': [('2026-05-22', 'Truck2')],
    }
    return p, expected


def scenario_q03_fix3_forbid_relaxes_commit_window():
    """Regression for Bug #3 (forbid+commit interaction).

    Urgent client (DTE=1d) is forbidden day 0 — commit-window logic must
    relax to allow day 1 instead of deferring.

    Setup: 1 client, DTE=1.0, forbidden day 0. Horizon = Fri + Sat.
    """
    forbid = Forbid(client_id='U', dates=(date(2026, 5, 22),),
                     reason='client closed Fri')
    p = build(
        today='2026-05-22',
        horizon_days=2,
        depot=depot(33.515, -112.167),
        trucks=[truck('Truck2')],
        clients=[client('U', tank=1000, rate=200, current=200,
                         lat=33.520, lon=-112.170)],
        overrides=Overrides(forbids=(forbid,)),
    )
    expected = {
        'description': 'Regression #3: forbid on commit day → relax to next day',
        'hypothesis': 'U served on Sat (commit-window relaxed by forbid)',
        'must_serve_by_day': {'2026-05-23': ['U']},
        'no_route_on': [('2026-05-22', 'Truck2')],
    }
    return p, expected


def scenario_q04_fix4_zero_rate_deferred_with_reason():
    """Regression for Bug #4 (zero-rate clients must defer cleanly).

    Setup: 1 client with rate=0. Should defer with reason INSUFFICIENT.
    """
    p = build(
        today='2026-05-22',
        horizon_days=3,
        depot=depot(33.515, -112.167),
        trucks=[truck('Truck2')],
        clients=[client('NORATE', tank=1000, rate=0, current=500,
                         lat=33.520, lon=-112.170)],
    )
    expected = {
        'description': 'Regression #4: rate=0 client defers with proper reason',
        'must_defer_with_reason': {'NORATE': 'INSUFFICIENT_CONSUMPTION_DATA'},
    }
    return p, expected


# ════════════════════════════════════════════════════════════════════════════
# CATEGORY R: SATURDAY RULE — proper assertion (per Critic R3-05)
# ════════════════════════════════════════════════════════════════════════════

def scenario_r01_saturday_no_truck9_assertion():
    """RC-8 with PROPER assertion. Force Saturday work; verify Truck9 idle.

    Setup: 8 urgent clients, total Friday service > Truck2 capacity. Some
    work must spill to Saturday. Truck9 cannot run Sat.
    """
    cs = []
    for i in range(8):
        cs.append(client(f'S{i}', tank=400, rate=200, current=200,
                          lat=33.50 + 0.02 * i, lon=-112.20 + 0.02 * i))
    p = build(
        today='2026-05-22',
        horizon_days=2,
        depot=depot(33.515, -112.167),
        trucks=[truck('Truck2'), truck('Truck9')],
        clients=cs,
    )
    expected = {
        'description': 'Saturday rule with custom no_route_on assertion',
        'hypothesis': 'Truck9 may not have a Saturday route',
        'must_serve': [f'S{i}' for i in range(8)],
        'no_route_on': [('2026-05-23', 'Truck9')],
    }
    return p, expected


# ════════════════════════════════════════════════════════════════════════════
# CATEGORY S: CAPACITY DIMENSION (Critic R3-03 — variable refill probe)
# ════════════════════════════════════════════════════════════════════════════

def scenario_s01_variable_refill_p75_capacity():
    """RC-4 probe: client whose refill grows over horizon — p75 demand
    estimate should let multiple clients fit on one truck-day even though
    max refill would exceed compartment.

    Setup: 3 clients. Big has tank=2000, current=0, rate=100. Refills:
    day 0 = 2000, day 4 = 2000 (always at cap), so max = p75 = 2000.
    Plus 3 small clients refill 300 each.
    Truck cap 10000, compartment 5000.

    Confirms: all 4 clients can fit on truck (sum demand = 2000+900=2900 < 5000).
    """
    p = build(
        today='2026-05-22',
        horizon_days=5,
        depot=depot(33.515, -112.167),
        trucks=[truck('Truck2', cap=10000, compartments=2, compartment_cap=5000)],
        clients=[
            client('BIG',    tank=2000, rate=100, current=0,   lat=33.520, lon=-112.170),
            client('SMALL1', tank=400,  rate=80,  current=100, lat=33.521, lon=-112.171),
            client('SMALL2', tank=400,  rate=80,  current=100, lat=33.522, lon=-112.172),
            client('SMALL3', tank=400,  rate=80,  current=100, lat=33.523, lon=-112.173),
        ],
    )
    expected = {
        'description': 'p75 capacity allows mixing variable-refill + small clients',
        'must_serve': ['BIG', 'SMALL1', 'SMALL2', 'SMALL3'],
        'no_overflow': True,
    }
    return p, expected


# ════════════════════════════════════════════════════════════════════════════
# CATEGORY T: COMMIT-WINDOW OVERFLOW (Critic R3-01)
# ════════════════════════════════════════════════════════════════════════════

def scenario_t01_commit_overflow_uses_both_trucks():
    """RC-7 / Critic R3-01: more urgent clients than one truck-day can serve.

    Setup: 14 urgent (DTE=1.0) clients in a tight cluster. 2 trucks,
    commit_days=1. Single truck day = ~280 min (over target 304? Let's
    see: 14 stops × ~20 min stop time = 280 min, plus travel). Both
    trucks needed.
    """
    cs = []
    for i in range(14):
        row, col = divmod(i, 4)
        cs.append(client(f'U{i:02d}', tank=400, rate=200, current=200,
                          lat=33.50 + 0.02 * row, lon=-112.20 + 0.02 * col))
    p = build(
        today='2026-05-22',
        horizon_days=2,
        depot=depot(33.515, -112.167),
        trucks=[truck('Truck2'), truck('Truck9')],
        clients=cs,
    )
    expected = {
        'description': 'Commit overflow forces both trucks on day 0',
        'must_serve_by_day': {'2026-05-22': [f'U{i:02d}' for i in range(14)]},
        'must_use_trucks': ['Truck2', 'Truck9'],
    }
    return p, expected


# ════════════════════════════════════════════════════════════════════════════
# CATEGORY U: DNS + PIN (Critic R3-04)
# ════════════════════════════════════════════════════════════════════════════

def scenario_u01_dns_beats_pin():
    """RC-8 / Critic R3-04: DNS must beat Pin (business rule).

    Setup: 1 client, do_not_schedule=True, also pinned. Per business rule
    DNS wins; client deferred with DNS reason.
    """
    pin = Pin(client_id='DNSPIN', date=date(2026, 5, 22), reason='operator error')
    p = build(
        today='2026-05-22',
        horizon_days=2,
        depot=depot(33.515, -112.167),
        trucks=[truck('Truck2')],
        clients=[client('DNSPIN', tank=1000, rate=200, current=200,
                         lat=33.520, lon=-112.170, do_not_schedule=True)],
        overrides=Overrides(pins=(pin,)),
    )
    expected = {
        'description': 'DNS overrides Pin (business rule)',
        'hypothesis': 'DNSPIN deferred with DO_NOT_SCHEDULE — DNS wins',
        'must_defer_with_reason': {'DNSPIN': 'DO_NOT_SCHEDULE'},
    }
    return p, expected
