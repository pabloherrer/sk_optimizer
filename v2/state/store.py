"""
v2.state.store — durable per-client inventory state.

Three responsibilities:

  1. Read / write the JSON state file that carries inventory levels
     between runs. Format is v2 (versioned) and compatible with the
     legacy `data/inventory_state.json` schema for migration.

  2. Atomic writes. We write to `<state_file>.tmp` first, then
     `os.replace` it onto the target. A crash mid-write leaves the
     previous state untouched, never a half-written file.

  3. Per-run audit trail in `runs.jsonl` (sibling to the state file),
     one JSON object per line. Cheap to read, forever-appendable, and
     never corrupts the main state file.

Schema (top-level):
    {
      "version": 2,
      "as_of": "2026-05-20T00:00:00",
      "saved_at": "2026-05-21T18:02:04Z",
      "clients": {
        "1054": {
          "id": "1054",
          "current_lbs": 326.0,
          "confidence": "sensor",
          "updated_at": "..."
        },
        ...
      }
    }

The `clients` map is opaque to this module — we round-trip whatever the
caller writes — so it can carry source/confidence/last_delivery fields
without this module needing to know about them.
"""
from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from v2.domain import Plan


STATE_VERSION = 2


class StateStore:
    """Atomic, versioned per-client state on disk."""

    def __init__(self, state_file: Path):
        self.state_file = Path(state_file)
        self.backup_file = self.state_file.with_suffix(self.state_file.suffix + '.bak')
        self.runs_log = self.state_file.parent / 'runs.jsonl'

    # ── Read ───────────────────────────────────────────────────────────────

    def load(self) -> dict[str, dict]:
        """
        Return `{client_id: {current_lbs, source, as_of, ...}}`.

        Returns an empty dict if the file is missing, empty, or invalid
        JSON. (The caller already has to handle "fresh start" — better
        to surface that as an empty dict than to raise.)
        """
        if not self.state_file.exists():
            return {}
        try:
            raw_bytes = self.state_file.read_bytes()
        except OSError:
            return {}
        if not raw_bytes.strip():
            return {}
        try:
            payload = json.loads(raw_bytes.decode('utf-8'))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

        # v1 legacy format: flat {id: lbs}
        if isinstance(payload, dict) and 'clients' not in payload and 'version' not in payload:
            return {
                str(cid): {'id': str(cid), 'current_lbs': float(lvl)}
                for cid, lvl in payload.items()
                if isinstance(lvl, (int, float))
            }

        clients = payload.get('clients', {}) if isinstance(payload, dict) else {}
        if not isinstance(clients, dict):
            return {}
        # Defensive copy so callers can't mutate our return value via aliasing
        return {str(cid): dict(rec) for cid, rec in clients.items() if isinstance(rec, dict)}

    # ── Write ──────────────────────────────────────────────────────────────

    def save(self, state: Dict[str, Dict[str, Any]], plan: Optional[Plan] = None) -> None:
        """
        Persist `state` to disk atomically.

        Procedure:
          1. If a current state file exists, copy it to `<file>.bak`.
             (We do this BEFORE writing so a crash mid-write leaves us
             with the previous good state on the main file AND a copy
             at .bak.)
          2. Write the new state to `<file>.tmp` (with fsync).
          3. `os.replace(tmp, file)` — atomic on POSIX.

        The `plan` argument is recorded in the run log if non-None; it
        does not affect the state file content itself (the state file
        is the inventory snapshot, the plan is separately persisted by
        whatever module writes plan.json).
        """
        self.state_file.parent.mkdir(parents=True, exist_ok=True)

        # Step 1: backup current state if present
        if self.state_file.exists():
            try:
                shutil.copy2(self.state_file, self.backup_file)
            except OSError:
                # Backup failure shouldn't block the save; the new write
                # is still atomic. But surface it via the run log if we
                # had a plan to record.
                pass

        # Step 2: build payload
        now_iso = datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + 'Z'
        # Derive as_of from the plan if we have one; otherwise use now.
        if plan is not None:
            as_of = datetime.combine(plan.today, datetime.min.time()).isoformat()
        else:
            as_of = now_iso.rstrip('Z')

        payload = {
            'version': STATE_VERSION,
            'as_of': as_of,
            'saved_at': now_iso,
            'clients': {str(cid): dict(rec) for cid, rec in state.items()},
        }

        # Step 3: atomic write
        tmp = self.state_file.with_suffix(self.state_file.suffix + '.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(payload, f, indent=2, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.state_file)

        # Optional: log the run
        if plan is not None:
            self.record_run(
                run_id=plan.run_id,
                metadata={
                    'today': str(plan.today),
                    'total_stops': plan.total_stops,
                    'total_lbs_delivered': plan.total_lbs_delivered,
                    'total_miles': plan.total_miles,
                    'objective_cost_dollars': plan.objective_cost_dollars,
                    'solver_status': plan.solver_status,
                    'solve_seconds': plan.solve_seconds,
                },
            )

    def record_run(self, run_id: str, metadata: Dict[str, Any]) -> None:
        """
        Append one line to `runs.jsonl`. Pure append, never rewrites.
        """
        self.runs_log.parent.mkdir(parents=True, exist_ok=True)
        record = {
            'run_id': run_id,
            'logged_at': datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + 'Z',
            'metadata': dict(metadata) if metadata else {},
        }
        with open(self.runs_log, 'a', encoding='utf-8') as f:
            f.write(json.dumps(record, default=str) + '\n')
