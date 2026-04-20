"""
scheduler.py — Phase 1: Cluster-Based Day Assignment
=====================================================
The scheduling unit is the GEOGRAPHIC CLUSTER, not the individual client.
Clients in the same neighbourhood always get served together on the same
day by the same truck.  No hardcoded territory assignment — the optimizer
is free to assign any truck to any cluster on any day.

Cost model
----------
  Dominant cost: depot-to-area round-trip (~60 km, $50+ per trip).
  Marginal cost: one more stop in-area (~2 km detour, ~$8).

  ⇒  Never visit the same area twice in one week.
  ⇒  When you're there, fill everyone whose tank has room (≥ 50%).

Algorithm
---------
1. K-means sub-clustering on ALL routable clients → tight geographic zones.
2. For each cluster, compute urgency and active member count.
3. Cluster-level EDF: sort by urgency, assign each cluster to the (truck, day)
   with the most remaining capacity.  No truck territory constraint.
4. On the assigned day, fill ALL cluster members with fill ≥ OPPORTUNISTIC_FILL_PCT.
"""

import math
import numpy as np
import pandas as pd
from typing import Optional, Tuple, List, Dict
from config import (
    DAYS, NUM_DAYS, TRUCKS, TRUCK_NAMES, NUM_TRUCKS,
    MIN_FILL_PCT, SOFT_MIN_FILL_PCT, CRITICAL_DAYS, URGENT_DAYS,
    URGENCY_WEIGHTS, ACCOUNT_ALPHA, BALANCE_WEIGHT,
    DEPOT_LAT, DEPOT_LON,
    OPPORTUNISTIC_FILL_PCT,
)
from inventory import compute_refill, fill_efficiency, days_until_stockout, urgency_tier

# ── Clustering parameters ─────────────────────────────────────────────────────
TARGET_CLUSTER_SIZE = 16


# ── Public API ────────────────────────────────────────────────────────────────

def assign_customers_to_days(
    clients_df: pd.DataFrame,
    start_day:  int = 0,
) -> pd.DataFrame:
    routable = (
        clients_df['Lat'].notna() & clients_df['Lon'].notna()
        & clients_df['Tank_lbs'].notna() & (clients_df['Tank_lbs'] > 0)
        & clients_df['Avg_LbsPerDay'].notna() & (clients_df['Avg_LbsPerDay'] > 0)
        & clients_df['Current_lbs'].notna()
    )
    df_rt   = clients_df[routable].copy().reset_index(drop=True)
    df_skip = clients_df[~routable].copy()

    _init_output_cols(df_rt)
    n = len(df_rt)

    # ── Phase 0: Geographic sub-clustering (no territory — free assignment) ──
    df_rt['SubCluster'] = _assign_subclusters(df_rt)

    # ── Account weights ──────────────────────────────────────────────────────
    max_tank = df_rt['Tank_lbs'].max()
    max_rate = df_rt['Avg_LbsPerDay'].max()
    df_rt['_acct_w'] = (
        ACCOUNT_ALPHA * df_rt['Tank_lbs'] / max_tank
        + (1 - ACCOUNT_ALPHA) * df_rt['Avg_LbsPerDay'] / max_rate
    ).values

    # ── Per-slot state ────────────────────────────────────────────────────────
    active_slots = [(t, d) for d in range(start_day, NUM_DAYS) for t in TRUCK_NAMES]
    slot_cap     = {s: TRUCKS[s[0]]['capacity_lbs'] for s in active_slots}
    slot_lat     = {s: [] for s in active_slots}
    slot_lon     = {s: [] for s in active_slots}
    assigned     = set()

    # ── Refill / fill / days-left matrices ────────────────────────────────────
    refill_mat    = np.zeros((n, NUM_DAYS))
    fill_pct_mat  = np.zeros((n, NUM_DAYS))
    days_left_mat = np.zeros((n, NUM_DAYS))

    for i in range(n):
        row = df_rt.iloc[i]
        for d in range(start_day, NUM_DAYS):
            refill = compute_refill(row['Current_lbs'], row['Avg_LbsPerDay'],
                                    d, row['Tank_lbs'])
            fill   = refill / row['Tank_lbs'] if row['Tank_lbs'] > 0 else 0.0
            level  = row['Tank_lbs'] - refill
            dl     = days_until_stockout(level, row['Avg_LbsPerDay'], row['Tank_lbs'])
            refill_mat[i, d]    = refill
            fill_pct_mat[i, d]  = fill
            days_left_mat[i, d] = dl

    # ── Build cluster profiles ────────────────────────────────────────────────
    cluster_ids = sorted(df_rt['SubCluster'].unique())
    clusters = []

    for cid in cluster_ids:
        members = df_rt.index[df_rt['SubCluster'] == cid].tolist()

        active_members = []
        for i in members:
            if any(fill_pct_mat[i, d] >= OPPORTUNISTIC_FILL_PCT
                   for d in range(start_day, NUM_DAYS)):
                active_members.append(i)

        if not active_members:
            continue

        min_stockout = min(df_rt.iloc[i]['Days_Until_Stockout'] for i in active_members)
        has_critical = any(
            df_rt.iloc[i]['Days_Until_Stockout'] <= CRITICAL_DAYS
            for i in active_members
        )

        # Earliest day any member becomes eligible
        earliest = NUM_DAYS
        for i in active_members:
            for d in range(start_day, NUM_DAYS):
                if fill_pct_mat[i, d] >= OPPORTUNISTIC_FILL_PCT:
                    earliest = min(earliest, d)
                    break
        if earliest >= NUM_DAYS:
            earliest = start_day

        # Estimate total demand on earliest day
        demand_est = sum(refill_mat[i, earliest] for i in active_members
                         if fill_pct_mat[i, earliest] >= OPPORTUNISTIC_FILL_PCT)

        clusters.append({
            'id':             cid,
            'members':        active_members,
            'all_members':    members,
            'min_stockout':   min_stockout,
            'has_critical':   has_critical,
            'earliest':       earliest,
            'demand_est':     demand_est,
        })

    # ── Cluster scheduling: EDF + balance (no territory constraint) ──────────
    MIN_CLUSTER_ACTIVE = 3

    schedulable = [
        c for c in clusters
        if (len(c['members']) >= MIN_CLUSTER_ACTIVE or c['has_critical'])
    ]
    schedulable.sort(key=lambda c: (c['min_stockout'],))

    for cluster in schedulable:
        if all(i in assigned for i in cluster['members']):
            continue

        best_truck = best_day = None
        best_score = float('-inf')

        for d in range(cluster['earliest'], NUM_DAYS):
            day_demand = sum(
                refill_mat[i, d]
                for i in cluster['members']
                if i not in assigned and fill_pct_mat[i, d] >= OPPORTUNISTIC_FILL_PCT
            )
            if day_demand <= 0:
                continue

            for truck in TRUCK_NAMES:
                if slot_cap.get((truck, d), 0) < day_demand * 0.6:
                    continue
                used    = TRUCKS[truck]['capacity_lbs'] - slot_cap[(truck, d)]
                balance = 1.0 - used / TRUCKS[truck]['capacity_lbs']
                if balance > best_score:
                    best_score = balance
                    best_truck, best_day = truck, d

        if best_truck is None:
            continue

        _schedule_cluster(df_rt, cluster, best_truck, best_day,
                          refill_mat, fill_pct_mat, days_left_mat,
                          slot_cap, slot_lat, slot_lon, assigned)

    # ── Cleanup ──────────────────────────────────────────────────────────────
    df_rt.drop(columns=['_acct_w'], inplace=True)

    result = pd.concat([
        df_rt,
        df_skip.assign(
            SubCluster=-1,
            AssignedTruck='Deferred', AssignedDay='Deferred',
            AssignedDayIndex=np.nan, ProjectedRefill_lbs=0.0,
            Fill_Pct_Assigned=0.0, VisitScore=0.0,
            GeoScore=0.0, DaysToStockoutAtVisit=np.nan,
        ),
    ], ignore_index=True)

    _print_summary(result, slot_cap, active_slots, start_day)
    return result


