"""
v2.ingest.matrix — OSRM distance/time matrix loader.

The matrix is precomputed by an offline OSRM call against the depot +
every client. Stored as an .npz with keys:
  dm_meters    — int meters, N×N
  tm_seconds   — int seconds, N×N    (car speed, raw)
  client_ids   — list of length N    (node 0 = depot, 1..N-1 = clients)

This loader applies a configurable truck-speed factor (loaded trucks are
slower than cars: 1.25× is the default for SK's metro fleet) and returns:
  • distance matrix in meters (int64)
  • time matrix in minutes (int64, with the speed factor applied)
  • node-index map {client_id → row index}
"""
from __future__ import annotations

from pathlib import Path

import numpy as np


_DEFAULT_TRUCK_SPEED_FACTOR = 1.25


def load_matrix(
    matrix_file: Path,
    truck_speed_factor: float = _DEFAULT_TRUCK_SPEED_FACTOR,
) -> tuple[np.ndarray, np.ndarray, dict[str, int]]:
    """
    Load the precomputed OSRM matrix.

    Parameters
    ----------
    matrix_file        : path to the .npz produced by the offline OSRM build
    truck_speed_factor : multiplier on car-speed travel times
                          (1.0  = unchanged car speed,
                           1.25 = trucks 25% slower than OSRM cars)

    Returns
    -------
    (distance_matrix_m, time_matrix_min, node_index_map)
      distance_matrix_m : (N×N) int64 meters
      time_matrix_min   : (N×N) int64 minutes, with speed factor applied
      node_index_map    : {client_id: row_index}
    """
    matrix_file = Path(matrix_file)
    data = np.load(str(matrix_file), allow_pickle=True)

    dm_m = data['dm_meters'].astype(np.int64)
    tm_min = np.ceil(
        data['tm_seconds'] / 60.0 * float(truck_speed_factor)
    ).astype(np.int64)

    if 'client_ids' in data.files:
        ids = data['client_ids'].tolist()
    elif 'labels' in data.files:
        ids = data['labels'].tolist()
    else:
        raise KeyError(
            "Matrix file has no 'client_ids' or 'labels' key."
        )

    node_index_map: dict[str, int] = {str(cid): i for i, cid in enumerate(ids)}
    return dm_m, tm_min, node_index_map
