"""
v2.schemas — Pydantic schemas that validate config/*.yaml at load time.

Every parameter must:
  • Have a type-validated bound (Field(ge=, le=, ...))
  • Have a description (shown in error messages)
  • Be the ONLY place that parameter exists in the system

`model_config = ConfigDict(extra='forbid')` is critical — a typo in a YAML key
fails fast at startup, never silently ignored.
"""
from __future__ import annotations
from pathlib import Path
from typing import List, Literal
import yaml
from pydantic import BaseModel, Field, ConfigDict


# ─────────────────────────────────────────────────────────────────────────────
# economics.yaml
# ─────────────────────────────────────────────────────────────────────────────

class Economics(BaseModel):
    """All cost parameters, in real $ per natural unit."""
    model_config = ConfigDict(extra='forbid')

    cost_per_mile: float = Field(
        ge=0.0, le=10.0,
        description='Fuel + wear per truck-mile driven, in dollars.',
    )
    cost_per_minute_labor: float = Field(
        ge=0.0, le=5.0,
        description='Loaded labor cost per minute (regular hours), in dollars.',
    )
    overtime_multiplier: float = Field(
        ge=1.0, le=3.0,
        description='OT premium multiplier on minutes past target shift.',
    )
    truck_dispatch_cost: float = Field(
        ge=0.0, le=1000.0,
        description='Fixed cost of dispatching one truck on one day, in dollars.',
    )
    stockout_cost_per_lb_day: float = Field(
        ge=0.0, le=1000.0,
        description='Penalty per pound-day of tank deficit below safety reserve.',
    )
    terminal_value_per_lb: float = Field(
        ge=0.0, le=10.0,
        description='Value of each pound remaining in tanks at horizon end ($/lb).',
    )


# ─────────────────────────────────────────────────────────────────────────────
# fleet.yaml
# ─────────────────────────────────────────────────────────────────────────────

class Depot(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str
    lat: float = Field(ge=-90.0, le=90.0)
    lon: float = Field(ge=-180.0, le=180.0)


class Compartment(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str
    capacity_lbs: int = Field(gt=0, le=20000)


class Truck(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str
    capacity_lbs: int = Field(gt=0, le=20000)
    compartments: List[Compartment]
    pump_rate_lbs_per_min: float = Field(gt=0.0, le=1000.0)
    fixed_setup_min: int = Field(ge=0, le=120)


class Shift(BaseModel):
    model_config = ConfigDict(extra='forbid')
    start_hour: int = Field(ge=0, le=23)
    target_minutes: int = Field(gt=0, le=720)
    hard_max_minutes: int = Field(gt=0, le=1440)
    weekly_max_minutes: int = Field(gt=0, le=10000)


class Fleet(BaseModel):
    model_config = ConfigDict(extra='forbid')
    depot: Depot
    trucks: List[Truck]
    shift: Shift
    working_days: List[Literal['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']]
    saturday_trucks: List[str]   # Subset of truck IDs allowed on Saturdays
    products: List[str]


# ─────────────────────────────────────────────────────────────────────────────
# policy.yaml
# ─────────────────────────────────────────────────────────────────────────────

class Policy(BaseModel):
    """Business rules — how aggressively we want to plan."""
    model_config = ConfigDict(extra='forbid')

    horizon_days: int = Field(
        ge=2, le=30,
        description='Number of working days to plan ahead.',
    )
    commit_days: int = Field(
        ge=1, le=10,
        description='Number of days locked in for dispatch (rest is preview).',
    )
    min_stop_lbs: int = Field(
        ge=0, le=5000,
        description='Minimum economic delivery size — solver either delivers '
                    'at least this much or skips the client today.',
    )
    min_reserve_fraction: float = Field(
        ge=0.0, le=0.5,
        description='Soft floor — tanks below this fraction trigger stockout penalty.',
    )
    target_empty_fraction: float = Field(
        ge=0.3, le=1.0,
        description='Target "empty before visit" — drives fill quality.',
    )
    consumption_percentile: float = Field(
        ge=0.5, le=0.99,
        description='Use this percentile of historical consumption (75th = mild safety).',
    )
    team_overlap_penalty_dollars: float = Field(
        ge=0.0, le=10000.0,
        description='Penalty when both trucks visit same cluster same day.',
    )
    num_territory_clusters: int = Field(
        ge=1, le=10,
        description='K for the geographic k-means territory pre-pass.',
    )
    override_conflict_policy: Literal['abort', 'relax']


# ─────────────────────────────────────────────────────────────────────────────
# Aggregate AppConfig
# ─────────────────────────────────────────────────────────────────────────────

class AppConfig(BaseModel):
    """All config in one immutable object."""
    model_config = ConfigDict(extra='forbid', frozen=True)
    economics: Economics
    fleet: Fleet
    policy: Policy


def load_app_config(config_dir: Path) -> AppConfig:
    """
    Load and validate all three YAMLs from `config_dir`.

    Raises pydantic.ValidationError on any malformed value.
    """
    def _load(name: str) -> dict:
        path = Path(config_dir) / name
        with path.open('r') as f:
            return yaml.safe_load(f)

    return AppConfig(
        economics=Economics(**_load('economics.yaml')),
        fleet=Fleet(**_load('fleet.yaml')),
        policy=Policy(**_load('policy.yaml')),
    )
