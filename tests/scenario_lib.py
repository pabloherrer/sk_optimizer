"""
scenario_lib.py — Diverse synthetic scenarios for stress-testing the solver.

The original bench_ab uses a single synthetic-50 generator. That's a narrow
slice of data-space. This library produces scenarios across several axes:

  * Density:        urban (tight) / normal / rural (sparse)
  * Size:           tiny / small / medium / large / huge
  * Demand:         low (off-season) / balanced / peak
  * Windows:        none / sparse / dense / tight (hard to schedule)
  * Closures:       none / sporadic / heavy
  * Geography:      single-cluster / bi-cluster / scattered
  * Tank-mix:       uniform / heterogeneous
  * Product-mix:    single-product / 50-50 / 80-20
  * Topology:       euclidean / grid / asymmetric

Every generator returns the same scenario-dict shape expected by bench_ab's
_run_once() helper, so they're drop-in replacements.
"""

import numpy as np
import pandas as pd
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import PRODUCTS, DAYS, NUM_DAYS


def _euclid_matrix(coords):
    """Build integer dist (meters) + time (min) matrices from (lat,lon) list."""
    n = len(coords)
    dist = np.zeros((n, n), dtype=int)
    tm = np.zeros((n, n), dtype=int)
    for i in range(n):
        for j in range(n):
            if i != j:
                dx = (coords[i][0] - coords[j][0]) * 69.0  # deg lat → miles
                dy = (coords[i][1] - coords[j][1]) * 60.0  # deg lon → miles (AZ latitude)
                d_mi = (dx * dx + dy * dy) ** 0.5
                dist[i, j] = int(d_mi * 1609)
                tm[i, j] = max(1, int(d_mi * 2 + 1))
    return dist, tm


def _finalize(clients, coords, label, time_windows=None, closures=None):
    df = pd.DataFrame(clients)
    n = len(df)
    dist, tm = _euclid_matrix(coords)

    # Derive inventory fields if not already set
    if 'Current_lbs' not in df.columns:
        df['Current_lbs'] = (df['Tank_lbs'] * 0.4).astype(int)
    df['Est_Current_lbs'] = df['Current_lbs']
    df['Refill_lbs'] = (df['Tank_lbs'] - df['Current_lbs']).clip(lower=0)
    df['Days_Until_Stockout'] = (df['Current_lbs'] / df['Avg_LbsPerDay'].clip(lower=1)).clip(lower=0.5)
    if 'Urgency' not in df.columns:
        df['Urgency'] = 'normal'
    df['Refill_Today_lbs'] = df['Refill_lbs']
    df['Fill_Pct_Today'] = df['Refill_lbs'] / df['Tank_lbs']

    node_index_map = {'DEPOT': 0}
    for idx, cid in enumerate(df['ID'].tolist(), 1):
        node_index_map[cid] = idx

    tw_df = pd.DataFrame(time_windows or [],
                         columns=['Client_ID', 'Day_of_Week', 'Open_Min', 'Close_Min'])
    cl_df = pd.DataFrame(closures or [],
                         columns=['Client_ID', 'Start_Date', 'End_Date', 'Reason'])

    return {
        'label': label,
        'clients_df': df,
        'time_windows_df': tw_df,
        'closures_df': cl_df,
        'dist_matrix': dist,
        'time_matrix': tm,
        'node_index_map': node_index_map,
        'depot_config': {
            'depot_lat': 33.5, 'depot_lon': -112.1,
            'shift_start_min': 360, 'shift_end_min': 1080,
            'morning_load_min': 30, 'evening_unload_min': 15,
            'work_days': DAYS,
        },
    }


# ─── Density ────────────────────────────────────────────────────────────────

def urban(n=50, seed=42):
    """Tight cluster — heavy density; stops close together; measures dense-route performance."""
    rng = np.random.default_rng(seed)
    spread = 0.10
    clients = []
    coords = [(33.5, -112.1)]
    for i in range(n):
        lat = 33.45 + rng.uniform(0, spread)
        lon = -112.15 + rng.uniform(0, spread)
        coords.append((lat, lon))
        clients.append({
            'ID': f'U{i:04d}', 'Customer': f'Urban {i}',
            'Lat': lat, 'Lon': lon,
            'Tank_lbs': int(rng.choice([5000, 8000, 10000])),
            'Product': PRODUCTS[int(rng.integers(0, 2))],
            'Avg_LbsPerDay': int(rng.integers(150, 450)),
            'Days_Since_Last': int(rng.integers(3, 12)),
        })
    for c in clients:
        c['Current_lbs'] = int(c['Tank_lbs'] * rng.uniform(0.1, 0.6))
    return _finalize(clients, coords, f'urban-{n}')


