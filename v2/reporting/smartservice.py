"""
v2.reporting.smartservice — SmartService-importable CSV for next-day dispatch.

Mirrors the v1 schema in `reporting/smartservice.py` so the file round-trips
into SmartService cleanly.
"""
from __future__ import annotations
import csv
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, Optional

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
    # %-m/%-d not portable on Windows — strip leading zeros manually for safety.
    s = d.strftime('%m/%d/%Y %I:%M:%S %p')
    parts = s.split(' ', 1)
    date_part = '/'.join(str(int(p)) for p in parts[0].split('/'))
    time_part = parts[1]
    # Strip leading zero from hour
    if time_part.startswith('0'):
        time_part = time_part[1:]
    return f'{date_part} {time_part}'


def write_smartservice_csv(
    plan,
    target_date: date,
    output_path: Path,
    truck_to_driver: Optional[Dict[str, str]] = None,
    shift_start_min: int = 360,
) -> int:
    """
    Write a SmartService-importable CSV with one row per stop on `target_date`.

    If no routes are scheduled for that date, a CSV with only the header is
    written (so downstream pipelines don't choke on a missing file).

    Returns the number of data rows written.
    """
    truck_to_driver = truck_to_driver or DEFAULT_TRUCK_TO_DRIVER
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    rows = []
    routes_today = [r for (d, _), r in sorted(plan.routes.items()) if d == target_date]
    midnight = datetime(target_date.year, target_date.month, target_date.day)

    for route in routes_today:
        truck = route.truck_id
        driver = truck_to_driver.get(truck, truck)
        for stop in route.stops:
            arrival = midnight + timedelta(minutes=shift_start_min + stop.arrival_min)
            depart = midnight + timedelta(minutes=shift_start_min + stop.depart_min)
            duration_min = stop.setup_min + stop.pump_min
            route_label = f'{truck}/{target_date.strftime("%a")}'
            rows.append({
                'Customer':                stop.customer,
                'Primary Contact Details': '',
                'Service Address':         stop.address,
                'Latitude':                stop.lat,
                'Longitude':               stop.lon,
                'Revenue':                 '$0.00',
                'Route':                   route_label,
                'Day':                     target_date.strftime('%A'),
                'Stop':                    int(stop.sequence),
                'Employee Full Name':      driver,
                'Crew':                    truck,
                'Start Date Time':         _format_dt(arrival),
                'End Date Time':           _format_dt(depart),
                'Duration':                str(duration_min),
                'Locked':                  'False',
                'Travel Minutes':          int(round(stop.travel_miles * 2)),  # rough est, no time field
                'Travel Minute Info':      f'{round(stop.travel_miles, 1)} mi from prev',
                'Travel Miles':            round(stop.travel_miles, 2),
                'Travel Mile Info':        f'{round(stop.travel_miles, 1)} mi from prev',
                'Appointment':             f'IRP refill {round(stop.delivery_lbs):,} lbs',
                'Quote':                   '',
                'Work Order':              '',
            })

    with open(output_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=SMARTSERVICE_HEADER, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return len(rows)
