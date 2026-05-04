"""
warm_start.py — Plan continuity across rolling-horizon runs
===========================================================

Why warm-start
--------------
Each daily re-solve currently runs OR-Tools from a cold start. The
previous-day solution is thrown away. Two costs from this:

  1. CPU: cold metaheuristic search wastes most of its time-budget
     re-discovering the structure of yesterday's solution. With a
     warm start, the same time budget yields strictly better solutions
     (verified across IRP literature: Coelho-Cordeau-Laporte 2014 §6.3
     report 5–12 % cost reduction from warm-start alone).

  2. Operational: drivers see different routes day-to-day for what
     should be a stable plan. Tomorrow's "day-1" tentative plan today
     is shown to no one; tomorrow it becomes "day-0" with a totally
     different sequence. Bad for trust.

What this module does
---------------------
Reads `plan.json` (saved by state_manager.save_plan), maps yesterday's
day-N visits onto today's day-(N-1), then constructs an OR-Tools
"initial routes" matrix consumable by `ReadAssignmentFromRoutes`.

How OR-Tools warm-start works
-----------------------------
`routing.ReadAssignmentFromRoutes(routes, ignore_inactive_indices=True)`
takes a list (one per vehicle) of node-index sequences. The solver
treats this as the starting solution for local search. We don't have
to make it feasible; OR-Tools repairs infeasibilities during search.

Mapping yesterday → today
-------------------------
Yesterday's plan was for day_indices [0..H-1] starting yesterday.
Today, day i became day i-1. So:
    yesterday's day 1 visits → today's day 0
    yesterday's day 2 visits → today's day 1
    ...
    yesterday's day H-1 visits → today's day H-2
    today's day H-1: empty (no warm-start info)

The vehicle index also depends on the truck name + day + config.
We re-derive the vehicle index from (truck, day, config) using the
same scheme as unified_solver.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Vehicle-index helpers (must match unified_solver's vehicle layout)
# ─────────────────────────────────────────────────────────────────────────────

def _vehicle_index(
    truck_idx: int,
    day_idx: int,
    config_idx: int,
    *,
    num_days: int,
    num_configs: int = 3,
) -> int:
    """
    Mirror of unified_solver's vehicle layout:
      v = truck_idx * (num_days * num_configs) + day_idx * num_configs + config_idx
    """
    return truck_idx * (num_days * num_configs) + day_idx * num_configs + config_idx


# ─────────────────────────────────────────────────────────────────────────────
# Build OR-Tools initial routes from yesterday's plan
# ─────────────────────────────────────────────────────────────────────────────

def shift_plan_for_today(
    *,
    plan: Dict[str, Any],
    today: pd.Timestamp,
) -> Dict[Tuple[int, str, int], List[str]]:
    """
    Shift yesterday's plan onto today's day grid.

    Returns a dict keyed by (day_index_today, truck_name, config_idx)
    with values = ordered lists of client IDs.

    config_idx is inferred from the original plan's product mix per
    truck-day. For now we conservatively pin to SPLIT (config 0); the
    solver is free to swap. (Future improvement: persist the actual
    config index in plan.json.)
    """
    if plan is None:
        return {}
    plan_today = pd.Timestamp(plan.get('today', today)).normalize()
    today = pd.Timestamp(today).normalize()
    day_shift = (today - plan_today).days   # how many days have passed
    if day_shift < 0:
        log.warning('Plan is from the future (today=%s, plan=%s) — skipping warm start.',
                    today.date(), plan_today.date())
        return {}

    # Group yesterday's visits by (day_today, truck)
    grouped: Dict[Tuple[int, str, int], List[Tuple[int, str]]] = {}
    for v in plan.get('visits', []):
        day_old = int(v['day'])
        day_new = day_old - day_shift
        if day_new < 0:
            continue   # already in the past
        truck = v.get('truck', '')
        cid = str(v.get('client_id', ''))
        stop = int(v.get('stop', 0))
        key = (day_new, truck, 0)   # config 0 (SPLIT) — solver may upgrade
        grouped.setdefault(key, []).append((stop, cid))

    # Sort by stop order to preserve sequence
    out: Dict[Tuple[int, str, int], List[str]] = {}
    for key, stops in grouped.items():
        stops.sort(key=lambda x: x[0])
        out[key] = [cid for _, cid in stops]
    return out


def build_initial_routes(
    *,
    plan: Optional[Dict[str, Any]],
    today: pd.Timestamp,
    truck_names: List[str],
    horizon_days: int,
    num_configs: int,
    n_vehicles: int,
    client_id_to_node: Dict[str, int],
) -> Optional[List[List[int]]]:
    """
    Translate yesterday's plan into the list-of-lists form that
    OR-Tools' ReadAssignmentFromRoutes expects.

    Returns None if the plan is missing or unusable.

    Output shape: list of length n_vehicles. Each element is a list
    of node indices (no depot). OR-Tools' API auto-prepends/appends
    the depot.
    """
    if plan is None:
        log.info('No prior plan found — solving from cold.')
        return None

    shifted = shift_plan_for_today(plan=plan, today=today)
    if not shifted:
        log.info('Prior plan had no usable visits after shift — cold start.')
        return None

    truck_idx_of = {name: i for i, name in enumerate(truck_names)}
    routes: List[List[int]] = [[] for _ in range(n_vehicles)]
    matched, missed = 0, 0

    for (day, truck, cfg), client_ids in shifted.items():
        if day >= horizon_days:
            continue
        if truck not in truck_idx_of:
            continue
        v = _vehicle_index(
            truck_idx_of[truck], day, cfg,
            num_days=horizon_days, num_configs=num_configs,
        )
        if v >= n_vehicles:
            continue
        for cid in client_ids:
            node = client_id_to_node.get(str(cid))
            if node is None:
                missed += 1
                continue
            routes[v].append(node)
            matched += 1

    if matched == 0:
        log.info('Plan had visits but none mapped to current node set — cold start.')
        return None

    log.info('Warm start: %d visits matched, %d missed (renamed/excluded clients).',
             matched, missed)
    return routes


# ─────────────────────────────────────────────────────────────────────────────
# Plan-stability metric (used by backtests)
# ─────────────────────────────────────────────────────────────────────────────

def plan_overlap(
    plan_old: Optional[Dict[str, Any]],
    plan_new: Dict[str, Any],
    *,
    day_offset_old: int = 1,
    day_offset_new: int = 0,
) -> float:
    """
    Fraction of clients who appear on yesterday's day-1 plan AND
    today's day-0 plan. 1.0 = perfect stability; 0.0 = whiplash.

    Use to compare warm-start vs cold-start runs in backtests.
    """
    if plan_old is None:
        return 0.0
    old_set = {
        str(v.get('client_id', ''))
        for v in plan_old.get('visits', [])
        if int(v.get('day', -1)) == day_offset_old
    }
    new_set = {
        str(v.get('client_id', ''))
        for v in plan_new.get('visits', [])
        if int(v.get('day', -1)) == day_offset_new
    }
    if not old_set and not new_set:
        return 1.0
    if not old_set or not new_set:
        return 0.0
    return len(old_set & new_set) / len(old_set | new_set)
