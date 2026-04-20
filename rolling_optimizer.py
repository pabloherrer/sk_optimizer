"""
rolling_optimizer.py
====================
Orchestrates the full two-phase rolling-horizon optimizer.

Workflow
--------
Monday AM  : plan_week(start_day=0) → lock Monday routes → dispatch
Monday PM  : update_after_day(0, delivered_ids=[...])

Tuesday AM : plan_week(start_day=1) → re-plans Tue–Fri with updated state
              → lock Tuesday routes → dispatch
...and so on.

The class holds all mutable state (inventory levels, locked routes) so
repeated calls within the same session are incremental — not full rebuilds.
"""

import logging
from pathlib import Path
from typing import Dict, List, Optional
import numpy as np
import pandas as pd

from config import (
    DAYS, NUM_DAYS, TRUCK_NAMES, TRUCKS, INPUT_FILE, MATRIX_FILE, STATE_FILE,
)
from load_data import load_all
from forecast_consumption import estimate_consumption_rates
from inventory import enrich_snapshot
from scheduler import assign_customers_to_days
from router import solve_day_routes, _solve_single_truck, load_matrix
from state import load_state, save_state, update_state, initialise_state_from_snapshot

log = logging.getLogger(__name__)


class RollingHorizonOptimizer:
    """
    Entry point for daily route planning.

    Parameters
    ----------
    clients_df      : master client table (from load_data)
    dist_matrix     : (N×N) integer metres
    time_matrix_min : (N×N) integer minutes
    node_index_map  : {client_id: matrix_row_index}
    inventory_state : {client_id: current_lbs}  — updated each evening
    """

    def __init__(
        self,
        clients_df:      pd.DataFrame,
        dist_matrix:     np.ndarray,
        time_matrix_min: np.ndarray,
        node_index_map:  dict,
        inventory_state: Optional[Dict[str, float]] = None,
    ):
        self.clients_df      = clients_df
        self.dm              = dist_matrix
        self.tm              = time_matrix_min
        self.node_index_map  = node_index_map
        self.inventory_state = inventory_state or {}
        self.locked_routes:  Dict[int, pd.DataFrame] = {}
        self.last_assignment: Optional[pd.DataFrame] = None

    # ── Factory ──────────────────────────────────────────────────────────────

    @classmethod
    def build(
        cls,
        input_file:  str | Path = INPUT_FILE,
        matrix_file: str | Path = MATRIX_FILE,
        state_file:  str | Path = STATE_FILE,
        today:       Optional[pd.Timestamp] = None,
    ) -> 'RollingHorizonOptimizer':
        """
        Full build from disk.  Call this once per session.
        """
        today = today or pd.Timestamp.today().normalize()

        print("─" * 60)
        print("S&K Route Optimizer — Loading")
        print("─" * 60)

        # Data
        print("\n[1/4] Loading client list and delivery log...")
        clients_raw, deliveries = load_all(input_file)

        # Consumption rates
        print("\n[2/4] Estimating consumption rates...")
        clients_df = estimate_consumption_rates(deliveries, clients_raw, today=today)

        # Distance matrix
        print("\n[3/4] Loading distance/time matrix...")
        dm, tm, node_index_map = load_matrix(matrix_file)

        # Inventory state
        print("\n[4/4] Loading inventory state...")
        state = load_state(state_file)
        if not state:
            print("  No prior state — initialising from delivery-log estimates.")
            state = initialise_state_from_snapshot(clients_df)

        print("\n✓ Ready.\n")
        return cls(clients_df, dm, tm, node_index_map, state)

    # ── Planning ─────────────────────────────────────────────────────────────

    def plan_week(
        self,
        start_day: int = 0,
        save_assignment: bool = True,
    ) -> Dict[int, pd.DataFrame]:
        """
        Run the full two-phase optimizer for days [start_day … 4].

        Returns {day_index: route_DataFrame}.
        Locked days are returned as-is without re-solving.
        """
        print(f"\n{'═' * 60}")
        print(f"  Planning from {DAYS[start_day]}  "
              f"({'full week' if start_day == 0 else 're-plan'})")
        print(f"{'═' * 60}")

        # Enrich snapshot with current inventory
        snapshot = enrich_snapshot(self.clients_df, self.inventory_state)
        self._print_pool_summary(snapshot, start_day)

        # Phase 1 — day assignment
        print(f"\n{'─' * 40}")
        print("  Phase 1: Day Assignment")
        print(f"{'─' * 40}")
        assignment = assign_customers_to_days(snapshot, start_day=start_day)
        self.last_assignment = assignment

        # Phase 2 — route per day
        all_routes: Dict[int, pd.DataFrame] = {}

        for d in range(start_day, NUM_DAYS):
            # Return locked routes unchanged
            if d in self.locked_routes:
                all_routes[d] = self.locked_routes[d]
                continue

            day_clients = assignment[assignment['AssignedDayIndex'] == d].copy()
            n = len(day_clients)

            print(f"\n{'─' * 40}")
            print(f"  Phase 2: Routing {DAYS[d]}  ({n} clients)")
            print(f"{'─' * 40}")

            if n == 0:
                print("  No clients assigned.")
                all_routes[d] = pd.DataFrame()
                continue

            has_truck_col = (
                'AssignedTruck' in day_clients.columns
                and day_clients['AssignedTruck'].isin(TRUCK_NAMES).any()
            )

            if has_truck_col:
                # ── Per-truck path: Phase 1 already geo-clustered by truck ──
                # Solve each truck independently; retry per truck if infeasible.
                truck_routes = []
                for truck_name in TRUCK_NAMES:
                    tc = day_clients[day_clients['AssignedTruck'] == truck_name].copy()
                    if len(tc) == 0:
                        continue
                    rt = _solve_single_truck(
                        tc, d, truck_name, self.dm, self.tm, self.node_index_map
                    )
                    if rt.empty and len(tc) > 1:
                        print(f"  Retrying {truck_name}/{DAYS[d]} with load reduction...")
                        tc_relaxed = _drop_lowest_score(tc)
                        rt = _solve_single_truck(
                            tc_relaxed, d, truck_name,
                            self.dm, self.tm, self.node_index_map
                        )
                        if not rt.empty:
                            dropped = set(tc['ID']) - set(tc_relaxed['ID'])
                            print(f"  Retry succeeded. Deferred: {dropped}")
                    if not rt.empty:
                        truck_routes.append(rt)

                routes = (
                    pd.concat(truck_routes, ignore_index=True)
                    if truck_routes else pd.DataFrame()
                )
            else:
                # ── Legacy two-truck CVRP path ────────────────────────────
                routes = solve_day_routes(
                    day_clients, d, self.dm, self.tm, self.node_index_map
                )
                if routes.empty and len(day_clients) > 1:
                    print(f"  Retrying with load reduction...")
                    relaxed_clients = _drop_lowest_score(day_clients)
                    routes = solve_day_routes(
                        relaxed_clients, d, self.dm, self.tm, self.node_index_map
                    )
                    if not routes.empty:
                        dropped = set(day_clients['ID']) - set(relaxed_clients['ID'])
                        print(f"  Retry succeeded. Deferred: {dropped}")

            all_routes[d] = routes
            if not routes.empty:
                self._print_day_summary(routes, d)

        return all_routes

    def lock_day(self, day_index: int, routes: pd.DataFrame) -> None:
        """
        Lock a day's routes.  Call after drivers depart.
        Locked routes are not re-solved in subsequent plan_week() calls.
        """
        self.locked_routes[day_index] = routes
        print(f"  Locked {DAYS[day_index]} routes "
              f"({len(routes[routes['Stop'] == 1])} trucks active).")

    # ── State update ─────────────────────────────────────────────────────────

    def update_after_day(
        self,
        day_index:      int,
        delivered_ids:  List[str],
        n_days_elapsed: int = 1,
        state_file:     str | Path = STATE_FILE,
    ) -> None:
        """
        Update inventory state after a day's deliveries.

        Call this each evening BEFORE next morning's plan_week().

        delivered_ids  : IDs of clients that were actually served today.
        n_days_elapsed : 1 for weekdays; 3 for Friday→Monday.
        """
        print(f"\nUpdating inventory after {DAYS[day_index]}  "
              f"(+{n_days_elapsed} day(s))...")
        print(f"  Delivered: {len(delivered_ids)} clients")

        self.inventory_state = update_state(
            self.inventory_state,
            self.clients_df,
            delivered_ids,
            n_days_elapsed=n_days_elapsed,
        )

        save_state(self.inventory_state, state_file)
        print(f"  State saved → {state_file}")

        # Clear locks for days already past
        for d in list(self.locked_routes.keys()):
            if d <= day_index:
                del self.locked_routes[d]

    # ── Reporting helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _print_pool_summary(df: pd.DataFrame, start_day: int) -> None:
        urgency_counts = df['Urgency'].value_counts()
        print(f"\n  Client pool:  {len(df)} total")
        for tier in ['stockout', 'critical', 'urgent', 'normal']:
            n = urgency_counts.get(tier, 0)
            if n:
                marker = ' ← ACTION REQUIRED' if tier in ('stockout', 'critical') else ''
                print(f"    {tier:<10}: {n:>3}{marker}")

    @staticmethod
    def _print_day_summary(routes: pd.DataFrame, day_index: int) -> None:
        for truck in TRUCK_NAMES:
            sub = routes[routes['Truck'] == truck]
            if sub.empty:
                continue
            load   = sub['Refill_lbs'].sum()
            cap_p  = load / TRUCKS[truck]['capacity_lbs'] * 100
            time_v = sub['Route_Time_min'].iloc[0]
            shift_p= time_v / 600 * 100
            dist_v = sub['Route_Dist_km'].iloc[-1]
            print(f"    {truck}: {len(sub):>2} stops | "
                  f"{load:>6,.0f} lbs ({cap_p:>4.0f}% cap) | "
                  f"{time_v:>3} min ({shift_p:>4.0f}% shift) | "
                  f"{dist_v:.0f} km")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _drop_lowest_score(day_clients: pd.DataFrame) -> pd.DataFrame:
    """
    Remove the single lowest-score non-critical client from a day's pool.
    Used as a fallback when Phase-2 finds the day infeasible.
    """
    non_critical = day_clients[day_clients['Urgency'].isin(['urgent', 'normal'])]
    if non_critical.empty:
        return day_clients
    drop_idx = non_critical['VisitScore'].idxmin()
    return day_clients.drop(index=drop_idx).reset_index(drop=True)
