"""
state_manager.py — Atomic, durable state for the rolling-horizon IRP
====================================================================

The original `state.py` had `save_state()` / `load_state()` but they were
never called from production. This module fixes that gap with three
stronger guarantees:

  1. **Atomic writes**: state is written to a temp file then renamed,
     so an interrupted run never corrupts the file.
  2. **Append-only delivery log**: every confirmed delivery is recorded
     in `deliveries.log.jsonl`, giving a forensic audit trail and a
     replay source for backtests.
  3. **Plan persistence**: the optimizer's full output (committed +
     tentative routes) is written to `plan.json` so the next run can
     warm-start from it instead of re-solving from cold.

State machine for each client:
    NEW           — no entry in state file (first run)
        ↓
    ESTIMATED     — level estimated from delivery log
        ↓ (after first save_state)
    TRACKED       — level is being updated daily
        ↓ (each evening)
    DELIVERED     — visited today, level reset to Tank_lbs
        ↓ (next day)
    DECAYED       — level reduced by 1 day of forecast consumption

Files written under DATA_DIR:
    state.json              { "version", "as_of", "clients": {id: {...}} }
    plan.json               { "version", "solved_at", "horizon_days",
                              "committed": {...}, "tentative": {...} }
    deliveries.log.jsonl    one JSON object per line, append-only
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import pandas as pd

log = logging.getLogger(__name__)

STATE_VERSION = 2  # bumped from legacy v1 (flat dict)


# ─────────────────────────────────────────────────────────────────────────────
# Atomic write helper
# ─────────────────────────────────────────────────────────────────────────────

@contextmanager
def _atomic_writer(target: Path):
    """
    Yield a writeable file handle that, on success, atomically replaces
    `target`. On exception, the temp file is removed and `target` is
    untouched. Survives Ctrl-C, power loss, and concurrent runs.
    """
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=f'.{target.name}.', suffix='.tmp', dir=target.parent
    )
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            yield f
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
    except Exception:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


# ─────────────────────────────────────────────────────────────────────────────
# Per-client state record
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ClientState:
    """One record per client in state.json."""
    id: str
    current_lbs: float
    last_delivery: Optional[str] = None     # ISO date string or None
    last_delivery_qty: Optional[float] = None
    days_since_last: Optional[float] = None
    confidence: str = 'observed'            # observed | estimated | new
    updated_at: Optional[str] = None        # ISO timestamp

    def to_json(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}

    @classmethod
    def from_json(cls, d: dict) -> 'ClientState':
        return cls(**{k: d.get(k) for k in cls.__dataclass_fields__})


# ─────────────────────────────────────────────────────────────────────────────
# Persistent state container
# ─────────────────────────────────────────────────────────────────────────────

class InventoryState:
    """
    The system's working memory between runs.

    Loads a previously-saved state, exposes per-client lookup, applies
    daily decay + delivery resets, and persists atomically.
    """

    def __init__(
        self,
        as_of: pd.Timestamp,
        clients: Optional[Dict[str, ClientState]] = None,
    ):
        self.as_of = pd.Timestamp(as_of).normalize()
        self.clients: Dict[str, ClientState] = clients or {}

    # ── I/O ────────────────────────────────────────────────────────────────

    @classmethod
    def load(cls, path: Path) -> 'InventoryState':
        path = Path(path)
        if not path.exists() or path.stat().st_size <= 2:  # empty {} counts as fresh
            log.info('State file %s is missing or empty — starting fresh.', path)
            return cls(as_of=pd.Timestamp.today().normalize())
        try:
            raw = json.loads(path.read_text(encoding='utf-8'))
        except json.JSONDecodeError as e:
            log.error('State file corrupt (%s); starting fresh.', e)
            return cls(as_of=pd.Timestamp.today().normalize())

        # ── v1 (legacy) compatibility: flat {id: lbs}
        if 'version' not in raw:
            log.info('Migrating legacy v1 state file to v%d.', STATE_VERSION)
            clients = {
                str(k): ClientState(id=str(k), current_lbs=float(v))
                for k, v in raw.items()
            }
            return cls(as_of=pd.Timestamp.today().normalize(), clients=clients)

        as_of = pd.Timestamp(raw.get('as_of', pd.Timestamp.today())).normalize()
        clients = {
            cid: ClientState.from_json(rec)
            for cid, rec in raw.get('clients', {}).items()
        }
        log.info('Loaded state for %d clients (as-of %s).', len(clients), as_of.date())
        return cls(as_of=as_of, clients=clients)

    def save(self, path: Path) -> None:
        payload = {
            'version': STATE_VERSION,
            'as_of': self.as_of.isoformat(),
            'saved_at': datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + 'Z',
            'clients': {cid: rec.to_json() for cid, rec in self.clients.items()},
        }
        with _atomic_writer(path) as f:
            json.dump(payload, f, indent=2, default=str)
        log.info('Saved state for %d clients to %s.', len(self.clients), path)

    # ── Lookup ─────────────────────────────────────────────────────────────

    def level(self, client_id: str, default: Optional[float] = None) -> Optional[float]:
        rec = self.clients.get(str(client_id))
        return rec.current_lbs if rec else default

    def as_dict(self) -> Dict[str, float]:
        """Backwards-compatible flat {id: lbs} view."""
        return {cid: rec.current_lbs for cid, rec in self.clients.items()}

    # ── Mutation: daily roll-forward ──────────────────────────────────────

    def apply_consumption(
        self,
        clients_df: pd.DataFrame,
        n_days: int = 1,
        rate_col: str = 'Avg_LbsPerDay',
        floor_pct: float = 0.0,
    ) -> None:
        """
        Decrement every tracked client by `n_days × forecast_rate`.
        Use this each evening BEFORE recording the day's deliveries.
        """
        for _, row in clients_df.iterrows():
            cid = str(row['ID'])
            rec = self.clients.get(cid)
            tank = float(row['Tank_lbs'])
            rate = float(row.get(rate_col, 0) or 0)
            floor = tank * floor_pct
            if rec is None:
                # Brand-new client — initialise from estimate column if present
                est = row.get('Est_Current_lbs')
                lvl = float(est) if est is not None and pd.notna(est) else tank * 0.5
                rec = ClientState(
                    id=cid, current_lbs=max(lvl, floor),
                    confidence='new', updated_at=datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
                )
                self.clients[cid] = rec
            else:
                rec.current_lbs = max(rec.current_lbs - n_days * rate, floor)
                rec.days_since_last = (rec.days_since_last or 0) + n_days
                rec.updated_at = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()

    def apply_deliveries(
        self,
        deliveries: Iterable[Dict[str, Any]],
        clients_df: pd.DataFrame,
    ) -> int:
        """
        Reset visited clients to Tank_lbs (S&K policy: always fill to 100%).
        `deliveries` is an iterable of dicts with at least {id, qty_lbs, date}.
        Returns count of records applied.
        """
        tank_by_id = {str(r['ID']): float(r['Tank_lbs']) for _, r in clients_df.iterrows()}
        now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        applied = 0
        for d in deliveries:
            cid = str(d['id'])
            tank = tank_by_id.get(cid)
            if tank is None:
                log.warning('Delivery for unknown client %s — skipped.', cid)
                continue
            qty = float(d.get('qty_lbs', tank))
            self.clients[cid] = ClientState(
                id=cid,
                current_lbs=tank,                # filled to full
                last_delivery=str(d.get('date', self.as_of.date())),
                last_delivery_qty=qty,
                days_since_last=0,
                confidence='observed',
                updated_at=now,
            )
            applied += 1
        return applied

    def advance(self, new_as_of: pd.Timestamp) -> None:
        self.as_of = pd.Timestamp(new_as_of).normalize()


# ─────────────────────────────────────────────────────────────────────────────
# Append-only delivery log
# ─────────────────────────────────────────────────────────────────────────────

class DeliveryLog:
    """JSONL audit trail. Each line is a confirmed delivery event."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(
        self,
        deliveries: Iterable[Dict[str, Any]],
        run_id: Optional[str] = None,
    ) -> int:
        """Append delivery records. Returns count appended."""
        n = 0
        with open(self.path, 'a', encoding='utf-8') as f:
            ts = datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + 'Z'
            for d in deliveries:
                rec = {
                    'logged_at': ts,
                    'run_id': run_id,
                    **d,
                }
                f.write(json.dumps(rec, default=str) + '\n')
                n += 1
        return n

    def read_all(self) -> List[Dict[str, Any]]:
        if not self.path.exists():
            return []
        out = []
        with open(self.path, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return out


# ─────────────────────────────────────────────────────────────────────────────
# Plan persistence (warm-start source)
# ─────────────────────────────────────────────────────────────────────────────

def save_plan(
    path: Path,
    routes: Dict[int, pd.DataFrame],
    deferred: pd.DataFrame,
    plan_dates: List[pd.Timestamp],
    today: pd.Timestamp,
    horizon_days: int,
    commit_days: int,
    objective_value: Optional[float] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Persist the full solver output for warm-starting tomorrow's run.

    The plan is keyed by (client_id, day_index), so when tomorrow rolls
    around, today's day-1 plan becomes tomorrow's day-0 starting point.
    """
    visits = []
    for d, df in routes.items():
        if df is None or df.empty:
            continue
        for _, r in df.iterrows():
            visits.append({
                'day': int(d),
                'date': str(r.get('Date', plan_dates[d].date() if d < len(plan_dates) else '')),
                'client_id': str(r.get('ID', '')),
                'truck': str(r.get('Truck', '')),
                'stop': int(r.get('Stop', 0) or 0),
                'refill_lbs': float(r.get('Refill_lbs', 0) or 0),
                'status': str(r.get('Status', 'TENTATIVE')),
                'arrival_hhmm': str(r.get('Arrival_HHMM', '')),
                'depart_hhmm':  str(r.get('Depart_HHMM', '')),
                'service_min':  int(r.get('Service_Min', 0) or 0),
                'travel_min':   int(r.get('Travel_To_Min', 0) or 0),
            })

    deferred_ids = []
    if deferred is not None and not deferred.empty and 'ID' in deferred.columns:
        deferred_ids = [str(x) for x in deferred['ID'].tolist()]

    payload = {
        'version': STATE_VERSION,
        'solved_at': datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + 'Z',
        'today': str(today.date()),
        'horizon_days': int(horizon_days),
        'commit_days': int(commit_days),
        'plan_dates': [str(d.date()) for d in plan_dates],
        'objective_value': objective_value,
        'metadata': metadata or {},
        'visits': visits,
        'deferred_ids': deferred_ids,
    }
    with _atomic_writer(path) as f:
        json.dump(payload, f, indent=2, default=str)
    log.info('Saved plan with %d visits to %s.', len(visits), path)


def load_plan(path: Path) -> Optional[Dict[str, Any]]:
    """Load yesterday's plan for warm-starting. Returns None if absent."""
    path = Path(path)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except json.JSONDecodeError as e:
        log.warning('Plan file %s corrupt: %s — skipping warm start.', path, e)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# End-of-run commit: the function that closes the rolling-horizon loop
# ─────────────────────────────────────────────────────────────────────────────

def commit_run(
    *,
    state: InventoryState,
    state_file: Path,
    routes: Dict[int, pd.DataFrame],
    deferred: pd.DataFrame,
    plan_dates: List[pd.Timestamp],
    today: pd.Timestamp,
    horizon_days: int,
    commit_days: int,
    plan_file: Path,
    delivery_log: Optional[DeliveryLog] = None,
    auto_apply_committed: bool = False,
    clients_df: Optional[pd.DataFrame] = None,
    objective_value: Optional[float] = None,
    metadata: Optional[Dict[str, Any]] = None,
    run_id: Optional[str] = None,
) -> None:
    """
    Atomic end-of-run handoff:

      1. Persist the full plan (committed + tentative) to plan.json.
      2. Optionally pre-apply the day-0 committed deliveries to state
         (only set this for autonomous runs where you trust the plan to
         execute as-written; for human-in-loop, defer to a separate
         `confirm_deliveries()` call once drivers report actuals).
      3. Save state.json atomically.

    This is the single function that closes the rolling-horizon loop.
    Calling it makes the next run **start where this one left off**.
    """
    save_plan(
        plan_file, routes, deferred, plan_dates, today,
        horizon_days, commit_days, objective_value, metadata,
    )

    if auto_apply_committed:
        if clients_df is None:
            raise ValueError('auto_apply_committed=True requires clients_df.')
        committed_visits = []
        # Day 0 only — that's "today's" committed work.
        df0 = routes.get(0, pd.DataFrame())
        if df0 is not None and not df0.empty:
            for _, r in df0.iterrows():
                committed_visits.append({
                    'id': str(r.get('ID', '')),
                    'qty_lbs': float(r.get('Refill_lbs', 0) or 0),
                    'date': str(r.get('Date', today.date())),
                    'truck': str(r.get('Truck', '')),
                    'planned': True,
                })
        if committed_visits:
            n = state.apply_deliveries(committed_visits, clients_df)
            log.info('Auto-applied %d committed day-0 deliveries to state.', n)
            if delivery_log is not None:
                delivery_log.append(committed_visits, run_id=run_id)

    state.save(state_file)


# ─────────────────────────────────────────────────────────────────────────────
# Reconciliation: replace planned deliveries with actuals when reported
# ─────────────────────────────────────────────────────────────────────────────

def confirm_deliveries(
    *,
    state: InventoryState,
    state_file: Path,
    actuals: Iterable[Dict[str, Any]],
    clients_df: pd.DataFrame,
    delivery_log: Optional[DeliveryLog] = None,
    run_id: Optional[str] = None,
) -> int:
    """
    Apply ACTUAL (driver-confirmed) deliveries to state and persist.

    Each `actual` is a dict: {id, qty_lbs, date, truck?}.
    Returns count applied.
    """
    n = state.apply_deliveries(actuals, clients_df)
    state.save(state_file)
    if delivery_log is not None:
        delivery_log.append(
            ({**a, 'confirmed': True} for a in actuals),
            run_id=run_id,
        )
    return n
