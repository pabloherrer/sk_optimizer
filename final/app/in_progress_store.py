"""
In-progress / assumed-delivered sidecar store.

Problem this solves
-------------------
The solver rebuilds tank levels every run from the Delivery_Log. Deliveries
that happened TODAY (or were dispatched and are mid-route) usually aren't
in the log yet when the afternoon run happens — so those clients look empty
and get re-scheduled for tomorrow. See memory/design note "assumed-delivered".

The fix: a sidecar list of deliveries that are out-for-delivery / just
delivered but not yet logged. At ingest, each entry becomes an ASSUMED
delivery: the tank is treated as refilled to capacity on the entry date
(source='assumed'), unless better evidence exists.

During shadow mode the office seeds this list each morning from the manual
route sheet (dashboard "Deliveries in progress" card). Once the optimizer
goes live, the solver's committed plan can seed it automatically and the
office only edits deltas.

Reconciliation rules (applied in `apply_assumed_deliveries`)
------------------------------------------------------------
An entry for client C dated D is IGNORED when:
  1. expired      — D is more than EXPIRY_DAYS before the plan date
                    (slow logging shouldn't let fiction live forever);
  2. future       — D is on/after the plan date (not delivered yet as of
                    day 0; plan date is normally tomorrow);
  3. confirmed    — the Delivery_Log already has a delivery for C on/after D
                    (real data wins; formula/sheet paths now see the fill);
  4. manual       — a ManualReading override exists for C (operator-stated
                    levels always win);
  5. fresh sensor — an Anova reading with timestamp strictly AFTER the end
                    of day D exists (post-fill measurement — trust it).
Otherwise the entry wins over formula estimates, spreadsheet Est Current,
and sensor readings taken BEFORE the fill (which show the pre-fill level —
same hazard as the MANUEL PEORIA safeguard in v2.ingest.build_problem).

Schema (data/in_progress.json)
------------------------------
    {
      "entries": [
        {"client_id": "1054", "date": "2026-07-02",
         "qty_lbs": null,                 # optional; null → full tank
         "note": "manual route T2",       # optional free text
         "created_at": "2026-07-02T08:15:00"}
      ]
    }
"""
from __future__ import annotations

import json
from dataclasses import dataclass, replace as _dc_replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Days after which an unconfirmed assumed delivery expires.
EXPIRY_DAYS = 4


@dataclass(frozen=True)
class InProgressEntry:
    client_id: str
    date: date
    qty_lbs: Optional[float] = None      # None → assume full tank
    note: str = ''
    created_at: Optional[datetime] = None


# ── Load / save ──────────────────────────────────────────────────────────────

def load_in_progress(path: Path) -> Tuple[InProgressEntry, ...]:
    """Read sidecar JSON → tuple of entries (empty if absent/invalid)."""
    path = Path(path)
    if not path.exists():
        return ()
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return ()
    out: List[InProgressEntry] = []
    for e in data.get('entries', []):
        try:
            out.append(InProgressEntry(
                client_id=str(e['client_id']),
                date=date.fromisoformat(str(e['date'])),
                qty_lbs=(float(e['qty_lbs'])
                         if e.get('qty_lbs') not in (None, '', 'null') else None),
                note=str(e.get('note', '')),
                created_at=_parse_dt(e.get('created_at')),
            ))
        except Exception:
            continue    # skip malformed rows; never block a solve on them
    return tuple(out)


def save_in_progress(path: Path, entries: List[dict]) -> None:
    """Write sidecar JSON atomically (tmp + replace, like v2 StateStore)."""
    import os
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {'entries': entries}
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(payload, indent=2, default=str), encoding='utf-8')
    os.replace(tmp, path)


# ── Reconciliation (pure — unit-tested in final/tests_in_progress.py) ────────

def apply_assumed_deliveries(
    initial_tanks: Dict[str, 'TankState'],
    tank_cap_by_id: Dict[str, float],
    entries: Tuple[InProgressEntry, ...],
    manual_reading_ids: frozenset,
    plan_today: date,
) -> Tuple[Dict[str, 'TankState'], List[str]]:
    """
    Return (updated_tanks, log_lines). Does not mutate inputs.

    For each in-progress entry that survives the reconciliation rules, the
    client's TankState is replaced with an assumed post-fill level decayed
    to `plan_today` at the client's consumption rate:

        level(plan_today) = fill_level − rate × (plan_today − entry.date).days

    where fill_level = tank capacity (default) or pre_level + qty_lbs when
    a quantity was recorded. source='assumed', as_of=plan_today — matching
    the convention that current_lbs is the level at day 0.
    """
    updated = dict(initial_tanks)
    log: List[str] = []
    # Keep only the LATEST entry per client (office may re-add after a miss).
    latest: Dict[str, InProgressEntry] = {}
    for e in entries:
        cur = latest.get(e.client_id)
        if cur is None or e.date > cur.date:
            latest[e.client_id] = e

    n_applied = n_confirmed = n_expired = n_skipped = 0
    for cid, e in sorted(latest.items()):
        ts = updated.get(cid)
        if ts is None:
            log.append(f"  ⚠ in-progress entry for unknown client {cid} — ignored")
            n_skipped += 1
            continue

        # Rule 2: future — not delivered yet as of plan day 0.
        if e.date >= plan_today:
            n_skipped += 1
            continue

        # Rule 1: expired.
        if (plan_today - e.date).days > EXPIRY_DAYS:
            log.append(f"  ⚠ assumed delivery for {cid} on {e.date} EXPIRED "
                       f"(>{EXPIRY_DAYS}d, still not in Delivery_Log)")
            n_expired += 1
            continue

        # Rule 3: confirmed by a real log entry on/after the entry date.
        if ts.last_delivery_date is not None:
            try:
                last_dt = date.fromisoformat(str(ts.last_delivery_date)[:10])
                if last_dt >= e.date:
                    n_confirmed += 1
                    continue
            except ValueError:
                pass    # unparseable last-delivery date → fall through

        # Rule 4: manual reading override wins.
        if cid in manual_reading_ids:
            n_skipped += 1
            continue

        # Rule 5: post-fill sensor reading wins (timestamp after end of
        # the delivery day). A sensor reading ON or BEFORE the delivery
        # day may be pre-fill → the assumption wins instead.
        if ts.source in ('sensor', 'sensor-projected') and ts.as_of is not None:
            as_of_d = ts.as_of.date() if hasattr(ts.as_of, 'date') else ts.as_of
            if as_of_d > e.date:
                n_skipped += 1
                continue

        cap = float(tank_cap_by_id.get(cid, 0.0))
        if cap <= 0:
            n_skipped += 1
            continue
        if e.qty_lbs is not None:
            fill_level = min(cap, max(0.0, ts.current_lbs) + float(e.qty_lbs))
        else:
            fill_level = cap    # default: assume topped off
        days_since = max(0, (plan_today - e.date).days)
        level = max(0.0, fill_level - ts.rate_lbs_per_day * days_since)

        updated[cid] = _dc_replace(
            ts,
            current_lbs=level,
            source='assumed',
            as_of=datetime.combine(plan_today, datetime.min.time()),
        )
        n_applied += 1

    log.insert(0, (f"  In-progress deliveries: {n_applied} assumed full, "
                   f"{n_confirmed} already confirmed in log, "
                   f"{n_expired} expired, {n_skipped} superseded/skipped"))
    return updated, log


def _parse_dt(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None
