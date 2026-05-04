"""
state.py
========
Persists the rolling inventory state between daily runs.

The state is simply a {client_id: current_lbs} dictionary, saved as JSON.
It is updated each evening after the day's deliveries are confirmed:
  - Delivered clients → level reset to Tank_lbs (filled to full)
  - Unvisited clients → level decremented by one day of consumption

This file is the memory of the rolling horizon — without it, every run
starts cold from the delivery-log estimates.
"""

import json
import logging
from pathlib import Path
from typing import Dict, Optional
import pandas as pd

log = logging.getLogger(__name__)


def load_state(state_file: str | Path) -> Dict[str, float]:
    """
    Load persisted inventory state.  Returns empty dict if file absent.

    Accepts both formats:
      v1 (legacy):  {client_id: lbs_float, ...}
      v2 (irp_core):  {"version": 2, "as_of": "...", "clients": {id: {current_lbs: ...}}}
    Always returns a flat {id: lbs_float} dict for backwards compatibility.
    """
    p = Path(state_file)
    if not p.exists():
        log.info("No state file found at %s — starting fresh.", p)
        return {}
    with open(p) as f:
        raw = json.load(f)

    # v2: structured format from irp_core.state_manager
    if isinstance(raw, dict) and 'clients' in raw and isinstance(raw.get('clients'), dict):
        out = {}
        for cid, rec in raw['clients'].items():
            if isinstance(rec, dict) and 'current_lbs' in rec:
                try:
                    out[str(cid)] = float(rec['current_lbs'])
                except (TypeError, ValueError):
                    continue
        log.info("Loaded v2 inventory state for %d clients from %s", len(out), p)
        return out

    # v1: flat {id: lbs} dict
    out = {}
    for k, v in raw.items():
        try:
            out[str(k)] = float(v)
        except (TypeError, ValueError):
            continue   # skip metadata keys silently
    log.info("Loaded v1 inventory state for %d clients from %s", len(out), p)
    return out


def save_state(state: Dict[str, float], state_file: str | Path) -> None:
    """Persist inventory state to JSON."""
    p = Path(state_file)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, 'w') as f:
        json.dump({k: round(v, 2) for k, v in state.items()}, f, indent=2)
    log.info("Saved inventory state for %d clients to %s", len(state), p)


def update_state(
    state:        Dict[str, float],
    clients_df:   pd.DataFrame,
    delivered_ids: list[str],
    n_days_elapsed: int = 1,
) -> Dict[str, float]:
    """
    Update inventory levels after `n_days_elapsed` days have passed.

    delivered_ids : client IDs that were actually visited and filled today.
    n_days_elapsed: normally 1 (called once per day); use 3 for a weekend gap.

    Rules:
      - Delivered clients → reset to Tank_lbs (always filled to full).
      - All others        → deduct n_days_elapsed × Avg_LbsPerDay.
      - Clamp to [5% of tank, tank_lbs].
    """
    delivered_set = set(delivered_ids)
    new_state     = {}

    for _, row in clients_df.iterrows():
        cid      = row['ID']
        tank     = float(row['Tank_lbs'])
        rate     = float(row['Avg_LbsPerDay'])
        floor    = tank * 0.05

        if cid in delivered_set:
            new_state[cid] = tank          # Filled to full
        else:
            prior = state.get(cid, float(row.get('Est_Current_lbs', tank * 0.5)))
            new_state[cid] = max(prior - n_days_elapsed * rate, floor)

    return new_state


def initialise_state_from_snapshot(clients_df: pd.DataFrame) -> Dict[str, float]:
    """
    Build a fresh inventory state from the delivery-log estimates.
    Called on first run (no state file exists yet).
    """
    state = {}
    for _, row in clients_df.iterrows():
        est = row.get('Est_Current_lbs')
        if est is not None and pd.notna(est):
            state[row['ID']] = float(est)
        else:
            state[row['ID']] = float(row['Tank_lbs']) * 0.5
    return state
