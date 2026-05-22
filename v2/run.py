"""
v2.run — CLI entry point for the v2 solver.

Usage:
    python -m v2.run                       # plan starting tomorrow
    python -m v2.run --today 2026-05-22    # plan starting specified date
    python -m v2.run --solve-sec 60        # short solve (for testing)
    python -m v2.run --input-file ...      # override input Excel path
"""
from __future__ import annotations
import argparse
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

# Make v2 imports work when run as `python -m v2.run` or `python v2/run.py`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    parser = argparse.ArgumentParser(description='SK Route Optimizer v2')
    parser.add_argument('--today', default=None,
                        help='Plan start date (YYYY-MM-DD). Default: tomorrow.')
    parser.add_argument('--solve-sec', type=int, default=180,
                        help='Solver time limit in seconds (default: 180)')
    parser.add_argument('--input-file', type=Path, default=None,
                        help='SK_Delivery_System.xlsx path')
    parser.add_argument('--matrix-file', type=Path, default=None,
                        help='OSRM matrix .npz path')
    parser.add_argument('--config-dir', type=Path, default=None,
                        help='Directory with economics/fleet/policy YAMLs')
    parser.add_argument('--output-dir', type=Path, default=None,
                        help='Where to write Excel/PDFs/CSV/map')
    parser.add_argument('--state-file', type=Path, default=None,
                        help='Persistent state JSON path')
    args = parser.parse_args()

    # Resolve defaults
    base = Path(__file__).resolve().parent
    config_dir = args.config_dir or (base / 'config')
    repo_root = base.parent

    # Resolve input_file in priority order (matches v1 behavior):
    #   1. --input-file CLI flag
    #   2. SK_INPUT_FILE environment variable
    #   3. local_config.json::input_file  (OPERATOR's pointer — usually OneDrive)
    #   4. fallback: data/SK_Delivery_System.xlsx (likely STALE)
    import os, json
    input_file = args.input_file
    if input_file is None:
        env_path = os.environ.get('SK_INPUT_FILE')
        if env_path:
            input_file = Path(env_path)
    if input_file is None:
        local_cfg = repo_root / 'local_config.json'
        if local_cfg.exists():
            try:
                cfg_path = json.loads(local_cfg.read_text(encoding='utf-8')).get('input_file')
                if cfg_path:
                    input_file = Path(cfg_path)
            except Exception:
                pass
    if input_file is None:
        input_file = repo_root / 'data' / 'SK_Delivery_System.xlsx'
        print(f"⚠ No local_config.json input_file — falling back to (possibly stale) "
              f"{input_file.name}", file=sys.stderr)

    matrix_file = args.matrix_file or (repo_root / 'data' / 'osrm_full_matrix_with_ids.npz')
    output_dir = args.output_dir or (repo_root / 'v2_output')
    state_file = args.state_file or (repo_root / 'data' / 'inventory_state_v2.json')

    print(f"  Input file:  {input_file}")
    print(f"  Last modified: {datetime.fromtimestamp(input_file.stat().st_mtime).strftime('%Y-%m-%d %H:%M') if input_file.exists() else 'MISSING'}")

    if args.today:
        today = date.fromisoformat(args.today)
    else:
        # Default: tomorrow (planning the next workday)
        today = date.today() + timedelta(days=1)

    # Validate input files exist
    for p in (input_file, matrix_file, config_dir):
        if not p.exists():
            print(f"ERROR: required file/dir not found: {p}", file=sys.stderr)
            return 2

    from v2.pipeline import plan_day
    result = plan_day(
        today=today,
        config_dir=config_dir,
        input_file=input_file,
        matrix_file=matrix_file,
        output_dir=output_dir,
        state_file=state_file,
        solve_seconds=args.solve_sec,
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
