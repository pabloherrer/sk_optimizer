"""
anova_integration.py — Live tank-level integration for IRP
==========================================================

Some restaurants have ANOVA Transcend tank sensors that report level
in real time. For those clients, the "estimated current level" from
the delivery log is replaced by the **observed** level from sensor
readings — an order-of-magnitude tighter estimate.

Why this matters
----------------
Without sensors, we estimate `current_lbs = tank − days_since_last × rate`.
Both `days_since_last` and `rate` carry error. With sensors, we have
the actual reading from minutes ago. The forecast σ̂ for that client
shrinks dramatically.

For the IRP:
  • `state.json` levels for monitored clients can be replaced/overridden
  • `DemandModel.sigma` for monitored clients can be tightened (lower
    quantile buffer needed → less aggressive scheduling)
  • `confidence` field flips from 'observed-from-log' to 'sensor'

Data sources
------------
1. `anova_data/readings.csv` — push-webhook receiver output (event-driven)
2. `anova_live_readings.xlsx` — periodic API pull (snapshot)

Both formats: one row per (asset_id, timestamp, level_lbs).

Mapping ANOVA asset_id → SK client_id
-------------------------------------
ANOVA names are like 'CROWN_PLAZA'. SK customer names are like
'CRO - 2031 - CROWN PLAZA'. Mapping is:
  1. Manual override map (in case of name mismatches)
  2. Normalised substring match: lowercase, strip non-alpha, find
     the asset_id token inside the customer name
"""

from __future__ import annotations

import csv
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Reading record
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TankReading:
    """One sensor sample: a tank level at a specific moment."""
    asset_id: str
    timestamp: pd.Timestamp
    level_lbs: float
    product: Optional[str] = None
    fresh: bool = True   # within freshness window

    @property
    def age_hours(self) -> float:
        now = pd.Timestamp.now(tz='UTC').tz_localize(None)
        ts = self.timestamp.tz_localize(None) if self.timestamp.tz else self.timestamp
        return max((now - ts).total_seconds() / 3600.0, 0.0)


# ─────────────────────────────────────────────────────────────────────────────
# Manual override map (asset_id → SK client_id). Edit as needed.
# ─────────────────────────────────────────────────────────────────────────────

ANOVA_TO_CLIENT_MAP: Dict[str, str] = {
    # 'CROWN_PLAZA': '2031',
    # 'HERMOSA_INN': '5012',
    # Populate as ANOVA expansion happens. Kept empty by default to
    # ensure we never silently mis-map.
}


# ─────────────────────────────────────────────────────────────────────────────
# Loaders
# ─────────────────────────────────────────────────────────────────────────────

