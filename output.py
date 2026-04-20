"""
output.py
=========
Generates the two deliverables drivers and dispatchers actually use:
  1. sk_weekly_schedule.xlsx  — formatted per-truck/per-day route sheets
  2. sk_route_map.html        — interactive Folium map

Both accept a {day_index: routes_DataFrame} dict as input so they can
be regenerated at any point in the rolling horizon.
"""

import math
import time
import urllib.request
import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import folium
import numpy as np
import pandas as pd
from openpyxl import load_workbook
from openpyxl.styles import PatternFill, Font, Alignment, Border, Side
from openpyxl.utils import get_column_letter

from config import (
    DAYS, NUM_DAYS, TRUCK_NAMES, TRUCKS, DEPOT_LAT, DEPOT_LON,
    TRUCK_HEX, TRUCK_MAP_COLORS, URGENCY_FILL_COLORS, SHIFT_MIN, OUTPUT_DIR,
    DATA_DIR,
)

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# Persisted route polyline cache (so the map renders with real roads even when
# OSRM is unreachable at view time). Keyed by a hash of the waypoint sequence.
GEOM_CACHE_FILE = DATA_DIR / 'route_geom_cache.json'

# ── Excel ─────────────────────────────────────────────────────────────────────

ROUTE_COLS = [
    'Stop', 'Date', 'Travel_To_Min', 'Dist_To_mi', 'Customer', 'Zone',
    'Days_Until_Stockout', 'DaysToStockoutAtVisit', 'Urgency',
    'Tank_lbs', 'Current_lbs', 'Refill_lbs', 'Fill_Pct',
    'Avg_LbsPerDay', 'Service_Min', 'Product',
    'Route_Dist_mi', 'Route_Time_min', 'Shift_Pct', 'Cap_Pct',
]

# Display labels (shown as header row in driver sheets — more readable than raw column names)
ROUTE_COL_LABELS = {
    'Stop':                  '#',
    'Date':                  'Date',
    'Travel_To_Min':         'Drive (min)',
    'Dist_To_mi':            'Dist (mi)',
    'Customer':              'Customer',
    'Zone':                  'Zone',
    'Days_Until_Stockout':   'Days→Empty',
    'DaysToStockoutAtVisit': 'Days@Visit',
    'Urgency':               'Urgency',
    'Tank_lbs':              'Tank (lbs)',
    'Current_lbs':           'Curr (lbs)',
    'Refill_lbs':            'Refill (lbs)',
    'Fill_Pct':              'Fill %',
    'Avg_LbsPerDay':         'Avg lbs/day',
    'Service_Min':           'Svc (min)',
    'Product':               'Product',
    'Route_Dist_mi':         'Route Total (mi)',
    'Route_Time_min':        'Route (min)',
    'Shift_Pct':             'Shift %',
    'Cap_Pct':               'Cap %',
}

# Numeric formats per column (openpyxl number_format strings)
ROUTE_COL_FORMATS = {
    'Travel_To_Min':         '0',
    'Dist_To_mi':            '0.0',
    'Days_Until_Stockout':   '0.0',
    'DaysToStockoutAtVisit': '0.0',
    'Tank_lbs':              '#,##0',
    'Current_lbs':           '#,##0',
    'Refill_lbs':            '#,##0',
    'Fill_Pct':              '0.0"%"',
    'Avg_LbsPerDay':         '0.0',
    'Service_Min':           '0',
    'Route_Dist_mi':         '0.0',
    'Route_Time_min':        '0',
    'Shift_Pct':             '0.0"%"',
    'Cap_Pct':               '0.0"%"',
}


