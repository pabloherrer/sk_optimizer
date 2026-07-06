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
    anova_ids=None,
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
                     problem=problem, anova_ids=anova_ids)

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

    # Snapshot each client's PRE-RUN tank urgency so the dashboard can
    # render "what was urgent before the run" next to "what the solver
    # picked." Reasoning: once the run commits, Excel formulas re-project
    # tank state and colors shift — the operator loses the baseline they
    # used to prioritize. Freezing the bucket here preserves it forever.
    #
    # Buckets match the spreadsheet's color coding (Optimizer_Input):
    #   RED  : days-to-reserve ≤ 2
    #   YEL  : days-to-reserve ≤ 5
    #   GRN  : days-to-reserve ≤ 7
    #   GRY  : days-to-reserve > 7  (or no rate / unknown)
    extras: Dict[str, object] = {}
    if problem is not None:
        reserve_pct = float(getattr(problem, 'min_reserve_fraction', 0.10) or 0.10)
        snapshot = []
        client_by_id = {c.id: c for c in problem.clients}
        for cid, ts in problem.initial_tanks.items():
            c = client_by_id.get(cid)
            if c is None:
                continue
            rate = float(ts.rate_lbs_per_day or 0.0)
            tank = float(c.tank_capacity_lbs or 0.0)
            current = float(ts.current_lbs or 0.0)
            reserve_lbs = tank * reserve_pct
            if rate > 0 and tank > 0:
                dte_to_reserve = max(0.0, (current - reserve_lbs) / rate)
            else:
                dte_to_reserve = None
            if dte_to_reserve is None:
                bucket = 'GRY'
            elif dte_to_reserve <= 2:
                bucket = 'RED'
            elif dte_to_reserve <= 5:
                bucket = 'YEL'
            elif dte_to_reserve <= 7:
                bucket = 'GRN'
            else:
                bucket = 'GRY'
            snapshot.append({
                'client_id': cid,
                'customer':  c.customer,
                'tank_lbs':  tank,
                'current_lbs': round(current, 1),
                'rate_lbs_per_day': round(rate, 1),
                'reserve_lbs': round(reserve_lbs, 1),
                'dte_to_reserve': (round(dte_to_reserve, 2)
                                    if dte_to_reserve is not None else None),
                'urgency_bucket': bucket,
            })
        extras['pre_run_urgency'] = snapshot
        extras['pre_run_reserve_pct'] = reserve_pct

    archive_path = write_plan_archive(plan, output_dir, extras=extras)

    return {
        'excel':   excel_path,
        'csv':     csv_path,
        'map':     map_path,
        'archive': archive_path,
    }