def rural(n=30, seed=42):
    """Sparse scatter — big distances; measures over-long-route behavior."""
    rng = np.random.default_rng(seed)
    spread = 0.9
    clients = []
    coords = [(33.5, -112.1)]
    for i in range(n):
        lat = 33.0 + rng.uniform(0, spread)
        lon = -112.6 + rng.uniform(0, spread)
        coords.append((lat, lon))
        clients.append({
            'ID': f'R{i:04d}', 'Customer': f'Rural {i}',
            'Lat': lat, 'Lon': lon,
            'Tank_lbs': int(rng.choice([5000, 8000])),
            'Product': PRODUCTS[int(rng.integers(0, 2))],
            'Avg_LbsPerDay': int(rng.integers(50, 250)),
            'Days_Since_Last': int(rng.integers(5, 14)),
        })
    for c in clients:
        c['Current_lbs'] = int(c['Tank_lbs'] * rng.uniform(0.1, 0.5))
    return _finalize(clients, coords, f'rural-{n}')


def mixed(n=60, seed=42):
    """Standard mixed scatter, 0.4° box, heterogeneous tanks/rates."""
    rng = np.random.default_rng(seed)
    clients = []
    coords = [(33.5, -112.1)]
    for i in range(n):
        lat = 33.4 + rng.uniform(0, 0.4)
        lon = -112.3 + rng.uniform(0, 0.4)
        coords.append((lat, lon))
        clients.append({
            'ID': f'M{i:04d}', 'Customer': f'Mix {i}',
            'Lat': lat, 'Lon': lon,
            'Tank_lbs': int(rng.choice([5000, 8000, 10000])),
            'Product': PRODUCTS[int(rng.integers(0, 2))],
            'Avg_LbsPerDay': int(rng.integers(50, 400)),
            'Days_Since_Last': int(rng.integers(3, 12)),
        })
    for c in clients:
        c['Current_lbs'] = int(c['Tank_lbs'] * rng.uniform(0.1, 0.7))
    return _finalize(clients, coords, f'mixed-{n}')


# ─── Geography ───────────────────────────────────────────────────────────────

def bi_cluster(n=60, seed=42):
    """Two separated blobs — forces territory split."""
    rng = np.random.default_rng(seed)
    clients = []
    coords = [(33.5, -112.1)]
    for i in range(n):
        which = i % 2
        # Blob A: NE of depot, Blob B: SW of depot
        if which == 0:
            lat = 33.55 + rng.uniform(0, 0.10)
            lon = -112.00 + rng.uniform(0, 0.10)
        else:
            lat = 33.30 + rng.uniform(0, 0.10)
            lon = -112.30 + rng.uniform(0, 0.10)
        coords.append((lat, lon))
        clients.append({
            'ID': f'B{i:04d}', 'Customer': f'Blob{which} {i}',
            'Lat': lat, 'Lon': lon,
            'Tank_lbs': int(rng.choice([5000, 8000])),
            'Product': PRODUCTS[int(rng.integers(0, 2))],
            'Avg_LbsPerDay': int(rng.integers(80, 350)),
            'Days_Since_Last': int(rng.integers(3, 12)),
        })
    for c in clients:
        c['Current_lbs'] = int(c['Tank_lbs'] * rng.uniform(0.1, 0.6))
    return _finalize(clients, coords, f'bi-cluster-{n}')


# ─── Demand profile ──────────────────────────────────────────────────────────

def peak_season(n=60, seed=42):
    """High consumption — many clients become urgent/critical within a week."""
    rng = np.random.default_rng(seed)
    clients = []
    coords = [(33.5, -112.1)]
    for i in range(n):
        lat = 33.4 + rng.uniform(0, 0.4)
        lon = -112.3 + rng.uniform(0, 0.4)
        coords.append((lat, lon))
        clients.append({
            'ID': f'P{i:04d}', 'Customer': f'Peak {i}',
            'Lat': lat, 'Lon': lon,
            'Tank_lbs': int(rng.choice([5000, 8000, 10000])),
            'Product': PRODUCTS[int(rng.integers(0, 2))],
            'Avg_LbsPerDay': int(rng.integers(250, 600)),  # high rate
            'Days_Since_Last': int(rng.integers(5, 12)),
        })
    for c in clients:
        # Tanks run lower during peak
        c['Current_lbs'] = int(c['Tank_lbs'] * rng.uniform(0.05, 0.40))
    return _finalize(clients, coords, f'peak-{n}')


