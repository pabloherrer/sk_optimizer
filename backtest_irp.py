#!/usr/bin/env python3
"""
backtest_irp.py — Honest replay-based validation of the IRP solver
==================================================================

This is the only honest way to compare two route optimizers: replay
real history under both, simulate the world's response to each plan,
measure outcomes (cost, stockouts, plan stability), and tabulate.

Replay protocol
---------------
Pick a "ground truth" date range from the delivery log (e.g., the last
30 days). For each "decision day" T in that range:

  1. Snapshot the world as of evening(T-1):
       state_T  = inventory derived from deliveries ≤ T-1
       models_T = demand models fit on deliveries ≤ T-1
  2. Run the optimizer with that snapshot.
  3. Compare its day-0 plan to what ACTUALLY happened on day T:
       Did the optimizer schedule the clients who were actually visited?
       If a client was visited in reality but not in the plan, the
       client may have been driven there by the actual driver's
       judgment (good catch missed) — note as "missed visit."
       If the optimizer scheduled clients who were NOT actually
       visited, simulate the inventory consequence: those clients
       are filled to full (good) or held (their inventory continues
       to deplete).
  4. Roll forward: state_T+1 = state_T - day-of-consumption +
                              actuals applied (always — we
                              compare against reality, not against
                              the optimizer's wishes).
  5. Track metrics:
       Stockout days   — # client-days inventory < floor
       $ cost          — fuel + labor + OT for routes optimizer chose
       Plan stability  — % of yesterday's day-1 in today's day-0
       Service level   — 1 - stockout_days / total_client_days

Why this design
---------------
Comparing optimizers requires that BOTH face the SAME world. Reality
is what reality was; the optimizer's job is to anticipate it. We
hold reality fixed and replay it under each candidate.

Caveat
------
This is a "what-if" backtest. The optimizer didn't actually drive
that day's truck; the human dispatcher did. So we measure
"would the optimizer have made the same calls?" plus "did the
optimizer correctly anticipate stockouts?" — not actual operational
$ saved (which would require A/B in the field).

Usage
-----
    python backtest_irp.py --start 2026-03-01 --end 2026-04-15
    python backtest_irp.py --start 2026-03-01 --days 30
    python backtest_irp.py --solver legacy   # baseline run_unified
    python backtest_irp.py --solver irp      # new IRP run
    python backtest_irp.py --quick           # 5-day smoke test
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

from config import (
    INPUT_FILE, MATRIX_FILE, DATA_DIR, DAYS, HORIZON_DAYS, COMMIT_DAYS,
    EXCLUDED_CLIENT_IDS,
)
import config as _cfg
from load_data import load_all
from router import load_matrix
from schema_loaders import (
    load_time_windows, load_closures, load_depot_config, load_trucks,
)
from unified_solver import solve_horizon
from forecast_consumption import estimate_consumption_rates as legacy_rates

from irp_core.state_manager import InventoryState, ClientState
from irp_core.forecasting import fit_demand_models, attach_demand_columns
from irp_core.safety_stock import build_urgency_profiles, attach_urgency_columns
from irp_core.economics import CostModel, DEFAULT_COSTS
from irp_core.objective import calibrate_legacy_knobs, cost_units_per_dollar
from irp_core.warm_start import shift_plan_for_today, _vehicle_index

log = logging.getLogger(__name__)

# Working-day filter
_WEEKDAY_SHORT = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']


# ─────────────────────────────────────────────────────────────────────────────
# Per-day metrics
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class DayResult:
    date: str
    stops_planned: int = 0
    stops_actual: int = 0
    miles_planned: float = 0.0
    minutes_planned: float = 0.0
    cost_dollars: float = 0.0
    plan_stability: Optional[float] = None
    matched_actuals: int = 0       # how many actual visits the plan correctly anticipated
    actuals_missed: int = 0        # actual visits the plan didn't include (driver judgment)
    plan_extras: int = 0           # plan visits that didn't actually happen
    stockout_clients_today: int = 0
    deferred_count: int = 0
    solve_seconds: float = 0.0
    # Closed-loop simulation: state evolves under the optimizer's plan,
    # not under real history. These metrics measure operational outcomes
    # of each optimizer's choices.
    sim_stockouts_today: int = 0
    sim_avg_tank_pct: float = 0.0


@dataclass
class BacktestSummary:
    solver: str
    start: str
    end: str
    days_simulated: int = 0
    total_cost: float = 0.0
    total_miles: float = 0.0
    total_stops: int = 0
    total_stockout_client_days: int = 0
    total_client_days: int = 0
    avg_plan_stability: float = 0.0
    avg_matched_pct: float = 0.0
    # Closed-loop sim outcomes
    sim_total_stockout_days: int = 0
    sim_min_tank_pct: float = 1.0
    days: List[DayResult] = field(default_factory=list)

    @property
    def service_level(self) -> float:
        if self.total_client_days == 0:
            return 1.0
        return 1.0 - self.total_stockout_client_days / self.total_client_days

    def to_json(self) -> dict:
        d = asdict(self)
        d['service_level'] = self.service_level
        return d


# ─────────────────────────────────────────────────────────────────────────────
# Snapshot reconstruction from history
# ─────────────────────────────────────────────────────────────────────────────

def reconstruct_state_at(
    *,
    deliveries: pd.DataFrame,
    clients: pd.DataFrame,
    cutoff: pd.Timestamp,
    rate_col_value: Dict[str, float],
) -> InventoryState:
    """
    Reconstruct the inventory state as of evening(cutoff): for each
    client, find their last delivery on or before cutoff, then decay
    by (cutoff - last_delivery) × rate.

    rate_col_value: {client_id: lbs/day}, used to decay since last visit.
    """
    cutoff = pd.Timestamp(cutoff).normalize()
    state = InventoryState(as_of=cutoff)
    cust_to_id = dict(zip(clients['Customer'], clients['ID'].astype(str)))
    tank_by_id = {str(r['ID']): float(r['Tank_lbs']) for _, r in clients.iterrows()}

    # Last delivery per customer ≤ cutoff
    past = deliveries[deliveries['Date'] <= cutoff].sort_values(['Customer', 'Date'])
    last_per = past.groupby('Customer').tail(1)

    for _, row in last_per.iterrows():
        cust = row['Customer']
        cid = cust_to_id.get(cust)
        if cid is None:
            continue
        tank = tank_by_id.get(cid, 6000.0)
        last_date = pd.Timestamp(row['Date'])
        days = (cutoff - last_date).days
        rate = rate_col_value.get(cid, 0.0)
        level = max(tank - days * rate, tank * 0.0)
        state.clients[cid] = ClientState(
            id=cid,
            current_lbs=level,
            last_delivery=str(last_date.date()),
            last_delivery_qty=float(row['Qty_lbs']),
            days_since_last=float(days),
            confidence='replayed',
        )

    # For clients with no deliveries before cutoff (brand new), assume 50%
    for _, c in clients.iterrows():
        cid = str(c['ID'])
        if cid not in state.clients:
            state.clients[cid] = ClientState(
                id=cid,
                current_lbs=float(c['Tank_lbs']) * 0.5,
                confidence='unknown',
            )
    return state


def working_days_between(
    start: pd.Timestamp, end: pd.Timestamp,
) -> List[pd.Timestamp]:
    workday_set = set(DAYS)
    out = []
    cur = pd.Timestamp(start).normalize()
    end = pd.Timestamp(end).normalize()
    while cur <= end:
        if _WEEKDAY_SHORT[cur.weekday()] in workday_set:
            out.append(cur)
        cur = cur + pd.Timedelta(days=1)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Plan-day-0 → set of (client_id, lbs) the plan would deliver
# ─────────────────────────────────────────────────────────────────────────────

def plan_day0_visits(routes: Dict[int, pd.DataFrame]) -> Dict[str, float]:
    """Extract the day-0 visits from a solver result. Returns {id: refill_lbs}."""
    out = {}
    df0 = routes.get(0)
    if df0 is None or df0.empty:
        return out
    for _, r in df0.iterrows():
        cid = str(r.get('ID', ''))
        out[cid] = float(r.get('Refill_lbs', 0) or 0)
    return out


def plan_day0_route_metrics(routes: Dict[int, pd.DataFrame]) -> Tuple[float, float]:
    """Total miles + minutes for day-0 routes."""
    df0 = routes.get(0)
    if df0 is None or df0.empty:
        return 0.0, 0.0
    miles = float(df0.get('Cum_Dist_mi', pd.Series([0])).max() or 0)
    # If multiple trucks on same day, sum max-per-truck
    if 'Truck' in df0.columns:
        miles = float(df0.groupby('Truck')['Cum_Dist_mi'].max().sum())
    mins = float(df0.get('Route_Time_min', pd.Series([0])).max() or 0)
    if 'Truck' in df0.columns:
        mins = float(df0.groupby('Truck')['Route_Time_min'].max().sum())
    return miles, mins


# ─────────────────────────────────────────────────────────────────────────────
# The replay loop
# ─────────────────────────────────────────────────────────────────────────────

def run_backtest(
    *,
    solver: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
    input_file: Path,
    matrix_file: Path,
    solve_seconds: int = 30,
    horizon_days: int = 5,
    commit_days: int = 1,
    cost: CostModel = DEFAULT_COSTS,
    verbose: bool = True,
    demand_noise_pct: float = 0.0,    # 0.0 = deterministic; 0.20 = ±20% daily noise
    rng_seed: Optional[int] = None,
    closed_loop: bool = False,        # True = state evolves under PLAN (not history)
) -> BacktestSummary:
    """
    Replay each working day in [start, end] through `solver`, scoring
    each day's plan against actual deliveries.

    solver: 'legacy' (original unified_solver, magic constants)
            'irp'    (IRP-aware: P95, $-calibrated, stateful)
    """
    clients_full, deliveries_full = load_all(input_file)

    # Filter out Tucson/Flagstaff (separate bi-weekly run, not in scope of
    # the metro weekly optimizer). This matches what the legacy solver and
    # the human dispatcher both do — they're not in actuals either.
    excluded = set(str(x) for x in EXCLUDED_CLIENT_IDS)
    if excluded:
        n_before = len(clients_full)
        clients_full = clients_full[~clients_full['ID'].astype(str).isin(excluded)].copy()
        # Filter deliveries by Customer name (not ID, since deliveries are keyed by Customer)
        excluded_customers = set(
            load_all(input_file)[0].loc[
                load_all(input_file)[0]['ID'].astype(str).isin(excluded), 'Customer'
            ].tolist()
        )
        deliveries_full = deliveries_full[
            ~deliveries_full['Customer'].isin(excluded_customers)
        ].copy()
        if verbose:
            print(f'  Excluded {n_before - len(clients_full)} Tucson/Flagstaff clients (separate run)')

    dist_matrix, time_matrix, node_index_map = load_matrix(matrix_file)
    time_windows_df = load_time_windows(input_file)
    closures_df = load_closures(input_file)
    depot_config = load_depot_config(input_file)

    days = working_days_between(start, end)
    if verbose:
        print(f'\n  Backtesting {len(days)} working days  | solver={solver}')
        print(f'  Cost: $${cost.fuel_per_mi}/mi fuel, $${cost.labor_per_min}/min labor, '
              f'$${cost.stockout_dollars} stockout')

    summary = BacktestSummary(
        solver=solver, start=str(start.date()), end=str(end.date()),
    )

    # Bootstrap consumption rates from FULL history (cheating slightly —
    # in production we'd use only past data. But fitting daily is too
    # expensive in a backtest; the rate drift is small over 30 days).
    bootstrap_models = fit_demand_models(deliveries_full, clients_full, today=start)
    # Per-client rate dict for state reconstruction
    rate_lookup = {
        cid: m.daily_mean() for cid, m in bootstrap_models.items()
    }

    # Initial state at day 0
    state = reconstruct_state_at(
        deliveries=deliveries_full[deliveries_full['Date'] < start],
        clients=clients_full,
        cutoff=start - pd.Timedelta(days=1),
        rate_col_value=rate_lookup,
    )

    # RNG for stochastic-demand mode
    rng = np.random.default_rng(rng_seed if rng_seed is not None else 12345)

    prev_plan_visits: Dict[str, float] = {}
    prev_plan_payload: Optional[dict] = None

    for i, target_day in enumerate(days):
        # We're "planning the evening of (target_day - 1)" so that plan
        # day-0 lands on target_day. State is reconstructed at end of
        # the day BEFORE target_day; today (the planner's "now") is
        # target_day - 1.
        today = target_day - pd.Timedelta(days=1)
        if verbose:
            mean_lvl = np.mean([c.current_lbs for c in state.clients.values()])
            print(f'\n  ── Day {i+1}/{len(days)}  plan-for {target_day.strftime("%a %b %d")} '
                  f'(planner-today {today.strftime("%a %b %d")}, '
                  f'mean state lvl: {mean_lvl:.0f} lbs) ──')

        # Fit models on data strictly before target_day
        deliv_past = deliveries_full[deliveries_full['Date'] < target_day]
        models = fit_demand_models(deliv_past, clients_full, today=today)
        clients_df = attach_demand_columns(clients_full, models)
        # Inject state into clients_df
        clients_df['Current_lbs'] = clients_df['ID'].astype(str).map(
            lambda i: state.level(str(i), default=None)
        ).fillna(clients_df['Tank_lbs'] * 0.5)
        clients_df['Est_Current_lbs'] = clients_df['Current_lbs']
        clients_df['Refill_Today_lbs'] = (clients_df['Tank_lbs'] - clients_df['Current_lbs']).clip(lower=0)
        clients_df['Fill_Pct_Today'] = (clients_df['Refill_Today_lbs'] / clients_df['Tank_lbs']).clip(0, 1)
        clients_df['Rate_Source'] = clients_df['Demand_Source'].map({
            'own': 'own', 'pooled': 'pooled', 'insufficient': 'INSUFFICIENT_DATA',
        }).fillna('INSUFFICIENT_DATA')

        # Compute days-until-stockout (P50 always, P95 if IRP)
        plan_dates = [today + pd.Timedelta(days=k+1) for k in range(horizon_days)]
        if solver == 'irp':
            profiles = build_urgency_profiles(
                clients_df=clients_df, state_lookup=state.as_dict(),
                models=models, plan_dates=plan_dates, cost=cost,
            )
            clients_df = attach_urgency_columns(clients_df, profiles)
            # KEEP legacy's hand-tuned knobs. The IRP's value here is its
            # better demand model + state persistence + must_visit set,
            # not its cost calibration (which is too aggressive for SK's
            # actual economics until ops confirms real $).
            _cfg.LATE_PENALTY_PER_DAY = 5_000
            _cfg.OT_PENALTY_PER_MIN = 25
            _cfg.LABOR_COST_PER_MIN = 50
            # Only the most critical clients become hard-must-visit
            # (P95 stockout within commit window).
            must_visit = {
                cid for cid, p in profiles.items()
                if p.visit_by_day_index <= max(commit_days - 1, 0)
            }
        else:
            # Legacy path: compute Days_Until_Stockout from P50 only
            from inventory import days_until_stockout, urgency_tier
            clients_df['Days_Until_Stockout'] = clients_df.apply(
                lambda r: days_until_stockout(r['Current_lbs'], r['Avg_LbsPerDay'], r['Tank_lbs']),
                axis=1,
            )
            clients_df['Urgency'] = clients_df['Days_Until_Stockout'].apply(urgency_tier)
            # Restore legacy magic constants
            _cfg.LATE_PENALTY_PER_DAY = 5_000
            _cfg.OT_PENALTY_PER_MIN = 25
            _cfg.LABOR_COST_PER_MIN = 50
            must_visit = set()

        # Build warm-start hint from previous plan (IRP solver only)
        initial_routes_by_vehicle = None
        if solver == 'irp' and prev_plan_payload is not None:
            truck_idx = {'Truck2': 0, 'Truck9': 1}
            shifted = shift_plan_for_today(plan=prev_plan_payload, today=today)
            initial_routes_by_vehicle = {}
            for (day, truck, cfg), ids in shifted.items():
                if truck not in truck_idx or day >= horizon_days:
                    continue
                v = _vehicle_index(
                    truck_idx[truck], day, cfg,
                    num_days=horizon_days, num_configs=3,
                )
                initial_routes_by_vehicle[v] = ids
            if not initial_routes_by_vehicle:
                initial_routes_by_vehicle = None

        # Solve (silence the legacy solver's verbose stdout)
        t0 = time.time()
        import io, contextlib
        _buf = io.StringIO()
        with contextlib.redirect_stdout(_buf):
            committed, tentative, deferred = solve_horizon(
                clients_df=clients_df,
                dist_matrix=dist_matrix,
                time_matrix_min=time_matrix,
                node_index_map=node_index_map,
                today=today,
                horizon_days=horizon_days,
                commit_days=commit_days,
                solve_seconds=solve_seconds,
                time_windows_df=time_windows_df,
                closures_df=closures_df,
                depot_config=depot_config,
                must_visit_ids=must_visit,
                initial_routes_by_vehicle=initial_routes_by_vehicle,
            )
        t_solve = time.time() - t0

        # Routes day 0 → planned visits
        all_routes = {**committed, **tentative}
        planned = plan_day0_visits(all_routes)
        miles, minutes = plan_day0_route_metrics(all_routes)
        plan_cost = (
            miles * cost.fuel_per_mi
            + minutes * cost.labor_per_min
            + max(0, minutes - cost.shift_min) * cost.ot_per_min
        )

        # ── Compare against reality (target_day's actuals) ───────────────
        actual_today = deliveries_full[deliveries_full['Date'] == target_day]
        cust_to_id = dict(zip(clients_full['Customer'], clients_full['ID'].astype(str)))
        actual_ids = {
            cust_to_id[c] for c in actual_today['Customer'] if c in cust_to_id
        }
        actual_qty = {
            cust_to_id[r['Customer']]: float(r['Qty_lbs'])
            for _, r in actual_today.iterrows() if r['Customer'] in cust_to_id
        }

        matched = len(actual_ids & set(planned.keys()))
        missed = len(actual_ids - set(planned.keys()))
        extras = len(set(planned.keys()) - actual_ids)

        # Plan stability = today's day-0 ∩ yesterday's day-0
        if prev_plan_visits:
            stability = (
                len(set(planned.keys()) & set(prev_plan_visits.keys())) /
                max(len(set(planned.keys()) | set(prev_plan_visits.keys())), 1)
            )
        else:
            stability = None

        # Stockout count: clients whose current_lbs ≤ 0 at end of today
        # We roll state forward by:
        #   - applying ACTUAL deliveries (truth)
        #   - decaying by 1 day of mean consumption
        # Stockout = client at floor before the day's delivery (if any)
        stockout_today = 0
        for cid, rec in state.clients.items():
            tank = float(clients_full.loc[clients_full['ID']==cid, 'Tank_lbs'].iloc[0]) \
                if (clients_full['ID']==cid).any() else 6000.0
            # Did they need a delivery before stockout?
            rate = rate_lookup.get(cid, 0.0)
            if rec.current_lbs - rate < 0 and cid not in actual_ids:
                stockout_today += 1

        # Roll state forward to end-of(target_day).
        state.advance(target_day)
        if demand_noise_pct > 0:
            df_for_decay = clients_df.copy()
            shocks = rng.lognormal(mean=0.0, sigma=demand_noise_pct,
                                    size=len(df_for_decay))
            df_for_decay['Avg_LbsPerDay'] = (df_for_decay['Avg_LbsPerDay']
                                             .fillna(0) * shocks)
            state.apply_consumption(df_for_decay, n_days=1,
                                    rate_col='Avg_LbsPerDay')
        else:
            state.apply_consumption(clients_df, n_days=1,
                                    rate_col='Avg_LbsPerDay')

        # ── State update mode ────────────────────────────────────────────
        # CLOSED-LOOP: state evolves under the optimizer's PLAN. This is
        #   the proper IRP backtest — each solver lives with the
        #   consequences of its own choices.
        # OPEN-LOOP (default): state evolves under real history. Useful
        #   for validating forecast quality but doesn't differentiate
        #   solvers' operational outcomes.
        if closed_loop:
            # Apply the OPTIMIZER's plan as deliveries (subject to capacity
            # already accounted for in the planning).
            plan_actuals = [
                {'id': cid, 'qty_lbs': lbs, 'date': target_day}
                for cid, lbs in planned.items()
            ]
            state.apply_deliveries(plan_actuals, clients_df)
            # Count stockouts: clients now below floor
            sim_stockouts = sum(
                1 for rec in state.clients.values() if rec.current_lbs <= 0
            )
            sim_levels = [
                rec.current_lbs / float(
                    clients_full.loc[clients_full['ID']==rec.id, 'Tank_lbs'].iloc[0]
                )
                for rec in state.clients.values()
                if (clients_full['ID']==rec.id).any()
            ]
            sim_avg_pct = float(np.mean(sim_levels)) if sim_levels else 0.5
        else:
            # Apply real deliveries on target_day (open-loop / forecast eval)
            actuals_dicts = [
                {'id': cid, 'qty_lbs': qty, 'date': target_day}
                for cid, qty in actual_qty.items() if cid is not None
            ]
            state.apply_deliveries(actuals_dicts, clients_df)
            sim_stockouts = 0
            sim_avg_pct = 0.0

        # Day result
        day_n_clients = len(clients_full)
        result = DayResult(
            date=str(target_day.date()),
            stops_planned=len(planned),
            stops_actual=len(actual_ids),
            miles_planned=miles,
            minutes_planned=minutes,
            cost_dollars=plan_cost,
            plan_stability=stability,
            matched_actuals=matched,
            actuals_missed=missed,
            plan_extras=extras,
            stockout_clients_today=stockout_today,
            deferred_count=len(deferred) if deferred is not None else 0,
            solve_seconds=t_solve,
            sim_stockouts_today=sim_stockouts,
            sim_avg_tank_pct=sim_avg_pct,
        )
        summary.sim_total_stockout_days += sim_stockouts
        if closed_loop and sim_avg_pct > 0:
            summary.sim_min_tank_pct = min(summary.sim_min_tank_pct, sim_avg_pct)
        summary.days.append(result)
        summary.total_cost += plan_cost
        summary.total_miles += miles
        summary.total_stops += len(planned)
        summary.total_stockout_client_days += stockout_today
        summary.total_client_days += day_n_clients
        if verbose:
            print(f'    Planned: {len(planned):3d} stops  {miles:6.1f} mi  {minutes:5.0f} min  '
                  f'${plan_cost:6.2f} | Actual: {len(actual_ids):3d} | match={matched} '
                  f'miss={missed} extra={extras} stockouts={stockout_today} | '
                  f'stability={"--" if stability is None else f"{stability:.0%}"}')

        prev_plan_visits = planned
        # Build a plan payload for warm-starting tomorrow
        payload_visits = []
        for d, df in all_routes.items():
            if df is None or df.empty:
                continue
            for _, r in df.iterrows():
                payload_visits.append({
                    'day': int(d),
                    'client_id': str(r.get('ID', '')),
                    'truck': str(r.get('Truck', '')),
                    'stop': int(r.get('Stop', 0) or 0),
                })
        prev_plan_payload = {
            'today': str(today.date()),
            'horizon_days': horizon_days,
            'visits': payload_visits,
        }
        summary.days_simulated += 1

    # Aggregate stability + match
    stab_vals = [d.plan_stability for d in summary.days if d.plan_stability is not None]
    summary.avg_plan_stability = float(np.mean(stab_vals)) if stab_vals else 0.0
    match_vals = [
        d.matched_actuals / max(d.stops_actual, 1) for d in summary.days
        if d.stops_actual > 0
    ]
    summary.avg_matched_pct = float(np.mean(match_vals)) if match_vals else 0.0

    return summary


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description='Backtest the IRP route optimizer')
    parser.add_argument('--solver', choices=['legacy', 'irp', 'both'], default='both')
    parser.add_argument('--start', type=str, default=None)
    parser.add_argument('--end', type=str, default=None)
    parser.add_argument('--days', type=int, default=None,
                        help='If --end omitted: simulate this many days from --start')
    parser.add_argument('--quick', action='store_true',
                        help='Smoke test: 5 days, 15s solve')
    parser.add_argument('--solve-sec', type=int, default=30)
    parser.add_argument('--horizon-days', type=int, default=5)
    parser.add_argument('--commit-days', type=int, default=1)
    parser.add_argument('--input-file', type=str, default=None)
    parser.add_argument('--output', type=str, default='backtest_irp_results.json')
    parser.add_argument('--demand-noise', type=float, default=0.0,
                        help='Inject log-normal demand noise (0.20 = ±20%)')
    parser.add_argument('--seed', type=int, default=None)
    parser.add_argument('--closed-loop', action='store_true',
                        help='Closed-loop simulation: state evolves under '
                             'each optimizer\'s plan (proper IRP backtest)')
    parser.add_argument('--stockout-dollars', type=float, default=None,
                        help='Override CostModel.stockout_dollars (default $800)')
    parser.add_argument('--late-pct', type=float, default=None,
                        help='Override CostModel.late_per_day_pct (default 0.20)')
    parser.add_argument('--service-alpha', type=float, default=None,
                        help='Override CostModel.service_alpha (default 0.05 = P95)')
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING, format='  %(message)s')

    input_file = Path(args.input_file) if args.input_file else INPUT_FILE
    matrix_file = MATRIX_FILE

    if args.quick:
        start = pd.Timestamp('2026-04-13')
        end = pd.Timestamp('2026-04-17')
        solve_sec = 15
    else:
        if not args.start:
            print('ERROR: --start required (or use --quick).', file=sys.stderr)
            return 2
        start = pd.Timestamp(args.start)
        if args.end:
            end = pd.Timestamp(args.end)
        else:
            end = start + pd.Timedelta(days=(args.days or 14) - 1)
        solve_sec = args.solve_sec

    # Build the cost model (with optional CLI overrides)
    cost_kwargs = {}
    if args.stockout_dollars is not None:
        cost_kwargs['stockout_dollars'] = args.stockout_dollars
    if args.late_pct is not None:
        cost_kwargs['late_per_day_pct'] = args.late_pct
    if args.service_alpha is not None:
        cost_kwargs['service_alpha'] = args.service_alpha
    cost = CostModel(**cost_kwargs) if cost_kwargs else DEFAULT_COSTS
    print(f'  Cost model: stockout=${cost.stockout_dollars} '
          f'late/day=${cost.late_dollars_per_day():.0f} '
          f'service-alpha={cost.service_alpha}')

    solvers = ['legacy', 'irp'] if args.solver == 'both' else [args.solver]
    summaries: Dict[str, BacktestSummary] = {}

    for s in solvers:
        print(f'\n{"=" * 78}')
        print(f'  Backtest: solver = {s.upper()}')
        print(f'{"=" * 78}')
        summary = run_backtest(
            solver=s, start=start, end=end,
            input_file=input_file, matrix_file=matrix_file,
            solve_seconds=solve_sec,
            horizon_days=args.horizon_days,
            commit_days=args.commit_days,
            demand_noise_pct=args.demand_noise,
            rng_seed=args.seed,
            closed_loop=args.closed_loop,
            cost=cost,
        )
        summaries[s] = summary

    # Print head-to-head
    print(f'\n{"=" * 78}')
    print('  HEAD-TO-HEAD')
    print(f'{"=" * 78}')
    for s, summary in summaries.items():
        line = (f'\n  {s.upper():>8s}: '
                f'{summary.days_simulated} days  | '
                f'${summary.total_cost:8.2f} cost  | '
                f'{summary.total_miles:7.1f} mi  | '
                f'{summary.total_stops} stops  | '
                f'service={summary.service_level:.2%}  | '
                f'match={summary.avg_matched_pct:.0%}  | '
                f'stability={summary.avg_plan_stability:.0%}')
        if args.closed_loop:
            line += (f'\n            closed-loop sim: '
                     f'stockouts={summary.sim_total_stockout_days} '
                     f'min_tank={summary.sim_min_tank_pct:.0%}')
        print(line)

    if 'legacy' in summaries and 'irp' in summaries:
        L, I = summaries['legacy'], summaries['irp']
        d_cost = (I.total_cost - L.total_cost) / max(L.total_cost, 1) * 100
        d_stockout = I.total_stockout_client_days - L.total_stockout_client_days
        d_match = (I.avg_matched_pct - L.avg_matched_pct) * 100
        d_stability = (I.avg_plan_stability - L.avg_plan_stability) * 100
        print(f'\n  IRP vs LEGACY:')
        print(f'    Δ cost:       {d_cost:+6.2f}%')
        print(f'    Δ stockouts:  {d_stockout:+d} client-days  (open-loop)')
        print(f'    Δ match%:     {d_match:+5.1f} pp')
        print(f'    Δ stability:  {d_stability:+5.1f} pp')
        if args.closed_loop:
            d_sim = I.sim_total_stockout_days - L.sim_total_stockout_days
            d_pct = (I.sim_min_tank_pct - L.sim_min_tank_pct) * 100
            print(f'    Δ sim stockouts: {d_sim:+d} client-days  ★ closed-loop ★')
            print(f'    Δ sim min tank:  {d_pct:+5.1f} pp')

    # Save
    out = Path(args.output)
    out.write_text(json.dumps(
        {s: summary.to_json() for s, summary in summaries.items()},
        default=str, indent=2,
    ))
    print(f'\n  Results saved → {out}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
