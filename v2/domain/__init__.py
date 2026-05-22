"""
v2.domain — pure data model. NO I/O, NO side-effects.

These dataclasses are the lingua franca between modules:
  • ingest/  produces ProblemInstance, ResolvedState
  • solver/  consumes ProblemInstance, produces Plan
  • reporting/ consumes Plan

Everything is frozen (immutable). State is rebuilt, not mutated.
"""
from v2.domain.client import Client, TankState
from v2.domain.fleet import Truck, Depot, Compartment
from v2.domain.problem import ProblemInstance
from v2.domain.plan import Plan, Route, Stop
from v2.domain.overrides import (
    Pin, Forbid, Lock, ManualReading, ManualConsumption, Overrides,
)

__all__ = [
    'Client', 'TankState',
    'Truck', 'Depot', 'Compartment',
    'ProblemInstance',
    'Plan', 'Route', 'Stop',
    'Pin', 'Forbid', 'Lock', 'ManualReading', 'ManualConsumption', 'Overrides',
]
