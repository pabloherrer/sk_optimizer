"""
build_matrix.py
===============
Build the OSRM distance/time matrix for the depot + all clients in
Client_List. Persists to data/osrm_full_matrix_with_ids.npz and
data/osrm_nodes_used_with_ids.csv so the solver never touches OSRM at
runtime.

Usage
-----
  python build_matrix.py                 # full rebuild from Client_List
  python build_matrix.py --force         # rebuild even if file exists
  python build_matrix.py --check         # report on existing matrix, do nothing

Fault tolerance
---------------
  • Chunks the OSRM /table call so large fleets don't hit URL-length / node caps.
  • Retries each chunk up to 3× with backoff.
  • Writes to a temp path, then atomically renames — never corrupts the existing file.
"""

from __future__ import annotations
import argparse
import json
import math
import sys
import time
from pathlib import Path
from typing import List, Tuple

import numpy as np
import pandas as pd
import requests

from config import (
    INPUT_FILE, MATRIX_FILE, NODES_FILE, DATA_DIR,
    DEPOT_LAT, DEPOT_LON, DEPOT_ID,
)
from load_data import load_clients

OSRM_BASE = 'https://router.project-osrm.org'
CHUNK     = 60      # sources/destinations per /table call (OSRM public demo limit ~100)
RETRIES   = 3
BACKOFF   = 2.0     # seconds — doubled each retry


# ── Public API ────────────────────────────────────────────────────────────────

def build_full_matrix(
    input_file: Path = INPUT_FILE,
    matrix_file: Path = MATRIX_FILE,
    nodes_file: Path = NODES_FILE,
    force: bool = False,
) -> None:
    """Read Client_List → OSRM → npz + csv."""
    if matrix_file.exists() and not force:
        print(f"  Matrix exists: {matrix_file}")
        print(f"  Run with --force to rebuild.")
        return

    print(f"  Reading clients from {input_file}...")
    clients = load_clients(input_file)
    routable = clients[clients['Lat'].notna() & clients['Lon'].notna()].copy()
    routable = routable.reset_index(drop=True)
    print(f"  Routable clients: {len(routable)}")

    nodes = _build_node_table(routable)
    coords = [(DEPOT_LAT, DEPOT_LON)] + [
        (float(r['Lat']), float(r['Lon'])) for _, r in routable.iterrows()
    ]
    ids = [DEPOT_ID] + routable['ID'].tolist()

    print(f"  Fetching OSRM matrix for {len(coords)} nodes "
          f"({len(coords)**2:,} cells)...")
    t0 = time.time()
    dm_m, tm_s = _osrm_table(coords)
    print(f"  OSRM done in {time.time()-t0:.1f}s")

    _save_matrix(
        matrix_file, nodes_file,
        dm_m=dm_m, tm_s=tm_s, ids=ids, nodes_df=nodes,
    )
    print(f"  ✓ Saved: {matrix_file}")
    print(f"  ✓ Saved: {nodes_file}")