def save_excel_schedule(
    all_routes:  Dict[int, pd.DataFrame],
    deferred_df: Optional[pd.DataFrame] = None,
    filename:    str = 'sk_weekly_schedule.xlsx',
    output_dir:  Path = OUTPUT_DIR,
    plan_dates:  Optional[List[pd.Timestamp]] = None,
    today:       Optional[pd.Timestamp] = None,
) -> Path:
    """Write a styled Excel workbook."""
    filepath = output_dir / filename

    # Combine all route data for the All_Routes sheet
    all_data = [r for r in all_routes.values() if r is not None and not r.empty]
    combined = pd.concat(all_data, ignore_index=True) if all_data else pd.DataFrame()

    # Build summary (manual loop — avoids pandas 3.x groupby.apply breakage
    # where grouping columns are excluded from the group by default)
    if not combined.empty:
        rows = []
        for (truck, day), grp in combined.groupby(['Truck', 'Day']):
            rows.append(_day_summary(grp, truck, day))
        summary = pd.DataFrame(rows)
    else:
        summary = pd.DataFrame()

    with pd.ExcelWriter(str(filepath), engine='openpyxl') as writer:
        # Sheet 0: Plan info (run metadata)
        run_ts = pd.Timestamp.now().strftime('%Y-%m-%d %H:%M')
        plan_info_rows = [{'Field': 'Generated', 'Value': run_ts}]
        if today is not None:
            plan_info_rows.append({'Field': 'Solving as-of', 'Value': today.strftime('%A %b %d, %Y')})
        if plan_dates:
            plan_info_rows.append({'Field': 'Plan start', 'Value': plan_dates[0].strftime('%A %b %d, %Y')})
            plan_info_rows.append({'Field': 'Plan end',   'Value': plan_dates[-1].strftime('%A %b %d, %Y')})
            plan_info_rows.append({'Field': 'Delivery days', 'Value': len(plan_dates)})
            for i, dt in enumerate(plan_dates):
                plan_info_rows.append({'Field': f'  Day {i+1}', 'Value': dt.strftime('%a %b %d, %Y')})
        pd.DataFrame(plan_info_rows).to_excel(writer, sheet_name='0_Plan_Info', index=False)

        # Sheet 1: Week summary
        if not summary.empty:
            summary.to_excel(writer, sheet_name='1_Week_Summary', index=False)

        # Per-truck per-day sheets
        for d in range(NUM_DAYS):
            routes = all_routes.get(d)
            if routes is None or routes.empty:
                continue

            # Resolve day label and date string from plan_dates
            if plan_dates and d < len(plan_dates):
                _day_short = plan_dates[d].strftime('%a')
                _date_str  = plan_dates[d].strftime('%b %d')      # e.g. "Apr 17"
                _date_tag  = plan_dates[d].strftime('%b%d')        # e.g. "Apr17"
            else:
                _day_short = DAYS[d]
                _date_str  = ''
                _date_tag  = ''

            for truck in TRUCK_NAMES:
                sub = routes[routes['Truck'] == truck].sort_values('Stop')
                if sub.empty:
                    continue
                # Sheet name: "Truck2_Fri_Apr17" (max 31 chars, Excel limit)
                sn = f'{truck}_{_day_short}_{_date_tag}' if _date_tag else f'{truck}_{_day_short}'
                sn = sn[:31]

                # ── Loading manifest (2 rows above the route table) ──────
                banner_label = f'{truck} — {_day_short} {_date_str}'.strip()
                comp_a_prod = sub['Comp_A_Product'].iloc[0] if 'Comp_A_Product' in sub.columns else '—'
                comp_a_lbs  = sub['Comp_A_lbs'].iloc[0]    if 'Comp_A_lbs'     in sub.columns else 0
                comp_b_prod = sub['Comp_B_Product'].iloc[0] if 'Comp_B_Product' in sub.columns else '—'
                comp_b_lbs  = sub['Comp_B_lbs'].iloc[0]    if 'Comp_B_lbs'     in sub.columns else 0
                total_load  = int(sub['Refill_lbs'].sum())

                manifest = pd.DataFrame([
                    {
                        'LOADING MANIFEST': banner_label,
                        'Compartment A': f'{comp_a_prod}  {comp_a_lbs:,} lbs',
                        'Compartment B': f'{comp_b_prod}  {comp_b_lbs:,} lbs' if comp_b_lbs > 0 else '—',
                        'Total Load': f'{total_load:,} lbs',
                        'Stops': len(sub),
                    }
                ])
                manifest.to_excel(writer, sheet_name=sn, index=False, startrow=0)

                # Route table starts 3 rows below manifest
                cols = [c for c in ROUTE_COLS if c in sub.columns]
                sub[cols].reset_index(drop=True).to_excel(
                    writer, sheet_name=sn, index=False, startrow=3
                )

        # All routes flat
        if not combined.empty:
            combined.to_excel(writer, sheet_name='All_Routes', index=False)

        # Deferred
        if deferred_df is not None and not deferred_df.empty:
            deferred_df.to_excel(writer, sheet_name='Deferred', index=False)

    # Post-process: apply styling
    _apply_excel_styles(filepath, all_routes)

    print(f"  Excel saved: {filepath}")
    return filepath


def _day_summary(group, truck=None, day=None) -> pd.Series:
    # Grouping columns may be absent in pandas 3.x groupby.apply; accept them
    # as explicit args when available and fall back to group lookup otherwise.
    if truck is None:
        truck = group['Truck'].iloc[0]
    if day is None:
        day = group['Day'].iloc[0]
    first = group.sort_values('Stop').iloc[0]
    last  = group.sort_values('Stop').iloc[-1]
    load  = group['Refill_lbs'].sum()

    comp_a = (f"{first.get('Comp_A_Product','—')}  {int(first.get('Comp_A_lbs',0)):,} lbs"
              if 'Comp_A_Product' in first.index else '—')
    comp_b_lbs = int(first.get('Comp_B_lbs', 0)) if 'Comp_B_lbs' in first.index else 0
    comp_b = (f"{first.get('Comp_B_Product','—')}  {comp_b_lbs:,} lbs"
              if 'Comp_B_Product' in first.index and comp_b_lbs > 0 else '—')

    date_val = group['Date'].iloc[0] if 'Date' in group.columns else ''
    return pd.Series({
        'Truck':         truck,
        'Day':           day,
        'Date':          date_val,
        'Stops':         len(group),
        'Load_lbs':      int(load),
        'Compartment_A': comp_a,
        'Compartment_B': comp_b,
        'Dist_mi':       round(float(first.get('Route_Dist_mi', 0)), 1),
        'Time_min':      first.get('Route_Time_min', 0),
        'Cap_Pct':       round(load / TRUCKS[truck]['capacity_lbs'] * 100, 1),
        'Shift_Pct':     round(first.get('Route_Time_min', 0) / SHIFT_MIN * 100, 1),
        'Avg_Fill_Pct':  round(group['Fill_Pct'].mean(), 1),
    })


