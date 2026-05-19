"""
validator.py
============
Pre-solve input validation for the SK route optimizer.

Catches data problems before the solver runs, with plain-English error messages
that tell users exactly where to fix issues.
"""

import sys
import pandas as pd
import numpy as np
from dataclasses import dataclass, field
from typing import List, Optional, Tuple
from pathlib import Path

from config import (
    PRODUCTS, PRODUCT_ALIASES, TRUCKS, COMPARTMENT_CAPACITY_LBS,
    SHIFT_MIN, DAYS, NUM_DAYS,
)


@dataclass
class ValidationReport:
    """Result of input validation."""
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    info: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True if no errors."""
        return len(self.errors) == 0

    def pretty_print(self, file=None):
        """Print a formatted report to terminal."""
        if file is None:
            file = sys.stdout

        # Header
        status = "✓ PASS" if self.ok else "✗ FAIL"
        print(f"\n{'='*70}", file=file)
        print(f"  Validation Report: {status}", file=file)
        print(f"{'='*70}", file=file)

        # Errors
        if self.errors:
            print(f"\n⛔ ERRORS ({len(self.errors)}):", file=file)
            for i, err in enumerate(self.errors, 1):
                print(f"   {i}. {err}", file=file)

        # Warnings
        if self.warnings:
            print(f"\n⚠  WARNINGS ({len(self.warnings)}):", file=file)
            for i, warn in enumerate(self.warnings, 1):
                print(f"   {i}. {warn}", file=file)

        # Info
        if self.info:
            print(f"\nℹ  INFO ({len(self.info)}):", file=file)
            for i, inf in enumerate(self.info, 1):
                print(f"   {i}. {inf}", file=file)

        # Summary
        print(f"\n{'='*70}", file=file)
        if self.ok:
            print("  All validations passed. Ready to solve.", file=file)
        else:
            print(f"  FIX {len(self.errors)} ERROR(S) BEFORE SOLVING.", file=file)
        print(f"{'='*70}\n", file=file)


def validate_inputs(
    clients_df: pd.DataFrame,
    deliveries_df: pd.DataFrame,
    time_windows_df: Optional[pd.DataFrame] = None,
    closures_df: Optional[pd.DataFrame] = None,
    trucks_cfg: Optional[dict] = None,
    depot_config: Optional[dict] = None,
    matrix_nodes: Optional[dict] = None,
) -> ValidationReport:
    """
    Validate all inputs before solving.

    Returns ValidationReport with .errors, .warnings, .info, .ok
    """
    report = ValidationReport()

    # Defaults
    if trucks_cfg is None:
        trucks_cfg = TRUCKS
    if time_windows_df is None:
        time_windows_df = pd.DataFrame(columns=['Client_ID', 'Day_of_Week', 'Open_Min', 'Close_Min'])
    if closures_df is None:
        closures_df = pd.DataFrame(columns=['Client_ID', 'Start_Date', 'End_Date', 'Reason'])
    if matrix_nodes is None:
        matrix_nodes = {}

    # Validate
    _validate_clients(clients_df, report)
    _validate_deliveries(clients_df, deliveries_df, report)
    _validate_time_windows(clients_df, time_windows_df, report)
    _validate_closures(clients_df, closures_df, report)
    _validate_depot(depot_config, report)
    _validate_trucks(trucks_cfg, report)
    _validate_capacity_sanity(clients_df, trucks_cfg, report)
    _validate_matrix_coverage(clients_df, matrix_nodes, report)

    return report


def _validate_clients(clients_df: pd.DataFrame, report: ValidationReport):
    """Check Client_List integrity."""
    if clients_df.empty:
        report.errors.append("Client_List is empty.")
        return

    # Duplicates
    dups = clients_df[clients_df.duplicated(subset=['ID'], keep=False)]['ID'].unique()
    if len(dups) > 0:
        for cid in dups:
            report.errors.append(f"Duplicate Client_ID '{cid}' in Client_List.")

    # GPS range
    for idx, row in clients_df.iterrows():
        cid = row['ID']
        lat = row.get('Lat')
        lon = row.get('Lon')

        if pd.notna(lat) and pd.notna(lon):
            if not (33.0 <= lat <= 34.0):
                report.warnings.append(f"Client '{cid}': latitude {lat} outside Phoenix range.")
            if not (-113.0 <= lon <= -111.0):
                report.warnings.append(f"Client '{cid}': longitude {lon} outside Phoenix range.")

    # Tank size
    for idx, row in clients_df.iterrows():
        cid = row['ID']
        tank = row.get('Tank_lbs')
        if pd.isna(tank) or tank <= 0:
            report.warnings.append(f"Client '{cid}': Tank_lbs missing/zero. Will be deferred.")

    # Products
    for idx, row in clients_df.iterrows():
        cid = row['ID']
        prod = str(row.get('Product', '')).strip().upper()
        if prod and prod not in PRODUCT_ALIASES and prod not in PRODUCTS:
            report.errors.append(f"Client '{cid}': Unknown product '{prod}'.")

    # Info
    no_gps = clients_df[clients_df['Lat'].isna() | clients_df['Lon'].isna()]
    if len(no_gps) > 0:
        ids = ', '.join(no_gps['ID'].unique()[:3])
        report.info.append(f"{len(no_gps)} client(s) missing GPS (e.g., {ids}) — will be deferred.")


def _validate_deliveries(clients_df: pd.DataFrame, deliveries_df: pd.DataFrame, report):
    """Check Delivery_Log integrity."""
    if deliveries_df.empty:
        report.warnings.append("Delivery_Log is empty. Using fallback rates.")
        return

    known = set(clients_df['Customer'].unique())
    unknown = deliveries_df[~deliveries_df['Customer'].isin(known)]['Customer'].unique()
    if len(unknown) > 0:
        report.warnings.append(f"{len(unknown)} unknown customer(s) in Delivery_Log.")

    today = pd.Timestamp.today()
    future = deliveries_df[deliveries_df['Date'] > today]
    if len(future) > 0:
        report.errors.append(f"{len(future)} delivery records have future dates.")


def _validate_time_windows(clients_df: pd.DataFrame, time_windows_df: pd.DataFrame, report):
    """Check Client_Time_Windows integrity."""
    if time_windows_df.empty:
        return

    known_ids = set(clients_df['ID'].unique())
    valid_days = set(DAYS)

    for idx, row in time_windows_df.iterrows():
        cid = row.get('Client_ID')
        day = row.get('Day_of_Week', '').strip()
        open_min = row.get('Open_Min')
        close_min = row.get('Close_Min')

        if cid not in known_ids:
            report.errors.append(f"Time window: Client '{cid}' not in Client_List.")
            continue

        if day not in valid_days:
            report.errors.append(f"Time window: Invalid day '{day}' for '{cid}'.")

        if pd.isna(open_min) or pd.isna(close_min):
            report.errors.append(f"Time window: Missing times for '{cid}'.")
            continue

        if open_min >= close_min:
            report.errors.append(f"Time window: Open >= Close for '{cid}'.")

        if close_min - open_min < 30:
            report.warnings.append(f"Time window: '{cid}' has very tight window.")


def _validate_closures(clients_df: pd.DataFrame, closures_df: pd.DataFrame, report):
    """Check Client_Closures integrity."""
    if closures_df.empty:
        return

    known_ids = set(clients_df['ID'].unique())

    for idx, row in closures_df.iterrows():
        cid = row.get('Client_ID')
        start = row.get('Start_Date')
        end = row.get('End_Date')

        if cid not in known_ids:
            report.errors.append(f"Closure: Client '{cid}' not in Client_List.")
            continue

        if pd.isna(start) or pd.isna(end):
            report.errors.append(f"Closure: Missing dates for '{cid}'.")
            continue

        start = pd.Timestamp(start)
        end = pd.Timestamp(end)

        if end < start:
            report.errors.append(f"Closure: End < Start for '{cid}'.")

        today = pd.Timestamp.today()
        if end < today:
            report.info.append(f"Closure: '{cid}' closure is in the past.")


def _validate_depot(depot_config: Optional[dict], report):
    """Check depot configuration."""
    if not depot_config:
        depot_config = {}

    lat = depot_config.get('depot_lat')
    lon = depot_config.get('depot_lon')
    if pd.isna(lat) or pd.isna(lon):
        report.errors.append("Depot: Missing Lat/Lon.")

    shift_start = depot_config.get('shift_start_min')
    shift_end = depot_config.get('shift_end_min')

    if pd.notna(shift_start) and pd.notna(shift_end):
        if shift_end <= shift_start:
            report.errors.append("Depot: Shift_End must be > Shift_Start.")

    work_days = depot_config.get('work_days', DAYS)
    if not work_days or len(work_days) == 0:
        report.errors.append("Depot: Work_Days is empty.")


def _validate_trucks(trucks_cfg: dict, report):
    """Check truck configuration."""
    if not trucks_cfg:
        report.errors.append("Trucks config is empty.")
        return

    active = [t for t in trucks_cfg.values() if t.get('active', True)]
    if len(active) == 0:
        report.errors.append("No active trucks configured.")

    for truck_id, cfg in trucks_cfg.items():
        if not cfg.get('active', True):
            continue

        cap = cfg.get('capacity_lbs')
        pump = cfg.get('pump_rate_lbs_per_min')

        if pd.isna(cap) or cap <= 0:
            report.errors.append(f"Truck '{truck_id}': Invalid capacity.")

        if pd.isna(pump) or pump <= 0:
            report.errors.append(f"Truck '{truck_id}': Invalid pump rate.")


def _validate_capacity_sanity(clients_df: pd.DataFrame, trucks_cfg: dict, report):
    """Warn if fleet looks undersized."""
    active = [t for t in trucks_cfg.values() if t.get('active', True)]
    if not active:
        return

    truck_cap = active[0].get('capacity_lbs', 10000)
    fleet_cap = len(active) * truck_cap * NUM_DAYS

    routable = clients_df[
        clients_df['Lat'].notna() & clients_df['Lon'].notna() &
        (clients_df['Tank_lbs'] > 0) & (clients_df['Avg_LbsPerDay'] > 0)
    ]
    avg_per_client = clients_df['Avg_LbsPerDay'].fillna(0).mean()
    estimated_weekly = len(routable) * avg_per_client * NUM_DAYS

    if estimated_weekly > 1.2 * fleet_cap:
        report.warnings.append(f"Capacity: Fleet may be undersized ({estimated_weekly:,.0f} vs {fleet_cap:,.0f}).")


def _validate_matrix_coverage(clients_df: pd.DataFrame, matrix_nodes: dict, report):
    """Check that every routable client is in the matrix."""
    if not matrix_nodes:
        return

    routable = clients_df[
        clients_df['Lat'].notna() & clients_df['Lon'].notna() & (clients_df['Tank_lbs'] > 0)
    ]

    for idx, row in routable.iterrows():
        cid = row['ID']
        if cid not in matrix_nodes:
            report.warnings.append(f"Matrix: Client '{cid}' not in distance matrix — will be deferred. Rebuild with: python build_matrix.py --force")