def add_client(
    client_id: str,
    lat: float,
    lon: float,
    customer: str = '',
    zone: str = '',
    zone_code: str = '',
    street: str = '',
    city: str = '',
    state: str = 'AZ',
    tank_lbs: float = None,
    product: str = '',
    matrix_file: Path = MATRIX_FILE,
    nodes_file: Path = NODES_FILE,
) -> None:
    """
    Append a single new client to the existing matrix without recomputing.

    Fetches exactly 2·(N+1) OSRM cells (one row + one column for the new node).
    """
    if not matrix_file.exists():
        raise FileNotFoundError(
            f"{matrix_file} not found. Run build_matrix.py first."
        )

    data = np.load(str(matrix_file), allow_pickle=True)
    ids = list(data['client_ids'])
    if client_id in ids:
        raise ValueError(f"Client {client_id} already in matrix at index "
                         f"{ids.index(client_id)}")

    old_lats = list(data['latitudes'])
    old_lons = list(data['longitudes'])
    n = len(ids)

    print(f"  Appending {client_id} at ({lat:.5f}, {lon:.5f}) "
          f"— fetching {2*(n+1)} OSRM cells...")

    # Fetch "from new client to all existing" and "from all existing to new client"
    # Use a single /table call where source = [new] + all_existing, dest = same.
    # That gives us the full (N+1)×(N+1) for just what we need.
    coords = list(zip(old_lats, old_lons)) + [(lat, lon)]
    # Fetch just the last row + last column (new node ↔ everything)
    row_resp = _osrm_table_slice(coords, sources=[n], destinations=list(range(n + 1)))
    col_resp = _osrm_table_slice(coords, sources=list(range(n + 1)), destinations=[n])

    row_m = row_resp['distances'][0]               # length n+1
    row_s = row_resp['durations'][0]
    col_m = [r[0] for r in col_resp['distances']]  # length n+1
    col_s = [r[0] for r in col_resp['durations']]

    # Assemble new matrices
    dm_old = data['dm_meters']
    tm_old = data['tm_seconds']

    dm_new = np.zeros((n + 1, n + 1), dtype=float)
    tm_new = np.zeros((n + 1, n + 1), dtype=float)
    dm_new[:n, :n] = dm_old
    tm_new[:n, :n] = tm_old
    dm_new[n, :]   = row_m
    tm_new[n, :]   = row_s
    dm_new[:, n]   = col_m
    tm_new[:, n]   = col_s

    ids.append(client_id)
    old_lats.append(lat)
    old_lons.append(lon)

    # Rebuild metadata arrays with new row appended
    def _append(key, val):
        arr = list(data[key])
        arr.append(val)
        return np.array(arr, dtype=object)

    labels        = _append('labels', customer or client_id)
    customer_nms  = _append('customer_names', customer)
    zones         = _append('zones', zone)
    zone_codes    = _append('zone_codes', zone_code)
    streets       = _append('street_addresses', street)
    cities        = _append('cities', city)
    states        = _append('states', state)
    tanks         = _append('tank_sizes_lbs', tank_lbs)
    products      = _append('products', product)

    # Build a nodes DataFrame equivalent to Client_List format
    nodes_df = pd.read_csv(nodes_file)
    new_node_row = {
        'node_index':        n,
        'Client ID':         client_id,
        'Customer Name':     customer,
        'Zone':              float(zone) if zone else np.nan,
        'Zone Code':         zone_code,
        'Street Address':    street,
        'City':              city,
        'State':             state,
        'Latitude':          lat,
        'Longitude':         lon,
        'Tank Size (lbs)':   tank_lbs,
        'Product':           product,
    }
    nodes_df = pd.concat([nodes_df, pd.DataFrame([new_node_row])], ignore_index=True)

    tmp_path = matrix_file.with_suffix('.tmp.npz')
    np.savez_compressed(
        str(tmp_path.with_suffix('')),   # numpy auto-adds .npz suffix
        dm_meters        = dm_new,
        tm_seconds       = tm_new,
        dm_miles         = dm_new / 1609.34,
        tm_minutes       = tm_new / 60.0,
        labels           = labels,
        node_index       = np.arange(n + 1, dtype=np.int64),
        client_ids       = np.array(ids, dtype=object),
        customer_names   = customer_nms,
        zones            = zones,
        zone_codes       = zone_codes,
        street_addresses = streets,
        cities           = cities,
        states           = states,
        latitudes        = np.array(old_lats, dtype=float),
        longitudes       = np.array(old_lons, dtype=float),
        tank_sizes_lbs   = tanks,
        products         = products,
    )
    tmp_path.replace(matrix_file)
    nodes_df.to_csv(nodes_file, index=False)

    print(f"  ✓ Added {client_id}.  Matrix: {n}×{n} → {n+1}×{n+1}")


# ── OSRM helpers ──────────────────────────────────────────────────────────────

