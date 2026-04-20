"""
schema_loaders.py
=================
Load configuration and constraints from SK_Delivery_System.xlsx sheets.

Functions expose Client_Time_Windows, Client_Closures, Trucks, and Depot
configuration as Python objects, with graceful fallbacks to config.py defaults
when sheets are missing or incomplete.
"""

import warnings
import pandas as pd
from openpyxl import load_workbook
from pathlib import Path
from config import INPUT_FILE, TRUCKS as DEFAULT_TRUCKS

warnings.filterwarnings('ignore')


# ── Public API ────────────────────────────────────────────────────────────────

def load_time_windows(input_file: str | Path = INPUT_FILE) -> pd.DataFrame:
    """
    Load Client_Time_Windows sheet.

    Returns DataFrame with columns:
      - Client_ID (str)
      - Day_of_Week (str: Mon, Tue, Wed, Thu, Fri, Sat, Sun)
      - Open_Min (int: minutes since midnight)
      - Close_Min (int: minutes since midnight)

    Skips rows where Client_ID starts with 'EXAMPLE_'.
    Returns empty DataFrame if sheet is missing or has no data.
    """
    try:
        wb = load_workbook(str(input_file), data_only=True)
        if 'Client_Time_Windows' not in wb.sheetnames:
            return pd.DataFrame(columns=['Client_ID', 'Day_of_Week', 'Open_Min', 'Close_Min'])

        ws = wb['Client_Time_Windows']
        records = []

        for row in ws.iter_rows(min_row=4, values_only=True):
            # Skip if empty or starts with EXAMPLE_
            if not row[0]:
                continue
            client_id = str(row[0]).strip()
            if client_id.startswith('EXAMPLE_'):
                continue

            # Parse day and times
            day = str(row[1]).strip() if row[1] else None
            open_hhmm = str(row[2]).strip() if row[2] else None
            close_hhmm = str(row[3]).strip() if row[3] else None

            if not (day and open_hhmm and close_hhmm):
                continue

            try:
                open_min = _time_to_min(open_hhmm)
                close_min = _time_to_min(close_hhmm)

                records.append({
                    'Client_ID': client_id,
                    'Day_of_Week': day,
                    'Open_Min': open_min,
                    'Close_Min': close_min,
                })
            except ValueError:
                # Skip malformed time entries
                continue

        return pd.DataFrame(records)
    except Exception:
        return pd.DataFrame(columns=['Client_ID', 'Day_of_Week', 'Open_Min', 'Close_Min'])


def load_closures(input_file: str | Path = INPUT_FILE) -> pd.DataFrame:
    """
    Load Client_Closures sheet.

    Returns DataFrame with columns:
      - Client_ID (str)
      - Start_Date (pd.Timestamp)
      - End_Date (pd.Timestamp)
      - Reason (str)

    Skips rows where Client_ID starts with 'EXAMPLE_'.
    Returns empty DataFrame if sheet is missing or has no data.
    """
    try:
        wb = load_workbook(str(input_file), data_only=True)
        if 'Client_Closures' not in wb.sheetnames:
            return pd.DataFrame(columns=['Client_ID', 'Start_Date', 'End_Date', 'Reason'])

        ws = wb['Client_Closures']
        records = []

        for row in ws.iter_rows(min_row=4, values_only=True):
            if not row[0]:
                continue
            client_id = str(row[0]).strip()
            if client_id.startswith('EXAMPLE_'):
                continue

            start_date = row[1]
            end_date = row[2]
            reason = str(row[3]).strip() if row[3] else ''

            # Parse dates robustly
            try:
                if isinstance(start_date, str):
                    start_date = pd.Timestamp(start_date)
                else:
                    start_date = pd.Timestamp(start_date.date() if hasattr(start_date, 'date') else start_date)

                if isinstance(end_date, str):
                    end_date = pd.Timestamp(end_date)
                else:
                    end_date = pd.Timestamp(end_date.date() if hasattr(end_date, 'date') else end_date)

                records.append({
                    'Client_ID': client_id,
                    'Start_Date': start_date,
                    'End_Date': end_date,
                    'Reason': reason,
                })
            except (ValueError, AttributeError):
                # Skip rows with unparseable dates
                continue

        return pd.DataFrame(records)
    except Exception:
        return pd.DataFrame(columns=['Client_ID', 'Start_Date', 'End_Date', 'Reason'])


def load_trucks(input_file: str | Path = INPUT_FILE) -> dict:
    """
    Load Trucks sheet and return as dict matching config.TRUCKS format.

    Dict structure:
      {truck_id: {
          'capacity_lbs': int,
          'pump_rate_lbs_per_min': float,
          'fixed_setup_min': int,
          'shift_min': int,
          'cost_per_mile': float,
          'compartment_a_lbs': int,
          'compartment_b_lbs': int,
          'active': bool
      }}

    Only includes rows where Active='yes' (case-insensitive).
    Falls back to config.TRUCKS if sheet is missing or all rows inactive.
    """
    try:
        wb = load_workbook(str(input_file), data_only=True)
        if 'Trucks' not in wb.sheetnames:
            return DEFAULT_TRUCKS

        ws = wb['Trucks']
        trucks_dict = {}

        for row in ws.iter_rows(min_row=4, values_only=True):
            if not row[0]:
                continue

            truck_id = str(row[0]).strip()
            if truck_id.startswith('EXAMPLE_'):
                continue

            # Check if active
            active = str(row[7]).strip().lower() if row[7] else 'no'
            if active != 'yes':
                continue

            try:
                comp_a = float(row[1])
                comp_b = float(row[2])
                pump_rate = float(row[3])
                setup_min = int(float(row[4]))
                shift_min = int(float(row[5]))
                cost_per_mile = float(row[6])

                trucks_dict[truck_id] = {
                    'capacity_lbs': comp_a + comp_b,
                    'pump_rate_lbs_per_min': pump_rate,
                    'fixed_setup_min': setup_min,
                    'shift_min': shift_min,
                    'cost_per_mile': cost_per_mile,
                    'compartment_a_lbs': comp_a,
                    'compartment_b_lbs': comp_b,
                    'active': True,
                }
            except (ValueError, TypeError, IndexError):
                # Skip malformed rows
                continue

        # Fall back to defaults if no valid trucks found
        return trucks_dict if trucks_dict else DEFAULT_TRUCKS
    except Exception:
        return DEFAULT_TRUCKS


