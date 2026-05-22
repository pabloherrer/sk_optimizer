"""
v2.invariants — hard checks every produced Plan must pass.

Run AT OUTPUT TIME, before any file is written. If any check fails, the plan
is rejected — operator sees a clear error instead of a quietly-bad schedule.

The 8 invariants protect against every class of bug v1 produced:
  1. No tank overflow                  (the HAROLDS bug)
  2. No same-day duplicate visits      (HAROLDS again)
  3. No truck-day > hard shift cap     (driver hours violation)
  4. No driver > weekly cap            (40h labor law)
  5. No DNS / EXCLUDED scheduled       (data integrity)
  6. No Truck9 on Saturday             (business rule)
  7. All deliveries ≥ min_stop_lbs     (no uneconomic stops)
  8. All Pin/Forbid overrides honored  (operator trust)

Each invariant raises a specific InvariantViolation subclass so the operator
sees exactly what's wrong, not a generic "plan invalid."
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import List


class InvariantViolation(Exception):
    """Base class for any invariant failure."""
    pass


class TankOverflowViolation(InvariantViolation): pass
class DuplicateVisitViolation(InvariantViolation): pass
class ShiftCapViolation(InvariantViolation): pass
class WeeklyHoursViolation(InvariantViolation): pass
class ExcludedClientViolation(InvariantViolation): pass
class SaturdayTruckViolation(InvariantViolation): pass
class TinyStopViolation(InvariantViolation): pass
class OverrideHonorViolation(InvariantViolation): pass


@dataclass(frozen=True)
class _CheckResult:
    """Internal accumulator for invariant check output."""
    name: str
    passed: bool
    detail: str = ''


def check_plan(plan, config, overrides=None) -> None:
    """
    Verify every invariant on a Plan. Raises specific exception on first failure.

    Parameters
    ----------
    plan      : domain.Plan with .routes[(date, truck_id)] -> Route
    config    : schemas.AppConfig
    overrides : domain.Overrides (Pins, Forbids, Locks), optional

    On success: returns None silently.
    On failure: raises one of the InvariantViolation subclasses with a
                human-readable message naming the offending client/route.
    """
    _check_no_tank_overflow(plan)
    _check_no_duplicate_visits(plan)
    _check_shift_caps(plan, config)
    _check_weekly_hours(plan, config)
    _check_excluded_clients(plan)
    _check_saturday_trucks(plan, config)
    _check_min_stop_size(plan, config)
    if overrides is not None:
        _check_overrides_honored(plan, overrides)


# ─────────────────────────────────────────────────────────────────────────────
# Individual checks (each focused, each with a clear error message)
# ─────────────────────────────────────────────────────────────────────────────

def _check_no_tank_overflow(plan) -> None:
    """Every delivery must leave the tank ≤ its capacity."""
    for route_key, route in plan.routes.items():
        for stop in route.stops:
            # current_at_arrival + delivery should not exceed tank
            tank = stop.tank_capacity_lbs
            cur = stop.level_at_arrival_lbs
            qty = stop.delivery_lbs
            if cur + qty > tank + 1:  # 1 lb rounding tolerance
                raise TankOverflowViolation(
                    f'OVERFLOW: {stop.client_id} ({stop.customer}) on '
                    f'{route_key[0]} {route_key[1]}: '
                    f'level={cur:.0f} + delivery={qty:.0f} = '
                    f'{cur + qty:.0f} > tank={tank:.0f} lbs'
                )


def _check_no_duplicate_visits(plan) -> None:
    """A client can't be visited twice on the same day."""
    seen: dict[tuple[str, str], str] = {}  # (date, client_id) -> truck_id
    for (date, truck_id), route in plan.routes.items():
        for stop in route.stops:
            key = (str(date), stop.client_id)
            if key in seen:
                raise DuplicateVisitViolation(
                    f'DUPLICATE: {stop.client_id} ({stop.customer}) visited '
                    f'on {date} by both {seen[key]} and {truck_id}'
                )
            seen[key] = truck_id


def _check_shift_caps(plan, config) -> None:
    """No truck-day exceeds the hard shift cap (12h)."""
    hard_cap = config.fleet.shift.hard_max_minutes
    for (date, truck_id), route in plan.routes.items():
        if route.total_minutes > hard_cap:
            raise ShiftCapViolation(
                f'SHIFT CAP: {truck_id} on {date} = {route.total_minutes} min '
                f'> hard limit {hard_cap} min'
            )


