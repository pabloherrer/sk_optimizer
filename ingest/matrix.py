"""
matrix_loader.py — OSRM distance/time matrix loader
=====================================================
Loads the precomputed OSRM matrix and applies the truck-speed factor.

Extracted from router.py during ADR-003 consolidation. The rest of
router.py (Phase-2 day TSP/CVRP code) was superseded by unified_solver
and has been archived.
"""
import numpy as np


def load_matrix(matrix_file: str) -> tuple[np.ndarray, np.ndarray, dict]:
    """
    Load the precomputed OSRM matrix.

    Returns
    -------
    dist_int       : (N×N) integer metres
    time_int_min   : (N×N) integer minutes (adjusted by TRUCK_SPEED_FACTOR)
    node_index_map : {client_id: matrix_row_index}
    """
    data = np.load(str(matrix_file), allow_pickle=True)

    from config import TRUCK_SPEED_FACTOR
    dm_m   = data['dm_meters'].astype(np.int64)
    tm_min = np.ceil(data['tm_seconds'] / 60 * TRUCK_SPEED_FACTOR).astype(np.int64)

    if 'client_ids' in data:
        ids = data['client_ids'].tolist()
    elif 'labels' in data:
        ids = data['labels'].tolist()
    else:
        raise KeyError("Matrix file has no 'client_ids' or 'labels' key.")

    node_index_map = {str(cid): i for i, cid in enumerate(ids)}
    print(f"  Matrix loaded: {dm_m.shape[0]} nodes  "
          f"| max dist {dm_m.max()/1000:.0f} km  "
          f"| max time {tm_min.max()} min")

    return dm_m, tm_min, node_index_map
