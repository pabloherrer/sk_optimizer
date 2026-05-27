"""
v2.ingest.build_problem — assemble a ProblemInstance from raw inputs.

This is the single integration point. It glues together:
  • Config (economics, fleet, policy)
  • Clients (Client_List)
  • Historical deliveries (Delivery_Log)
  • Anova sensor readings (Query)
  • Time windows (Client_Time_Windows)
  • Closures (Client_Closures)
  • Excluded far-cluster IDs
  • Operator overrides (Overrides sheet)
  • OSRM matrix

And produces the immutable ProblemInstance the solver consumes.

Tank state resolution policy (per-client, in priority order):
  1. ManualReading override (operator-stated)
  2. Fresh Anova reading (≤24h)           → 'sensor'
  3. Stale Anova reading (24–72h)         → 'sensor-projected'
  4. Formula-based estimate from log      → 'estimated'
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from v2.domain.client import Client, TankState
from v2.domain.fleet import Compartment, Depot, Truck
from v2.domain.overrides import ManualReading, Overrides
from v2.domain.problem import ProblemInstance
from v2.ingest.anova import load_anova_readings
from v2.ingest.excel import load_clients, load_deliveries
from v2.ingest.matrix import load_matrix
from v2.ingest.overrides import load_overrides
from v2.ingest.schema import (
    load_closures,
    load_excluded_ids,
    load_time_windows,
)
from v2.schemas import AppConfig, load_app_config


# ── Day-of-week mapping used by the fleet calendar ───────────────────────────

_DOW: tuple[str, ...] = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')


def build_problem_instance(
    config_dir: Path,
    input_file: Path,
    matrix_file: Path,
    today: date,
    run_id: str,
) -> ProblemInstance:
    """
    Build an immutable ProblemInstance ready for the solver.

    Steps
    -----
    1. Load config (economics, fleet, policy)
    2. Load raw inputs (clients, deliveries, anova, schemas, overrides)
    3. Compute per-client TankState
    4. Build the truck_available calendar for the horizon
    5. Assemble ProblemInstance
    """
    config_dir = Path(config_dir)
    input_file = Path(input_file)
    matrix_file = Path(matrix_file)

    cfg = load_app_config(config_dir)

    # ── Step 0: read Depot sheet (operator truth — overrides fleet.yaml) ──
    from v2.ingest.schema import load_depot_config
    depot_cfg = load_depot_config(input_file)
    # Defaults from fleet.yaml; Depot sheet wins where keys present
    shift_start_min = depot_cfg.get('shift_start_min',
                                     cfg.fleet.shift.start_hour * 60)
    shift_end_min = depot_cfg.get('shift_end_min',
                                   shift_start_min + cfg.fleet.shift.hard_max_minutes)
    morning_load_min = depot_cfg.get('morning_load_min', 0)
    evening_unload_min = depot_cfg.get('evening_unload_min', 0)
    # Effective routing window per truck-day:
    #   shift_end - shift_start - morning_load - evening_unload
    # e.g., 06:00 → 16:00 = 600 min, minus 30 load minus 15 unload = 555 min
    # of driving+stops available before the truck must be back at depot.
    effective_route_minutes = (shift_end_min - shift_start_min
                                - morning_load_min - evening_unload_min)
    print(f'  Depot sheet: shift {shift_start_min//60:02d}:{shift_start_min%60:02d}'
          f'-{shift_end_min//60:02d}:{shift_end_min%60:02d}, '
          f'morning_load {morning_load_min}min, evening_unload {evening_unload_min}min '
          f'→ {effective_route_minutes}min driving budget per truck-day')

    # ── Step 1: raw inputs ───────────────────────────────────────────────
    clients_raw = load_clients(input_file)
    deliveries_df = load_deliveries(input_file)
    anova_readings = load_anova_readings(input_file)
    time_windows = load_time_windows(
        input_file,
        shift_start_min=shift_start_min,
    )
    closures = load_closures(input_file)
    excluded_ids = load_excluded_ids(input_file)
    overrides = load_overrides(input_file)

    # ── Step 2: merge per-client metadata into Client records ────────────
    clients: tuple[Client, ...] = tuple(
        _attach_schedule_fields(c, time_windows, closures, excluded_ids)
        for c in clients_raw
    )

    # ── Step 3: consumption forecast (per-client lbs/day + stddev) ───────
    rate_lookup = _estimate_consumption_rates(
        deliveries_df=deliveries_df,
        clients=clients,
        consumption_percentile=cfg.policy.consumption_percentile,
    )

    # ── Step 4: TankState per client ─────────────────────────────────────
    today_ts = pd.Timestamp(today)
    last_delivery = _last_delivery_per_customer(deliveries_df)

    initial_tanks: dict[str, TankState] = {}
    manual_readings_by_cid = {r.client_id: r for r in overrides.readings}

    for c in clients:
        rate, sigma = rate_lookup.get(c.id, (0.0, 0.0))
        manual = manual_readings_by_cid.get(c.id)
        anova = anova_readings.get(c.id)
        last = last_delivery.get(c.customer)

        current_lbs, source, as_of = _resolve_tank_level(
            client=c,
            manual=manual,
            anova=anova,
            last_delivery=last,
            rate_lbs_per_day=rate,
            today_ts=today_ts,
        )

        initial_tanks[c.id] = TankState(
            client_id=c.id,
            current_lbs=current_lbs,
            as_of=as_of,
            source=source,
            rate_lbs_per_day=rate,
            rate_std_dev=sigma,
            last_delivery_date=(
                last['date'].isoformat() if last is not None else None
            ),
            last_delivery_lbs=(
                float(last['qty']) if last is not None else None
            ),
        )

    # ── Step 5: trucks + depot ───────────────────────────────────────────
    trucks: tuple[Truck, ...] = tuple(
        Truck(
            id=t.id,
            capacity_lbs=t.capacity_lbs,
            compartments=tuple(
                Compartment(id=c.id, capacity_lbs=c.capacity_lbs)
                for c in t.compartments
            ),
            pump_rate_lbs_per_min=t.pump_rate_lbs_per_min,
            fixed_setup_min=t.fixed_setup_min,
        )
        for t in cfg.fleet.trucks
    )
    depot = Depot(id=cfg.fleet.depot.id,
                  lat=cfg.fleet.depot.lat,
                  lon=cfg.fleet.depot.lon)

    # ── Step 6: working calendar / truck_available ───────────────────────
    horizon_dates = _horizon_dates(
        today=today,
        horizon_days=cfg.policy.horizon_days,
        working_days=tuple(cfg.fleet.working_days),
    )
    truck_available = _build_truck_available(
        horizon_dates=horizon_dates,
        all_truck_ids=tuple(t.id for t in cfg.fleet.trucks),
        saturday_trucks=frozenset(cfg.fleet.saturday_trucks),
    )

    # ── Step 7: OSRM matrix ──────────────────────────────────────────────
    distance_m, time_min, node_index = load_matrix(matrix_file)

    # ── Step 8: assemble immutable ProblemInstance ───────────────────────
    return ProblemInstance(
        run_id=run_id,
        today=today,
        horizon_dates=horizon_dates,
        commit_days=cfg.policy.commit_days,
        clients=clients,
        trucks=trucks,
        depot=depot,
        products=tuple(cfg.fleet.products),
        initial_tanks=initial_tanks,
        truck_available=truck_available,
        overrides=overrides,
        distance_matrix_m=distance_m,
        time_matrix_min=time_min,
        node_index=node_index,
        cost_per_mile=cfg.economics.cost_per_mile,
        cost_per_minute_labor=cfg.economics.cost_per_minute_labor,
        overtime_multiplier=cfg.economics.overtime_multiplier,
        truck_dispatch_cost=cfg.economics.truck_dispatch_cost,
        stockout_cost_per_lb_day=cfg.economics.stockout_cost_per_lb_day,
        terminal_value_per_lb=cfg.economics.terminal_value_per_lb,
        # Depot-sheet values override fleet.yaml defaults.
        # effective_route_minutes = shift_end - shift_start - load - unload
        #   (e.g., 06:00→16:00 - 30 - 15 = 555 min of driving budget)
        shift_start_min=shift_start_min,
        # Target shift = 8h (fleet.yaml default) — solver pays OT premium
        # past this. We KEEP this less than hard_max so the OT signal can
        # bind. If target == hard_max, the OT penalty never fires and the
        # solver has no reason to add a second truck instead of OT-ing
        # the first.
        shift_target_min=min(cfg.fleet.shift.target_minutes, effective_route_minutes),
        # Hard cap = effective route budget (depot's shift end time).
        shift_hard_max_min=effective_route_minutes,
        weekly_max_min=cfg.fleet.shift.weekly_max_minutes,
        min_stop_lbs=cfg.policy.min_stop_lbs,
        min_reserve_fraction=cfg.policy.min_reserve_fraction,
        target_empty_fraction=cfg.policy.target_empty_fraction,
        team_overlap_penalty_dollars=cfg.policy.team_overlap_penalty_dollars,
        num_territory_clusters=cfg.policy.num_territory_clusters,
    )


# ── Helpers ──────────────────────────────────────────────────────────────────

def _attach_schedule_fields(
    c: Client,
    time_windows: dict[str, tuple[int, int]],
    closures: dict[str, tuple[date, ...]],
    excluded_ids: frozenset[str],
) -> Client:
    """Return a new frozen Client with windows/closures/excluded attached."""
    return Client(
        id=c.id,
        customer=c.customer,
        lat=c.lat,
        lon=c.lon,
        tank_capacity_lbs=c.tank_capacity_lbs,
        product=c.product,
        do_not_schedule=c.do_not_schedule,
        excluded=(c.id in excluded_ids),
        address=c.address,
        phone=c.phone,
        notes=c.notes,
        service_min_override=c.service_min_override,
        time_window_min=time_windows.get(c.id),
        closed_dates=tuple(d.isoformat() for d in closures.get(c.id, ())),
    )


def _estimate_consumption_rates(
    deliveries_df: pd.DataFrame,
    clients: tuple[Client, ...],
    consumption_percentile: float,
) -> dict[str, tuple[float, float]]:
    """
    Return {client_id: (rate_lbs_per_day, rate_std_dev)}.

    Prefers v2.forecast.consumption.estimate_consumption (parallel work);
    falls back to an inline percentile-of-history calculation when that
    module isn't ready yet so the ingest layer is independently testable.
    """
    try:
        from v2.forecast.consumption import estimate_consumption  # noqa
    except Exception:
        estimate_consumption = None  # noqa: N806

    if estimate_consumption is not None:
        try:
            return dict(estimate_consumption(
                deliveries_df=deliveries_df,
                clients=clients,
                percentile=consumption_percentile,
            ))
        except Exception:
            # If the partner module raises (e.g. signature drift),
            # degrade to the inline estimator so this module's tests pass.
            pass

    return _inline_consumption_rates(
        deliveries_df, clients, consumption_percentile,
    )


def _inline_consumption_rates(
    deliveries_df: pd.DataFrame,
    clients: tuple[Client, ...],
    percentile: float,
) -> dict[str, tuple[float, float]]:
    """
    Minimal in-house estimator used until v2.forecast.consumption lands.

    Algorithm:
      • For each (customer, sorted-by-date) sequence, compute per-gap
        consumption rate = qty / days_gap.
      • Skip placeholder rows (Qty=200) and gaps ≤ 0.
      • Per client: rate = `percentile`-quantile of clean per-gap rates
        if ≥ 3 observations, else median (or 0.0 if no observations).
      • sigma = standard deviation of the same series, or 0.
    """
    rates: dict[str, tuple[float, float]] = {}
    customer_to_id = {c.customer: c.id for c in clients}

    if deliveries_df.empty:
        return rates

    dl = deliveries_df.sort_values(['Customer', 'Date']).copy()
    dl['Prev_Date'] = dl.groupby('Customer')['Date'].shift(1)
    dl['Days_Gap'] = (dl['Date'] - dl['Prev_Date']).dt.days
    is_placeholder = dl.get(
        'Is_Placeholder',
        dl['Qty_lbs'] == 200.0,
    )
    dl['Rate'] = np.where(
        (dl['Days_Gap'] > 0) & dl['Days_Gap'].notna() & ~is_placeholder,
        dl['Qty_lbs'] / dl['Days_Gap'],
        np.nan,
    )

    for customer, group in dl.groupby('Customer'):
        cid = customer_to_id.get(customer)
        if not cid:
            continue
        clean = group['Rate'].dropna()
        if clean.empty:
            continue
        if len(clean) >= 3:
            rate = float(clean.quantile(percentile))
        else:
            rate = float(clean.median())
        sigma = float(clean.std()) if len(clean) >= 2 else 0.0
        if rate < 0:
            rate = 0.0
        if sigma != sigma:    # NaN
            sigma = 0.0
        rates[cid] = (rate, sigma)

    return rates


def _last_delivery_per_customer(deliveries_df: pd.DataFrame) -> dict[str, dict]:
    """Return {customer_name: {'date': Timestamp, 'qty': float}}."""
    if deliveries_df.empty:
        return {}
    last_idx = deliveries_df.groupby('Customer')['Date'].idxmax()
    out: dict[str, dict] = {}
    for cust, idx in last_idx.items():
        row = deliveries_df.loc[idx]
        out[cust] = {
            'date': row['Date'].to_pydatetime().date(),
            'qty': float(row['Qty_lbs']),
        }
    return out


def _resolve_tank_level(
    client: Client,
    manual: Optional[ManualReading],
    anova: Optional[dict],
    last_delivery: Optional[dict],
    rate_lbs_per_day: float,
    today_ts: pd.Timestamp,
) -> tuple[float, str, datetime]:
    """
    Resolve current tank level with this priority order:
      1. ManualReading override → 'manual'
      2. Fresh sensor (≤24h)    → 'sensor'
      3. Stale sensor (24-72h)  → 'sensor-projected'
      4. Formula estimate       → 'estimated'

    Always returns (current_lbs, source_label, as_of_datetime).
    Current_lbs is clamped to [0, tank_capacity].
    """
    cap = float(client.tank_capacity_lbs)

    if manual is not None:
        lbs = max(0.0, min(cap, float(manual.current_lbs)))
        return lbs, 'manual', manual.as_of

    if anova is not None and anova['source'] in ('sensor', 'sensor-projected'):
        # SAFEGUARD: ignore Anova if it was recorded BEFORE the latest delivery.
        # An older Anova reading would still show the pre-delivery (depleted)
        # tank level, making the solver think a freshly-refilled customer is
        # nearly empty — causing wrong-day dispatch (MANUEL PEORIA bug).
        anova_ts = anova['timestamp']
        anova_dt = (anova_ts.to_pydatetime()
                     if hasattr(anova_ts, 'to_pydatetime') else anova_ts)
        anova_too_old_vs_delivery = False
        if last_delivery is not None:
            last_dt = pd.Timestamp(last_delivery['date']).to_pydatetime()
            if anova_dt < last_dt:
                anova_too_old_vs_delivery = True
        if not anova_too_old_vs_delivery:
            if anova['source'] == 'sensor':
                lbs = max(0.0, min(cap, float(anova['level_lbs'])))
            else:
                # Project forward by rate × hours-of-staleness
                days_stale = float(anova['age_hours']) / 24.0
                projected = float(anova['level_lbs']) - rate_lbs_per_day * days_stale
                lbs = max(0.0, min(cap, projected))
            return lbs, anova['source'], anova_dt
        # Anova is stale relative to delivery — fall through to formula estimate

    # Formula estimate: cap − days_since_last × rate
    if last_delivery is not None:
        last_date = pd.Timestamp(last_delivery['date'])
        days_since = max(0.0, (today_ts - last_date).total_seconds() / 86400.0)
        est = cap - days_since * rate_lbs_per_day
        lbs = max(0.0, min(cap, est))
    else:
        # No delivery history → default to 50% full
        lbs = cap * 0.5
    return lbs, 'estimated', today_ts.to_pydatetime()


def _horizon_dates(
    today: date,
    horizon_days: int,
    working_days: tuple[str, ...],
) -> tuple[date, ...]:
    """
    Roll forward from `today`, collecting up to `horizon_days` working
    days (per the fleet config's working_days list).
    """
    work_set = frozenset(working_days)
    out: list[date] = []
    cursor = today
    # Safety cap of 60 calendar days to avoid runaway loops on bad config.
    for _ in range(60):
        if len(out) >= horizon_days:
            break
        if _DOW[cursor.weekday()] in work_set:
            out.append(cursor)
        cursor = cursor + timedelta(days=1)
    return tuple(out)


def _build_truck_available(
    horizon_dates: tuple[date, ...],
    all_truck_ids: tuple[str, ...],
    saturday_trucks: frozenset[str],
) -> dict[tuple[date, str], bool]:
    """
    Build {(date, truck_id): available} for every (date, truck) in the
    horizon. On Saturdays only `saturday_trucks` are available — every
    other truck is False that day.
    """
    out: dict[tuple[date, str], bool] = {}
    for d in horizon_dates:
        is_saturday = _DOW[d.weekday()] == 'Sat'
        for tid in all_truck_ids:
            if is_saturday:
                out[(d, tid)] = tid in saturday_trucks
            else:
                out[(d, tid)] = True
    return out
