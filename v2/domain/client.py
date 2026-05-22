"""
v2.domain.client — Client and TankState.

A Client is the static description of a restaurant. TankState is the
time-evolving inventory at that restaurant.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Tuple


@dataclass(frozen=True)
class Client:
    """Static client record. Doesn't change during planning."""
    id: str
    customer: str
    lat: float
    lon: float
    tank_capacity_lbs: int
    product: str                          # CANOLA or FRYERS CHOICE
    do_not_schedule: bool = False
    excluded: bool = False                # Tucson/Flagstaff far-run clients
    # Optional metadata
    address: str = ''
    phone: str = ''
    notes: str = ''
    service_min_override: Optional[int] = None   # Per-client setup time override
    # Time window for delivery, in minutes from depot shift start (None = open all day)
    time_window_min: Optional[Tuple[int, int]] = None
    # Closure dates (operator-specified days the client is closed)
    closed_dates: Tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class TankState:
    """Current inventory at a client. Evolves through the horizon."""
    client_id: str
    current_lbs: float
    as_of: datetime
    source: str                           # 'sensor', 'sensor-projected', 'estimated'
    # Per-client consumption forecast (lbs/day at the chosen percentile)
    rate_lbs_per_day: float
    # Standard deviation of historical daily consumption — used for safety stock
    rate_std_dev: float = 0.0
    last_delivery_date: Optional[str] = None
    last_delivery_lbs: Optional[float] = None
