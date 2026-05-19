"""
territory.py — Stable truck zone assignment via angular sweep
=============================================================
Computes a soft geographic partition of the client pool into two truck
territories. Used by Phase 1 day assignment as a preference, not a hard
constraint.

Algorithm (from Consistent VRP literature — Groër, Golden & Wasil 2009):
  1. Compute bearing from depot to each client.
  2. Sort clients by bearing.
  3. Walk the sorted list and split into two groups such that the demand
     imbalance is minimized.
  4. Assign the two groups to Truck2 and Truck9.

Far-cluster clients are assigned to whichever truck centroid is closer,
so they don't distort the metro sweep.
"""

import math
from typing import Dict, List, Tuple

DEPOT_LAT = 33.5152
DEPOT_LON = -112.1674


def _bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Compass bearing from point 1 to point 2, in degrees [0, 360)."""
    lat1r, lon1r = math.radians(lat1), math.radians(lon1)
    lat2r, lon2r = math.radians(lat2), math.radians(lon2)
    dlon = lon2r - lon1r
    x = math.sin(dlon) * math.cos(lat2r)
    y = math.cos(lat1r) * math.sin(lat2r) - math.sin(lat1r) * math.cos(lat2r) * math.cos(dlon)
    brng = math.atan2(x, y)
    return (math.degrees(brng) + 360) % 360


def _haversine_mi(lat1, lon1, lat2, lon2):
    R = 3958.8
    lat1r, lon1r, lat2r, lon2r = map(math.radians, [lat1, lon1, lat2, lon2])
    dlat = lat2r - lat1r
    dlon = lon2r - lon1r
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1r) * math.cos(lat2r) * math.sin(dlon / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


def assign_truck_territories(
    client_ids: List[str],
    lats: List[float],
    lons: List[float],
    demands: List[float],
    is_far: List[bool],
    truck_names: List[str],
    depot_lat: float = DEPOT_LAT,
    depot_lon: float = DEPOT_LON,
) -> Dict[str, str]:
    """
    Assign each client to a preferred truck based on geography.

    Returns
    -------
    {client_id: truck_name}  — soft preference, not a hard constraint.
    """
    n = len(client_ids)
    if n == 0:
        return {}
    if len(truck_names) < 2:
        return {cid: truck_names[0] for cid in client_ids}

    # Separate metro and far clients
    metro_idx = [i for i in range(n) if not is_far[i]]
    far_idx = [i for i in range(n) if is_far[i]]

    # --- Metro: angular sweep + demand-balanced split ---
    bearings = [(i, _bearing(depot_lat, depot_lon, lats[i], lons[i])) for i in metro_idx]
    bearings.sort(key=lambda x: x[1])

    sorted_metro = [b[0] for b in bearings]
    total_demand = sum(demands[i] for i in sorted_metro) or 1.0

    # Find the split point that minimizes demand imbalance
    best_split = len(sorted_metro) // 2
    best_imbalance = float('inf')
    running_sum = 0.0
    for k in range(1, len(sorted_metro)):
        running_sum += demands[sorted_metro[k - 1]]
        imbalance = abs(running_sum - (total_demand - running_sum))
        if imbalance < best_imbalance:
            best_imbalance = imbalance
            best_split = k

    group_a = set(sorted_metro[:best_split])
    group_b = set(sorted_metro[best_split:])

    assignment: Dict[str, str] = {}
    for i in group_a:
        assignment[client_ids[i]] = truck_names[0]
    for i in group_b:
        assignment[client_ids[i]] = truck_names[1]

    # --- Far clients: assign to nearest truck centroid ---
    if far_idx and group_a and group_b:
        centroid_a_lat = sum(lats[i] for i in group_a) / len(group_a)
        centroid_a_lon = sum(lons[i] for i in group_a) / len(group_a)
        centroid_b_lat = sum(lats[i] for i in group_b) / len(group_b)
        centroid_b_lon = sum(lons[i] for i in group_b) / len(group_b)

        for i in far_idx:
            d_a = _haversine_mi(lats[i], lons[i], centroid_a_lat, centroid_a_lon)
            d_b = _haversine_mi(lats[i], lons[i], centroid_b_lat, centroid_b_lon)
            assignment[client_ids[i]] = truck_names[0] if d_a <= d_b else truck_names[1]
    elif far_idx:
        # Only one group has clients — assign all far to the other
        for i in far_idx:
            assignment[client_ids[i]] = truck_names[1] if group_a else truck_names[0]

    return assignment
