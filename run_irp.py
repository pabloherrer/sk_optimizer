#!/usr/bin/env python3
"""
run_irp.py — IRP-aware rolling-horizon entry point
==================================================

This is the new front door for daily planning. It composes the existing
unified solver with the irp_core stack:

  1. Load atomic state                 → InventoryState (irp_core.state_manager)
  2. Fit quantile demand model         → DemandModel    (irp_core.forecasting)
  3. Build chance-constrained urgency  → UrgencyProfile (irp_core.safety_stock)
  4. Calibrate $-denominated knobs     → cost units     (irp_core.objective)
  5. Solve via legacy unified_solver   → routes
  6. Persist plan + state atomically   → next run starts here

Compared to run_unified.py, this entry point closes the rolling-horizon
loop that was previously broken: state.json is actually saved at the
end of every run, and tomorrow's plan is genuinely a continuation of
today's tentative plan, not a cold restart.

Usage
-----
    python run_irp.py                       # full IRP path
    python run_irp.py --validate-only       # validate inputs only
    python run_irp.py --confirm <file.csv>  # apply driver-confirmed
                                            # actuals to state
    python run_irp.py --dry-run             # don't save plan/state
    python run_irp.py --legacy-costs        # use legacy magic constants
                                            # (for A/B comparison)

CLI overrides honor environment variables (set by app.py):
    SK_SKIP_IDS, SK_MUST_VISIT_IDS, SK_ACTIVE_TRUCKS
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
import time
import uuid
from pathlib import Path
from typing import List, Optional

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

# ── Legacy modules (data + solver) ─────────────────────────────────────────────
from config import (
    INPUT_FILE, MATRIX_FILE, STATE_FILE, OUTPUT_DIR, DATA_DIR,
    SOLVE_SEC_WEEK, DAYS, HORIZON_DAYS, COMMIT_DAYS,
)
import config as _cfg
from load_data import load_all
from router import load_matrix
from schema_loaders import (
    load_time_windows, load_closures, load_depot_config, load_trucks,
)
from unified_solver import solve_horizon
from output import save_excel_schedule, save_route_map
from validator import validate_inputs

# ── New IRP core ───────────────────────────────────────────────────────────────
from irp_core.state_manager import (
    InventoryState, DeliveryLog, save_plan, load_plan, commit_run,
    confirm_deliveries,
)
from irp_core.forecasting import fit_demand_models, attach_demand_columns
from irp_core.safety_stock import build_urgency_profiles, attach_urgency_columns
from irp_core.economics import CostModel, DEFAULT_COSTS
from irp_core.objective import (
    calibrate_legacy_knobs, build_per_client_disjunction_penalties,
    cost_units_per_dollar, explain_objective_in_dollars,
)
from irp_core.warm_start import plan_overlap, shift_plan_for_today, _vehicle_index
from irp_core.anova_integration import (
    load_all_readings, apply_anova_to_state, tighten_sigma_for_monitored,
)
from irp_core.diagnostics import compute_plan_quality, pretty_print_quality
from irp_core.smartservice_export import export_smartservice_csv


# ── Paths ──────────────────────────────────────────────────────────────────────
PLAN_FILE = DATA_DIR / 'plan.json'
DELIVERY_LOG_FILE = DATA_DIR / 'deliveries.log.jsonl'

# ANOVA live-inventory data sources (set to None to disable)
ANOVA_CSV_FILE   = Path('/Users/pabloherrera/Documents/Claude/Projects/route optimization/anova_data/readings.csv')
ANOVA_EXCEL_FILE = Path('/Users/pabloherrera/Documents/Claude/Projects/route optimization/anova_live_readings.xlsx')

# ── Working day handling ───────────────────────────────────────────────────────
_WEEKDAY_SHORT = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']


def compute_plan_dates(today: pd.Timestamp, n_days: int = 10) -> List[pd.Timestamp]:
    """
    Return the next n working days INCLUDING today (if today is a workday).

    Semantics: "today" = the day we're planning FOR. If you run the
    optimizer on Tuesday morning for that day's deliveries, --today=Tue
    → plan day 0 = Tuesday. If today is a non-workday (Sun/Mon), day 0
    is the next working day.
    """
    workday_set = set(DAYS)
    dates: List[pd.Timestamp] = []
    cur = pd.Timestamp(today).normalize()
    for _ in range(60):
        if len(dates) >= n_days:
            break
        if _WEEKDAY_SHORT[cur.weekday()] in workday_set:
            dates.append(cur)
        cur = cur + pd.Timedelta(days=1)
    return dates


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format='  %(message)s',
    )


def banner(title: str, char: str = '═', width: int = 78) -> None:
    print(char * width)
    print(f'  {title}')
    print(char * width)


# ─────────────────────────────────────────────────────────────────────────────
# Driver-actuals confirmation (for end-of-day reconciliation)
# ─────────────────────────────────────────────────────────────────────────────

def confirm_actuals_from_csv(
    csv_path: Path,
    state: InventoryState,
    state_file: Path,
    clients_df: pd.DataFrame,
    delivery_log: DeliveryLog,
    run_id: str,
) -> int:
    """
    Read a CSV of confirmed deliveries and apply to state.
    Expected columns: id, qty_lbs, date [, truck]
    """
    actuals = []
    with open(csv_path, encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            actuals.append({
                'id': str(row['id']).strip(),
                'qty_lbs': float(row.get('qty_lbs', 0) or 0),
                'date': row.get('date', str(state.as_of.date())),
                'truck': row.get('truck', ''),
                'confirmed': True,
            })
    n = confirm_deliveries(
        state=state, state_file=state_file, actuals=actuals,
        clients_df=clients_df, delivery_log=delivery_log, run_id=run_id,
    )
    print(f'  Applied {n} confirmed deliveries from {csv_path.name}.')
    return n


# ─────────────────────────────────────────────────────────────────────────────
# Main planning loop
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description='S&K IRP-aware route optimizer')
    parser.add_argument('--demo', action='store_true', help='Use fictional demo data')
    parser.add_argument('--solve-sec', type=int, default=None, help='Solver time limit')
    parser.add_argument('--today', default=None, help="Override today's date (YYYY-MM-DD)")
    parser.add_argument('--horizon-days', type=int, default=None)
    parser.add_argument('--commit-days', type=int, default=None)
    parser.add_argument('--input-file', type=str, default=None)
    parser.add_argument('--output-prefix', type=str, default=None)
    parser.add_argument('--validate-only', action='store_true')
    parser.add_argument('--skip-validation', action='store_true')
    parser.add_argument('--dry-run', action='store_true',
                        help="Don't save plan or state (for testing)")
    parser.add_argument('--dollar-objective', action='store_true',
                        help='Override legacy hand-tuned knobs with full '
                             '$-calibrated values. Default keeps legacy knobs '
                             '(safer until ops confirms real $).')
    parser.add_argument('--enable-anova', action='store_true',
                        help='Enable ANOVA live-inventory override. Default '
                             'OFF until the receiver/pull pipeline is verified '
                             'and asset-to-client mapping is confirmed for the '
                             'real tank fleet.')
    parser.add_argument('--no-warm-start', action='store_true',
                        help='Solve from cold (ignore plan.json)')
    parser.add_argument('--auto-apply-day0', action='store_true',
                        help="Pre-apply day-0 committed deliveries to state at end of run "
                             '(use only if you trust the plan to execute as written)')
    parser.add_argument('--confirm', type=str, default=None,
                        help='Path to a CSV of driver-confirmed deliveries to apply, then exit')
    parser.add_argument('--verbose', action='store_true')
    args = parser.parse_args()

    setup_logging(args.verbose)

    # ── Inputs ────────────────────────────────────────────────────────────
    if args.demo:
        input_file = DATA_DIR / 'SK_Fictional_Demo.xlsx'
        prefix = args.output_prefix or 'demo_irp'
    elif args.input_file:
        input_file = Path(args.input_file)
        prefix = args.output_prefix or 'sk_irp'
    else:
        input_file = INPUT_FILE
        prefix = args.output_prefix or 'sk_irp'

    today = pd.Timestamp(args.today).normalize() if args.today else \
            pd.Timestamp.today().normalize()
    horizon_days = args.horizon_days or HORIZON_DAYS
    commit_days = args.commit_days or COMMIT_DAYS
    solve_sec = args.solve_sec or SOLVE_SEC_WEEK

    run_id = datetime_str() + '_' + uuid.uuid4().hex[:6]

    banner('S&K IRP Route Optimizer  (Stateful Rolling Horizon)')
    print(f'  Run ID:       {run_id}')
    print(f'  Today:        {today.date()}')
    print(f'  Horizon:      {horizon_days} working days  (commit {commit_days})')
    print(f'  Solve time:   {solve_sec}s')
    print(f'  Costs:        {"$-CALIBRATED" if args.dollar_objective else "LEGACY hand-tuned (default)"}')
    print()

    # ── Load delivery log and state ──────────────────────────────────────
    delivery_log = DeliveryLog(DELIVERY_LOG_FILE)
    state = InventoryState.load(STATE_FILE)
    print(f'  State as-of:   {state.as_of.date()}  ({len(state.clients)} clients tracked)')

    # ── Load raw client + delivery data ──────────────────────────────────
    print(f'\n[1/8] Loading data from {input_file.name}...')
    clients_raw, deliveries = load_all(input_file)

    # ── Confirm-actuals path ─────────────────────────────────────────────
    if args.confirm:
        confirm_path = Path(args.confirm)
        if not confirm_path.exists():
            print(f'  ERROR: --confirm path does not exist: {confirm_path}', file=sys.stderr)
            return 2
        confirm_actuals_from_csv(
            confirm_path, state, STATE_FILE, clients_raw, delivery_log, run_id,
        )
        return 0

    # ── Demand model ─────────────────────────────────────────────────────
    # Two layers, used differently:
    #   (1) Legacy median-of-recent-deliveries → primary `Avg_LbsPerDay`
    #       (more responsive to recent shifts; what high-consumers like
    #       HAROLDS actually consume right now).
    #   (2) IRP empirical-Bayes posterior → σ̂ for chance constraints
    #       and DOW pattern (when enabled).
    # We feed (1) to the solver as Avg_LbsPerDay; (2) is consulted by the
    # safety-stock layer.
    print(f'\n[2/8] Fitting demand models...')
    from forecast_consumption import estimate_consumption_rates as _legacy_rates
    clients_df = _legacy_rates(deliveries, clients_raw, today=today)
    # Now overlay the IRP posterior σ̂ (and rates) but DO NOT overwrite
    # Avg_LbsPerDay — keep the legacy responsive estimate.
    models = fit_demand_models(deliveries, clients_raw, today=today)
    clients_df['Demand_Sigma'] = clients_df['ID'].astype(str).map(
        lambda i: models[i].sigma if i in models else None
    )
    clients_df['Demand_Source'] = clients_df['ID'].astype(str).map(
        lambda i: models[i].source if i in models else 'insufficient'
    )
    # DOW pattern (optional — solver uses it only if Rate_By_DOW exists
    # and is reasonable). Currently OFF by default per user preference.
    enable_dow = False
    if enable_dow:
        clients_df['Rate_By_DOW'] = clients_df['ID'].astype(str).map(
            lambda i: list(models[i].rates) if i in models else None
        )

    # If state is empty (first run after migration), seed it from estimates
    if not state.clients:
        print('  State is empty — seeding from delivery-log estimates.')
        from forecast_consumption import estimate_consumption_rates
        from irp_core.state_manager import ClientState
        seeded = estimate_consumption_rates(deliveries, clients_raw, today=today)
        for _, row in seeded.iterrows():
            est = row.get('Est_Current_lbs')
            cid = str(row['ID'])
            if est is not None and pd.notna(est):
                state.clients[cid] = ClientState(
                    id=cid, current_lbs=float(est), confidence='estimated',
                )

    # ── Plan dates ───────────────────────────────────────────────────────
    plan_dates = compute_plan_dates(today, n_days=horizon_days)
    actual_horizon = len(plan_dates)
    if actual_horizon == 0:
        print('  ERROR: no working days in horizon.', file=sys.stderr)
        return 2

    # ── Cost model (used for IRP urgency profiles regardless of solver knobs) ──
    cost = DEFAULT_COSTS

    # ── ANOVA live-inventory override (DISABLED until real data flows) ───
    # ANOVA integration is scaffolded but NOT operational:
    #   • The receiver webhook isn't running in production
    #   • The pull script needs manual cookie auth
    #   • Asset-name → client-ID mapping is unverified for SK's tank fleet
    # Re-enable by setting --enable-anova on the command line once those
    # gaps are closed.
    monitored_ids: set = set()
    if args.enable_anova and (ANOVA_CSV_FILE.exists() or ANOVA_EXCEL_FILE.exists()):
        readings = load_all_readings(
            csv_path=ANOVA_CSV_FILE if ANOVA_CSV_FILE.exists() else None,
            excel_path=ANOVA_EXCEL_FILE if ANOVA_EXCEL_FILE.exists() else None,
            fresh_hours=24.0,
        )
        if readings:
            applied = apply_anova_to_state(
                state=state, readings=readings, clients_df=clients_raw,
            )
            monitored_ids = set(applied.keys())
            if applied:
                print(f'\n  ANOVA: applied live readings for {len(applied)} client(s).')
                tighten_sigma_for_monitored(
                    models=models, monitored_client_ids=monitored_ids,
                    sigma_multiplier=0.4,
                )

    # ── Build chance-constrained urgency profiles ────────────────────────
    print(f'\n[3/8] Building chance-constrained urgency profiles (α={cost.service_alpha:.0%})...')
    profiles = build_urgency_profiles(
        clients_df=clients_df,
        state_lookup=state.as_dict(),
        models=models,
        plan_dates=plan_dates,
        cost=cost,
    )

    # Inject P95-based urgency columns for legacy solver consumption
    clients_df = attach_urgency_columns(clients_df, profiles)
    # Also need Est_Current_lbs for legacy code paths
    clients_df['Est_Current_lbs'] = clients_df['ID'].map(
        lambda i: state.level(str(i), default=None)
    ).fillna(clients_df['Tank_lbs'] * 0.5)
    clients_df['Current_lbs'] = clients_df['Est_Current_lbs']
    # Some legacy code expects Refill_Today_lbs and Fill_Pct_Today
    clients_df['Refill_Today_lbs'] = (clients_df['Tank_lbs'] - clients_df['Current_lbs']).clip(lower=0)
    clients_df['Fill_Pct_Today'] = clients_df['Refill_Today_lbs'] / clients_df['Tank_lbs']
    # Mark clients with NaN consumption rate as INSUFFICIENT_DATA
    clients_df['Rate_Source'] = clients_df['Demand_Source'].map({
        'own': 'own', 'pooled': 'pooled', 'insufficient': 'INSUFFICIENT_DATA',
    }).fillna('INSUFFICIENT_DATA')

    n_mandatory = sum(1 for p in profiles.values() if p.is_mandatory)
    n_opportunistic = sum(1 for p in profiles.values() if p.is_opportunistic)
    print(f'  Mandatory (P95 stockout in horizon): {n_mandatory}')
    print(f'  Opportunistic (≥55% empty today):    {n_opportunistic}')

    # ── Calibrate solver knobs to dollars ────────────────────────────────
    print(f'\n[4/8] Calibrating solver knobs to ${cost.fuel_per_mi}/mi reference...')
    knobs = calibrate_legacy_knobs(cost)
    print(f'  LATE_PENALTY_PER_DAY: {knobs["LATE_PENALTY_PER_DAY"]:>10,} cost units '
          f'(${cost.late_dollars_per_day():.2f}/day)')
    print(f'  OT_PENALTY_PER_MIN:   {knobs["OT_PENALTY_PER_MIN"]:>10,} cost units '
          f'(${cost.ot_per_min:.4f}/min)')
    print(f'  COST_UNITS_PER_$:     {knobs["COST_UNITS_PER_DOLLAR"]:>10,.0f}')

    # Override legacy knobs only if user opts in. Default keeps legacy
    # hand-tuned values; they have years of tuning baked in.
    if args.dollar_objective:
        _cfg.LATE_PENALTY_PER_DAY = knobs['LATE_PENALTY_PER_DAY']
        _cfg.OT_PENALTY_PER_MIN = knobs['OT_PENALTY_PER_MIN']
        _cfg.LABOR_COST_PER_MIN = knobs['LABOR_COST_PER_MIN']

    # Per-client disjunction penalties (the real economic cost of dropping
    # each client). The legacy solver computes these internally based on
    # urgency tiers; we publish ours via env-var-style mechanism for an
    # eventual surgical patch. For now, the urgency tier injection above
    # gives the solver the right hard/soft rankings.
    drop_penalties = build_per_client_disjunction_penalties(
        profiles=profiles, cost=cost, horizon_days=actual_horizon,
    )
    if drop_penalties:
        avg = sum(drop_penalties.values()) / len(drop_penalties)
        print(f'  Per-client drop penalty (avg): {avg:>10,.0f} cost units '
              f'(${avg / knobs["COST_UNITS_PER_DOLLAR"]:.2f})')

    # ── Load constraints ─────────────────────────────────────────────────
    print(f'\n[5/8] Loading constraints (time windows, closures, depot)...')
    time_windows_df = load_time_windows(input_file)
    closures_df = load_closures(input_file)
    depot_config = load_depot_config(input_file)
    trucks_cfg = load_trucks(input_file)

    # ── Validate ─────────────────────────────────────────────────────────
    print(f'\n[6/8] Validating inputs...')
    dm, tm, node_index_map = load_matrix(MATRIX_FILE)
    if not args.skip_validation:
        report = validate_inputs(
            clients_df, deliveries,
            time_windows_df=time_windows_df,
            closures_df=closures_df,
            trucks_cfg=trucks_cfg,
            depot_config=depot_config,
            matrix_nodes=node_index_map,
        )
        report.pretty_print()
        if not report.ok:
            print('\n⛔ FIX INPUT ERRORS BEFORE SOLVING')
            return 1
        if args.validate_only:
            print('\n✓ Validation passed.\n')
            return 0

    # ── Operator overrides from env (set by app.py) ──────────────────────
    skip_ids = _env_set('SK_SKIP_IDS')
    must_visit_ids = _env_set('SK_MUST_VISIT_IDS')
    active_trucks_env = os.environ.get('SK_ACTIVE_TRUCKS', '')
    active_trucks = [s.strip() for s in active_trucks_env.split(',') if s.strip()] or None

    # Only ULTRA-mandatory clients (P95 stockout within commit window) are
    # forced via must_visit. The legacy solver's existing urgency tier
    # mechanism handles the broader "should serve this week" set via
    # disjunction penalties — over-using must_visit creates infeasibility
    # cascades.
    irp_must = {
        cid for cid, p in profiles.items()
        if p.visit_by_day_index <= max(commit_days - 1, 0)
    }
    must_visit_ids = must_visit_ids | irp_must

    if irp_must:
        print(f'  IRP critical (P95 stockout within commit window): '
              f'{len(irp_must)} clients (added to must-visit set)')

    # ── Build warm-start from prior plan ─────────────────────────────────
    prior_plan = load_plan(PLAN_FILE) if not args.no_warm_start else None
    initial_routes_by_vehicle: Optional[dict] = None
    if prior_plan is not None:
        plan_age_days = (today - pd.Timestamp(prior_plan['today'])).days
        print(f'  Prior plan: {prior_plan["solved_at"]} ({plan_age_days} day(s) old)')
        # Translate (day, truck, cfg) → vehicle_idx via the same scheme
        # the solver uses internally.
        truck_idx = {name: i for i, name in enumerate(['Truck2', 'Truck9'])}
        shifted = shift_plan_for_today(plan=prior_plan, today=today)
        initial_routes_by_vehicle = {}
        for (day, truck, cfg), ids in shifted.items():
            if truck not in truck_idx or day >= actual_horizon:
                continue
            v = _vehicle_index(
                truck_idx[truck], day, cfg,
                num_days=actual_horizon, num_configs=3,
            )
            initial_routes_by_vehicle[v] = ids
        if initial_routes_by_vehicle:
            n_warm = sum(len(v) for v in initial_routes_by_vehicle.values())
            print(f'  Warm-start hint: {n_warm} visits from prior plan')
        else:
            initial_routes_by_vehicle = None

    # ── Solve ────────────────────────────────────────────────────────────
    print(f'\n[7/8] Running solver ({solve_sec}s)...')
    t0 = time.time()
    committed, tentative, deferred = solve_horizon(
        clients_df=clients_df,
        dist_matrix=dm,
        time_matrix_min=tm,
        node_index_map=node_index_map,
        today=today,
        horizon_days=actual_horizon,
        commit_days=commit_days,
        solve_seconds=solve_sec,
        time_windows_df=time_windows_df,
        closures_df=closures_df,
        depot_config=depot_config,
        skip_ids=skip_ids,
        must_visit_ids=must_visit_ids,
        active_trucks=active_trucks,
        initial_routes_by_vehicle=initial_routes_by_vehicle,
    )
    t_solve = time.time() - t0

    # ── Merge committed + tentative ──────────────────────────────────────
    routes = {}
    for d, df in committed.items():
        if df is not None and not df.empty:
            df = df.copy()
            df['Status'] = 'COMMITTED'
        routes[d] = df
    for d, df in tentative.items():
        if df is not None and not df.empty:
            df = df.copy()
            df['Status'] = 'TENTATIVE'
        routes[d] = df

    # ── Plan stability vs prior plan ─────────────────────────────────────
    new_plan_payload = _routes_to_plan_dict(routes, today, plan_dates, horizon_days, commit_days)
    overlap = plan_overlap(prior_plan, new_plan_payload, day_offset_old=1, day_offset_new=0)
    print(f'  Solve time: {t_solve:.1f}s  | plan stability vs yesterday: {overlap:.0%}')

    # ── Plan-quality diagnostics ─────────────────────────────────────────
    quality = compute_plan_quality(
        routes=routes, clients_df=clients_df, deferred=deferred,
    )
    pretty_print_quality(quality)

    # ── Output (Excel + map) ─────────────────────────────────────────────
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    has_routes = any(df is not None and not df.empty for df in routes.values())
    if has_routes:
        # Add km columns for backward compat with output.py
        for d, df in routes.items():
            if df is None or df.empty:
                continue
            if 'Dist_To_km' not in df.columns and 'Dist_To_mi' in df.columns:
                df['Dist_To_km'] = round(df['Dist_To_mi'] * 1.60934, 2)
            if 'Cum_Dist_km' not in df.columns and 'Cum_Dist_mi' in df.columns:
                df['Cum_Dist_km'] = round(df['Cum_Dist_mi'] * 1.60934, 2)

        excel_name = f'{prefix}_schedule.xlsx'
        save_excel_schedule(
            routes, deferred, filename=excel_name, output_dir=OUTPUT_DIR,
            plan_dates=plan_dates, today=today, snapshot=clients_df,
        )
        save_route_map(routes, filename=f'{prefix}_map.html', output_dir=OUTPUT_DIR)

        # SmartService-importable CSV (round-trips back into SK's existing dispatch)
        ss_csv = OUTPUT_DIR / f'{prefix}_smartservice.csv'
        n_rows = export_smartservice_csv(
            routes=routes,
            output_path=ss_csv,
            plan_dates=plan_dates,
            clients_df=clients_raw,
            shift_start_min=int(depot_config.get('shift_start_min', 6 * 60)),
        )
        print(f'\n  Excel → {OUTPUT_DIR / excel_name}')
        print(f'  Map   → {OUTPUT_DIR / (prefix + "_map.html")}')
        print(f'  SS CSV→ {ss_csv}  ({n_rows} rows, importable into SmartService)')

    # ── Persist plan + state (CLOSE THE ROLLING HORIZON LOOP) ───────────
    print(f'\n[8/8] Persisting plan and state...')
    if args.dry_run:
        print('  (dry-run: skipping plan/state save)')
    else:
        commit_run(
            state=state,
            state_file=STATE_FILE,
            routes=routes,
            deferred=deferred,
            plan_dates=plan_dates,
            today=today,
            horizon_days=actual_horizon,
            commit_days=commit_days,
            plan_file=PLAN_FILE,
            delivery_log=delivery_log,
            auto_apply_committed=args.auto_apply_day0,
            clients_df=clients_df,
            metadata={
                'run_id': run_id,
                'cost_model': cost.to_dict(),
                'solve_seconds': solve_sec,
                'mandatory_clients': n_mandatory,
                'plan_stability_vs_prior': overlap,
            },
            run_id=run_id,
        )
        print(f'  Plan  → {PLAN_FILE}')
        print(f'  State → {STATE_FILE}')

    # ── Summary ──────────────────────────────────────────────────────────
    n_committed = sum(len(df) for df in committed.values() if df is not None and not df.empty)
    n_tentative = sum(len(df) for df in tentative.values() if df is not None and not df.empty)
    print(f'\n  ─────────────────────────────')
    print(f'  Committed stops:  {n_committed:>4d}')
    print(f'  Tentative stops:  {n_tentative:>4d}')
    print(f'  Deferred:         {len(deferred):>4d}')
    print(f'  Plan stability:   {overlap:>4.0%}')
    print()
    print('✓ Done.')
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def datetime_str() -> str:
    from datetime import datetime
    return datetime.utcnow().strftime('%Y%m%dT%H%M%S')


def _env_set(name: str) -> set:
    raw = os.environ.get(name, '')
    return {s.strip() for s in raw.split(',') if s.strip()} if raw else set()


def _routes_to_plan_dict(routes, today, plan_dates, horizon_days, commit_days) -> dict:
    """Build the plan_overlap-compatible dict directly from routes."""
    visits = []
    for d, df in routes.items():
        if df is None or df.empty:
            continue
        for _, r in df.iterrows():
            visits.append({
                'day': int(d),
                'client_id': str(r.get('ID', '')),
                'truck': str(r.get('Truck', '')),
                'stop': int(r.get('Stop', 0) or 0),
            })
    return {
        'today': str(today.date()),
        'horizon_days': horizon_days,
        'commit_days': commit_days,
        'visits': visits,
    }


if __name__ == '__main__':
    raise SystemExit(main())