def load_readings_from_csv(
    path: Path,
    fresh_hours: float = 24.0,
) -> List[TankReading]:
    """Read the receiver's append-only CSV. Most-recent reading per asset wins."""
    p = Path(path)
    if not p.exists():
        return []
    by_asset: Dict[str, TankReading] = {}
    with open(p, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                ts = pd.Timestamp(row.get('timestamp') or row.get('received_at'))
                lbs = float(row.get('display_value') or 0)
                aid = (row.get('asset_id') or '').strip()
                if not aid or lbs <= 0:
                    continue
                rec = TankReading(
                    asset_id=aid, timestamp=ts, level_lbs=lbs,
                    product=row.get('product'),
                )
                # Latest reading per asset
                if aid not in by_asset or ts > by_asset[aid].timestamp:
                    by_asset[aid] = rec
            except Exception:
                continue
    out = list(by_asset.values())
    for r in out:
        r.fresh = r.age_hours <= fresh_hours
    log.info('Loaded %d ANOVA readings from %s (%d fresh ≤%dh)',
             len(out), p, sum(1 for r in out if r.fresh), int(fresh_hours))
    return out


def load_readings_from_excel(path: Path, fresh_hours: float = 24.0) -> List[TankReading]:
    """Read a snapshot Excel from `anova_pull.py`. One row per asset."""
    p = Path(path)
    if not p.exists():
        return []
    df = pd.read_excel(p)
    out = []
    for _, row in df.iterrows():
        try:
            aid = str(row.get('asset_id', '')).strip()
            lbs = float(row.get('display_value') or 0)
            ts = pd.Timestamp(row.get('timestamp'))
            if not aid or lbs <= 0:
                continue
            out.append(TankReading(
                asset_id=aid, timestamp=ts, level_lbs=lbs,
                product=row.get('product'),
            ))
        except Exception:
            continue
    for r in out:
        r.fresh = r.age_hours <= fresh_hours
    return out


def load_all_readings(
    *,
    csv_path: Optional[Path] = None,
    excel_path: Optional[Path] = None,
    fresh_hours: float = 24.0,
) -> Dict[str, TankReading]:
    """Merge CSV + Excel sources, latest reading wins per asset."""
    merged: Dict[str, TankReading] = {}
    sources = []
    if csv_path:
        sources.extend(load_readings_from_csv(csv_path, fresh_hours))
    if excel_path:
        sources.extend(load_readings_from_excel(excel_path, fresh_hours))
    for r in sources:
        cur = merged.get(r.asset_id)
        if cur is None or r.timestamp > cur.timestamp:
            merged[r.asset_id] = r
    return merged


# ─────────────────────────────────────────────────────────────────────────────
# asset_id → client_id mapping
# ─────────────────────────────────────────────────────────────────────────────

def _normalize(s: str) -> str:
    """Strip non-alphanumeric, lowercase, collapse whitespace."""
    return re.sub(r'[^a-z0-9]+', '_', str(s).lower()).strip('_')


# Common English plurals/spelling variants — strip these to canonical form
_TOKEN_ALIASES = {
    'crowne': 'crown', 'wingstops': 'wingstop',
    'restaurants': 'restaurant', 'sheratons': 'sheraton',
}

_STOPWORDS = {
    'the', 'a', 'an', 'of', 'and', 'oils', 'sk', 'co', 'inc', 'llc',
}


def _tokens(s: str) -> set:
    """Token set: split on non-alphanumerics, lowercase, drop stopwords + numerics-only."""
    raw = re.split(r'[^a-z0-9]+', str(s).lower())
    out = set()
    for tok in raw:
        if not tok or tok.isdigit() or len(tok) < 3:
            continue
        tok = _TOKEN_ALIASES.get(tok, tok)
        if tok in _STOPWORDS:
            continue
        out.add(tok)
    return out


def _token_overlap_score(asset_tokens: set, customer_tokens: set) -> float:
    """
    Jaccard-like score: |intersection| / |asset_tokens|. Captures
    "asset name's tokens are mostly in customer name" without requiring
    word order to match. 1.0 = every asset token appears in customer.
    """
    if not asset_tokens:
        return 0.0
    inter = asset_tokens & customer_tokens
    return len(inter) / len(asset_tokens)


def map_asset_to_client(
    readings: Dict[str, TankReading],
    clients_df: pd.DataFrame,
    *,
    manual_overrides: Optional[Dict[str, str]] = None,
    min_token_score: float = 0.75,
    min_matched_tokens: int = 2,
) -> Dict[str, str]:
    """
    Return {asset_id: client_id (str)}. Mapping rules in order:
      1. Manual override map
      2. Normalised substring match (asset 'CROWN_PLAZA' inside customer
         'CRO - 2031 - CROWN PLAZA')
      3. Token overlap ≥ min_token_score with ≥ min_matched_tokens
         (rejects single-token coincidences like 'MESA' alone)

    A reading whose asset_id can't be confidently mapped is silently
    skipped — never silently mis-mapped. Use ANOVA_TO_CLIENT_MAP for
    edge cases.
    """
    overrides = {**ANOVA_TO_CLIENT_MAP, **(manual_overrides or {})}
    out: Dict[str, str] = {}

    cust_norm = clients_df.assign(
        _norm=clients_df['Customer'].astype(str).map(_normalize),
        _toks=clients_df['Customer'].astype(str).map(_tokens),
        _id=clients_df['ID'].astype(str),
    )

    for asset_id in readings:
        if asset_id in overrides:
            out[asset_id] = overrides[asset_id]
            continue
        norm_asset = _normalize(asset_id)
        if not norm_asset:
            continue

        # 1. Substring match
        match = cust_norm[cust_norm['_norm'].str.contains(norm_asset, regex=False)]
        if not match.empty:
            out[asset_id] = str(match.iloc[0]['_id'])
            continue

        # 2. Token overlap with stopword/alias normalisation
        asset_tokens = _tokens(asset_id)
        if not asset_tokens:
            continue
        scores = cust_norm['_toks'].apply(
            lambda toks: _token_overlap_score(asset_tokens, toks)
        )
        # n matched tokens for the best candidate
        intersections = cust_norm['_toks'].apply(
            lambda toks: len(asset_tokens & toks)
        )
        best_idx = scores.idxmax()
        best = scores.loc[best_idx]
        n_matched = intersections.loc[best_idx]
        if best >= min_token_score and n_matched >= min_matched_tokens:
            out[asset_id] = str(cust_norm.loc[best_idx, '_id'])

    return out


# ─────────────────────────────────────────────────────────────────────────────
# Apply ANOVA readings to the IRP state
# ─────────────────────────────────────────────────────────────────────────────

def apply_anova_to_state(
    *,
    state,                      # InventoryState — duck-typed import to avoid cycles
    readings: Dict[str, TankReading],
    clients_df: pd.DataFrame,
    manual_overrides: Optional[Dict[str, str]] = None,
    fresh_hours: float = 24.0,
) -> Dict[str, str]:
    """
    Override estimated state with live ANOVA readings (where fresh).

    Mutates `state.clients` in-place. Returns {client_id: asset_id} of
    the clients that received an override.

    The IRP's safety-stock layer can then look at `state.clients[cid].confidence`
    == 'sensor' and tighten σ for those clients.
    """
    from .state_manager import ClientState
    asset_to_cid = map_asset_to_client(
        readings, clients_df, manual_overrides=manual_overrides,
    )
    applied: Dict[str, str] = {}
    for asset_id, cid in asset_to_cid.items():
        rec = readings[asset_id]
        if not rec.fresh:
            continue
        # Update or create state entry
        prev = state.clients.get(cid)
        state.clients[cid] = ClientState(
            id=cid,
            current_lbs=float(rec.level_lbs),
            last_delivery=prev.last_delivery if prev else None,
            last_delivery_qty=prev.last_delivery_qty if prev else None,
            days_since_last=0.0,        # we just observed the level
            confidence='sensor',
            updated_at=str(rec.timestamp),
        )
        applied[cid] = asset_id
    if applied:
        log.info('Applied ANOVA overrides to %d clients (live sensor data).',
                 len(applied))
    return applied


# ─────────────────────────────────────────────────────────────────────────────
# Tighten demand σ̂ for monitored clients
# ─────────────────────────────────────────────────────────────────────────────

def tighten_sigma_for_monitored(
    *,
    models: Dict,                # {client_id: DemandModel}
    monitored_client_ids: set,
    sigma_multiplier: float = 0.4,
) -> int:
    """
    For ANOVA-monitored clients, the level uncertainty is much smaller
    (we know current_lbs precisely). The CONSUMPTION sigma is unchanged,
    but the cumulative-quantile buffer is dominated by level uncertainty
    × time, so we can shrink σ̂ by a fixed multiplier.

    Returns count of models tightened.
    """
    n = 0
    for cid in monitored_client_ids:
        m = models.get(cid)
        if m is None:
            continue
        m.sigma = m.sigma * sigma_multiplier
        n += 1
    return n
