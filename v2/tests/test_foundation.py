"""
Phase 1 smoke tests: verify config loads cleanly and domain types are sound.
Run: ./sk_venv/bin/python3 -m pytest v2/tests/test_foundation.py -v
"""
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

# Make v2 imports work whether tests run from project root or v2/tests
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def test_config_loads_and_validates():
    from v2.schemas import load_app_config
    cfg = load_app_config(Path(__file__).resolve().parents[1] / 'config')
    # Sanity check a few values
    assert cfg.economics.cost_per_mile > 0
    assert cfg.economics.overtime_multiplier >= 1.0
    assert cfg.fleet.depot.lat == pytest.approx(33.5152)
    assert len(cfg.fleet.trucks) == 2
    assert {t.id for t in cfg.fleet.trucks} == {'Truck2', 'Truck9'}
    assert cfg.policy.commit_days <= cfg.policy.horizon_days
    assert cfg.policy.override_conflict_policy in ('abort', 'relax')


def test_config_rejects_unknown_key(tmp_path):
    """A typo in YAML should fail loudly, not be silently ignored."""
    from v2.schemas import Economics
    with pytest.raises(Exception):  # pydantic.ValidationError
        Economics(
            cost_per_mile=0.55,
            cost_per_minute_labor=0.83,
            overtime_multiplier=1.5,
            truck_dispatch_cost=50.0,
            stockout_cost_per_lb_day=10.0,
            terminal_value_per_lb=0.10,
            mystery_typo=42,   # ← should be rejected
        )


def test_config_rejects_out_of_range():
    from v2.schemas import Economics
    with pytest.raises(Exception):  # OT multiplier > 3 not allowed
        Economics(
            cost_per_mile=0.55,
            cost_per_minute_labor=0.83,
            overtime_multiplier=99.0,
            truck_dispatch_cost=50.0,
            stockout_cost_per_lb_day=10.0,
            terminal_value_per_lb=0.10,
        )


def test_domain_imports():
    """All domain classes importable and instantiable."""
    from v2.domain import (
        Client, TankState, Truck, Depot, Compartment,
        Plan, Route, Stop, Pin, Forbid, Lock,
        ManualReading, ManualConsumption, Overrides,
    )
    c = Client(
        id='C001', customer='Test Restaurant',
        lat=33.5, lon=-112.1,
        tank_capacity_lbs=1000, product='CANOLA',
    )
    assert c.id == 'C001'
    assert c.do_not_schedule is False  # default
    # frozen?
    with pytest.raises(Exception):
        c.id = 'CHANGED'


def test_overrides_bundle():
    from v2.domain import Pin, Overrides
    p = Pin(client_id='C001', date=date(2026, 5, 22), reason='Customer call')
    ov = Overrides(pins=(p,))
    assert not ov.is_empty()
    assert ov.pins[0].client_id == 'C001'
    assert Overrides().is_empty()


def test_invariants_catch_tank_overflow():
    """A plan with overflow should raise TankOverflowViolation."""
    from v2.invariants import _check_no_tank_overflow, TankOverflowViolation
    from v2.domain.plan import Stop
    # Fake "plan" object — only needs .routes attribute
    class FakeRoute:
        def __init__(self, stops): self.stops = stops
    class FakePlan:
        routes = {(date(2026, 5, 22), 'Truck2'): FakeRoute([
            Stop(
                sequence=1, client_id='C1', customer='Test', address='', lat=0, lon=0,
                product='CANOLA',
                tank_capacity_lbs=1000,
                level_at_arrival_lbs=500,
                delivery_lbs=600,         # 500 + 600 = 1100 > 1000  → overflow!
                level_after_lbs=1100,
                arrival_min=0, setup_min=18, pump_min=4, depart_min=22,
                travel_miles=0, cumulative_miles=0,
                days_until_stockout_at_arrival=5.0,
            ),
        ])}

    with pytest.raises(TankOverflowViolation):
        _check_no_tank_overflow(FakePlan())


def test_invariants_catch_duplicate_visits():
    from v2.invariants import _check_no_duplicate_visits, DuplicateVisitViolation
    from v2.domain.plan import Stop
    s = Stop(
        sequence=1, client_id='C1', customer='Test', address='', lat=0, lon=0,
        product='CANOLA',
        tank_capacity_lbs=1000, level_at_arrival_lbs=300, delivery_lbs=500,
        level_after_lbs=800,
        arrival_min=0, setup_min=18, pump_min=4, depart_min=22,
        travel_miles=0, cumulative_miles=0,
        days_until_stockout_at_arrival=3.0,
    )
    class FakeRoute:
        def __init__(self, stops): self.stops = stops
    class FakePlan:
        routes = {
            (date(2026, 5, 22), 'Truck2'): FakeRoute([s]),
            (date(2026, 5, 22), 'Truck9'): FakeRoute([s]),  # same client, same day
        }
    with pytest.raises(DuplicateVisitViolation):
        _check_no_duplicate_visits(FakePlan())
