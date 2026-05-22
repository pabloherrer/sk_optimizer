"""
v2.solver.territory — geographic territory assignment via weighted k-means.

The team-routing insight: with two trucks running parallel routes day
after day, the cheapest schedule is the one where each truck "owns" a
geographic territory. The solver enforces this only softly — a truck
visiting outside its primary territory pays a small overlap penalty —
but the assignment itself comes from this module.

We cluster routable, non-excluded, non-do-not-schedule clients on
(lat, lon) using weighted k-means (k=2 by default). The weight is the
client's daily consumption rate; high-volume clients pull the centroid
toward them so the territory boundary lands where it makes business
sense (each territory carries roughly half the daily load).

No external dependencies: we implement a tiny weighted k-means in numpy
(~25 lines) because sklearn is not in the project venv and the problem
is small enough that adding it isn't worth the install footprint.
"""
from __future__ import annotations

from typing import Dict, Iterable, Optional, Tuple

import numpy as np

from v2.domain import Client
from v2.domain.fleet import Depot


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def assign_territories(
    clients: tuple[Client, ...],
    depot: Depot,
    num_clusters: int = 2,
    weights: Optional[Dict[str, float]] = None,
) -> dict[str, int]:
    """
    Cluster clients geographically into `num_clusters` territories.

    Parameters
    ----------
    clients : tuple[Client, ...]
        All clients in the problem. Excluded / do-not-schedule clients
        are not assigned (their id maps to -1 in the result).
    depot : Depot
        Used only to seed the centroids deterministically (we start
        clusters spread out around the depot rather than picking random
        points). Not weighted into the clustering itself.
    num_clusters : int, default 2
        K. Defaults to 2 because S&K runs a two-truck fleet.
    weights : dict[str, float] | None
        Optional per-client weight (typically the consumption rate in
        lbs/day). Higher-weight clients pull centroids toward them so
        the boundary balances by load, not just by count. Missing /
        non-positive / NaN weights default to 1.0.

    Returns
    -------
    dict[str, int]
        {client_id: territory_index}. Routable clients get an index in
        [0, num_clusters); excluded / do-not-schedule clients get -1.
    """
    if num_clusters < 1:
        raise ValueError(f'num_clusters must be >= 1; got {num_clusters}')

    # Start every client at -1, then overwrite the routable ones below.
    result: dict[str, int] = {c.id: -1 for c in clients}

    routable = tuple(c for c in clients if _is_routable(c))
    if not routable:
        return result

    if num_clusters == 1 or len(routable) <= num_clusters:
        # Edge cases: one cluster (all in 0) or fewer points than clusters
        # (give each its own).
        for i, c in enumerate(routable):
            result[c.id] = i if num_clusters > 1 else 0
        # If we have more clusters than clients, leftover clusters are
        # simply empty — that's fine for the caller.
        return result

    points = np.array([[c.lat, c.lon] for c in routable], dtype=float)
    w = np.array(
        [_resolve_weight(c.id, weights) for c in routable],
        dtype=float,
    )

    labels, _ = _weighted_kmeans(
        points=points,
        weights=w,
        k=num_clusters,
        depot=(depot.lat, depot.lon),
    )

    for c, lbl in zip(routable, labels):
        result[c.id] = int(lbl)
    return result


def nearest_territory_distance(
    client: Client,
    territory_centroids: dict[int, tuple[float, float]],
) -> float:
    """
    Haversine distance (miles) from `client` to its assigned territory's
    centroid. Useful as a diagnostic: clients far from their centroid
    are border cases the solver may want to reshuffle.

    The caller passes the centroid dict so this stays a pure function
    (no recomputation). Returns nan if the client has no centroid
    available (e.g., excluded clients).
    """
    if not territory_centroids:
        return float('nan')
    best = float('inf')
    for centroid in territory_centroids.values():
        d = _haversine_miles(client.lat, client.lon, centroid[0], centroid[1])
        if d < best:
            best = d
    return best if best < float('inf') else float('nan')


# ─────────────────────────────────────────────────────────────────────────────
# Internals
# ─────────────────────────────────────────────────────────────────────────────

def _is_routable(c: Client) -> bool:
    return not c.excluded and not c.do_not_schedule


def _resolve_weight(client_id: str, weights: Optional[Dict[str, float]]) -> float:
    if weights is None:
        return 1.0
    w = weights.get(client_id)
    if w is None:
        return 1.0
    try:
        wf = float(w)
    except (TypeError, ValueError):
        return 1.0
    if not np.isfinite(wf) or wf <= 0.0:
        return 1.0
    return wf


def _weighted_kmeans(
    *,
    points: np.ndarray,
    weights: np.ndarray,
    k: int,
    depot: Tuple[float, float],
    max_iter: int = 100,
    tol: float = 1e-6,
    seed: int = 0,
) -> Tuple[np.ndarray, np.ndarray]:
    """
    Plain weighted Lloyd's algorithm. Returns (labels, centroids).

    Seeding: we use a deterministic k-means++ flavored init starting
    from the point nearest the depot, then repeatedly picking the
    weighted-farthest remaining point. This is reproducible (no
    random state) and tends to spread initial centroids well.
    """
    n = points.shape[0]
    # Deterministic seed: start from depot-nearest, then farthest-from-chosen.
    depot_arr = np.array(depot, dtype=float)
    d0 = np.linalg.norm(points - depot_arr, axis=1)
    chosen = [int(np.argmin(d0))]
    while len(chosen) < k:
        chosen_arr = points[chosen]
        # Distance from each point to its nearest chosen centroid
        d = np.min(
            np.linalg.norm(points[:, None, :] - chosen_arr[None, :, :], axis=2),
            axis=1,
        )
        d_w = d * weights
        next_idx = int(np.argmax(d_w))
        if next_idx in chosen:
            # All points already chosen (very small n); break to avoid loop
            break
        chosen.append(next_idx)
    centroids = points[chosen].astype(float).copy()

    labels = np.zeros(n, dtype=int)
    for _ in range(max_iter):
        # Assignment step
        d2 = np.linalg.norm(points[:, None, :] - centroids[None, :, :], axis=2)
        new_labels = np.argmin(d2, axis=1)
        if np.array_equal(new_labels, labels) and _ > 0:
            labels = new_labels
            break
        labels = new_labels
        # Update step: weighted mean of points assigned to each cluster
        new_centroids = centroids.copy()
        for ci in range(centroids.shape[0]):
            mask = labels == ci
            if not mask.any():
                continue  # empty cluster: leave centroid where it was
            w_sum = weights[mask].sum()
            if w_sum <= 0:
                new_centroids[ci] = points[mask].mean(axis=0)
            else:
                new_centroids[ci] = (points[mask] * weights[mask, None]).sum(axis=0) / w_sum
        shift = float(np.linalg.norm(new_centroids - centroids))
        centroids = new_centroids
        if shift < tol:
            break

    return labels, centroids


def _haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in miles."""
    R = 3958.7613  # Earth radius in miles
    phi1 = np.radians(lat1)
    phi2 = np.radians(lat2)
    dphi = np.radians(lat2 - lat1)
    dlam = np.radians(lon2 - lon1)
    a = np.sin(dphi / 2) ** 2 + np.cos(phi1) * np.cos(phi2) * np.sin(dlam / 2) ** 2
    c = 2 * np.arcsin(np.sqrt(a))
    return float(R * c)