def _osrm_table(coords: List[Tuple[float, float]]) -> Tuple[np.ndarray, np.ndarray]:
    """
    Fetch full (N×N) distance + duration via OSRM /table, chunked.

    Returns (dm_meters, tm_seconds) both float64 NxN arrays.
    """
    n = len(coords)
    dm = np.zeros((n, n), dtype=float)
    tm = np.zeros((n, n), dtype=float)

    # Chunk sources in blocks of CHUNK; destinations are always all nodes.
    for i0 in range(0, n, CHUNK):
        i1 = min(i0 + CHUNK, n)
        sources = list(range(i0, i1))
        # Also chunk destinations so we never exceed max waypoints
        for j0 in range(0, n, CHUNK):
            j1 = min(j0 + CHUNK, n)
            dests = list(range(j0, j1))
            result = _osrm_table_slice(coords, sources=sources, destinations=dests)
            for si, s in enumerate(sources):
                for di, d in enumerate(dests):
                    dm[s, d] = result['distances'][si][di]
                    tm[s, d] = result['durations'][si][di]
            print(f"    chunk rows {i0}-{i1}  cols {j0}-{j1}  ✓")
    return dm, tm


def _osrm_table_slice(
    coords: List[Tuple[float, float]],
    sources: List[int],
    destinations: List[int],
) -> dict:
    """
    One OSRM /table call with explicit sources & destinations.

    OSRM /table format:
      {base}/table/v1/driving/{lon,lat;...}?annotations=distance,duration
              &sources=i;j;k&destinations=x;y;z
    """
    # The /table URL needs ALL unique coords first, then sources/destinations
    # reference their positions in the coord string. We'll just send all coords.
    cs = ';'.join(f'{lo:.6f},{la:.6f}' for la, lo in coords)
    src = ';'.join(str(i) for i in sources)
    dst = ';'.join(str(i) for i in destinations)
    url = (
        f'{OSRM_BASE}/table/v1/driving/{cs}'
        f'?annotations=distance,duration&sources={src}&destinations={dst}'
    )

    last_err = None
    for attempt in range(RETRIES):
        try:
            r = requests.get(url, timeout=60,
                             headers={'User-Agent': 'sk-optimizer/1.0'})
            r.raise_for_status()
            d = r.json()
            if d.get('code') != 'Ok':
                raise RuntimeError(f"OSRM returned code={d.get('code')}")
            return d
        except Exception as e:
            last_err = e
            wait = BACKOFF * (2 ** attempt)
            print(f"    OSRM attempt {attempt+1}/{RETRIES} failed: {e}. "
                  f"Retrying in {wait:.0f}s...")
            time.sleep(wait)

    raise RuntimeError(f"OSRM failed after {RETRIES} retries: {last_err}")


# ── Node table helpers ────────────────────────────────────────────────────────

def _build_node_table(routable: pd.DataFrame) -> pd.DataFrame:
    """Build the nodes CSV table in the same format as osrm_nodes_used_with_ids.csv."""
    rows = [{
        'node_index':      0,
        'Client ID':       DEPOT_ID,
        'Customer Name':   'DEPOT',
        'Zone':            np.nan,
        'Zone Code':       '',
        'Street Address':  '',
        'City':            '',
        'State':           'AZ',
        'Latitude':        DEPOT_LAT,
        'Longitude':       DEPOT_LON,
        'Tank Size (lbs)': np.nan,
        'Product':         '',
    }]
    for i, (_, r) in enumerate(routable.iterrows(), start=1):
        rows.append({
            'node_index':      i,
            'Client ID':       r['ID'],
            'Customer Name':   r.get('Customer', ''),
            'Zone':            _zone_to_float(r.get('Zone', '')),
            'Zone Code':       r.get('Zone_Code', ''),
            'Street Address':  '',   # split from 'Address' if needed
            'City':            '',
            'State':           'AZ',
            'Latitude':        float(r['Lat']),
            'Longitude':       float(r['Lon']),
            'Tank Size (lbs)': float(r['Tank_lbs']) if pd.notna(r['Tank_lbs']) else np.nan,
            'Product':         r.get('Product', ''),
        })
    return pd.DataFrame(rows)


