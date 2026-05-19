"""
main.py — CLI Entry Point
=========================
Run the S&K rolling-horizon route optimizer from the command line.

Usage examples
--------------
# Full week plan (Monday morning — first run)
python main.py

# Re-plan from Tuesday (after Monday's deliveries are confirmed)
python main.py --day tue

# Re-plan from Wednesday with a custom state file
python main.py --day wed --state data/my_state.json

# Update inventory state after today's deliveries (no new solve)
python main.py --update --day mon --delivered C001,C003,C007

# Diagnose current pool without solving (dry-run)
python main.py --diagnose

Outputs (saved to output/ folder)
----------------------------------
  sk_weekly_schedule.xlsx   — formatted Excel route sheets
  sk_route_map.html         — interactive Folium map
  data/inventory_state.json — rolling inventory state (auto-updated)
"""

import argparse
import logging
import sys
from pathlib import Path

import pandas as pd

# ── Make sure the package root is importable regardless of where you run from ─
sys.path.insert(0, str(Path(__file__).parent))

from config import (
    INPUT_FILE, MATRIX_FILE, STATE_FILE, OUTPUT_DIR, DAYS,
)
from rolling_optimizer import RollingHorizonOptimizer
from diagnostics import generate_report, get_deferred
from output import save_excel_schedule, save_route_map

logging.basicConfig(
    level=logging.WARNING,
    format='%(levelname)s  %(name)s  %(message)s',
)


# ── Day name → index ─────────────────────────────────────────────────────────
DAY_MAP = {d.lower(): i for i, d in enumerate(DAYS)}
DAY_MAP.update({'monday': 0, 'tuesday': 1, 'wednesday': 2,
                'thursday': 3, 'friday': 4})


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description='S&K Oil Sales — Rolling Route Optimizer',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        '--day', default='mon',
        help="Start day for (re-)planning: mon|tue|wed|thu|fri (default: mon)"
    )
    p.add_argument(
        '--state', default=str(STATE_FILE),
        help="Path to inventory state JSON (default: data/inventory_state.json)"
    )
    p.add_argument(
        '--input', default=str(INPUT_FILE),
        help="Path to SK_Delivery_System.xlsx"
    )
    p.add_argument(
        '--matrix', default=str(MATRIX_FILE),
        help="Path to precomputed .npz distance matrix"
    )
    p.add_argument(
        '--update', action='store_true',
        help="Update inventory state after today's deliveries (no new solve)"
    )
    p.add_argument(
        '--delivered', default='',
        help="Comma-separated client IDs delivered today (used with --update)"
    )
    p.add_argument(
        '--days-elapsed', type=int, default=1,
        help="Days elapsed since last run (default 1; use 3 for Mon after Fri)"
    )
    p.add_argument(
        '--diagnose', action='store_true',
        help="Print pool diagnostics only — do not solve"
    )
    p.add_argument(
        '--no-excel', action='store_true',
        help="Skip Excel output"
    )
    p.add_argument(
        '--no-map', action='store_true',
        help="Skip HTML map output"
    )
    p.add_argument(
        '--today', default=None,
        help="Override today's date (YYYY-MM-DD) — useful for back-testing"
    )
    return p.parse_args()


def main() -> None:
    args   = parse_args()
    today  = pd.Timestamp(args.today) if args.today else None
    day_idx = DAY_MAP.get(args.day.lower())

    if day_idx is None:
        print(f"ERROR: Unknown day '{args.day}'. Use mon|tue|wed|thu|fri.")
        sys.exit(1)

    # ── Build optimizer ────────────────────────────────────────────────────
    opt = RollingHorizonOptimizer.build(
        input_file  = args.input,
        matrix_file = args.matrix,
        state_file  = args.state,
        today       = today,
    )

    # ── Update-only mode ───────────────────────────────────────────────────
    if args.update:
        delivered = [x.strip() for x in args.delivered.split(',') if x.strip()]
        if not delivered:
            print("--update requires --delivered with at least one client ID.")
            sys.exit(1)
        opt.update_after_day(
            day_index      = day_idx,
            delivered_ids  = delivered,
            n_days_elapsed = args.days_elapsed,
            state_file     = args.state,
        )
        print(f"\n✓ Inventory state updated for {len(delivered)} deliveries.")
        return

    # ── Diagnose-only mode ─────────────────────────────────────────────────
    if args.diagnose:
        from inventory import enrich_snapshot
        snapshot = enrich_snapshot(opt.clients_df, opt.inventory_state)
        urgency = snapshot['Urgency'].value_counts()
        print("\nCurrent inventory pool:")
        for tier in ['stockout', 'critical', 'urgent', 'normal']:
            n = urgency.get(tier, 0)
            print(f"  {tier:<10}: {n:>3}")

        print("\nTop 20 most urgent clients:")
        cols = ['ID', 'Customer', 'Zone', 'Days_Until_Stockout',
                'Fill_Pct_Today', 'Tank_lbs', 'Avg_LbsPerDay']
        print(
            snapshot.nsmallest(20, 'Days_Until_Stockout')[cols]
            .to_string(index=False)
        )
        return

    # ── Full solve ─────────────────────────────────────────────────────────
    all_routes = opt.plan_week(start_day=day_idx)

    # Diagnostics report
    diag_df = generate_report(all_routes, opt.last_assignment, opt.clients_df)

    # Deferred clients
    deferred = get_deferred(opt.last_assignment)

    # ── Save outputs ───────────────────────────────────────────────────────
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    if not args.no_excel:
        excel_path = save_excel_schedule(all_routes, deferred_df=deferred)
        print(f"\n[Output] {excel_path}")

    if not args.no_map:
        map_path = save_route_map(all_routes)
        print(f"[Output] {map_path}")

    print("\n✓ Done.")


if __name__ == '__main__':
    main()