def _apply_excel_styles(filepath: Path, all_routes: Dict[int, pd.DataFrame]) -> None:
    wb  = load_workbook(str(filepath))
    bd  = Border(*[Side(style='thin', color='FFBBBBBB')] * 4)

    # Build a lookup: sheet_name → (truck, day_label, sub_df)
    # We iterate wb.sheetnames and match truck prefix to find the per-day sheets.
    for d in range(NUM_DAYS):
        routes = all_routes.get(d)
        if routes is None or routes.empty:
            continue
        for truck in TRUCK_NAMES:
            sub = routes[routes['Truck'] == truck].sort_values('Stop')
            if sub.empty:
                continue
            # Find the matching sheet: it starts with "{truck}_"
            # and was the one written for this day index
            day_label = sub['Day'].iloc[0] if 'Day' in sub.columns else DAYS[d]
            matching = [s for s in wb.sheetnames if s.startswith(f'{truck}_')]
            # Pick the sheet that contains the day label
            sn = None
            for candidate in matching:
                if f'_{day_label}' in candidate:
                    sn = candidate
                    break
            if sn is None:
                # Fallback: legacy naming
                sn = f'{truck}_{DAYS[d]}'
            if sn not in wb.sheetnames:
                continue
            ws  = wb[sn]
            _style_route_sheet(ws, truck, day_label, sub, bd)

    # Style summary sheet (try new name first, fall back to old)
    summary_sn = '1_Week_Summary' if '1_Week_Summary' in wb.sheetnames else '0_Week_Summary'
    if summary_sn in wb.sheetnames:
        ws0 = wb[summary_sn]
        dark = PatternFill('solid', fgColor='FF2C3E50')
        for cell in ws0[1]:
            cell.fill = dark
            cell.font = Font(name='Arial', bold=True, color='FFFFFFFF', size=10)
        for col in ws0.columns:
            ws0.column_dimensions[get_column_letter(col[0].column)].width = 13

    # Style Plan_Info sheet
    if '0_Plan_Info' in wb.sheetnames:
        ws_pi = wb['0_Plan_Info']
        dark = PatternFill('solid', fgColor='FF2C3E50')
        for cell in ws_pi[1]:
            cell.fill = dark
            cell.font = Font(name='Arial', bold=True, color='FFFFFFFF', size=10)
        ws_pi.column_dimensions['A'].width = 18
        ws_pi.column_dimensions['B'].width = 28

    wb.save(str(filepath))


def _style_route_sheet(ws, truck: str, day: str, sub: pd.DataFrame, bd) -> None:
    hfill = PatternFill('solid', fgColor=TRUCK_HEX.get(truck, 'FF333333'))
    load  = int(sub['Refill_lbs'].sum())
    dist  = float(sub['Route_Dist_mi'].iloc[-1]) if len(sub) else 0.0
    mins  = sub['Route_Time_min'].iloc[0] if len(sub) else 0
    hrs, m = divmod(int(mins), 60)
    cap_p  = round(load / TRUCKS[truck]['capacity_lbs'] * 100, 1)

    ws.insert_rows(1)
    ws.merge_cells(
        start_row=1, start_column=1,
        end_row=1, end_column=ws.max_column
    )
    hdr = ws.cell(1, 1,
        value=f"{truck}  |  {day}  |  {len(sub)} stops  |  "
              f"{load:,} lbs ({cap_p}%)  |  {dist:.1f} mi  |  {hrs}h {m:02d}m"
    )
    hdr.font      = Font(name='Arial', bold=True, color='FFFFFFFF', size=12)
    hdr.fill      = hfill
    hdr.alignment = Alignment(horizontal='left', vertical='center')
    ws.row_dimensions[1].height = 22

    # Layout after insert_rows(1):
    #   Row 1: banner (just set)
    #   Row 2: manifest column headers
    #   Row 3: manifest data
    #   Row 4: blank
    #   Row 5: route table column headers
    #   Row 6+: route data

    # Manifest header styling (row 2)
    manifest_hdr_fill = PatternFill('solid', fgColor='FFD9DEE3')
    for cell in ws[2]:
        cell.fill      = manifest_hdr_fill
        cell.font      = Font(name='Arial', bold=True, color='FF2C3E50', size=10)
        cell.alignment = Alignment(horizontal='left', vertical='center')
    ws.row_dimensions[2].height = 18

    # Manifest data row styling (row 3)
    for cell in ws[3]:
        cell.font      = Font(name='Arial', size=10)
        cell.alignment = Alignment(horizontal='left', vertical='center')

    # Determine which columns the route table actually wrote (in order)
    route_cols_present = [c for c in ROUTE_COLS if c in sub.columns]
    n_route_cols = len(route_cols_present)

    # Route table column headers (row 5) — apply friendly labels
    for ci, col_name in enumerate(route_cols_present, start=1):
        cell = ws.cell(5, ci)
        cell.value     = ROUTE_COL_LABELS.get(col_name, col_name)
        cell.fill      = PatternFill('solid', fgColor='FF2C3E50')
        cell.font      = Font(name='Arial', bold=True, color='FFFFFFFF', size=9)
        cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
        cell.border    = bd
    ws.row_dimensions[5].height = 28

    # Data rows — colour by urgency tier
    urgency_fills = {
        'stockout': PatternFill('solid', fgColor=URGENCY_FILL_COLORS['stockout']),
        'critical': PatternFill('solid', fgColor=URGENCY_FILL_COLORS['critical']),
        'urgent':   PatternFill('solid', fgColor=URGENCY_FILL_COLORS['urgent']),
        'normal':   PatternFill('solid', fgColor=URGENCY_FILL_COLORS['normal']),
    }
    # Columns that should be left-aligned (Customer, Zone, Product — by name)
    left_align_cols = {'Customer', 'Product'}

    for ri, (_, row) in enumerate(sub.iterrows(), start=6):
        tier = str(row.get('Urgency', 'normal')).lower()
        fill = urgency_fills.get(tier, urgency_fills['normal'])
        for ci, col_name in enumerate(route_cols_present, start=1):
            c         = ws.cell(ri, ci)
            c.fill    = fill
            c.border  = bd
            c.font    = Font(name='Arial', size=9)
            c.alignment = Alignment(
                horizontal='left' if col_name in left_align_cols else 'center',
                vertical='center',
            )
            if col_name in ROUTE_COL_FORMATS:
                c.number_format = ROUTE_COL_FORMATS[col_name]

    # Column widths keyed by canonical column name (works regardless of column order)
    col_width_by_name = {
        'Stop':                  5,
        'Date':                  12,
        'Travel_To_Min':         10,
        'Dist_To_mi':            9,
        'Customer':              34,
        'Zone':                  6,
        'Days_Until_Stockout':   11,
        'DaysToStockoutAtVisit': 11,
        'Urgency':               10,
        'Tank_lbs':              10,
        'Current_lbs':           10,
        'Refill_lbs':            11,
        'Fill_Pct':              8,
        'Avg_LbsPerDay':         11,
        'Service_Min':           9,
        'Product':               16,
        'Route_Dist_mi':         12,
        'Route_Time_min':        10,
        'Shift_Pct':             8,
        'Cap_Pct':               8,
    }
    for ci, col_name in enumerate(route_cols_present, start=1):
        ws.column_dimensions[get_column_letter(ci)].width = col_width_by_name.get(col_name, 10)
    # Pad remaining columns if manifest is wider than route table (rare)
    for ci in range(n_route_cols + 1, ws.max_column + 1):
        ws.column_dimensions[get_column_letter(ci)].width = 14

    # Freeze top area so header stays visible while scrolling
    ws.freeze_panes = 'A6'


