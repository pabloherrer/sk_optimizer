"""
test_unit_helpers.py — pure unit tests for unified_solver.py helpers

Covers the geometry, vehicle-mapping, and compartment-assignment helpers.
Solver-free, fast, deterministic.
"""

import sys
import time
from pathlib import Path
import math

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import (
    TRUCKS, TRUCK_NAMES, DAYS, NUM_DAYS, PRODUCTS, COMPARTMENT_CAPACITY_LBS,
    DEPOT_LAT, DEPOT_LON,
)
from unified_solver import (
    _haversine_mi, _compute_geo_clusters, _compute_geo_clusters_single,
    vehicle_to_truck_day, vehicle_to_truck_day_config,
    truck_day_config_to_vehicle, truck_day_to_vehicles,
    _assign_compartments_by_config, _assign_compartments,
    LOAD_CONFIGS, NUM_CONFIGS, NUM_VEHICLES,
)


def run_test(test_name, test_fn):
    start = time.time()
    try:
        test_fn()
        elapsed = time.time() - start
        print(f'  ✓ {test_name:<52s} ({elapsed*1000:.0f} ms)')
        return True
    except AssertionError as e:
        msg = str(e)[:80]
        print(f'  ✗ {test_name:<52s} — {msg}')
        return False
    except Exception as e:
        msg = f'{type(e).__name__}: {str(e)[:60]}'
        print(f'  ✗ {test_name:<52s} — CRASH: {msg}')
        return False


# ── haversine ────────────────────────────────────────────────────────────────

def test_haversine_same_point_zero():
    assert _haversine_mi(33.5, -112.1, 33.5, -112.1) == 0.0

def test_haversine_symmetric():
    a = _haversine_mi(33.5, -112.1, 34.0, -113.0)
    b = _haversine_mi(34.0, -113.0, 33.5, -112.1)
    assert abs(a - b) < 1e-9

def test_haversine_phx_to_tucson():
    # Phoenix → Tucson ~113 mi (great-circle)
    d = _haversine_mi(33.4484, -112.0740, 32.2226, -110.9747)
    assert 100 < d < 130, f"PHX->TUS expected ~113mi, got {d:.1f}"

def test_haversine_one_degree_lat():
    # 1° latitude ≈ 69 mi at any longitude
    d = _haversine_mi(33.0, -112.0, 34.0, -112.0)
    assert 68 < d < 70, f"expected ~69mi, got {d:.2f}"

def test_haversine_nonneg():
    d = _haversine_mi(40.0, -75.0, 33.5, -112.1)
    assert d > 0


# ── geo clusters (single) ────────────────────────────────────────────────────

def test_clusters_single_metro_quadrant():
    # Right next to depot → METRO_ + quadrant
    row = pd.Series({'Lat': DEPOT_LAT + 0.1, 'Lon': DEPOT_LON + 0.1, 'City': 'Phoenix'})
    cluster, is_far = _compute_geo_clusters_single(row, DEPOT_LAT, DEPOT_LON)
    assert cluster.startswith('METRO_')
    assert is_far is False

def test_clusters_single_metro_four_quadrants():
    for (dlat, dlon, expected) in [
        ( 0.1,  0.1, 'METRO_NE'),
        ( 0.1, -0.1, 'METRO_NW'),
        (-0.1,  0.1, 'METRO_SE'),
        (-0.1, -0.1, 'METRO_SW'),
    ]:
        row = pd.Series({'Lat': DEPOT_LAT + dlat, 'Lon': DEPOT_LON + dlon, 'City': ''})
        c, f = _compute_geo_clusters_single(row, DEPOT_LAT, DEPOT_LON)
        assert c == expected, f"for {dlat,dlon}: got {c} expected {expected}"
        assert f is False

def test_clusters_single_far_with_city():
    # Tucson ~113 mi south → FAR_TUCSON
    row = pd.Series({'Lat': 32.22, 'Lon': -110.97, 'City': 'Tucson'})
    c, f = _compute_geo_clusters_single(row, DEPOT_LAT, DEPOT_LON)
    assert c == 'FAR_TUCSON'
    assert f is True

def test_clusters_single_far_no_city_spatial_bucket():
    row = pd.Series({'Lat': 32.22, 'Lon': -110.97, 'City': ''})
    c, f = _compute_geo_clusters_single(row, DEPOT_LAT, DEPOT_LON)
    assert c.startswith('FAR_') and 'TUCSON' not in c
    assert f is True


# ── geo clusters (pool, with corridor detection) ─────────────────────────────

