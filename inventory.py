"""
inventory.py
============
Core inventory mathematics for the rolling-horizon IRP.

All functions are pure (no side-effects, no I/O) so they can be unit-tested
independently and reused in both Phase-1 scoring and post-solve reporting.

Key insight
-----------
Because S&K always fills to 100 %, the inventory trajectory between deliveries
is a straight line:

    Level(i, t) = Level(i, 0) - t × rate(i)     (clamped to [floor, tank])

where  t      = days from "now" (float OK — e.g. 0.5 = half a day)
       rate(i) = Avg_LbsPerDay (estimated from delivery log)
       floor   = Tank_lbs × MIN_OIL_PCT  (operational reserve)

This lets us answer:
  - What is the tank level when we visit on day d?
  - How many lbs would we deliver?
  - How long would the pump run on each truck?
  - How many days until the tank hits the minimum reserve?
"""

import math
import numpy as np
import pandas as pd
from typing import Dict, Optional
from config import (
    MIN_OIL_PCT, MIN_FILL_PCT, CRITICAL_DAYS, URGENT_DAYS,
    TRUCKS, TRUCK_NAMES, DAYS,
)


# ── Core scalar functions (fast, used inside tight loops) ─────────────────────

def project_level(
    current_lbs:  float,
    rate_lbs_day: float,
    days_forward: float,
    tank_lbs:     float,
    min_pct:      float = MIN_OIL_PCT,
) -> float:
    """Tank level `days_forward` days from now, clamped to [floor, tank].
    Uses constant cross-DOW mean rate. For DOW-aware projection use
    `project_level_dow()` instead."""
    floor = tank_lbs * min_pct
    return float(np.clip(current_lbs - days_forward * rate_lbs_day, floor, tank_lbs))


def project_level_dow(
    current_lbs:  float,
    rate_by_dow:  list,
    today,
    days_forward: int,
    tank_lbs:     float,
    min_pct:      float = MIN_OIL_PCT,
) -> float:
    """
    DOW-aware projection: sum the per-DOW rate from today+1 through
    today+days_forward, accounting for varying daily demand.

    Parameters
    ----------
    rate_by_dow : list of 7 floats, indexed by Timestamp.weekday()
                  (0=Mon, 1=Tue, ..., 6=Sun)
    today       : pd.Timestamp — anchor date (consumption starts the
                  next day)
    days_forward: int — how many calendar days into the future

    Returns clamped projected level.
    """
    import pandas as pd
    if not rate_by_dow or len(rate_by_dow) != 7:
        # Fallback: treat as constant if DOW rates unavailable
        avg = (sum(rate_by_dow) / 7.0) if rate_by_dow else 0.0
        return project_level(current_lbs, avg, days_forward, tank_lbs, min_pct)
    floor = tank_lbs * min_pct
    today = pd.Timestamp(today).normalize()
    cum = 0.0
    for k in range(1, int(days_forward) + 1):
        d = today + pd.Timedelta(days=k)
        cum += float(rate_by_dow[d.weekday()])
    return float(np.clip(current_lbs - cum, floor, tank_lbs))


def compute_refill(
    current_lbs:  float,
    rate_lbs_day: float,
    day_index:    int,
    tank_lbs:     float,
) -> float:
    """
    Lbs delivered if we visit on `day_index` (0 = Mon, …, 4 = Fri).
    Always fills to Tank_lbs.  Returns 0 if tank is already full.
    """
    level = project_level(current_lbs, rate_lbs_day, day_index, tank_lbs)
    return max(tank_lbs - level, 0.0)


def fill_efficiency(
    current_lbs:  float,
    rate_lbs_day: float,
    day_index:    int,
    tank_lbs:     float,
) -> float:
    """Fraction of the tank that would be filled: refill / tank_lbs ∈ [0, 1]."""
    if tank_lbs <= 0:
        return 0.0
    return compute_refill(current_lbs, rate_lbs_day, day_index, tank_lbs) / tank_lbs


def days_until_stockout(
    current_lbs:  float,
    rate_lbs_day: float,
    tank_lbs:     float,
    min_pct:      float = MIN_OIL_PCT,
) -> float:
    """Days until tank reaches MIN_OIL_PCT floor.  Returns 0 if already at/below floor."""
    floor = tank_lbs * min_pct
    if current_lbs <= floor or rate_lbs_day <= 0:
        return 0.0
    return (current_lbs - floor) / rate_lbs_day


