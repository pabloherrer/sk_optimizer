"""
v2.ingest — convert external inputs (Excel, Anova, OSRM matrix) into the
immutable domain objects the solver consumes.

Each module returns frozen / immutable types. No global state. All file
paths are explicit arguments — never hardcoded.
"""
from v2.ingest.excel import load_clients, load_deliveries
from v2.ingest.anova import load_anova_readings
from v2.ingest.schema import load_time_windows, load_closures, load_excluded_ids
from v2.ingest.matrix import load_matrix
from v2.ingest.overrides import load_overrides, OverrideValidationError
from v2.ingest.build_problem import build_problem_instance

__all__ = [
    'load_clients',
    'load_deliveries',
    'load_anova_readings',
    'load_time_windows',
    'load_closures',
    'load_excluded_ids',
    'load_matrix',
    'load_overrides',
    'OverrideValidationError',
    'build_problem_instance',
]