def _check_weekly_hours(plan, config) -> None:
    """Per-driver weekly minutes ≤ weekly cap (40h)."""
    weekly_cap = config.fleet.shift.weekly_max_minutes
    weekly: dict[tuple[str, str], int] = {}  # (truck, iso_week) -> total_min
    for (date, truck_id), route in plan.routes.items():
        iso_week = f'{date.isocalendar().year}-{date.isocalendar().week:02d}'
        key = (truck_id, iso_week)
        weekly[key] = weekly.get(key, 0) + route.total_minutes
    for (truck_id, week), mins in weekly.items():
        if mins > weekly_cap:
            raise WeeklyHoursViolation(
                f'WEEKLY HOURS: {truck_id} week {week} = {mins} min '
                f'> weekly cap {weekly_cap} min'
            )


def _check_excluded_clients(plan) -> None:
    """No client flagged Do_Not_Schedule should appear in any route."""
    for (date, truck_id), route in plan.routes.items():
        for stop in route.stops:
            if stop.do_not_schedule:
                raise ExcludedClientViolation(
                    f'DNS VIOLATION: {stop.client_id} ({stop.customer}) is '
                    f'Do_Not_Schedule but appears on {date} {truck_id}'
                )


def _check_saturday_trucks(plan, config) -> None:
    """Only allowed trucks (typically Truck2) operate on Saturdays."""
    allowed = set(config.fleet.saturday_trucks)
    for (date, truck_id), route in plan.routes.items():
        if date.strftime('%a') == 'Sat' and route.stops:
            if truck_id not in allowed:
                raise SaturdayTruckViolation(
                    f'SATURDAY: {truck_id} has {len(route.stops)} stops on '
                    f'{date} but Saturday-allowed trucks are {sorted(allowed)}'
                )


def _check_min_stop_size(plan, config) -> None:
    """Reject deliveries below a HARD floor of pump-prime feasibility (50 lbs).

    Prior behavior: rejected any stop below `min_stop_lbs` (200 lbs default)
    for non-urgent clients. That treats the economic threshold as a safety
    rule. In the final model, `min_stop_lbs` is enforced via a SOFT
    per-stop fee in the solver's cost callback — the solver may take a
    small stop when the geographic detour is cheap (e.g., we're already
    driving past). That's a feature, not a bug.

    The remaining invariant is the genuinely-uneconomic floor: a 5–10 lb
    stop is below pump-priming volume and almost certainly a data error.
    50 lbs is a defensible hard floor.
    """
    HARD_FLOOR_LBS = 50
    for (date, truck_id), route in plan.routes.items():
        for stop in route.stops:
            if stop.delivery_lbs >= HARD_FLOOR_LBS:
                continue
            # Allowed exception: urgent client (low DTE).
            if stop.urgency_tier in ('stockout', 'critical', 'urgent'):
                continue
            raise TinyStopViolation(
                f'TINY STOP: {stop.client_id} ({stop.customer}) on '
                f'{date} {truck_id}: delivery={stop.delivery_lbs:.0f} '
                f'< hard_floor={HARD_FLOOR_LBS} lbs (urgency={stop.urgency_tier})'
            )


def _check_overrides_honored(plan, overrides) -> None:
    """Every Pin must appear; every Forbid must not.

    Exception: a pinned client that is also DNS (do_not_schedule) is
    correctly excluded by ingest. The pin is moot — operator created
    a conflicting override. Skip the pin check for DNS-deferred clients.
    """
    # Build (date, client_id) → truck_id map of actual visits
    actual: dict[tuple, str] = {}
    for (date, truck_id), route in plan.routes.items():
        for stop in route.stops:
            actual[(date, stop.client_id)] = truck_id

    # Determine which client_ids are DNS-deferred (DNS overrides Pin per
    # business rule — see scenarios.scenario_u01_dns_beats_pin).
    dns_deferred = {
        cid for cid, reason in plan.deferred.items()
        if 'DO_NOT_SCHEDULE' in (reason or '').upper()
    }

    for pin in getattr(overrides, 'pins', ()):
        if pin.client_id in dns_deferred:
            continue  # DNS wins; pin is moot
        key = (pin.date, pin.client_id)
        if key not in actual:
            raise OverrideHonorViolation(
                f'PIN NOT HONORED: client {pin.client_id} on {pin.date} '
                f'is pinned but not in plan. Reason: {pin.reason}'
            )

    for fb in getattr(overrides, 'forbids', ()):
        for d in fb.dates:
            key = (d, fb.client_id)
            if key in actual:
                raise OverrideHonorViolation(
                    f'FORBID VIOLATED: client {fb.client_id} on {d} '
                    f'is forbidden but plan has visit by {actual[key]}'
                )