# ── Interactive road-geometry map (server-side OSRM fetch) ─────────────────────

import hashlib
import requests as _requests


def _waypoint_cache_key(waypoints: list) -> str:
    """Stable hash of a waypoint sequence. 5-decimal lat/lon so trivial
    rounding doesn't invalidate the cache."""
    s = ';'.join(f'{w["lat"]:.5f},{w["lon"]:.5f}' for w in waypoints)
    return hashlib.sha1(s.encode('utf-8')).hexdigest()


def _load_geom_cache() -> dict:
    if GEOM_CACHE_FILE.exists():
        try:
            return json.loads(GEOM_CACHE_FILE.read_text(encoding='utf-8'))
        except Exception as exc:
            print(f"    ⚠  polyline cache unreadable ({exc}); starting fresh")
    return {}


def _save_geom_cache(cache: dict) -> None:
    try:
        GEOM_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = GEOM_CACHE_FILE.with_suffix('.tmp.json')
        tmp.write_text(json.dumps(cache), encoding='utf-8')
        tmp.replace(GEOM_CACHE_FILE)
    except Exception as exc:
        print(f"    ⚠  could not persist polyline cache: {exc}")


def _osrm_route_line(coords: list) -> list:
    """Fetch road geometry from OSRM.  coords = [(lat, lon), ...].
    Returns [[lat, lon], ...] for Leaflet.  Falls back to straight lines."""
    if len(coords) < 2:
        return [[la, lo] for la, lo in coords]
    if len(coords) > 98:              # OSRM max-waypoints guard
        coords = coords[:98]
    cs = ';'.join(f'{lo:.5f},{la:.5f}' for la, lo in coords)
    try:
        r = _requests.get(
            f'https://router.project-osrm.org/route/v1/driving/{cs}'
            f'?overview=full&geometries=geojson',
            timeout=20,
            headers={'User-Agent': 'sk-optimizer/1.0'},
        )
        r.raise_for_status()
        d = r.json()
        if d.get('code') == 'Ok':
            return [[p[1], p[0]] for p in d['routes'][0]['geometry']['coordinates']]
    except Exception as exc:
        print(f"    OSRM fallback (straight lines): {exc}")
    return [[la, lo] for la, lo in coords]


def save_route_map(
    all_routes:  Dict[int, pd.DataFrame],
    filename:    str = 'sk_route_map.html',
    output_dir:  Path = OUTPUT_DIR,
    use_osrm:    bool = True,
) -> Path:
    """
    Generate a standalone HTML map with real road-geometry routes.

    Road paths are fetched from OSRM at generation time (server-side) and
    baked into the HTML — the browser does zero async network calls, so the
    map renders instantly.
    """
    filepath = output_dir / filename

    # ── Load persisted polyline cache ─────────────────────────────────────
    geom_cache   = _load_geom_cache()
    cache_hits   = 0
    cache_misses = 0
    cache_fails  = 0

    # ── Collect all routes into a JS-serialisable structure ───────────────
    route_groups = []   # list of dicts, one per (truck, day)

    for d in range(NUM_DAYS):
        routes = all_routes.get(d)
        if routes is None or (hasattr(routes, 'empty') and routes.empty):
            continue
        for truck in TRUCK_NAMES:
            sub = routes[routes['Truck'] == truck].sort_values('Stop')
            if sub.empty:
                continue

            color = TRUCK_MAP_COLORS[truck][d % len(TRUCK_MAP_COLORS[truck])]

            # Waypoints: depot → stops → depot
            waypoints = (
                [{'lat': DEPOT_LAT, 'lon': DEPOT_LON, 'label': 'Depot'}]
                + [
                    {
                        'lat':     float(r['Lat']),
                        'lon':     float(r['Lon']),
                        'stop':    int(r['Stop']),
                        'label':   str(r['Customer'])[:35],
                        'zone':    str(r.get('Zone', '')),
                        'refill':  int(r['Refill_lbs']),
                        'fill_pct': round(float(r.get('Fill_Pct', 0)), 1),
                        'days_left': round(float(r.get('DaysToStockoutAtVisit', 0)), 1),
                        'travel_min': int(r['Travel_To_Min']),
                        'dist_mi':  round(float(r.get('Dist_To_mi', 0)), 1),
                        'urgency':  str(r.get('Urgency', 'normal')).lower(),
                    }
                    for _, r in sub.iterrows()
                ]
                + [{'lat': DEPOT_LAT, 'lon': DEPOT_LON, 'label': 'Depot'}]
            )

            load      = int(sub['Refill_lbs'].sum())
            cap_pct   = round(load / TRUCKS[truck]['capacity_lbs'] * 100, 1)
            dist      = round(float(sub['Route_Dist_mi'].iloc[-1]), 1)
            time_min  = int(sub['Route_Time_min'].iloc[0])
            hrs, mins = divmod(time_min, 60)

            # ── Resolve road-geometry polyline via cache + OSRM fallback ──
            key = _waypoint_cache_key(waypoints)
            polyline = geom_cache.get(key)
            from_cache = polyline is not None
            if polyline is None and use_osrm:
                coords = [(w['lat'], w['lon']) for w in waypoints]
                polyline = _osrm_route_line(coords)
                # Heuristic: straight-line fallback returns exactly len(waypoints)
                # points. Only cache responses that look like real road geometry.
                if polyline and len(polyline) > len(waypoints):
                    geom_cache[key] = polyline
                    cache_misses += 1
                else:
                    cache_fails += 1
            elif from_cache:
                cache_hits += 1
            if polyline is None:
                polyline = [[w['lat'], w['lon']] for w in waypoints]

            route_groups.append({
                'id':        f"{truck}_{DAYS[d]}",
                'truck':     truck,
                'day':       DAYS[d],
                'color':     color,
                'label':     f"{truck} – {DAYS[d]}",
                'summary':   f"{len(sub)} stops · {load:,} lbs ({cap_pct}%) · {dist} mi · {hrs}h {mins:02d}m",
                'waypoints': waypoints,
                'polyline':  polyline,
                'from_cache': from_cache,
            })

    # ── Persist updated polyline cache ─────────────────────────────────────
    if cache_misses:
        _save_geom_cache(geom_cache)
    print(f"  Polyline cache: {cache_hits} hit, {cache_misses} fetched, "
          f"{cache_fails} fallback ({len(geom_cache)} total cached)")

    # ── Render HTML ────────────────────────────────────────────────────────
    html = _render_map_html(route_groups)
    filepath.write_text(html, encoding='utf-8')
    print(f"  Map saved:   {filepath}")
    return filepath


