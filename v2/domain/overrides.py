"""
v2.domain.overrides — first-class operator overrides.

Five override types, exhaustively cover the operator's "I need to nudge the
solver" use cases. Each is immutable, audited, and validated.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional, Tuple


@dataclass(frozen=True)
class Pin:
    """Must-visit: client_id MUST be visited on date (optional time window)."""
    client_id: str
    date: date
    reason: str
    operator: str = ''
    created_at: Optional[datetime] = None
    time_window_min: Optional[Tuple[int, int]] = None  # minutes from shift start


@dataclass(frozen=True)
class Forbid:
    """Must-not-visit: client_id is forbidden on one or more dates."""
    client_id: str
    dates: Tuple[date, ...]
    reason: str
    operator: str = ''
    created_at: Optional[datetime] = None


@dataclass(frozen=True)
class Lock:
    """Today's plan is final past stop N for truck T — re-solves preserve it."""
    date: date
    truck_id: str
    locked_through_stop: int    # 0 = entire day locked
    reason: str
    operator: str = ''
    created_at: Optional[datetime] = None


@dataclass(frozen=True)
class ManualReading:
    """Operator-set tank reading, overrides sensor or estimate."""
    client_id: str
    current_lbs: float
    as_of: datetime
    reason: str
    operator: str = ''


@dataclass(frozen=True)
class ManualConsumption:
    """Operator-set consumption rate, overrides forecast for a date range."""
    client_id: str
    rate_lbs_per_day: float
    effective_from: date
    effective_to: Optional[date]
    reason: str
    operator: str = ''


@dataclass(frozen=True)
class Overrides:
    """The full override bundle — passed atomically through the pipeline."""
    pins: Tuple[Pin, ...] = ()
    forbids: Tuple[Forbid, ...] = ()
    locks: Tuple[Lock, ...] = ()
    readings: Tuple[ManualReading, ...] = ()
    consumptions: Tuple[ManualConsumption, ...] = ()

    def is_empty(self) -> bool:
        return not (self.pins or self.forbids or self.locks
                    or self.readings or self.consumptions)