def load_depot_config(input_file: str | Path = INPUT_FILE) -> dict:
    """
    Load Depot sheet as key-value pairs.

    Returns dict with keys:
      - depot_lat (float)
      - depot_lon (float)
      - shift_start_min (int: minutes since midnight)
      - shift_end_min (int: minutes since midnight)
      - morning_load_min (int)
      - evening_unload_min (int)
      - work_days (list of str: ['Tue', 'Wed', ...])

    Falls back to config.py defaults if sheet is missing or incomplete.
    """
    from config import (DEPOT_LAT, DEPOT_LON, SHIFT_MIN, DAYS,
                        SHIFT_MIN as DEFAULT_SHIFT_MIN)

    defaults = {
        'depot_lat': DEPOT_LAT,
        'depot_lon': DEPOT_LON,
        'shift_start_min': 6 * 60,  # 06:00
        'shift_end_min': 16 * 60,   # 16:00
        'morning_load_min': 30,
        'evening_unload_min': 15,
        'work_days': DAYS,
    }

    try:
        wb = load_workbook(str(input_file), data_only=True)
        if 'Depot' not in wb.sheetnames:
            return defaults

        ws = wb['Depot']
        config_dict = dict(defaults)

        for row in ws.iter_rows(min_row=4, values_only=True):
            if not row[0]:
                continue

            param = str(row[0]).strip()
            value = row[1]

            try:
                if param == 'Depot_Lat':
                    config_dict['depot_lat'] = float(value)
                elif param == 'Depot_Lon':
                    config_dict['depot_lon'] = float(value)
                elif param == 'Shift_Start_HHMM':
                    config_dict['shift_start_min'] = _time_to_min(str(value).strip())
                elif param == 'Shift_End_HHMM':
                    config_dict['shift_end_min'] = _time_to_min(str(value).strip())
                elif param == 'Morning_Load_Min':
                    config_dict['morning_load_min'] = int(float(value))
                elif param == 'Evening_Unload_Min':
                    config_dict['evening_unload_min'] = int(float(value))
                elif param == 'Work_Days':
                    days_str = str(value).strip()
                    config_dict['work_days'] = [d.strip() for d in days_str.split(',')]
            except (ValueError, TypeError):
                # Keep default for this parameter
                pass

        return config_dict
    except Exception:
        return defaults


def is_client_open(client_id: str, day_name: str, arrival_min: int,
                   time_windows_df: pd.DataFrame) -> bool:
    """
    Check if a client is open on a given day at a given arrival time.

    Parameters
    ----------
    client_id : str
        Client ID (e.g., 'C001')
    day_name : str
        Day of week (e.g., 'Tue')
    arrival_min : int
        Arrival time in minutes since midnight (e.g., 540 for 9:00 AM)
    time_windows_df : pd.DataFrame
        Result from load_time_windows()

    Returns
    -------
    bool
        True if client is open at arrival_min.
        True if no window defined for (client_id, day_name) = no restriction.
    """
    if time_windows_df.empty:
        return True

    match = time_windows_df[
        (time_windows_df['Client_ID'] == client_id) &
        (time_windows_df['Day_of_Week'] == day_name)
    ]

    if match.empty:
        # No restriction defined
        return True

    row = match.iloc[0]
    return row['Open_Min'] <= arrival_min < row['Close_Min']


def is_client_closed_on(client_id: str, date, closures_df: pd.DataFrame) -> bool:
    """
    Check if a client is closed (due to closure) on a given date.

    Parameters
    ----------
    client_id : str
        Client ID (e.g., 'C001')
    date : str, datetime, or Timestamp
        Date to check (YYYY-MM-DD or datetime object)
    closures_df : pd.DataFrame
        Result from load_closures()

    Returns
    -------
    bool
        True if closures_df contains a row covering date for client_id.
    """
    if closures_df.empty:
        return False

    # Normalize date to Timestamp
    if isinstance(date, str):
        date = pd.Timestamp(date)
    elif hasattr(date, 'date'):
        date = pd.Timestamp(date.date())
    else:
        date = pd.Timestamp(date)

    match = closures_df[
        (closures_df['Client_ID'] == client_id) &
        (closures_df['Start_Date'] <= date) &
        (date <= closures_df['End_Date'])
    ]

    return len(match) > 0


# ── Helpers ───────────────────────────────────────────────────────────────────

def _time_to_min(time_str: str) -> int:
    """
    Convert HH:MM string to minutes since midnight.

    Raises ValueError if format is invalid.
    """
    parts = time_str.split(':')
    if len(parts) != 2:
        raise ValueError(f"Invalid time format: {time_str}")
    h, m = int(parts[0]), int(parts[1])
    return h * 60 + m