def _render_map_html(route_groups: list) -> str:
    import json as _json

    routes_js = _json.dumps(route_groups, ensure_ascii=False)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>S&amp;K Oil — Route Dispatch</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>

<style>
:root {{
  --bg: #ffffff;
  --panel: #f8f9fb;
  --panel-hover: #eef0f4;
  --panel-active: #e4e7ed;
  --accent: #d4542b;
  --blue: #2563eb;
  --text: #1e293b;
  --muted: #64748b;
  --border: #e2e8f0;
}}
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Inter', sans-serif;
       display: flex; height: 100vh; overflow: hidden; background: var(--bg); color: var(--text); }}

/* ── Sidebar ────────────────────────────────── */
#sidebar {{
    width: 320px; min-width: 320px; background: var(--bg);
    display: flex; flex-direction: column; overflow: hidden;
    border-right: 1px solid var(--border);
    box-shadow: 2px 0 12px rgba(0,0,0,.06);
    z-index: 1000;
}}
#sidebar-header {{
    padding: 20px 20px 16px; border-bottom: 1px solid var(--border);
}}
#sidebar-header h1 {{
    font-size: 13px; font-weight: 800; letter-spacing: 2px; text-transform: uppercase;
    color: var(--accent); margin-bottom: 2px;
}}
#sidebar-header h2 {{ font-size: 18px; color: var(--text); font-weight: 700; }}

/* Stats bar */
#stats-bar {{
    display: flex; gap: 0; border-bottom: 1px solid var(--border);
}}
.stat-cell {{
    flex: 1; text-align: center; padding: 10px 4px;
    border-right: 1px solid var(--border);
}}
.stat-cell:last-child {{ border-right: none; }}
.stat-num {{ font-size: 18px; font-weight: 800; color: var(--text); }}
.stat-label {{ font-size: 9px; text-transform: uppercase; letter-spacing: 0.5px; color: var(--muted); margin-top: 1px; }}

/* Day filter */
#day-filter {{
    display: flex; gap: 4px; padding: 10px 16px; border-bottom: 1px solid var(--border);
    flex-wrap: wrap;
}}
.day-btn {{
    flex: 1; padding: 6px 2px; border: 1px solid var(--border); border-radius: 6px;
    background: transparent; color: var(--muted); font-size: 11px; font-weight: 600;
    cursor: pointer; transition: all .15s; text-align: center; min-width: 48px;
}}
.day-btn:hover {{ border-color: var(--accent); color: var(--text); }}
.day-btn.active {{ background: var(--accent); border-color: var(--accent); color: white; }}

