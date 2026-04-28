"""
spot_check_clients.py — Compare each rate method against SK's hand-scheduled
numbers for specific reference clients.

Reference clients (from SK's scheduler worksheet):
  - 51ST AVE CAFE     : SK rate ~6.9 lbs/day
  - CARDINAL STADIUM  : SK rate 33.1 lbs/day, last delivery 4/17 qty=1655, next=6/16
  - BOOTY GOODYEAR    : SK rate ~72.7 lbs/day (latest gap)
  - KRI-11016         : SK rate ~9.7 lbs/day

This is a sanity check, not a metric — "does method X produce numbers SK
operators would recognize?"
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import INPUT_FILE, OUTLIER_IQR_FACTOR
from load_data import load_all
from tests.bench_rate_methods import (
    METHODS, _remove_outliers, _build_per_client_history
)


# Known SK numbers (for ones the user gave us)
REFERENCE = {
    '51ST':     {'search': '51ST',      'sk_rate': 6.9,  'note': 'user confirmed 6.9 lbs/day'},
    'CAR-3063': {'search': 'CARDINAL',  'sk_rate': 33.1, 'note': 'user: last del 4/17 qty 1655, next 6/16'},
    'BOO-2023': {'search': 'BOOTY',     'sk_rate': 72.7, 'note': 'user: latest-gap value'},
    'KRI-11016':{'search': 'KRI',       'sk_rate': 9.7,  'note': 'user quoted value'},
}


def main():
    print('═' * 85)
    print('  SPOT-CHECK: each rate method vs SK\'s hand-scheduled numbers')
    print('═' * 85)

    _, deliveries = load_all(INPUT_FILE)
    histories = _build_per_client_history(deliveries)

    # Header
    method_names = list(METHODS.keys())
    h = f'  {"Client":<28s} {"n":>3s} {"SK":>6s} '
    for m in method_names:
        h += f'{m[:8]:>9s}'
    print(h)
    print('  ' + '-' * (len(h) - 2))

    for key, spec in REFERENCE.items():
        # Find the customer by substring match
        matches = [c for c in histories if spec['search'] in c.upper()]
        if not matches:
            print(f'  {key:<28s}  ✗ no match for "{spec["search"]}"')
            continue
        # Prefer first/best match
        cust = matches[0]
        hist = histories[cust]

        # Apply IQR filter to all gap rates (same as production)
        rates = hist['Rate'].dropna()
        rates_clean = _remove_outliers(rates)

        sk = spec['sk_rate']
        row = f'  {cust[:28]:<28s} {len(hist):>3d} {sk:>6.1f} '
        for m in method_names:
            try:
                val = METHODS[m](rates_clean)
            except Exception:
                val = None
            if val is None:
                row += f'{"n/a":>9s}'
            else:
                # Error vs SK benchmark (for quick scan)
                err_pct = abs(val - sk) / max(sk, 1e-6) * 100
                flag = '' if err_pct <= 25 else ('!' if err_pct <= 75 else '✗')
                row += f'{val:>8.1f}{flag:>1s}'
        print(row)

    print()
    print('  Legend: no flag = within 25% of SK; "!" = 25-75% off; "✗" = >75% off')
    print('  SK value treated as ground truth per user ("our consumption estimates must match reality")')
    return 0


if __name__ == '__main__':
    sys.exit(main())
