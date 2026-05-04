"""
test_neighbor_sweep.py — Verify cross-day cluster cohesion via neighbor-sweep.

Bug this guards against: the unified solver computed a per-client preferred
day from each client's own fill economics, sending trucks to the same micro-
area on different days when consumption rates differed. Concrete case that
prompted the fix: DILLONS BAYOU (Peoria, preferred Sat) and TAILGATERS /
SARDELLA'S LAKE PLEASANT (Peoria, preferred Wed) — same parkway, three
trips, two days apart.

The sweep pulls non-mandatory neighbors toward earlier preferred days when
distance, stockout horizon, and fill economics permit. One-directional —
never pushes a client toward stockout.
"""
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config as _cfg


def _haversine_mi(lat1, lon1, lat2, lon2):
    R = 3958.8
    lat1r, lon1r, lat2r, lon2r = map(math.radians, [lat1, lon1, lat2, lon2])
    dlat, dlon = lat2r - lat1r, lon2r - lon1r
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1r) * math.cos(lat2r) * math.sin(dlon / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


def _run_sweep(clients, refills_by_day, *, radius_mi=None, min_fill=None, enabled=True):
    """Mirror of the sweep block in unified_solver.solve_week."""
    n = len(clients)
    lats  = [c['lat']  for c in clients]
    lons  = [c['lon']  for c in clients]
    tanks = [max(c['tank'], 1.0) for c in clients]
    d2sos = [c['d2so'] for c in clients]
    base_deadline   = [c['preferred_day'] for c in clients]
    is_mandatory    = [c['mandatory']     for c in clients]
    client_deadline = list(base_deadline)

    if not enabled or n < 2:
        return client_deadline

    radius   = radius_mi if radius_mi is not None else float(_cfg.NEIGHBOR_SWEEP_RADIUS_MI)
    minfill  = min_fill  if min_fill  is not None else float(_cfg.NEIGHBOR_SWEEP_MIN_FILL)

    for i in range(n):
        if is_mandatory[i]:
            continue
        target_day = client_deadline[i]
        for j in range(n):
            if i == j or base_deadline[j] >= target_day:
                continue
            if _haversine_mi(lats[i], lons[i], lats[j], lons[j]) > radius:
                continue
            j_day = base_deadline[j]
            if d2sos[i] < j_day:
                continue
            if j_day < len(refills_by_day) and (i + 1) < len(refills_by_day[j_day]):
                fill = refills_by_day[j_day][i + 1] / tanks[i] if tanks[i] > 0 else 0.0
                if fill < minfill:
                    continue
            target_day = j_day
        client_deadline[i] = target_day

    return client_deadline


def _refills(n_days, clients):
    """Synthetic refills_by_day[d][i+1] using d2so as a burn-rate proxy."""
    by_day = []
    for d in range(n_days):
        row = [0.0]  # depot
        for c in clients:
            burn = 1.0 / max(c['d2so'], 1)
            fill = min(0.4 + burn * d, 1.0)
            row.append(round(fill * c['tank']))
        by_day.append(row)
    return by_day


# ── Tests ───────────────────────────────────────────────────────────────────

def test_lake_pleasant_consolidates_on_earlier_day():
    """The original bug: 3 Peoria clients on 2 different days. Sweep merges them."""
    clients = [
        {'name': 'DILLONS BAYOU',           'lat': 33.84,   'lon': -112.24,   'tank': 450,  'd2so': 4.6, 'preferred_day': 1, 'mandatory': False},
        {'name': 'TAILGATERS LAKE PLEASANT','lat': 33.6783, 'lon': -112.2768, 'tank': 1050, 'd2so': 4.0, 'preferred_day': 4, 'mandatory': False},
        {'name': "SARDELLA'S LAKE PLEASANT",'lat': 33.69,   'lon': -112.27,   'tank': 620,  'd2so': 4.5, 'preferred_day': 4, 'mandatory': False},
    ]
    out = _run_sweep(clients, _refills(5, clients))
    assert out == [1, 1, 1], f"Lake Pleasant trio should consolidate on Day 1, got {out}"


def test_sweep_respects_radius():
    """A neighbor outside the radius should not pull."""
    clients = [
        {'name': 'EARLY METRO',  'lat': 33.84, 'lon': -112.24, 'tank': 1000, 'd2so': 4, 'preferred_day': 1, 'mandatory': False},
        {'name': 'LATE FAR',     'lat': 34.50, 'lon': -112.46, 'tank': 1000, 'd2so': 4, 'preferred_day': 4, 'mandatory': False},  # ~46 mi
    ]
    out = _run_sweep(clients, _refills(5, clients))
    assert out[1] == 4, f"Far client should keep Day 4, got {out[1]}"


def test_sweep_blocks_stockout():
    """A client whose d2so is shorter than the target day must NOT be pulled."""
    clients = [
        {'name': 'EARLY ANCHOR', 'lat': 33.50, 'lon': -112.10, 'tank': 1000, 'd2so': 1.0, 'preferred_day': 0, 'mandatory': False},
        {'name': 'FRESH NEIGHBOR (d2so 0.3)','lat': 33.51, 'lon': -112.11, 'tank': 1000, 'd2so': 0.3, 'preferred_day': 4, 'mandatory': False},
    ]
    # d2so 0.3 < target_day 0? — target_day=0 so d2so >= 0 is fine for Day 0.
    # Use a more demanding case: anchor on Day 2, neighbor stocks out before.
    clients = [
        {'name': 'ANCHOR DAY 2', 'lat': 33.50, 'lon': -112.10, 'tank': 1000, 'd2so': 4.0, 'preferred_day': 2, 'mandatory': False},
        {'name': 'WOULD STOCKOUT IF DELAYED', 'lat': 33.51, 'lon': -112.11, 'tank': 1000, 'd2so': 1.5, 'preferred_day': 4, 'mandatory': False},
    ]
    out = _run_sweep(clients, _refills(5, clients))
    # Neighbor (d2so 1.5) cannot be pulled to Day 2 because 1.5 < 2 (stocks out)
    assert out[1] == 4, f"Neighbor with insufficient d2so should not be pulled, got {out[1]}"


def test_sweep_skips_mandatory_clients():
    """Mandatory clients are pinned to Day 0 — sweep must not change them."""
    clients = [
        {'name': 'CRITICAL', 'lat': 33.50, 'lon': -112.10, 'tank': 1000, 'd2so': 0.5, 'preferred_day': 0, 'mandatory': True},
        {'name': 'FRESH',    'lat': 33.51, 'lon': -112.11, 'tank': 1000, 'd2so': 5.0, 'preferred_day': 3, 'mandatory': False},
    ]
    out = _run_sweep(clients, _refills(5, clients))
    assert out[0] == 0, "Mandatory client must stay Day 0"


def test_sweep_blocks_low_fill():
    """A neighbor whose fill on the target day is below MIN_FILL must NOT be pulled."""
    # Tank 1000, target day 0 refill ~50 lbs (5% fill) — under 20% threshold
    clients = [
        {'name': 'EARLY',  'lat': 33.50, 'lon': -112.10, 'tank': 1000, 'd2so': 5.0, 'preferred_day': 0, 'mandatory': False},
        {'name': 'NEARLY FULL', 'lat': 33.51, 'lon': -112.11, 'tank': 1000, 'd2so': 30.0, 'preferred_day': 4, 'mandatory': False},
    ]
    refills = _refills(5, clients)
    # Force NEARLY FULL's Day 0 refill to be tiny
    refills[0][2] = 50  # 5% of 1000
    out = _run_sweep(clients, refills)
    assert out[1] == 4, f"Low-fill neighbor should not be pulled, got {out[1]}"


def test_sweep_disabled_via_flag():
    """When the flag is False, no preferred days change."""
    clients = [
        {'name': 'A', 'lat': 33.50, 'lon': -112.10, 'tank': 1000, 'd2so': 5.0, 'preferred_day': 0, 'mandatory': False},
        {'name': 'B', 'lat': 33.51, 'lon': -112.11, 'tank': 1000, 'd2so': 5.0, 'preferred_day': 4, 'mandatory': False},
    ]
    out = _run_sweep(clients, _refills(5, clients), enabled=False)
    assert out == [0, 4], f"Disabled sweep should leave preferred days alone, got {out}"


def test_sweep_only_pulls_earlier():
    """Sweep is one-directional: it never pushes a client toward later days."""
    clients = [
        {'name': 'EARLY', 'lat': 33.50, 'lon': -112.10, 'tank': 1000, 'd2so': 5.0, 'preferred_day': 0, 'mandatory': False},
        {'name': 'LATE',  'lat': 33.51, 'lon': -112.11, 'tank': 1000, 'd2so': 5.0, 'preferred_day': 4, 'mandatory': False},
    ]
    out = _run_sweep(clients, _refills(5, clients))
    assert out[0] == 0, "Earlier client should not move later"
    assert out[1] == 0, "Later client should be pulled to earlier day"
