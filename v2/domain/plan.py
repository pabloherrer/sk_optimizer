"""
v2.domain.plan — Plan, Route, Stop (the solver's output).

Immutable. Reporting / invariants / state-store consume these.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, Tuple


@dataclass(frozen=True)
class Stop:
    """One delivery in a route — what driver actually does at one client."""
    sequence: int                           # 1, 2, 3, ... within the route
    client_id: str
    customer: str
    address: str
    lat: float
    lon: float
    product: str

    # Inventory math
    tank_capacity_lbs: int
    current_lbs_today: float                # Tank level RIGHT NOW (from Anova or estimate)
    days_to_arrival: int                    # How many days from today until truck arrives
    level_at_arrival_lbs: float             # Projected level when truck arrives
    delivery_lbs: float                     # What truck pumps
    level_after_lbs: float                  # = level_at_arrival + delivery

    # Time math (minutes from depot shift start)
    arrival_min: int
    setup_min: int
    pump_min: int
    depart_min: int                         # = arrival + setup + pump

    # Distance to this stop (from previous, in miles)
    travel_miles: float
    cumulative_miles: float

    # Diagnostic / business flags
    days_until_stockout_at_arrival: float   # If we hadn't visited
    urgency_tier: str = 'normal'            # stockout/critical/urgent/normal
    do_not_schedule: bool = False           # Should NEVER be True in output — invariant
    notes: str = ''
    pinned: bool = False                    # Came from operator Pin?


@dataclass(frozen=True)
class Route:
    """One truck-day's worth of stops + summary metrics."""
    date: date
    truck_id: str
    territory_label: str                    # e.g., 'NE', 'SW' — for visualization
    stops: Tuple[Stop, ...]

    # Compartment loading at depot
    compartment_a_product: str
    compartment_a_lbs: float
    compartment_b_product: str
    compartment_b_lbs: float

    # Time summary
    depart_depot_min: int                   # When truck leaves depot
    return_depot_min: int                   # When truck returns
    total_minutes: int                      # = return - depart
    overtime_minutes: int                   # = max(0, total - target_minutes)

    # Distance summary
    total_miles: float

    # Cost breakdown ($)
    cost_miles_dollars: float
    cost_labor_dollars: float
    cost_overtime_dollars: float
    cost_dispatch_dollars: float
    cost_total_dollars: float

    # Capacity utilization
    total_load_lbs: float
    cap_pct: float                          # total_load / truck_capacity


@dataclass(frozen=True)
class Plan:
    """The complete schedule output — what the solver returns."""
    run_id: str
    generated_at: datetime
    today: date                             # Plan start date
    horizon_dates: Tuple[date, ...]
    commit_days: int                        # First N days are firm

    # The routes — keyed by (date, truck_id). Missing key = truck idle that day.
    routes: Dict[Tuple[date, str], Route]

    # Clients deferred (not scheduled in this horizon, with reason)
    deferred: Dict[str, str]                # client_id → reason

    # Solver diagnostics
    solve_seconds: float
    objective_cost_dollars: float
    solver_status: str                      # 'OPTIMAL' / 'FEASIBLE' / 'INFEASIBLE'

    # Aggregate KPIs (precomputed for reporting)
    total_stops: int
    total_lbs_delivered: float
    total_miles: float
    total_minutes: int
    avg_fill_pct: float
    pct_stops_under_target_fill: float

    # Capacity outlook flags
    capacity_warnings: Tuple[str, ...] = ()  # e.g., "Tue May 26: 94% cap, consider extra truck"

    # Schedule reference times (so reporting can format ETAs correctly)
    shift_start_min: int = 360                  # 6 AM default; overridden from Depot sheet
    shift_target_min: int = 480                 # 8 h default; overridden from Depot sheet