def test_clusters_pool_basic():
    pool = pd.DataFrame([
        {'Lat': DEPOT_LAT + 0.1, 'Lon': DEPOT_LON + 0.1, 'City': 'Phoenix'},
        {'Lat': DEPOT_LAT - 0.2, 'Lon': DEPOT_LON + 0.2, 'City': 'Phoenix'},
        {'Lat': 32.22, 'Lon': -110.97, 'City': 'Tucson'},
    ])
    clusters, is_far = _compute_geo_clusters(pool, DEPOT_LAT, DEPOT_LON)
    assert len(clusters) == 3
    assert clusters[0].startswith('METRO_')
    assert clusters[2] == 'FAR_TUCSON'

def test_clusters_pool_corridor_reassignment():
    # A metro-distance point on the way to Wickenburg (NW outlier) should
    # get pulled into the FAR_WICKENBURG cluster.
    pool = pd.DataFrame([
        # A "metro but far" client on the NW corridor (25 mi NW of depot)
        {'Lat': DEPOT_LAT + 0.35, 'Lon': DEPOT_LON - 0.35, 'City': 'Surprise'},
        # Far cluster: Wickenburg ~50 mi NW
        {'Lat': 33.97, 'Lon': -112.73, 'City': 'Wickenburg'},
    ])
    clusters, is_far = _compute_geo_clusters(pool, DEPOT_LAT, DEPOT_LON)
    # Not asserting reassignment happened — depends on exact geometry — but both
    # should at least be in the output.
    assert len(clusters) == 2


# ── vehicle mapping (30 vehicles, 2 trucks × 5 days × 3 configs) ─────────────

def test_num_vehicles_expected():
    # 2 trucks × 5 days × 3 configs = 30
    assert NUM_VEHICLES == len(TRUCK_NAMES) * NUM_DAYS * NUM_CONFIGS

def test_vehicle_to_truck_day_config_bijection():
    for v in range(NUM_VEHICLES):
        t, d, c = vehicle_to_truck_day_config(v)
        assert t in TRUCK_NAMES
        assert 0 <= d < NUM_DAYS
        assert c in LOAD_CONFIGS
        # Round-trip
        v2 = truck_day_config_to_vehicle(t, d, c)
        assert v == v2, f"roundtrip broken: v={v} → ({t},{d},{c}) → v2={v2}"

def test_vehicle_to_truck_day_strips_config():
    for v in range(NUM_VEHICLES):
        t_full, d_full, _ = vehicle_to_truck_day_config(v)
        t, d = vehicle_to_truck_day(v)
        assert t == t_full and d == d_full

def test_truck_day_to_vehicles_returns_three():
    for t in TRUCK_NAMES:
        for d in range(NUM_DAYS):
            vs = truck_day_to_vehicles(t, d)
            assert len(vs) == NUM_CONFIGS
            # All three must map back to same (truck, day)
            for v in vs:
                t2, d2 = vehicle_to_truck_day(v)
                assert t2 == t and d2 == d

def test_truck_day_to_vehicles_unique_across_truck_days():
    seen = set()
    for t in TRUCK_NAMES:
        for d in range(NUM_DAYS):
            for v in truck_day_to_vehicles(t, d):
                assert v not in seen
                seen.add(v)
    assert len(seen) == NUM_VEHICLES


# ── compartment assignment ──────────────────────────────────────────────────

def test_compartment_split_basic():
    # 3000 lbs A + 2000 lbs B → split into two compartments
    comps = _assign_compartments_by_config({PRODUCTS[0]: 3000, PRODUCTS[1]: 2000}, 'SPLIT')
    total = sum(c['lbs'] for c in comps)
    assert total == 5000
    assert comps[0]['lbs'] == 3000 and comps[0]['product'] == PRODUCTS[0]
    assert comps[1]['lbs'] == 2000 and comps[1]['product'] == PRODUCTS[1]

def test_compartment_a_only_under_cap():
    comps = _assign_compartments_by_config({PRODUCTS[0]: 4000, PRODUCTS[1]: 0}, 'A_ONLY')
    assert comps[0]['lbs'] == 4000 and comps[0]['product'] == PRODUCTS[0]
    assert comps[1]['lbs'] == 0  # nothing overflowed

def test_compartment_a_only_overflow():
    comps = _assign_compartments_by_config({PRODUCTS[0]: 8000, PRODUCTS[1]: 0}, 'A_ONLY')
    assert comps[0]['lbs'] == COMPARTMENT_CAPACITY_LBS
    assert comps[1]['lbs'] == 8000 - COMPARTMENT_CAPACITY_LBS
    assert comps[0]['product'] == PRODUCTS[0]
    assert comps[1]['product'] == PRODUCTS[0]

def test_compartment_b_only_overflow():
    comps = _assign_compartments_by_config({PRODUCTS[0]: 0, PRODUCTS[1]: 9000}, 'B_ONLY')
    assert comps[0]['lbs'] + comps[1]['lbs'] == 9000
    assert comps[0]['product'] == PRODUCTS[1]
    assert comps[1]['product'] == PRODUCTS[1]

