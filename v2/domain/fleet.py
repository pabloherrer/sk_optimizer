"""
v2.domain.fleet — Truck, Depot, Compartment (immutable structural objects).
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Tuple


@dataclass(frozen=True)
class Depot:
    id: str
    lat: float
    lon: float


@dataclass(frozen=True)
class Compartment:
    id: str                   # 'A' or 'B'
    capacity_lbs: int         # 5000 typically


@dataclass(frozen=True)
class Truck:
    id: str                   # 'Truck2', 'Truck9'
    capacity_lbs: int         # 10000 total
    compartments: Tuple[Compartment, ...]
    pump_rate_lbs_per_min: float
    fixed_setup_min: int      # Per-stop setup time