/* Truck filter */
#truck-filter {{
    display: flex; gap: 4px; padding: 6px 16px 10px; border-bottom: 1px solid var(--border);
}}
.truck-btn {{
    flex: 1; padding: 5px 8px; border: 1px solid var(--border); border-radius: 6px;
    background: transparent; color: var(--muted); font-size: 11px; font-weight: 600;
    cursor: pointer; transition: all .15s; text-align: center;
}}
.truck-btn:hover {{ border-color: #4a90d9; color: var(--text); }}
.truck-btn.active {{ background: #eff6ff; border-color: var(--blue); color: var(--blue); }}
.truck-btn[data-truck="Truck9"].active {{ background: #fef2f2; border-color: var(--accent); color: var(--accent); }}
.truck-btn.all-btn.active {{ background: var(--panel-active); border-color: var(--muted); color: var(--text); }}

/* Route list */
#route-list {{ overflow-y: auto; flex: 1; }}

.route-card {{
    padding: 12px 16px; cursor: pointer; border-bottom: 1px solid var(--border);
    border-left: 4px solid transparent; transition: all .15s; opacity: 0.4;
}}
.route-card.visible {{ opacity: 1; }}
.route-card:hover {{ background: var(--panel-hover); }}
.route-card.focused {{ background: var(--panel-active); }}

.rc-header {{
    display: flex; align-items: center; justify-content: space-between; margin-bottom: 4px;
}}
.rc-title {{
    font-size: 13px; font-weight: 700; display: flex; align-items: center; gap: 8px;
}}
.rc-badge {{
    font-size: 9px; font-weight: 700; padding: 2px 6px; border-radius: 4px;
    text-transform: uppercase; letter-spacing: 0.5px;
}}
.rc-stats {{
    display: flex; gap: 12px; font-size: 11px; color: var(--muted);
}}
.rc-stats span {{ display: flex; align-items: center; gap: 3px; }}

/* Legend */
#legend-bar {{
    display: flex; align-items: center; gap: 12px; padding: 10px 16px;
    border-top: 1px solid var(--border); font-size: 10px; color: var(--muted);
    flex-wrap: wrap;
}}
.leg-item {{ display: flex; align-items: center; gap: 4px; }}
.leg-swatch {{
    width: 8px; height: 8px; border-radius: 50%;
}}

/* Map */
#map {{ flex: 1; }}

/* Popup */
.leaflet-popup-content-wrapper {{
    background: #ffffff !important; color: var(--text) !important;
    border-radius: 10px !important; box-shadow: 0 8px 30px rgba(0,0,0,.12) !important;
    border: 1px solid var(--border) !important;
}}
.leaflet-popup-tip {{ background: #ffffff !important; }}
.leaflet-popup-close-button {{ color: var(--muted) !important; font-size: 18px !important; }}

.sk-popup {{ min-width: 220px; }}
.sk-popup .pop-header {{
    display: flex; align-items: center; gap: 8px; margin-bottom: 8px;
    padding-bottom: 8px; border-bottom: 1px solid var(--border);
}}
.sk-popup .pop-num {{
    width: 28px; height: 28px; border-radius: 50%; display: flex;
    align-items: center; justify-content: center; font-size: 12px;
    font-weight: 800; color: white; flex-shrink: 0;
}}
.sk-popup .pop-name {{ font-size: 12px; font-weight: 700; line-height: 1.3; }}
.sk-popup .pop-route {{ font-size: 10px; color: var(--muted); }}
.sk-popup .pop-grid {{
    display: grid; grid-template-columns: 1fr 1fr; gap: 6px;
}}
.sk-popup .pop-metric {{
    background: #f1f5f9; border-radius: 6px; padding: 6px 8px;
}}
.sk-popup .pop-metric .pm-val {{ font-size: 14px; font-weight: 800; }}
.sk-popup .pop-metric .pm-label {{ font-size: 9px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.3px; }}
.sk-popup .pop-urg {{
    display: inline-block; margin-top: 6px; padding: 2px 8px; border-radius: 4px;
    font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px;
}}

/* Tooltip */
.leaflet-tooltip {{
    background: #1e293b !important; color: #ffffff !important;
    border: none !important; border-radius: 6px !important;
    font-size: 11px !important; font-weight: 600 !important; padding: 4px 8px !important;
    box-shadow: 0 4px 12px rgba(0,0,0,.2) !important;
}}
.leaflet-tooltip-top:before {{ border-top-color: #1e293b !important; }}
.leaflet-tooltip-bottom:before {{ border-bottom-color: #1e293b !important; }}
</style>
</head>
<body>

<div id="sidebar">
  <div id="sidebar-header">
    <h1>S&amp;K Oil Sales</h1>
    <h2>Route Dispatch</h2>
  </div>

  <div id="stats-bar"></div>

  <div id="day-filter"></div>
  <div id="truck-filter"></div>

  <div id="route-list"></div>

  <div id="legend-bar">
    <span style="font-weight:700;color:var(--text)">Urgency:</span>
    <div class="leg-item"><div class="leg-swatch" style="background:#b03030"></div> Stockout</div>
    <div class="leg-item"><div class="leg-swatch" style="background:#c47a1a"></div> Critical</div>
    <div class="leg-item"><div class="leg-swatch" style="background:#9e8a1e"></div> Urgent</div>
    <div class="leg-item"><div class="leg-swatch" style="background:#2a8a4a"></div> Normal</div>
  </div>
</div>

<div id="map"></div>

<script>
var ROUTES     = {routes_js};
var DEPOT_LAT  = {DEPOT_LAT};
var DEPOT_LON  = {DEPOT_LON};

// Muted urgency palette — easy on the eyes on a light map
var URG = {{
    stockout: {{ color: '#b03030', bg: 'rgba(176,48,48,.12)', label: 'STOCKOUT' }},
    critical: {{ color: '#c47a1a', bg: 'rgba(196,122,26,.12)', label: 'CRITICAL' }},
    urgent:   {{ color: '#9e8a1e', bg: 'rgba(158,138,30,.12)',  label: 'URGENT' }},
    normal:   {{ color: '#2a8a4a', bg: 'rgba(42,138,74,.12)',  label: 'NORMAL' }},
}};

// ── Map ─────────────────────────────────────────────
var map = L.map('map', {{ center: [33.48, -112.00], zoom: 11, zoomControl: false }});
L.control.zoom({{ position: 'topright' }}).addTo(map);

L.tileLayer('https://{{s}}.basemaps.cartocdn.com/rastertiles/voyager/{{z}}/{{x}}/{{y}}{{r}}.png', {{
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> &copy; <a href="https://carto.com/">CARTO</a>',
    maxZoom: 19, subdomains: 'abcd'
}}).addTo(map);

// Depot
L.marker([DEPOT_LAT, DEPOT_LON], {{
    icon: L.divIcon({{
        html: '<div style="width:36px;height:36px;background:#d4542b;border-radius:50%;'
            + 'display:flex;align-items:center;justify-content:center;font-size:16px;'
            + 'border:3px solid white;box-shadow:0 0 16px rgba(212,84,43,.35),0 4px 12px rgba(0,0,0,.2);">'
            + '<svg width="16" height="16" viewBox="0 0 24 24" fill="white"><path d="M10 20v-6h4v6h5v-8h3L12 3 2 12h3v8z"/></svg></div>',
        iconSize: [36, 36], iconAnchor: [18, 18], className: ''
    }}),
    zIndexOffset: 1000,
}}).bindTooltip('S&K Depot', {{ direction: 'top', offset: [0, -20] }}).addTo(map);

// ── State ───────────────────────────────────────────
var layerGroups = {{}};
var focusedRoute = null;
var activeDays   = new Set(['Tue','Wed','Thu','Fri','Sat']);
var activeTrucks = new Set(['Truck2','Truck9']);
var allDays = ['Tue','Wed','Thu','Fri','Sat'];

// ── Route polylines (pre-baked server-side from OSRM + disk cache) ──
// group.polyline is [[lat, lon], ...] — ready to render, no network needed.
// If the polyline has more points than waypoints it's real road geometry;
// otherwise it's the straight-line fallback and gets a dashed style.
function addRoadLine(group) {{
    var poly = group.polyline || group.waypoints.map(function(w) {{ return [w.lat, w.lon]; }});
    var isRoad = poly.length > group.waypoints.length;
    var style = isRoad
        ? {{ color: group.color, weight: 4, opacity: 0.85 }}
        : {{ color: group.color, weight: 3, opacity: 0.6, dashArray: '8 6' }};
    L.polyline(poly, style).addTo(layerGroups[group.id]);
}}

// ── Markers ─────────────────────────────────────────
function buildMarkers(group) {{
    var lg = layerGroups[group.id];
    var stops = group.waypoints.filter(function(w) {{ return w.stop !== undefined; }});
    stops.forEach(function(w) {{
        var u = URG[w.urgency] || URG.normal;
        var sz = w.urgency === 'critical' || w.urgency === 'stockout' ? 28 : 24;
        var fs = sz === 28 ? 11 : 10;
        var glow = w.urgency === 'critical' || w.urgency === 'stockout'
            ? 'box-shadow:0 0 6px ' + u.color + '88,0 2px 4px rgba(0,0,0,.18);'
            : 'box-shadow:0 2px 4px rgba(0,0,0,.12);';
        var icon = L.divIcon({{
            html: '<div style="width:'+sz+'px;height:'+sz+'px;background:'+group.color+';'
                + 'border-radius:50%;display:flex;align-items:center;justify-content:center;'
                + 'font-size:'+fs+'px;font-weight:800;color:white;'
                + 'border:2.5px solid '+u.color+';'+glow+'">' + w.stop + '</div>',
            iconSize: [sz, sz], iconAnchor: [sz/2, sz/2], className: ''
        }});

        var popup = '<div class="sk-popup">'
            + '<div class="pop-header">'
            + '<div class="pop-num" style="background:'+group.color+'">'+w.stop+'</div>'
            + '<div><div class="pop-name">'+w.label+'</div>'
            + '<div class="pop-route">'+group.truck+' \\u00b7 '+group.day+' \\u00b7 Zone '+w.zone+'</div></div>'
            + '</div>'
            + '<div class="pop-grid">'
            + '<div class="pop-metric"><div class="pm-val">'+w.refill.toLocaleString()+'</div><div class="pm-label">Lbs Refill</div></div>'
            + '<div class="pop-metric"><div class="pm-val">'+w.fill_pct+'%</div><div class="pm-label">Fill Efficiency</div></div>'
            + '<div class="pop-metric"><div class="pm-val">'+w.days_left+'d</div><div class="pm-label">To Stockout</div></div>'
            + '<div class="pop-metric"><div class="pm-val">'+w.travel_min+'m</div><div class="pm-label">Drive Time</div></div>'
            + '</div>'
            + '<div class="pop-urg" style="background:'+u.bg+';color:'+u.color+'">'+u.label+'</div>'
            + '</div>';

        L.marker([w.lat, w.lon], {{ icon: icon, zIndexOffset: w.urgency === 'critical' || w.urgency === 'stockout' ? 500 : 0 }})
         .bindPopup(popup, {{ maxWidth: 280, className: '' }})
         .bindTooltip('#'+w.stop+' '+w.label.substring(0,28), {{ direction: 'top', offset: [0, -sz/2 - 4] }})
         .addTo(lg);
    }});
}}

// ── Stats bar ───────────────────────────────────────
var totalStops = 0, totalLbs = 0, totalMi = 0;
ROUTES.forEach(function(g) {{
    var stops = g.waypoints.filter(function(w){{ return w.stop !== undefined; }});
    totalStops += stops.length;
    stops.forEach(function(w){{ totalLbs += w.refill; }});
}});
// Compute total miles from summary text
ROUTES.forEach(function(g) {{
    var m = g.summary.match(/([\\d.]+) mi/);
    if (m) totalMi += parseFloat(m[1]);
}});

document.getElementById('stats-bar').innerHTML =
    '<div class="stat-cell"><div class="stat-num">'+totalStops+'</div><div class="stat-label">Stops</div></div>'
  + '<div class="stat-cell"><div class="stat-num">'+Math.round(totalLbs/1000)+'k</div><div class="stat-label">Lbs</div></div>'
  + '<div class="stat-cell"><div class="stat-num">'+ROUTES.length+'</div><div class="stat-label">Routes</div></div>'
  + '<div class="stat-cell"><div class="stat-num">'+Math.round(totalMi)+'</div><div class="stat-label">Miles</div></div>';

// ── Day filter buttons ──────────────────────────────
var dayBar = document.getElementById('day-filter');
var allDayBtn = document.createElement('button');
allDayBtn.className = 'day-btn active';
allDayBtn.textContent = 'All';
allDayBtn.addEventListener('click', function() {{
    activeDays = new Set(allDays);
    updateFilters();
}});
dayBar.appendChild(allDayBtn);

allDays.forEach(function(day) {{
    var btn = document.createElement('button');
    btn.className = 'day-btn active';
    btn.textContent = day;
    btn.dataset.day = day;
    btn.addEventListener('click', function() {{
        if (activeDays.size === 5 || (activeDays.size === 1 && activeDays.has(day))) {{
            activeDays = new Set([day]);
        }} else if (activeDays.has(day)) {{
            activeDays.delete(day);
            if (activeDays.size === 0) activeDays = new Set(allDays);
        }} else {{
            activeDays.add(day);
        }}
        updateFilters();
    }});
    dayBar.appendChild(btn);
}});

// ── Truck filter buttons ────────────────────────────
var truckBar = document.getElementById('truck-filter');
['All', 'Truck2', 'Truck9'].forEach(function(t) {{
    var btn = document.createElement('button');
    btn.className = 'truck-btn' + (t === 'All' ? ' all-btn active' : ' active');
    btn.textContent = t === 'All' ? 'Both Trucks' : t.replace('ruck', '');
    btn.dataset.truck = t;
    btn.addEventListener('click', function() {{
        if (t === 'All') {{
            activeTrucks = new Set(['Truck2', 'Truck9']);
        }} else {{
            if (activeTrucks.size === 2) {{
                activeTrucks = new Set([t]);
            }} else if (activeTrucks.has(t) && activeTrucks.size === 1) {{
                activeTrucks = new Set(['Truck2', 'Truck9']);
            }} else {{
                activeTrucks = new Set([t]);
            }}
        }}
        updateFilters();
    }});
    truckBar.appendChild(btn);
}});

// ── Route cards ─────────────────────────────────────
var routeList = document.getElementById('route-list');
ROUTES.forEach(function(group) {{
    var lg = L.layerGroup().addTo(map);
    layerGroups[group.id] = lg;

    // Draw road-geometry polyline FIRST (underneath markers)
    addRoadLine(group);
    // Then markers on top
    buildMarkers(group);

    var card = document.createElement('div');
    card.className = 'route-card visible';
    card.dataset.id = group.id;
    card.dataset.day = group.day;
    card.dataset.truck = group.truck;
    card.style.borderLeftColor = group.color;

    var stops = group.waypoints.filter(function(w){{ return w.stop !== undefined; }});
    var critCount = stops.filter(function(w){{ return w.urgency==='critical'||w.urgency==='stockout'; }}).length;
    var badge = critCount > 0
        ? '<span class="rc-badge" style="background:rgba(176,48,48,.12);color:#b03030">'+critCount+' crit</span>'
        : '';

    card.innerHTML =
        '<div class="rc-header">'
        + '<div class="rc-title"><span style="color:'+group.color+'">\\u25CF</span> '+group.label+'</div>'
        + badge
        + '</div>'
        + '<div class="rc-stats">'
        + '<span>\\ud83d\\udccd '+stops.length+' stops</span>'
        + '<span>\\u2696 '+group.summary.match(/[\\d,]+ lbs/)[0]+'</span>'
        + '<span>\\ud83d\\udd52 '+group.summary.match(/\\d+h \\d+m/)[0]+'</span>'
        + '</div>';

    card.addEventListener('click', function() {{
        if (focusedRoute === group.id) {{
            focusedRoute = null;
            updateFilters();
            map.setView([33.48, -112.00], 11);
        }} else {{
            focusedRoute = group.id;
            updateFilters();
            // Zoom to this route's bounds
            var coords = stops.map(function(w){{ return [w.lat, w.lon]; }});
            if (coords.length) map.fitBounds(coords, {{ padding: [60, 60] }});
        }}
    }});
    routeList.appendChild(card);
}});

// ── Filter logic ────────────────────────────────────
function updateFilters() {{
    // Update button states
    document.querySelectorAll('.day-btn').forEach(function(b) {{
        if (b.textContent === 'All') {{
            b.classList.toggle('active', activeDays.size === 5);
        }} else {{
            b.classList.toggle('active', activeDays.has(b.dataset.day));
        }}
    }});
    document.querySelectorAll('.truck-btn').forEach(function(b) {{
        if (b.dataset.truck === 'All') {{
            b.classList.toggle('active', activeTrucks.size === 2);
        }} else {{
            b.classList.toggle('active', activeTrucks.has(b.dataset.truck));
        }}
    }});

    // Show/hide routes
    ROUTES.forEach(function(group) {{
        var visible = activeDays.has(group.day) && activeTrucks.has(group.truck);
        if (focusedRoute && focusedRoute !== group.id) visible = false;

        var card = document.querySelector('[data-id="'+group.id+'"]');
        var lg = layerGroups[group.id];

        if (visible) {{
            if (!map.hasLayer(lg)) map.addLayer(lg);
            card.classList.add('visible');
        }} else {{
            if (map.hasLayer(lg)) map.removeLayer(lg);
            card.classList.remove('visible');
        }}
        card.classList.toggle('focused', focusedRoute === group.id);
    }});
}}

</script>
</body>
</html>"""