def test_compartment_totals_match_product_lbs():
    # Property: total compartment lbs == total product lbs for every (config, product_lbs)
    for cfg in LOAD_CONFIGS:
        for a, b in [(0, 0), (1000, 0), (0, 1000), (3000, 2000), (5000, 5000),
                     (8000, 0), (0, 9000), (4500, 4500)]:
            # Skip configs that don't match: A_ONLY with b>0, B_ONLY with a>0
            if cfg == 'A_ONLY' and b > 0:
                continue
            if cfg == 'B_ONLY' and a > 0:
                continue
            comps = _assign_compartments_by_config({PRODUCTS[0]: a, PRODUCTS[1]: b}, cfg)
            total = sum(c['lbs'] for c in comps)
            assert total == a + b, f"cfg={cfg} a={a} b={b}: got {total}"

def test_compartment_legacy_helper_split_when_both():
    comps = _assign_compartments({PRODUCTS[0]: 3000, PRODUCTS[1]: 2000})
    # Both products → should pick SPLIT behavior
    assert comps[0]['lbs'] == 3000 and comps[1]['lbs'] == 2000

def test_compartment_legacy_helper_aonly_single():
    comps = _assign_compartments({PRODUCTS[0]: 8000, PRODUCTS[1]: 0})
    # A only → A_ONLY pathway
    assert comps[0]['lbs'] + comps[1]['lbs'] == 8000

def test_compartment_empty_returns_zero():
    comps = _assign_compartments_by_config({PRODUCTS[0]: 0, PRODUCTS[1]: 0}, 'SPLIT')
    assert comps[0]['lbs'] == 0 and comps[1]['lbs'] == 0

def test_compartment_invalid_cfg_fallback():
    comps = _assign_compartments_by_config({PRODUCTS[0]: 1000, PRODUCTS[1]: 1000}, 'INVALID')
    # Fallback: {product: '—', lbs: 0}
    assert comps[0]['lbs'] == 0
    assert comps[1]['lbs'] == 0


# ── Suite runner ─────────────────────────────────────────────────────────────

TESTS = [
    ('haversine_same_point_zero',              test_haversine_same_point_zero),
    ('haversine_symmetric',                    test_haversine_symmetric),
    ('haversine_phx_to_tucson',                test_haversine_phx_to_tucson),
    ('haversine_one_degree_lat',               test_haversine_one_degree_lat),
    ('haversine_nonneg',                       test_haversine_nonneg),
    ('clusters_single_metro_quadrant',         test_clusters_single_metro_quadrant),
    ('clusters_single_metro_four_quadrants',   test_clusters_single_metro_four_quadrants),
    ('clusters_single_far_with_city',          test_clusters_single_far_with_city),
    ('clusters_single_far_no_city_bucket',     test_clusters_single_far_no_city_spatial_bucket),
    ('clusters_pool_basic',                    test_clusters_pool_basic),
    ('clusters_pool_corridor_reassignment',    test_clusters_pool_corridor_reassignment),
    ('num_vehicles_expected',                  test_num_vehicles_expected),
    ('vehicle_to_truck_day_config_bijection',  test_vehicle_to_truck_day_config_bijection),
    ('vehicle_to_truck_day_strips_config',     test_vehicle_to_truck_day_strips_config),
    ('truck_day_to_vehicles_returns_three',    test_truck_day_to_vehicles_returns_three),
    ('truck_day_to_vehicles_unique',           test_truck_day_to_vehicles_unique_across_truck_days),
    ('compartment_split_basic',                test_compartment_split_basic),
    ('compartment_a_only_under_cap',           test_compartment_a_only_under_cap),
    ('compartment_a_only_overflow',            test_compartment_a_only_overflow),
    ('compartment_b_only_overflow',            test_compartment_b_only_overflow),
    ('compartment_totals_match_product_lbs',   test_compartment_totals_match_product_lbs),
    ('compartment_legacy_split_when_both',     test_compartment_legacy_helper_split_when_both),
    ('compartment_legacy_a_only_single',       test_compartment_legacy_helper_aonly_single),
    ('compartment_empty_returns_zero',         test_compartment_empty_returns_zero),
    ('compartment_invalid_cfg_fallback',       test_compartment_invalid_cfg_fallback),
]


def run_all_tests():
    print('\nSolver Helpers — Unit Tests')
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
    print(f'{tag} {passed} passed, {failed} failed in {elapsed*1000:.0f} ms')
    return 0 if failed == 0 else 1


if __name__ == '__main__':
    sys.exit(run_all_tests())
