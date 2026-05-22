"""
Sidecar override store for the dashboard.

The Excel input file lives on SharePoint and may have its own `Overrides`
sheet (read by `v2/ingest/overrides.py`). The dashboard does NOT write to
that sheet — Excel files on shared drives are too fragile to update from
a Flask process while operators may have them open.

Instead, dashboard Pin/Skip actions write to a local sidecar JSON
(`data/user_overrides.json`) which the FINAL solver loads at run time and
merges with anything in the Excel Overrides sheet.

Schema:
    {
        "pins":    [{"client_id": "...", "date": "YYYY-MM-DD", "reason": "..."}],
        "forbids": [{"client_id": "...", "dates": ["YYYY-MM-DD", ...], "reason": "..."}]
    }
"""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional

from v2.domain.overrides import Forbid, Overrides, Pin


def load_user_overrides(path: Path) -> Overrides:
    """Read sidecar JSON and return a v2 Overrides bundle (empty if absent/invalid)."""
    if not path.exists():
        return Overrides()
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return Overrides()

    pins = tuple(
        Pin(
            client_id=str(p['client_id']),
            date=date.fromisoformat(p['date']),
            reason=str(p.get('reason', 'dashboard')),
            operator='dashboard',
            created_at=_parse_dt(p.get('created_at')),
        )
        for p in data.get('pins', [])
    )
    forbids = tuple(
        Forbid(
            client_id=str(f['client_id']),
            dates=tuple(date.fromisoformat(d) for d in f.get('dates', [])),
            reason=str(f.get('reason', 'dashboard')),
            operator='dashboard',
            created_at=_parse_dt(f.get('created_at')),
        )
        for f in data.get('forbids', [])
    )
    return Overrides(pins=pins, forbids=forbids)


def save_user_overrides(path: Path, pins: List[dict], forbids: List[dict]) -> None:
    """Write the sidecar JSON. Caller passes plain-dict overrides."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {'pins': pins, 'forbids': forbids}
    path.write_text(json.dumps(payload, indent=2, default=str), encoding='utf-8')


def merge_overrides(*bundles: Overrides) -> Overrides:
    """Concat-merge multiple Overrides bundles (later wins on duplicates by client_id+date)."""
    seen_pins: Dict = {}
    seen_forbids: Dict = {}
    for b in bundles:
        if b is None:
            continue
        for p in b.pins:
            seen_pins[(p.client_id, p.date)] = p
        for f in b.forbids:
            for d in f.dates:
                seen_forbids[(f.client_id, d)] = f
    pins = tuple(seen_pins.values())
    # Re-bundle forbids by client_id
    by_client: Dict[str, List[date]] = {}
    forbid_reason: Dict[str, str] = {}
    forbid_op: Dict[str, str] = {}
    forbid_ctime = {}
    for (cid, d), fb in seen_forbids.items():
        by_client.setdefault(cid, []).append(d)
        forbid_reason[cid] = fb.reason
        forbid_op[cid] = fb.operator
        forbid_ctime[cid] = fb.created_at
    forbids = tuple(
        Forbid(
            client_id=cid,
            dates=tuple(sorted(dates)),
            reason=forbid_reason[cid],
            operator=forbid_op[cid],
            created_at=forbid_ctime[cid],
        )
        for cid, dates in by_client.items()
    )
    return Overrides(pins=pins, forbids=forbids)


def _parse_dt(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None
