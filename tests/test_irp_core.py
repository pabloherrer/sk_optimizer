"""
Unit tests for the irp_core stack.

Run with:  .venv/bin/python -m pytest tests/test_irp_core.py -v
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from irp_core.state_manager import (
    InventoryState, ClientState, DeliveryLog, save_plan, load_plan,
    commit_run, confirm_deliveries,
)
from irp_core.economics import CostModel, expected_stockout_cost, DEFAULT_COSTS
from irp_core.forecasting import (
    fit_demand_models, attach_demand_columns, _normal_quantile, DemandModel,
)
from irp_core.safety_stock import (
    days_to_stockout_quantile, build_urgency_profiles, attach_urgency_columns,
)
from irp_core.warm_start import (
    shift_plan_for_today, build_initial_routes, plan_overlap, _vehicle_index,
)
from irp_core.objective import (
    cost_units_per_dollar, calibrate_legacy_knobs,
    build_per_client_disjunction_penalties,
)


# ─────────────────────────────────────────────────────────────────────────────
# state_manager
# ─────────────────────────────────────────────────────────────────────────────

class TestState:
    def test_atomic_save_and_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / 'state.json'
            s = InventoryState(as_of=pd.Timestamp('2026-05-02'))
            s.clients['100'] = ClientState(id='100', current_lbs=4500.0)
            s.clients['200'] = ClientState(id='200', current_lbs=2300.0,
                                            last_delivery='2026-04-25')
            s.save(f)
            s2 = InventoryState.load(f)
            assert s2.level('100') == 4500.0
            assert s2.level('200') == 2300.0
            assert s2.clients['200'].last_delivery == '2026-04-25'

    def test_legacy_v1_migration(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / 'legacy.json'
            f.write_text('{"100": 1234.5, "200": 6000}')
            s = InventoryState.load(f)
            assert s.level('100') == 1234.5
            assert s.level('200') == 6000

    def test_empty_state_returns_clean(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = InventoryState.load(Path(tmp) / 'missing.json')
            assert len(s.clients) == 0

    def test_corrupt_state_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / 's.json'
            f.write_text('{ malformed json')
            s = InventoryState.load(f)
            assert len(s.clients) == 0

    def test_apply_consumption_decays_tracked(self):
        df = pd.DataFrame([
            {'ID': '1', 'Tank_lbs': 6000, 'Avg_LbsPerDay': 200, 'Est_Current_lbs': 3000},
        ])
        s = InventoryState(as_of=pd.Timestamp('2026-05-02'))
        s.clients['1'] = ClientState(id='1', current_lbs=4000.0)
        s.apply_consumption(df, n_days=2)
        assert s.level('1') == 4000.0 - 2 * 200

    def test_apply_consumption_initialises_new(self):
        df = pd.DataFrame([
            {'ID': '1', 'Tank_lbs': 6000, 'Avg_LbsPerDay': 200, 'Est_Current_lbs': 4000},
        ])
        s = InventoryState(as_of=pd.Timestamp('2026-05-02'))
        s.apply_consumption(df, n_days=3)
        # New client: initialise from estimate without decaying
        assert s.level('1') == 4000.0

    def test_apply_deliveries_resets_to_full(self):
        df = pd.DataFrame([
            {'ID': '1', 'Tank_lbs': 6000, 'Avg_LbsPerDay': 200, 'Est_Current_lbs': 3000},
        ])
        s = InventoryState(as_of=pd.Timestamp('2026-05-02'))
        s.clients['1'] = ClientState(id='1', current_lbs=1200.0)
        s.apply_deliveries(
            [{'id': '1', 'qty_lbs': 4800, 'date': '2026-05-02'}], df,
        )
        assert s.level('1') == 6000.0
        assert s.clients['1'].days_since_last == 0

    def test_delivery_log_append(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = DeliveryLog(Path(tmp) / 'd.log')
            log.append([{'id': '1', 'qty_lbs': 100, 'date': '2026-05-02'}])
            log.append([{'id': '2', 'qty_lbs': 200, 'date': '2026-05-02'}])
            recs = log.read_all()
            assert len(recs) == 2
            assert recs[0]['id'] == '1'
            assert recs[1]['qty_lbs'] == 200

    def test_save_and_load_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / 'plan.json'
            df = pd.DataFrame([
                {'ID': '1', 'Truck': 'Truck2', 'Stop': 1, 'Refill_lbs': 4000,
                 'Date': '2026-05-02', 'Status': 'COMMITTED'},
            ])
            save_plan(
                f, routes={0: df}, deferred=pd.DataFrame(),
                plan_dates=[pd.Timestamp('2026-05-02')],
                today=pd.Timestamp('2026-05-01'),
                horizon_days=1, commit_days=1,
            )
            loaded = load_plan(f)
            assert loaded is not None
            assert len(loaded['visits']) == 1
            assert loaded['visits'][0]['client_id'] == '1'


# ─────────────────────────────────────────────────────────────────────────────
# economics
# ─────────────────────────────────────────────────────────────────────────────

class TestEconomics:
    def test_default_cost_model_sane(self):
        c = CostModel()
        assert 0.10 < c.fuel_per_mi < 2.00
        assert 0.10 < c.labor_per_min < 2.00
        assert 100 < c.stockout_dollars < 5000

    def test_ot_per_min_consistency(self):
        c = CostModel(labor_per_min=0.5, ot_multiplier=1.5)
        assert c.ot_per_min == pytest.approx(0.25)
        assert c.loaded_labor_per_min == pytest.approx(0.75)

    def test_stockout_already_out_returns_full_event(self):
        c = DEFAULT_COSTS
        cost = expected_stockout_cost(
            cost=c, days_until_stockout=-1, horizon_days=10,
        )
        assert cost >= c.stockout_dollars

    def test_stockout_far_future_zero(self):
        c = DEFAULT_COSTS
        cost = expected_stockout_cost(
            cost=c, days_until_stockout=99, horizon_days=10,
        )
        assert cost == 0.0

    def test_stockout_monotone_in_days(self):
        c = DEFAULT_COSTS
        v1 = expected_stockout_cost(cost=c, days_until_stockout=2, horizon_days=10)
        v2 = expected_stockout_cost(cost=c, days_until_stockout=8, horizon_days=10)
        assert v1 > v2  # closer-to-stockout costs more


# ─────────────────────────────────────────────────────────────────────────────
# forecasting
# ─────────────────────────────────────────────────────────────────────────────

class TestForecasting:
    def test_normal_quantile_known(self):
        assert _normal_quantile(0.50) == 0.0
        assert _normal_quantile(0.95) == pytest.approx(1.6449)
        assert _normal_quantile(0.99) == pytest.approx(2.3263)

    def test_demand_model_quantile_grows(self):
        rates = np.full(7, 100.0)
        m = DemandModel(rates=rates, sigma=20.0, n_obs=50)
        dates = [pd.Timestamp('2026-05-02') + pd.Timedelta(days=i) for i in range(5)]
        p50 = m.cumulative_consumption_quantile(dates, 0.50)
        p95 = m.cumulative_consumption_quantile(dates, 0.95)
        assert (p95 >= p50).all()
        assert p95[-1] > p50[-1]   # variance accumulates

    def test_fit_runs_on_synthetic_data(self):
        clients = pd.DataFrame([
            {'ID': '1', 'Customer': 'A', 'Tank_lbs': 6000},
            {'ID': '2', 'Customer': 'B', 'Tank_lbs': 6000},
        ])
        deliveries = pd.DataFrame([
            {'Customer': 'A', 'Date': pd.Timestamp('2026-04-01'), 'Qty_lbs': 700},
            {'Customer': 'A', 'Date': pd.Timestamp('2026-04-08'), 'Qty_lbs': 700},
            {'Customer': 'A', 'Date': pd.Timestamp('2026-04-15'), 'Qty_lbs': 700},
            {'Customer': 'B', 'Date': pd.Timestamp('2026-04-05'), 'Qty_lbs': 500},
        ])
        models = fit_demand_models(deliveries, clients, today=pd.Timestamp('2026-05-01'))
        assert '1' in models
        assert '2' in models
        # Client A: ~100 lbs/day (700 / 7 day gap)
        assert 50 < models['1'].daily_mean() < 150


# ─────────────────────────────────────────────────────────────────────────────
# safety_stock
# ─────────────────────────────────────────────────────────────────────────────

class TestSafetyStock:
    def test_days_to_stockout_simple(self):
        rates = np.full(7, 100.0)
        m = DemandModel(rates=rates, sigma=10.0, n_obs=50)
        # 1000 lbs, 100 lbs/day → ~10 days at P50
        d = days_to_stockout_quantile(
            current_lbs=1000, floor_lbs=0,
            model=m, start_date=pd.Timestamp('2026-05-02'),
            quantile=0.50,
        )
        assert 9.0 <= d <= 10.5

    def test_days_to_stockout_p95_shorter_than_p50(self):
        rates = np.full(7, 100.0)
        m = DemandModel(rates=rates, sigma=20.0, n_obs=50)
        p50 = days_to_stockout_quantile(
            current_lbs=1000, floor_lbs=0, model=m,
            start_date=pd.Timestamp('2026-05-02'), quantile=0.50,
        )
        p95 = days_to_stockout_quantile(
            current_lbs=1000, floor_lbs=0, model=m,
            start_date=pd.Timestamp('2026-05-02'), quantile=0.95,
        )
        assert p95 < p50, f'P95={p95} should be more conservative than P50={p50}'

    def test_already_below_floor_returns_zero(self):
        rates = np.full(7, 100.0)
        m = DemandModel(rates=rates, sigma=10.0, n_obs=50)
        d = days_to_stockout_quantile(
            current_lbs=50, floor_lbs=100, model=m,
            start_date=pd.Timestamp('2026-05-02'),
        )
        assert d == 0.0

    def test_urgency_profiles_shape(self):
        clients = pd.DataFrame([
            {'ID': '1', 'Customer': 'A', 'Tank_lbs': 6000},
        ])
        models = {
            '1': DemandModel(rates=np.full(7, 100.0), sigma=10.0, n_obs=50),
        }
        plan_dates = [pd.Timestamp('2026-05-02') + pd.Timedelta(days=i) for i in range(5)]
        profiles = build_urgency_profiles(
            clients_df=clients, state_lookup={'1': 200},
            models=models, plan_dates=plan_dates,
        )
        assert '1' in profiles
        # 200 lbs at 100 lbs/day → very mandatory
        assert profiles['1'].is_mandatory
        assert profiles['1'].p95_days_to_stockout < profiles['1'].p50_days_to_stockout


# ─────────────────────────────────────────────────────────────────────────────
# warm_start
# ─────────────────────────────────────────────────────────────────────────────

class TestWarmStart:
    def test_vehicle_index_consistent(self):
        # Truck0/Day0/Cfg0 = 0
        assert _vehicle_index(0, 0, 0, num_days=5, num_configs=3) == 0
        # Truck1/Day0/Cfg0 = 15 (1 * 5 * 3)
        assert _vehicle_index(1, 0, 0, num_days=5, num_configs=3) == 15
        # Truck0/Day2/Cfg1 = 0 + 6 + 1 = 7
        assert _vehicle_index(0, 2, 1, num_days=5, num_configs=3) == 7

    def test_shift_plan_advances_one_day(self):
        plan = {
            'today': '2026-05-01',
            'visits': [
                {'day': 0, 'client_id': '1', 'truck': 'Truck2', 'stop': 1},
                {'day': 1, 'client_id': '2', 'truck': 'Truck2', 'stop': 1},
                {'day': 3, 'client_id': '3', 'truck': 'Truck9', 'stop': 1},
            ],
        }
        shifted = shift_plan_for_today(plan=plan, today=pd.Timestamp('2026-05-02'))
        # Day 0 is gone (yesterday), day 1 → 0, day 3 → 2
        assert (0, 'Truck2', 0) in shifted
        assert (2, 'Truck9', 0) in shifted
        assert shifted[(0, 'Truck2', 0)] == ['2']

    def test_plan_overlap_self_is_one(self):
        plan = {
            'today': '2026-05-01',
            'visits': [{'day': 1, 'client_id': '1', 'truck': 'T', 'stop': 1}],
        }
        # Compare day 1 of plan to day 1 of itself = full overlap
        assert plan_overlap(plan, plan, day_offset_old=1, day_offset_new=1) == 1.0

    def test_plan_overlap_disjoint_is_zero(self):
        a = {'visits': [{'day': 0, 'client_id': '1'}]}
        b = {'visits': [{'day': 0, 'client_id': '2'}]}
        assert plan_overlap(a, b, day_offset_old=0, day_offset_new=0) == 0.0


# ─────────────────────────────────────────────────────────────────────────────
# objective
# ─────────────────────────────────────────────────────────────────────────────

class TestObjective:
    def test_units_per_dollar_in_range(self):
        c = DEFAULT_COSTS
        units = cost_units_per_dollar(c)
        # Should be ~2926 for fuel=$0.55/mi
        assert 1500 < units < 5000

    def test_calibrated_knobs_positive(self):
        knobs = calibrate_legacy_knobs(DEFAULT_COSTS)
        assert knobs['LATE_PENALTY_PER_DAY'] > 0
        assert knobs['OT_PENALTY_PER_MIN'] > 0
        assert knobs['LABOR_COST_PER_MIN'] > 0


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-v']))