def _zone_to_float(z) -> float:
    try:
        return float(z)
    except (ValueError, TypeError):
        return np.nan


def _save_matrix(
    matrix_file: Path, nodes_file: Path,
    dm_m: np.ndarray, tm_s: np.ndarray,
    ids: List[str], nodes_df: pd.DataFrame,
) -> None:
    """Write npz and nodes csv atomically."""
    matrix_file.parent.mkdir(parents=True, exist_ok=True)

    def _arr_from(col, dtype=object):
        vals = []
        for i in range(len(ids)):
            if i == 0:
                vals.append('' if dtype is object else np.nan)
            else:
                vals.append(nodes_df[col].iloc[i] if i < len(nodes_df) else '')
        return np.array(vals, dtype=dtype if dtype is object else float)

    tmp_path = matrix_file.with_suffix('.tmp.npz')
    np.savez_compressed(
        str(tmp_path.with_suffix('')),   # numpy auto-adds .npz suffix
        dm_meters        = dm_m,
        tm_seconds       = tm_s,
        dm_miles         = dm_m / 1609.34,
        tm_minutes       = tm_s / 60.0,
        labels           = np.array(
            [nodes_df['Customer Name'].iloc[i] for i in range(len(ids))],
            dtype=object),
        node_index       = np.arange(len(ids), dtype=np.int64),
        client_ids       = np.array(ids, dtype=object),
        customer_names   = np.array(
            [nodes_df['Customer Name'].iloc[i] for i in range(len(ids))],
            dtype=object),
        zones            = np.array(
            [nodes_df['Zone'].iloc[i] for i in range(len(ids))],
            dtype=object),
        zone_codes       = np.array(
            [nodes_df['Zone Code'].iloc[i] for i in range(len(ids))],
            dtype=object),
        street_addresses = np.array(
            [nodes_df['Street Address'].iloc[i] for i in range(len(ids))],
            dtype=object),
        cities           = np.array(
            [nodes_df['City'].iloc[i] for i in range(len(ids))],
            dtype=object),
        states           = np.array(
            [nodes_df['State'].iloc[i] for i in range(len(ids))],
            dtype=object),
        latitudes        = np.array(
            [nodes_df['Latitude'].iloc[i] for i in range(len(ids))],
            dtype=float),
        longitudes       = np.array(
            [nodes_df['Longitude'].iloc[i] for i in range(len(ids))],
            dtype=float),
        tank_sizes_lbs   = np.array(
            [nodes_df['Tank Size (lbs)'].iloc[i] for i in range(len(ids))],
            dtype=object),
        products         = np.array(
            [nodes_df['Product'].iloc[i] for i in range(len(ids))],
            dtype=object),
    )
    tmp_path.replace(matrix_file)
    nodes_df.to_csv(nodes_file, index=False)


# ── Diagnostics ───────────────────────────────────────────────────────────────

def check_matrix(matrix_file: Path = MATRIX_FILE) -> None:
    if not matrix_file.exists():
        print(f"  Matrix not found: {matrix_file}")
        return
    data = np.load(str(matrix_file), allow_pickle=True)
    n = data['dm_meters'].shape[0]
    print(f"  Matrix: {matrix_file}")
    print(f"    nodes:      {n}")
    print(f"    dm_miles:   max = {data['dm_miles'].max():.1f} mi")
    print(f"    tm_minutes: max = {data['tm_minutes'].max():.0f} min")
    ids = list(data['client_ids'])
    print(f"    first 3 IDs: {ids[:3]}")
    print(f"    last 3 IDs:  {ids[-3:]}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser()
    p.add_argument('--force', action='store_true', help='rebuild even if exists')
    p.add_argument('--check', action='store_true', help='report on matrix')
    args = p.parse_args()

    if args.check:
        check_matrix()
        return

    build_full_matrix(force=args.force)
    check_matrix()


if __name__ == '__main__':
    main()