def off_season(n=40, seed=42):
    """Low consumption — many clients won't need service this week."""
    rng = np.random.default_rng(seed)
    clients = []
    coords = [(33.5, -112.1)]
    for i in range(n):
        lat = 33.4 + rng.uniform(0, 0.4)
        lon = -112.3 + rng.uniform(0, 0.4)
        coords.append((lat, lon))
        clients.append({
            'ID': f'O{i:04d}', 'Customer': f'Off {i}',
            'Lat': lat, 'Lon': lon,
            'Tank_lbs': int(rng.choice([5000, 8000, 10000])),
            'Product': PRODUCTS[int(rng.integers(0, 2))],
            'Avg_LbsPerDay': int(rng.integers(30, 150)),  # low rate
            'Days_Since_Last': int(rng.integers(1, 8)),
        })
    for c in clients:
        # Tanks still fairly full
        c['Current_lbs'] = int(c['Tank_lbs'] * rng.uniform(0.4, 0.85))
    return _finalize(clients, coords, f'off-{n}')


# ─── Time-window stress ──────────────────────────────────────────────────────

def tight_windows(n=40, tw_fraction=0.75, seed=42):
    """
    75% of clients have tight (2-hour) per-day windows. Solver must schedule
    visits precisely — failure mode is cascading infeasibility.
    """
    rng = np.random.default_rng(seed)
    clients = []
    coords = [(33.5, -112.1)]
    for i in range(n):
        lat = 33.4 + rng.uniform(0, 0.4)
        lon = -112.3 + rng.uniform(0, 0.4)
        coords.append((lat, lon))
        clients.append({
            'ID': f'T{i:04d}', 'Customer': f'Tight {i}',
            'Lat': lat, 'Lon': lon,
            'Tank_lbs': int(rng.choice([5000, 8000])),
            'Product': PRODUCTS[int(rng.integers(0, 2))],
            'Avg_LbsPerDay': int(rng.integers(100, 350)),
            'Days_Since_Last': int(rng.integers(3, 10)),
        })
    for c in clients:
        c['Current_lbs'] = int(c['Tank_lbs'] * rng.uniform(0.15, 0.55))

    # Scatter 2-hour windows across days
    tw = []
    for idx, c in enumerate(clients):
        if rng.random() < tw_fraction:
            # Random 2-hour window within business hours (7am-4pm)
            open_min = int(rng.choice([420, 480, 540, 600, 660, 720, 780, 840, 900]))  # 7am..3pm
            close_min = open_min + 120
            day = DAYS[int(rng.integers(0, NUM_DAYS))]
            tw.append({'Client_ID': c['ID'], 'Day_of_Week': day,
                       'Open_Min': open_min, 'Close_Min': close_min})
    return _finalize(clients, coords, f'tight-tw-{n}', time_windows=tw)


# ─── Closures stress ─────────────────────────────────────────────────────────

def heavy_closures(n=50, closure_fraction=0.3, seed=42):
    """30% of clients closed somewhere this week — tests closure handling."""
    rng = np.random.default_rng(seed)
    clients = []
    coords = [(33.5, -112.1)]
    for i in range(n):
        lat = 33.4 + rng.uniform(0, 0.4)
        lon = -112.3 + rng.uniform(0, 0.4)
        coords.append((lat, lon))
        clients.append({
            'ID': f'X{i:04d}', 'Customer': f'Closure {i}',
            'Lat': lat, 'Lon': lon,
            'Tank_lbs': int(rng.choice([5000, 8000])),
            'Product': PRODUCTS[int(rng.integers(0, 2))],
            'Avg_LbsPerDay': int(rng.integers(80, 300)),
            'Days_Since_Last': int(rng.integers(3, 12)),
        })
    for c in clients:
        c['Current_lbs'] = int(c['Tank_lbs'] * rng.uniform(0.15, 0.60))

    closures = []
    base_date = pd.Timestamp('2026-04-14')  # Tuesday
    for idx, c in enumerate(clients):
        if rng.random() < closure_fraction:
            # random contiguous 1-3 day closure in the week
            start_offset = int(rng.integers(0, NUM_DAYS))
            span = int(rng.integers(1, 4))
            closures.append({
                'Client_ID': c['ID'],
                'Start_Date': base_date + pd.Timedelta(days=start_offset),
                'End_Date': base_date + pd.Timedelta(days=start_offset + span - 1),
                'Reason': 'Holiday'
            })
    return _finalize(clients, coords, f'closure-heavy-{n}', closures=closures)


# ─── Edge cases ──────────────────────────────────────────────────────────────

def tiny(n=3, seed=42):
    """3 clients only — bottom end."""
    return mixed(n=n, seed=seed) | {'label': f'tiny-{n}'}


