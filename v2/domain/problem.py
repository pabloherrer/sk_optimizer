"""
v2.domain.problem — ProblemInstance (solver's input).

A frozen snapshot of everything the solver needs:
  • Static client data
  • Initial tank levels + consumption forecasts
  • Fleet + working calendar
  • Operator overrides
  • Cost parameters
  • OSRM distance/time matrix

The solver is a pure function: ProblemInstance -> Plan.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import date
from typing import Dict, Tuple

import numpy as np

from v2.domain.client import Client, TankState
from v2.domain.fleet import Truck, Depot
from v2.domain.overrides import Overrides


@dataclass(frozen=True)
class ProblemInstance:
    """Everything the solver needs to produce a Plan."""

    # ── Identity ─────────────────────────────────────────────────────────
    run_id: str
    today: date
    horizon_dates: Tuple[date, ...]         # Working days in horizon
    commit_days: int

    # ── Static data ──────────────────────────────────────────────────────
    clients: Tuple[Client, ...]
    trucks: Tuple[Truck, ...]
    depot: Depot
    products: Tuple[str, ...]

    # ── Dynamic state ────────────────────────────────────────────────────
    initial_tanks: Dict[str, TankState]     # client_id → current state

    # ── Fleet schedule ───────────────────────────────────────────────────
    # For each (date, truck_id), is this combination allowed?
    # e.g., {(2026-05-23, 'Truck9'): False, ...}  for Saturday Truck9
    truck_available: Dict[Tuple[date, str], bool]

    # ── Overrides ────────────────────────────────────────────────────────
    overrides: Overrides

    # ── Geometry ─────────────────────────────────────────────────────────
    # NxN integer-meter distance matrix; node 0 = depot, nodes 1..N-1 = clients
    distance_matrix_m: np.ndarray
    # NxN integer-minute travel time matrix (matches distance_matrix shape)
    time_matrix_min: np.ndarray
    # client_id → matrix row index
    node_index: Dict[str, int]

    # ── Costs (in $ — solver scales to integer cost units internally) ────
    cost_per_mile: float
    cost_per_minute_labor: float
    overtime_multiplier: float
    truck_dispatch_cost: float
    stockout_cost_per_lb_day: float
    terminal_value_per_lb: float

    # ── Shift parameters ─────────────────────────────────────────────────
    shift_start_min: int                    # Minutes since midnight (6 AM = 360)
    shift_target_min: int                   # OT past this
    shift_hard_max_min: int                 # Never exceed
    weekly_max_min: int

    # ── Policy ───────────────────────────────────────────────────────────
    min_stop_lbs: int
    min_reserve_fraction: float
    target_empty_fraction: float
    team_overlap_penalty_dollars: float
    num_territory_clusters: int

    # ── Solver knobs ─────────────────────────────────────────────────────
    solve_seconds: int = 180
