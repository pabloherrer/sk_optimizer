"""
v2.reporting — operator-facing artifacts from a solver Plan.

Public API:
    write_plan_excel       — multi-sheet Excel workbook (dispatcher + driver PRINT sheets)
    write_route_map        — interactive Leaflet HTML map (OSRM polylines)
    write_smartservice_csv — next-day SmartService import CSV
    write_plan_archive     — permanent JSON record
    write_all_outputs      — convenience: produces the whole bundle

Driver PDFs are no longer produced — the per-day PRINT sheets inside the
Excel workbook are landscape, fit-to-width, and stamp directly. Hand the
xlsx to the driver, print the relevant tab.
"""
from __future__ import annotations
from datetime import timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .archive import write_plan_archive
from .excel import write_plan_excel
from .map import write_route_map
from .smartservice import write_smartservice_csv

__all__ = [
    'write_plan_excel',
    'write_smartservice_csv',
    'write_route_map',
    'write_plan_archive',
    'write_all_outputs',
]


def write_all_outputs(
    plan,
    output_dir: Path,
    invariant_results: Optional[List[Tuple[str, bool, str]]] = None,
    shift_start_min: int = 360,
    problem=None,
) -> Dict[str, object]:
    """Write the full operator artifact set under `output_dir`.

    Layout
    ------
        <output_dir>/plan_<today>.xlsx
        <output_dir>/route_map_<today>.html
        <output_dir>/smartservice_<next-delivery-day>.csv
        <output_dir>/archive/plan_<today>.json

    Returns a dict mapping artifact name → path.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    today_str = plan.today.isoformat()

    excel_path = output_dir / f'plan_{today_str}.xlsx'
    write_plan_excel(plan, excel_path,
                     invariant_results=invariant_results,
                     problem=problem)

    map_path = output_dir / f'route_map_{today_str}.html'
    write_route_map(plan, map_path,
                    shift_start_min=shift_start_min,
                    problem=problem)

    # SmartService CSV: name it after the next delivery day in the horizon
    # (typically today+1 — what dispatch hands to drivers tomorrow morning).
    target_day = plan.horizon_dates[0] if plan.horizon_dates else plan.today
    csv_target = target_day if target_day != plan.today else plan.today + timedelta(days=1)
    csv_path = output_dir / f'smartservice_{csv_target.isoformat()}.csv'
    write_smartservice_csv(plan, target_day, csv_path,
                           shift_start_min=shift_start_min)

    archive_path = write_plan_archive(plan, output_dir)

    return {
        'excel':   excel_path,
        'csv':     csv_path,
        'map':     map_path,
        'archive': archive_path,
    }
