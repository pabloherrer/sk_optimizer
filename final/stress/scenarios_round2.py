"""
Round-2 stress scenarios targeting:
  (a) The 4 fixes applied after round 1 — verify they didn't BREAK anything
  (b) Interactions between overrides (pin+forbid, pin on Saturday, etc.)
  (c) Edge cases the round-1 scenarios didn't probe (multi-product compartments,
      multi-day stockouts, large pool)

Each scenario follows the same hypothesis/expected pattern. The Designer
(this code) writes assertions BEFORE knowing the solver output.
"""
from __future__ import annotations
from datetime import date
from final.stress.scenario_lib import build, client, truck, depot
from v2.domain.overrides import Pin, Forbid, Overrides


# ════════════════════════════════════════════════════════════════════════════
# CATEGORY J: PIN×FORBID INTERACTIONS
# ════════════════════════════════════════════════════════════════════════════

def scenario_j01_pin_on_forbidden_date_drops():
    """Pin date conflicts with Forbid date on same date. Operator created an
    impossible constraint. Expected behavior: client deferred (model can't
    satisfy both); invariant should flag the pin violation.

    Setup: client pinned to day 0, also forbidden on day 0.
    """
    pin = Pin(client_id='C', date=date(2026, 5, 22), reason='conflict')
    forbid = Forbid(client_id='C', dates=(date(2026, 5, 22),),
                     reason='conflict')
    p = build(
        today='2026-05-22',
        horizon_days=2,
        depot=depot(33.515, -112.167),
        trucks=[truck('Truck2')],
        clients=[client('C', tank=1000, rate=200, current=400,
                         lat=33.520, lon=-112.170)],
        overrides=Overrides(pins=(pin,), forbids=(forbid,)),
    )
    expected = {
        'description': 'Pin + Forbid on same date — invariant should catch',
        'hypothesis': 'Solver either crashes with OverrideHonorViolation OR resolves to drop',
        # We can't predict which — record what happens.
    }
    return p, expected


def scenario_j02_pin_on_saturday_for_truck9():
    """Pin a client to Saturday — only Truck2 available — Truck2 must serve.

    Setup: client pinned to Saturday 2026-05-23. 2 trucks but Saturday rule
    blocks Truck9. Truck2 must take this stop.
    """
    pin = Pin(client_id='C', date=date(2026, 5, 23), reason='sat-only')
    p = build(
        today='2026-05-22',
        horizon_days=2,        # Fri + Sat
        depot=depot(33.515, -112.167),
        trucks=[truck('Truck2'), truck('Truck9')],
        clients=[client('C', tank=1000, rate=100, current=600,
                         lat=33.520, lon=-112.170)],
        overrides=Overrides(pins=(pin,)),
    )
    expected = {
        'description': 'Pin to Saturday — only Truck2 may serve',
        'hypothesis': 'C is on Truck2 (not Truck9) on Sat May 23',
        'must_serve_by_day': {'2026-05-23': ['C']},
        'must_use_trucks': ['Truck2'],
    }
    return p, expected


# ════════════════════════════════════════════════════════════════════════════
# CATEGORY K: MULTI-PRODUCT COMPARTMENTS
# ════════════════════════════════════════════════════════════════════════════

def scenario_k01_two_products_share_truck():
    """Two clients with different products on the same truck — each
    compartment caps at 5000 lbs. Total truck cap 10,000 lbs.

    Setup: 2 clients, one CANOLA (8000 lb need), one FRYERS (3000 lb need).
    Truck cap 10k, each compartment 5k. CANOLA refill would exceed 5k →
    compartment capacity infeasible → solver must split or partial.

    Note: refills are computed from tank size in the model. CANOLA tank=8000,
    rate=200, current=0 → refill=8000. Compartment cap=5000 → INFEASIBLE
    for single-day single-truck. Expected: defer or partial.
    """
    p = build(
        today='2026-05-22',
        horizon_days=2,
        depot=depot(33.515, -112.167),
        trucks=[truck('Truck2', cap=10000, compartments=2, compartment_cap=5000)],
        clients=[
            client('CAN_BIG', tank=8000, rate=400, current=0, product='CANOLA',
                    lat=33.520, lon=-112.170),
            client('FRY',     tank=3000, rate=200, current=500, product='FRYERS CHOICE',
                    lat=33.521, lon=-112.171),
        ],
    )
    expected = {
        'description': '8000-lb CANOLA need exceeds 5000-lb compartment',
        'hypothesis': 'CAN_BIG is either DEFERRED (refill > compartment cap) or '
                       'served across multiple days. Solver must respect compartment.',
        'no_overflow': True,
    }
    return p, expected