def service_time_min(
    refill_lbs: float,
    truck_name: str,
) -> float:
    """
    Total service time in minutes for a single stop.
    = fixed_setup + pump_time
    = fixed_setup_min + refill_lbs / pump_rate_lbs_per_min
    """
    cfg = TRUCKS[truck_name]
    return cfg['fixed_setup_min'] + refill_lbs / cfg['pump_rate_lbs_per_min']


def urgency_tier(days_to_stockout_at_visit: float) -> str:
    """Classify urgency based on days remaining WHEN THE TRUCK ARRIVES."""
    if days_to_stockout_at_visit <= 0:
        return 'stockout'
    if days_to_stockout_at_visit <= CRITICAL_DAYS:
        return 'critical'
    if days_to_stockout_at_visit <= URGENT_DAYS:
        return 'urgent'
    return 'normal'


# ── DataFrame-level enrichment ─────────────────────────────────────────────────

def enrich_snapshot(
    clients_df:      pd.DataFrame,
    inventory_state: Optional[Dict[str, float]] = None,
) -> pd.DataFrame:
    """
    Add current-state inventory metrics to clients_df.

    If `inventory_state` is supplied (from rolling-horizon state file), those
    values override Est_Current_lbs computed during load.

    Added columns
    -------------
    Current_lbs         : best estimate of current tank level
    Days_Until_Stockout : days until MIN_OIL_PCT floor at current consumption
    Urgency             : 'stockout' | 'critical' | 'urgent' | 'normal'
    Refill_Today_lbs    : how much we'd deliver if visiting today (day_index=0)
    Fill_Pct_Today      : Refill_Today / Tank_lbs
    """
    df = clients_df.copy()

    # Override with live state if available
    if inventory_state:
        df['Current_lbs'] = df['ID'].map(inventory_state)
        # Fall back to estimated level for any client not yet in state
        df['Current_lbs'] = df['Current_lbs'].fillna(
            df.get('Est_Current_lbs', df['Tank_lbs'] * 0.5)
        )
    else:
        df['Current_lbs'] = df.get('Est_Current_lbs', df['Tank_lbs'] * 0.5)

    df['Current_lbs'] = df['Current_lbs'].clip(
        lower=df['Tank_lbs'] * MIN_OIL_PCT,
        upper=df['Tank_lbs'],
    )

    df['Days_Until_Stockout'] = df.apply(
        lambda r: days_until_stockout(r['Current_lbs'], r['Avg_LbsPerDay'], r['Tank_lbs']),
        axis=1,
    )
    df['Urgency'] = df['Days_Until_Stockout'].apply(urgency_tier)

    df['Refill_Today_lbs'] = df.apply(
        lambda r: compute_refill(r['Current_lbs'], r['Avg_LbsPerDay'], 0, r['Tank_lbs']),
        axis=1,
    )
    df['Fill_Pct_Today'] = df.apply(
        lambda r: fill_efficiency(r['Current_lbs'], r['Avg_LbsPerDay'], 0, r['Tank_lbs']),
        axis=1,
    )

    return df


def build_refill_matrix(
    clients_df: pd.DataFrame,
    n_days:     int = 5,
) -> np.ndarray:
    """
    Build an (n_clients × n_days) matrix of projected refill amounts.

    refill_matrix[i, d] = lbs delivered to client i if visited on day d.

    Useful for the Phase-1 scheduler to quickly evaluate all (client, day)
    combinations without re-running scalar functions in Python loops.
    """
    n = len(clients_df)
    matrix = np.zeros((n, n_days), dtype=float)
    for d in range(n_days):
        matrix[:, d] = clients_df.apply(
            lambda r: compute_refill(r['Current_lbs'], r['Avg_LbsPerDay'], d, r['Tank_lbs']),
            axis=1,
        ).values
    return matrix


def build_fill_pct_matrix(
    clients_df: pd.DataFrame,
    n_days:     int = 5,
) -> np.ndarray:
    """(n_clients × n_days) fill-efficiency matrix."""
    n = len(clients_df)
    matrix = np.zeros((n, n_days), dtype=float)
    for d in range(n_days):
        matrix[:, d] = clients_df.apply(
            lambda r: fill_efficiency(r['Current_lbs'], r['Avg_LbsPerDay'], d, r['Tank_lbs']),
            axis=1,
        ).values
    return matrix