# ── Cluster scheduling helper ─────────────────────────────────────────────────

def _schedule_cluster(
    df_rt, cluster, truck, day,
    refill_mat, fill_pct_mat, days_left_mat,
    slot_cap, slot_lat, slot_lon, assigned,
):
    placed = 0
    for i in cluster['members']:
        if i in assigned:
            continue
        row    = df_rt.iloc[i]
        refill = refill_mat[i, day]
        fill   = fill_pct_mat[i, day]

        if fill < OPPORTUNISTIC_FILL_PCT:
            continue

        if slot_cap.get((truck, day), 0) < refill:
            continue

        tier    = urgency_tier(days_left_mat[i, day])
        urgency = URGENCY_WEIGHTS.get(tier, 1.0)
        score   = fill * row['_acct_w'] * urgency

        _assign(df_rt, i, truck, day, refill, score, days_left_mat[i, day])
        slot_lat[(truck, day)].append(row['Lat'])
        slot_lon[(truck, day)].append(row['Lon'])
        slot_cap[(truck, day)] -= refill
        assigned.add(i)
        placed += 1

    if placed:
        lats = [df_rt.iloc[i]['Lat'] for i in cluster['members'] if i in assigned
                and df_rt.iloc[i]['AssignedDayIndex'] == day]
        lons = [df_rt.iloc[i]['Lon'] for i in cluster['members'] if i in assigned
                and df_rt.iloc[i]['AssignedDayIndex'] == day]
        if lats:
            lon_span = abs(max(lons) - min(lons)) * 111.32 * math.cos(math.radians(sum(lats)/len(lats)))
            print(f"  Cluster {cluster['id']:>2d} → {truck}/{DAYS[day]}:"
                  f" {placed} stops | {lon_span:.0f} km spread")


# ── Clustering ─────────────────────────────────────────────────────────────────

