"""
build_matrix.py — rebuild the OSRM distance/time matrix.

Run this whenever a NEW client is added to the spreadsheet (and the
solver's Deferred sheet lists them as `NOT_IN_MATRIX`).

The matrix is `data/osrm_full_matrix_with_ids.npz`. It's pre-computed
so the solver never hits OSRM at runtime — fast, deterministic,
offline-safe.

Usage (from sk_optimizer/):
    .venv/bin/python -m final.build_matrix              # rebuild if needed
    .venv/bin/python -m final.build_matrix --force      # rebuild always
    .venv/bin/python -m final.build_matrix --check      # report, don't write
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
import requests

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))

from v2.ingest.excel import load_clients
from v2.ingest.schema import load_depot_config

OSRM_BASE = 'https://router.project-osrm.org'
CHUNK = 60       # OSRM public demo /table cap
RETRIES = 3
BACKOFF = 2.0    # seconds — doubled per retry


def main(argv=None):
    parser = argparse.ArgumentParser(description='Rebuild OSRM distance matrix.')
    parser.add_argument('--input-file', type=Path, default=None)
    parser.add_argument('--matrix-file', type=Path,
                         default=REPO / 'data' / 'osrm_full_matrix_with_ids.npz')
    parser.add_argument('--force', action='store_true',
                         help='Rebuild even if file exists')
    parser.add_argument('--check', action='store_true',
                         help='Report on existing matrix, do not modify')
    args = parser.parse_args(argv)

    matrix_file = args.matrix_file
    input_file = args.input_file
    if input_file is None:
        local_cfg = REPO / 'local_config.json'
        if local_cfg.exists():
            cfg = json.loads(local_cfg.read_text(encoding='utf-8'))
            cfg_path = cfg.get('input_file')
            if cfg_path:
                input_file = Path(cfg_path)
    if input_file is None or not input_file.exists():
        print(f'ERROR: input file not found: {input_file}', file=sys.stderr)
        return 2

    print(f'  Input:  {input_file}')
    print(f'  Matrix: {matrix_file}')

    # Load clients from spreadsheet
    print('  Loading clients from Optimizer_Input...')
    clients = load_clients(input_file)
    routable = [c for c in clients if c.lat is not None and c.lon is not None]
    print(f'  Routable clients: {len(routable)}')

    # Depot
    depot_cfg = load_depot_config(input_file)
    depot_lat = depot_cfg.get('depot_lat', 33.5152)
    depot_lon = depot_cfg.get('depot_lon', -112.1674)
    print(f'  Depot: ({depot_lat}, {depot_lon})')

    if args.check:
        if matrix_file.exists():
            data = np.load(matrix_file, allow_pickle=True)
            ids_in_matrix = list(data['client_ids'])
            ids_in_sheet = ['DEPOT'] + [c.id for c in routable]
            missing = [c for c in routable if c.id not in ids_in_matrix]
            extra = [i for i in ids_in_matrix if i != 'DEPOT' and i not in {c.id for c in routable}]
            print(f'\n  Matrix has {len(ids_in_matrix)} entries')
            print(f'  Spreadsheet has {len(ids_in_sheet)} routable entries (incl DEPOT)')
            if missing:
                print(f'\n  ⚠ {len(missing)} client(s) MISSING from matrix:')
                for c in missing:
                    print(f'    {c.id:>8}  {c.customer[:40]}')
            else:
                print('\n  ✓ All spreadsheet clients are in the matrix')
            if extra:
                print(f'\n  ⓘ {len(extra)} matrix entries not in current spreadsheet '
                       '(may be deleted clients — OK to leave):')
                for cid in extra[:10]: print(f'    {cid}')
        else:
            print(f'\n  ⚠ Matrix file does not exist: {matrix_file}')
        return 0

    # Decide whether to rebuild
    if matrix_file.exists() and not args.force:
        data = np.load(matrix_file, allow_pickle=True)
        ids_in_matrix = set(data['client_ids'])
        missing = [c for c in routable if c.id not in ids_in_matrix]
        if not missing:
            print('  ✓ Matrix is up to date. Use --force to rebuild anyway.')
            return 0
        print(f'  → {len(missing)} new client(s) found, rebuilding:')
        for c in missing:
            print(f'    {c.id}  {c.customer[:40]}')

    # Build the matrix
    coords = [(depot_lat, depot_lon)] + [(c.lat, c.lon) for c in routable]
    ids = ['DEPOT'] + [c.id for c in routable]
    labels = ['DEPOT'] + [f'{c.id} {c.customer}' for c in routable]

    print(f'\n  Fetching OSRM table for {len(coords)} nodes '
          f'({len(coords)**2:,} cells, {math.ceil(len(coords)/CHUNK)} chunks each side)...')
    t0 = time.time()
    dm_m, tm_s = _osrm_table(coords)
    print(f'  OSRM done in {time.time()-t0:.1f}s')

    # Convert to derived units
    dm_miles = dm_m / 1609.34
    tm_min = tm_s / 60.0

    # Build extra metadata to mirror what the existing matrix carries
    customer_names = ['DEPOT'] + [c.customer for c in routable]
    zones = [''] + [getattr(c, 'zone', '') or '' for c in routable]
    zone_codes = [''] + [getattr(c, 'zone_code', '') or '' for c in routable]
    addresses = [''] + [c.address or '' for c in routable]
    cities = [''] + [getattr(c, 'city', '') or '' for c in routable]
    states = [''] + [getattr(c, 'state', '') or '' for c in routable]
    lats = [depot_lat] + [c.lat for c in routable]
    lons = [depot_lon] + [c.lon for c in routable]
    tank_sizes = [0] + [c.tank_capacity_lbs for c in routable]
    products = [''] + [c.product or '' for c in routable]
    node_index = {cid: i for i, cid in enumerate(ids)}

    # Atomic write — np.savez_compressed auto-appends .npz, so give it a
    # base path that DOESN'T end in .npz; it produces <stem>.tmp.npz which
    # we then rename to the final file.
    tmp_base = matrix_file.with_suffix('.tmp')        # …osrm_full_matrix_with_ids.tmp
    tmp_actual = Path(str(tmp_base) + '.npz')         # …osrm_full_matrix_with_ids.tmp.npz
    np.savez_compressed(
        tmp_base,
        dm_meters=dm_m.astype(np.int32),
        tm_seconds=tm_s.astype(np.int32),
        dm_miles=dm_miles.astype(np.float32),
        tm_minutes=tm_min.astype(np.float32),
        labels=np.array(labels, dtype=object),
        node_index=np.array(node_index, dtype=object),
        client_ids=np.array(ids, dtype=object),
        customer_names=np.array(customer_names, dtype=object),
        zones=np.array(zones, dtype=object),
        zone_codes=np.array(zone_codes, dtype=object),
        street_addresses=np.array(addresses, dtype=object),
        cities=np.array(cities, dtype=object),
        states=np.array(states, dtype=object),
        latitudes=np.array(lats, dtype=np.float64),
        longitudes=np.array(lons, dtype=np.float64),
        tank_sizes_lbs=np.array(tank_sizes, dtype=np.float64),
        products=np.array(products, dtype=object),
    )
    tmp_actual.replace(matrix_file)
    print(f'\n  ✓ Wrote {matrix_file}')
    print(f'    {len(ids)} nodes, {dm_m.shape[0]*dm_m.shape[1]:,} cells, '
          f'{matrix_file.stat().st_size/1024:.0f} KB')
    return 0


# ── OSRM table call with chunking + retries ──────────────────────────────

def _osrm_table(coords: List[Tuple[float, float]]):
    """Build full N×N (distance, duration) matrices by chunking OSRM /table."""
    n = len(coords)
    dm = np.zeros((n, n), dtype=np.float64)
    tm = np.zeros((n, n), dtype=np.float64)

    for i_lo in range(0, n, CHUNK):
        i_hi = min(i_lo + CHUNK, n)
        for j_lo in range(0, n, CHUNK):
            j_hi = min(j_lo + CHUNK, n)
            sub_coords = coords[i_lo:i_hi] + coords[j_lo:j_hi]
            n_src = i_hi - i_lo
            n_dst = j_hi - j_lo
            sources = list(range(0, n_src))
            destinations = list(range(n_src, n_src + n_dst))

            url = (f'{OSRM_BASE}/table/v1/driving/'
                    + ';'.join(f'{lon},{lat}' for lat, lon in sub_coords)
                    + f'?sources={";".join(map(str, sources))}'
                    + f'&destinations={";".join(map(str, destinations))}'
                    + '&annotations=duration,distance')
            for attempt in range(RETRIES):
                try:
                    print(f'    chunk [{i_lo}:{i_hi}]→[{j_lo}:{j_hi}] '
                          f'(attempt {attempt+1}) ...', end='', flush=True)
                    r = requests.get(url, timeout=120)
                    r.raise_for_status()
                    j = r.json()
                    durations = j.get('durations')
                    distances = j.get('distances')
                    if durations is None or distances is None:
                        raise ValueError(f'OSRM returned no annotations: {j}')
                    for li in range(n_src):
                        for lj in range(n_dst):
                            dur = durations[li][lj]
                            dis = distances[li][lj]
                            tm[i_lo + li, j_lo + lj] = dur if dur is not None else 0
                            dm[i_lo + li, j_lo + lj] = dis if dis is not None else 0
                    print(' ok')
                    break
                except Exception as e:
                    print(f' FAIL ({e})')
                    if attempt + 1 == RETRIES:
                        raise
                    time.sleep(BACKOFF * (attempt + 1))
    return dm, tm


if __name__ == '__main__':
    raise SystemExit(main())
