"""
Per-day truck-availability overrides set from the dashboard.

The fleet config (v2/config/fleet.yaml) declares which trucks exist and the
Saturday rule (only Truck2 on Saturdays). But the operator may need to mark
a truck unavailable on a specific weekday (broken, driver out, in service).
This module persists those exceptions to a JSON sidecar.

Schema (data/truck_unavailable.json):
    {
        "unavailable": [
            {"date": "2026-05-26", "truck_id": "Truck2", "reason": "in service"}
        ]
    }
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Dict, List, Set, Tuple


def load_unavailability(path: Path) -> Set[Tuple[date, str]]:
    """Return set of (date, truck_id) pairs that are unavailable."""
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return set()
    out: Set[Tuple[date, str]] = set()
    for entry in data.get('unavailable', []):
        try:
            d = date.fromisoformat(entry['date'])
            tid = str(entry['truck_id'])
            out.add((d, tid))
        except Exception:
            continue
    return out


def save_unavailability(path: Path, entries: List[Dict]) -> None:
    """Write the sidecar JSON; entries is list of dicts with keys date, truck_id, reason."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {'unavailable': entries}
    path.write_text(json.dumps(payload, indent=2, default=str), encoding='utf-8')