def _kmeans_n(lats, lons, k, n_iter=50):
    """K-means with k-means++ init, Haversine distance."""
    n = len(lats)
    if n <= k:
        return np.arange(n)

    rng     = np.random.RandomState(42)
    centers = np.zeros((k, 2))
    idx0    = rng.randint(n)
    centers[0] = [lats[idx0], lons[idx0]]

    for c in range(1, k):
        dists = np.array([
            min(_haversine_km(lats[i], lons[i], centers[j, 0], centers[j, 1]) ** 2
                for j in range(c))
            for i in range(n)
        ])
        probs = dists / (dists.sum() + 1e-12)
        chosen = rng.choice(n, p=probs)
        centers[c] = [lats[chosen], lons[chosen]]

    labels = np.zeros(n, dtype=int)
    for _ in range(n_iter):
        for i in range(n):
            dists = [_haversine_km(lats[i], lons[i], centers[c, 0], centers[c, 1])
                     for c in range(k)]
            labels[i] = int(np.argmin(dists))
        new_centers = np.zeros_like(centers)
        for c in range(k):
            mask = labels == c
            if mask.any():
                new_centers[c] = [lats[mask].mean(), lons[mask].mean()]
            else:
                new_centers[c] = centers[c]
        if np.allclose(new_centers, centers, atol=1e-6):
            break
        centers = new_centers
    return labels


def _assign_subclusters(df_rt):
    """Global geographic sub-clustering — no territory constraints."""
    lats = df_rt['Lat'].values.astype(float)
    lons = df_rt['Lon'].values.astype(float)

    k = max(3, round(len(lats) / TARGET_CLUSTER_SIZE))
    labels = _kmeans_n(lats, lons, k)

    for c in range(k):
        mask = labels == c
        if not mask.any():
            continue
        c_lats, c_lons = lats[mask], lons[mask]
        span = abs(c_lons.max() - c_lons.min()) * 111.32 * math.cos(math.radians(c_lats.mean()))
        print(f"    cluster {c}: {mask.sum()} clients  span {span:.0f} km  "
              f"lon [{c_lons.min():.3f}–{c_lons.max():.3f}]")

    return pd.Series(labels, index=df_rt.index, dtype=int)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _haversine_km(lat1, lon1, lat2, lon2):
    R  = 6371.0
    φ1 = math.radians(lat1); φ2 = math.radians(lat2)
    dφ = math.radians(lat2 - lat1)
    dλ = math.radians(lon2 - lon1)
    a  = math.sin(dφ / 2) ** 2 + math.cos(φ1) * math.cos(φ2) * math.sin(dλ / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _assign(df, i, truck, day, refill, score, days_left):
    row = df.iloc[i]
    df.at[i, 'AssignedTruck']         = truck
    df.at[i, 'AssignedDayIndex']      = day
    df.at[i, 'AssignedDay']           = DAYS[day]
    df.at[i, 'ProjectedRefill_lbs']   = round(refill)
    df.at[i, 'Fill_Pct_Assigned']     = round(refill / row['Tank_lbs'] * 100, 1)
    df.at[i, 'VisitScore']            = round(score, 4)
    df.at[i, 'GeoScore']              = round(score, 4)
    df.at[i, 'DaysToStockoutAtVisit'] = round(days_left, 1)


def _init_output_cols(df):
    for col, val in [
        ('SubCluster',          -1),
        ('AssignedTruck',       'Deferred'),
        ('AssignedDay',         'Deferred'),
        ('AssignedDayIndex',    np.nan),
        ('ProjectedRefill_lbs', 0.0),
        ('Fill_Pct_Assigned',   0.0),
        ('VisitScore',          0.0),
        ('GeoScore',            0.0),
        ('DaysToStockoutAtVisit', np.nan),
    ]:
        df[col] = val


def _print_summary(df, slot_cap, active_slots, start_day):
    print("\nPhase 1 — Cluster Assignment Summary:")
    print(f"  {'Slot':<14} {'Clients':>7} {'Refill lbs':>11} {'Cap %':>7}  Lon range")
    print(f"  {'-' * 60}")
    for truck in TRUCK_NAMES:
        for d in range(start_day, NUM_DAYS):
            sub = df[(df['AssignedTruck'] == truck) & (df['AssignedDayIndex'] == d)]
            if sub.empty:
                continue
            load   = sub['ProjectedRefill_lbs'].sum()
            cap    = TRUCKS[truck]['capacity_lbs']
            pct    = load / cap * 100
            marker = ' ⚠' if pct > 90 else ''
            lon_rng = f"{sub['Lon'].min():.3f}–{sub['Lon'].max():.3f}"
            clusters = sorted(sub['SubCluster'].unique())
            print(f"  {truck}/{DAYS[d]:<8} {len(sub):>7} {load:>11,.0f} {pct:>6.1f}%{marker}"
                  f"  [{lon_rng}]  cl {clusters}")

    active   = df[~df['AssignedDay'].isin(['Deferred', 'OVERFLOW'])]
    deferred = df[df['AssignedDay'] == 'Deferred']
    overflow = df[df['AssignedDay'] == 'OVERFLOW']
    print(f"  {'Active':<14} {len(active):>7}")
    print(f"  {'Deferred':<14} {len(deferred):>7}")
    if len(overflow):
        print(f"  ⚠  {len(overflow)} client(s) overflowed capacity")