def huge(n=200, seed=42):
    """200 clients — scale limit."""
    # Copy mixed() logic but bigger
    rng = np.random.default_rng(seed)
    clients = []
    coords = [(33.5, -112.1)]
    for i in range(n):
        lat = 33.3 + rng.uniform(0, 0.6)
        lon = -112.4 + rng.uniform(0, 0.6)
        coords.append((lat, lon))
        clients.append({
            'ID': f'H{i:04d}', 'Customer': f'Huge {i}',
            'Lat': lat, 'Lon': lon,
            'Tank_lbs': int(rng.choice([5000, 8000, 10000])),
            'Product': PRODUCTS[int(rng.integers(0, 2))],
            'Avg_LbsPerDay': int(rng.integers(50, 400)),
            'Days_Since_Last': int(rng.integers(3, 14)),
        })
    for c in clients:
        c['Current_lbs'] = int(c['Tank_lbs'] * rng.uniform(0.1, 0.7))
    return _finalize(clients, coords, f'huge-{n}')


def single_client():
    """1 client."""
    clients = [{
        'ID': 'S0001', 'Customer': 'Solo',
        'Lat': 33.55, 'Lon': -112.05,
        'Tank_lbs': 8000, 'Product': PRODUCTS[0],
        'Avg_LbsPerDay': 200, 'Days_Since_Last': 7,
        'Current_lbs': 3200,
    }]
    return _finalize(clients, [(33.5, -112.1), (33.55, -112.05)], 'single')


def colocated(n=5):
    """All clients at same lat/lon — degenerate routing."""
    clients = []
    coords = [(33.5, -112.1)]
    for i in range(n):
        clients.append({
            'ID': f'CL{i:04d}', 'Customer': f'Col {i}',
            'Lat': 33.55, 'Lon': -112.05,
            'Tank_lbs': 5000, 'Product': PRODUCTS[i % 2],
            'Avg_LbsPerDay': 200, 'Days_Since_Last': 7,
            'Current_lbs': 2000,
        })
        coords.append((33.55, -112.05))
    return _finalize(clients, coords, f'colocated-{n}')


def all_urgent(n=30, seed=42):
    """Everyone's tank is near-empty — tests stockout-priority logic."""
    rng = np.random.default_rng(seed)
    clients = []
    coords = [(33.5, -112.1)]
    for i in range(n):
        lat = 33.4 + rng.uniform(0, 0.4)
        lon = -112.3 + rng.uniform(0, 0.4)
        coords.append((lat, lon))
        clients.append({
            'ID': f'UG{i:04d}', 'Customer': f'Urgent {i}',
            'Lat': lat, 'Lon': lon,
            'Tank_lbs': int(rng.choice([5000, 8000])),
            'Product': PRODUCTS[int(rng.integers(0, 2))],
            'Avg_LbsPerDay': int(rng.integers(200, 500)),
            'Days_Since_Last': int(rng.integers(8, 16)),
        })
    for c in clients:
        c['Current_lbs'] = int(c['Tank_lbs'] * rng.uniform(0.03, 0.15))  # very low
    return _finalize(clients, coords, f'all-urgent-{n}')


# ─── Registry ────────────────────────────────────────────────────────────────

REGISTRY = {
    'urban-50':         lambda: urban(50, 42),
    'rural-30':         lambda: rural(30, 42),
    'mixed-60':         lambda: mixed(60, 42),
    'bi-cluster-60':    lambda: bi_cluster(60, 42),
    'peak-60':          lambda: peak_season(60, 42),
    'off-40':           lambda: off_season(40, 42),
    'tight-tw-40':      lambda: tight_windows(40, 0.75, 42),
    'closure-heavy-50': lambda: heavy_closures(50, 0.30, 42),
    'tiny-3':           lambda: tiny(3, 42),
    'single':           lambda: single_client(),
    'colocated-5':      lambda: colocated(5),
    'all-urgent-30':    lambda: all_urgent(30, 42),
    'huge-200':         lambda: huge(200, 42),
}


def get_scenario(name):
    if name not in REGISTRY:
        raise ValueError(f"unknown scenario {name}. Known: {list(REGISTRY.keys())}")
    return REGISTRY[name]()


if __name__ == '__main__':
    # Quick smoke — build each, print stats
    for name in REGISTRY:
        s = get_scenario(name)
        df = s['clients_df']
        print(f"{name:<22s}  n={len(df):3d}  "
              f"avg_tank={df['Tank_lbs'].mean():.0f}  "
              f"avg_lbs/day={df['Avg_LbsPerDay'].mean():.0f}  "
              f"med_dus={df['Days_Until_Stockout'].median():.1f}  "
              f"n_tw={len(s['time_windows_df'])}  "
              f"n_closures={len(s['closures_df'])}")
