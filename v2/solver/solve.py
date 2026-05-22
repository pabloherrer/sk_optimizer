"""
v2.solver.solve — top-level entry point.

solve(problem) → Plan. Pure function. Builds the model, runs OR-Tools,
extracts the Plan, validates invariants. Either returns a valid plan or raises.
"""
from __future__ import annotations
import time
from typing import Optional

from ortools.constraint_solver import pywrapcp, routing_enums_pb2

from v2.domain.plan import Plan
from v2.domain.problem import ProblemInstance
from v2.solver.model import build_routing_model
from v2.solver.extract import extract_plan
from v2.invariants import check_plan


class SolverFailure(Exception):
    """Raised when the solver fails to produce a feasible plan."""
    pass


def solve(problem: ProblemInstance, solve_seconds: Optional[int] = None) -> Plan:
    """
    Solve a ProblemInstance and return a validated Plan.

    Raises:
        SolverFailure: if no feasible solution is found within the time limit.
        InvariantViolation: if the produced plan violates any of the 8 hard
            invariants (should never happen — indicates a model bug).
    """
    print(f"\n  Building model: {len(problem.clients)} clients, "
          f"{len(problem.trucks)} trucks × {len(problem.horizon_dates)} days")
    t0 = time.time()
    artifacts = build_routing_model(problem)
    print(f"  Model built in {time.time() - t0:.2f}s "
          f"({artifacts.n_clients} routable, "
          f"{artifacts.n_trucks * artifacts.n_days} vehicles)")

    # ── Search parameters ────────────────────────────────────────────────
    search_params = pywrapcp.DefaultRoutingSearchParameters()
    search_params.first_solution_strategy = (
        routing_enums_pb2.FirstSolutionStrategy.PARALLEL_CHEAPEST_INSERTION
    )
    search_params.local_search_metaheuristic = (
        routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    )
    secs = solve_seconds or problem.solve_seconds
    search_params.time_limit.FromSeconds(int(secs))
    search_params.log_search = False

    # ── Solve ────────────────────────────────────────────────────────────
    print(f"  Solving for up to {secs}s ...")
    t1 = time.time()
    solution = artifacts.routing.SolveWithParameters(search_params)
    solve_time = time.time() - t1

    if solution is None:
        raise SolverFailure(
            f"OR-Tools returned no solution after {solve_time:.1f}s. "
            f"Check for infeasibility (e.g., conflicting Pins, all clients DNS)."
        )

    print(f"  Solution found in {solve_time:.1f}s. "
          f"Objective: ${solution.ObjectiveValue()/1000:,.2f}")

    # ── Extract Plan ─────────────────────────────────────────────────────
    plan = extract_plan(problem, artifacts, solution, solve_time)

    # ── Validate invariants ──────────────────────────────────────────────
    # If any invariant fails, the plan is rejected — operator never sees a
    # bad schedule.
    print("  Validating invariants ...")
    check_plan(plan, _make_config_proxy(problem), overrides=problem.overrides)
    print("  All invariants passed ✓")

    print(f"\n  Plan summary:")
    print(f"    Total stops:    {plan.total_stops}")
    print(f"    Total lbs:      {plan.total_lbs_delivered:,.0f}")
    print(f"    Total miles:    {plan.total_miles:,.1f}")
    print(f"    Avg fill %:     {plan.avg_fill_pct:.0f}%")
    print(f"    Stops <70% fill: {plan.pct_stops_under_target_fill:.0f}%")
    print(f"    Truck-days:     {len(plan.routes)}")
    print(f"    Deferred:       {len(plan.deferred)} clients")
    if plan.capacity_warnings:
        print(f"    Warnings:       {len(plan.capacity_warnings)}")
        for w in plan.capacity_warnings:
            print(f"      ⚠ {w}")

    return plan


def _make_config_proxy(problem: ProblemInstance):
    """Build a minimal config object for invariant checks."""
    class _Shift:
        hard_max_minutes = problem.shift_hard_max_min
        weekly_max_minutes = problem.weekly_max_min

    class _Fleet:
        saturday_trucks = getattr(problem, 'saturday_truck_ids', ['Truck2'])
        shift = _Shift()

    class _Policy:
        min_stop_lbs = problem.min_stop_lbs

    class _Config:
        fleet = _Fleet()
        policy = _Policy()

    return _Config()