# ════════════════════════════════════════════════════════════════════════════
# CATEGORY L: MULTI-DAY STOCKOUT (already-empty start)
# ════════════════════════════════════════════════════════════════════════════

def scenario_l01_already_stocked_out():
    """A client whose tank is already AT ZERO at horizon start. Must be served
    immediately (DTE=0, HARD penalty). Should land on day 0.
    """
    p = build(
        today='2026-05-22',
        horizon_days=3,
        depot=depot(33.515, -112.167),
        trucks=[truck('Truck2')],
        clients=[
            client('EMPTY', tank=1000, rate=100, current=0, lat=33.520, lon=-112.170),
        ],
    )
    expected = {
        'description': 'Already-empty tank: HARD penalty, must serve on day 0',
        'hypothesis': 'EMPTY served on 2026-05-22 (day 0)',
        'must_serve_by_day': {'2026-05-22': ['EMPTY']},
        'no_overflow': True,
    }
    return p, expected


def scenario_l02_stockout_within_horizon():
    """Tank has 1 day of supply on Mon. 5-day horizon. Tank will be at 0 by
    day 1. The HIGH-tier penalty should force a day-0 visit.
    """
    p = build(
        today='2026-05-22',
        horizon_days=5,
        depot=depot(33.515, -112.167),
        trucks=[truck('Truck2')],
        clients=[
            client('DRY', tank=1000, rate=200, current=200, lat=33.520, lon=-112.170),
        ],
    )
    expected = {
        'description': 'Tank dries within horizon (1d supply, 5d horizon)',
        'hypothesis': 'DRY served on day 0 or 1 (HIGH-tier penalty $500 must fire)',
        'must_serve_by_day': {'2026-05-22': ['DRY']},
        'no_overflow': True,
    }
    return p, expected


# ════════════════════════════════════════════════════════════════════════════
# CATEGORY M: LARGE POOL (perf + correctness)
# ════════════════════════════════════════════════════════════════════════════

def scenario_m01_fifty_client_grid():
    """50 clients on a 7×7 grid, mix of urgencies. Verify solver completes
    within reasonable time and serves all urgent clients.
    """
    cs = []
    for i in range(50):
        row, col = divmod(i, 7)
        lat = 33.45 + 0.03 * row
        lon = -112.25 + 0.03 * col
        urgent = (i % 6 == 0)  # 1 in 6 urgent
        current = 100 if urgent else 700  # urgent → low; safe → mid
        cs.append(client(f'M{i:02d}', tank=1000, rate=100, current=current,
                          lat=lat, lon=lon))
    p = build(
        today='2026-05-22',
        horizon_days=5,
        depot=depot(33.515, -112.167),
        trucks=[truck('Truck2'), truck('Truck9')],
        clients=cs,
    )
    urgent_ids = [f'M{i:02d}' for i in range(50) if i % 6 == 0]
    expected = {
        'description': '50-client grid; verify scale and urgency coverage',
        'hypothesis': 'All ~9 urgent clients served within horizon',
        'must_serve': urgent_ids,
        'min_total_stops': len(urgent_ids),
        'no_overflow': True,
    }
    return p, expected


# ════════════════════════════════════════════════════════════════════════════
# CATEGORY N: PROOF OF NO-FUTURE-LEAKAGE
# ════════════════════════════════════════════════════════════════════════════

def scenario_n01_solver_doesnt_peek_future():
    """The solver should serve a client BASED ON CURRENT STATE only. Two
    similar clients: one with 1d DTE today, one with 1d DTE on day 10.
    Solver should serve the day-0 urgent one, defer the day-10 one (LOW now).
    """
    p = build(
        today='2026-05-22',
        horizon_days=3,
        depot=depot(33.515, -112.167),
        trucks=[truck('Truck2')],
        clients=[
            # NOW_URGENT: DTE = 200/200 = 1
            client('NOW_URGENT', tank=1000, rate=200, current=200,
                    lat=33.520, lon=-112.170),
            # FUTURE_URGENT: DTE today = 4000/200 = 20 → LOW.
            # (We set current=4000 which exceeds tank=1000; clamp in model
            # makes it 1000. days_supply = 1000/200 = 5. So MED tier?
            # Adjust: tank=2000, current=2000, rate=200 → days_supply=10,
            # days_to_target = (2000−600)/200 = 7. MED.)
            client('FUTURE', tank=2000, rate=200, current=2000,
                    lat=33.523, lon=-112.173),
        ],
    )
    expected = {
        'description': 'Same-day urgency wins over future urgency',
        'hypothesis': 'NOW_URGENT served day 0; FUTURE may or may not (MED tier)',
        'must_serve': ['NOW_URGENT'],
        'must_serve_by_day': {'2026-05-22': ['NOW_URGENT']},
    }
    return p, expected
