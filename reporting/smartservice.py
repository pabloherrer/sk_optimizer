"""
smartservice_export.py — Export IRP plan as SmartService-importable CSV
========================================================================

SmartService's "RoutePlanDetails" export uses this CSV schema:

  Customer, Primary Contact Details, Service Address, Latitude, Longitude,
  Revenue, Route, Day, Stop, Employee Full Name, Crew, Start Date Time,
  End Date Time, Duration, Locked, Travel Minutes, Travel Minute Info,
  Travel Miles, Travel Mile Info, Appointment, Quote, Work Order

If we EXPORT in this format, the same file should round-trip back into
SmartService for dispatch — driver/crew assignments, arrival windows,
and travel info all preserved.

This module takes the routes dict from the IRP solver and writes a CSV
with one row per stop. Driver assignments are mapped from truck names.
"""

from __future__ import annotations

import csv
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional

import pandas as pd


# Default driver mapping. If SK changes who drives which truck, update here
# or pass `truck_to_driver` into export_smartservice_csv().
DEFAULT_TRUCK_TO_DRIVER = {
    'Truck2': 'JOE',
    'Truck9': 'JASON B',
}


SMARTSERVICE_HEADER = [
    'Customer', 'Primary Contact Details', 'Service Address',
    'Latitude', 'Longitude', 'Revenue', 'Route', 'Day', 'Stop',
    'Employee Full Name', 'Crew', 'Start Date Time', 'End Date Time',
    'Duration', 'Locked', 'Travel Minutes', 'Travel Minute Info',
    'Travel Miles', 'Travel Mile Info',
    'Appointment', 'Quote', 'Work Order',
]


def _format_dt(d: datetime) -> str:
    """SmartService uses '4/30/2026 9:00:00 AM' style."""
    return d.strftime('%-m/%-d/%Y %-I:%M:%S %p')


def export_smartservice_csv(
    *,
    routes: Dict[int, pd.DataFrame],
    output_path: Path,
    plan_dates: list,
    clients_df: pd.DataFrame,
    truck_to_driver: Optional[Dict[str, str]] = None,
    shift_start_min: int = 360,
) -> int:
    """
    Write a SmartService-importable CSV of the plan.

    Parameters
    ----------
    routes        : {day_index: DataFrame of stops} from solver output
    output_path   : where to write the CSV
    plan_dates    : ordered list of pd.Timestamp, one per planning day
    clients_df    : full client list (for contact info, address, etc.)
    truck_to_driver: optional override of {truck: driver} mapping
    shift_start_min : minutes-from-midnight when driver leaves depot
                     (default 360 = 6:00 AM)

    Returns count of rows written.
    """
    truck_to_driver = truck_to_driver or DEFAULT_TRUCK_TO_DRIVER

    # Lookup tables for client info
    clients_df = clients_df.copy()
    clients_df['ID'] = clients_df['ID'].astype(str)
    addr_lookup = dict(zip(clients_df['ID'], clients_df.get('Address', '')))
    phone_lookup = dict(zip(clients_df['ID'], clients_df.get('Phone', '')))

    rows = []
    for d, df in routes.items():
        if df is None or df.empty:
            continue
        # The actual delivery date for this day index
        if d < len(plan_dates):
            day_date = plan_dates[d]
        else:
            day_date = pd.Timestamp.today() + pd.Timedelta(days=d)

        for _, r in df.iterrows():
            cid = str(r.get('ID', ''))
            truck = r.get('Truck', '')
            driver = truck_to_driver.get(truck, truck)

            # Arrival/departure absolute timestamps
            arrival = day_date.normalize() + pd.Timedelta(minutes=int(r.get('Arrival_Min', shift_start_min)))
            depart = day_date.normalize() + pd.Timedelta(minutes=int(r.get('Depart_Min', shift_start_min + 30)))
            duration_min = int(r.get('Service_Min', 0))

            # SmartService treats Crew as the truck label, Route as a free
            # text (we'll use the truck name + day).
            route_label = f'{truck}/{day_date.strftime("%a")}'

            rows.append({
                'Customer':              r.get('Customer', ''),
                'Primary Contact Details': phone_lookup.get(cid, ''),
                'Service Address':       addr_lookup.get(cid, ''),
                'Latitude':              r.get('Lat', ''),
                'Longitude':             r.get('Lon', ''),
                'Revenue':               '$0.00',          # we don't price
                'Route':                 route_label,
                'Day':                   day_date.strftime('%A'),
                'Stop':                  int(r.get('Stop', 0)),
                'Employee Full Name':    driver,
                'Crew':                  truck,
                'Start Date Time':       _format_dt(arrival),
                'End Date Time':         _format_dt(depart),
                'Duration':              f'{duration_min}',
                'Locked':                'False',
                'Travel Minutes':        int(r.get('Travel_To_Min', 0)),
                'Travel Minute Info':    f'{int(r.get("Travel_To_Min",0))} min from prev',
                'Travel Miles':          float(r.get('Dist_To_mi', 0)),
                'Travel Mile Info':      f'{r.get("Dist_To_mi",0)} mi from prev',
                'Appointment':           f'IRP refill {int(r.get("Refill_lbs",0))} lbs',
                'Quote':                 '',
                'Work Order':            '',
            })

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=SMARTSERVICE_HEADER, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return len(rows)
