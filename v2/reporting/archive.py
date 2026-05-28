"""
v2.reporting.archive — forever-archive of the Plan as JSON.

This is the permanent record. Humans rarely open it; engineers use it for
post-mortems and the optimizer reads old archives during backtests.
"""
from __future__ import annotations
import json
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any


def _json_default(obj: Any):
    """Handle date / datetime / dataclass / tuple — everything else falls through."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, date):
        return obj.isoformat()
    if is_dataclass(obj):
        return asdict(obj)
    if isinstance(obj, tuple):
        return list(obj)
    # Final fallback — string representation. Better than crashing.
    return str(obj)


def _plan_to_dict(plan) -> dict:
    """Convert Plan (with tuple-keyed routes dict) into a JSON-safe dict."""
    d = asdict(plan)
    # The `routes` dict uses tuple keys (date, truck_id) — JSON can't serialize
    # tuple keys, so flatten to a list of records.
    d['routes'] = [
        {
            'date': rd.isoformat() if isinstance(rd, date) else str(rd),
            'truck_id': truck_id,
            'route': asdict(route),
        }
        for (rd, truck_id), route in plan.routes.items()
    ]
    return d


def write_plan_archive(plan, output_dir: Path,
                        extras: dict | None = None) -> Path:
    """
    Dump `plan` as JSON at `<output_dir>/archive/plan_<today>.json`.

    `extras` is merged into the top-level payload (non-overlapping keys
    only — Plan fields win). Useful for stashing one-time snapshots like
    pre-run tank urgency so dashboards can render "what was urgent
    BEFORE the run" alongside "what the solver picked."

    Returns the path written.
    """
    archive_dir = Path(output_dir) / 'archive'
    archive_dir.mkdir(parents=True, exist_ok=True)
    today = plan.today.isoformat() if isinstance(plan.today, date) else str(plan.today)
    path = archive_dir / f'plan_{today}.json'
    payload = _plan_to_dict(plan)
    if extras:
        for k, v in extras.items():
            payload.setdefault(k, v)   # don't overwrite Plan fields
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, default=_json_default, indent=2)
    return path
