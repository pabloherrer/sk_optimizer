"""
bench_rate_methods.py — Backtest 6 rate-estimation methods on real SK delivery data.

Design
------
For each client with >= 3 deliveries in the real log:
  1. Hold out the LAST delivery.
  2. Using only deliveries strictly before that date, estimate the consumption
     rate via each of 6 methods.
  3. The actual rate realized at the held-out delivery is:
        actual_rate = held_out_qty / days_since_previous_delivery
  4. Per-method error = (predicted - actual).

Methods evaluated
-----------------
  1. mean_of_gaps      — average of all per-delivery rates (Qty_i / Gap_i)
  2. median_of_gaps    — median of per-delivery rates (robust to outliers)
  3. last_gap          — rate from the most-recent valid gap (CURRENT system)
  4. ewma_a30          — EWMA with alpha=0.3 (weighted mean, recent-heavy)
  5. ewma_a50          — EWMA with alpha=0.5
  6. blended_60_40     — 0.6 * last_gap + 0.4 * mean_of_gaps
  7. trailing_3        — mean of last 3 gap rates

All methods apply the same IQR outlier filter (factor = 3.0) to the gap rates
before aggregating, so the comparison isolates the aggregation choice.

Metrics reported per method
---------------------------
  - MAE  (Mean Absolute Error, lbs/day)
  - MAPE (Mean Absolute Percentage Error, %)
  - Median APE (robust to a few bad clients)
  - Bias (mean of signed error — positive = overestimates)
  - Max error
  - % predictions within 25% of actual
  - % predictions within 50% of actual

Outputs
-------
  - Markdown summary printed to stdout
  - CSV with per-client errors: output/bench_rate_methods_YYYYMMDD.csv
  - Markdown report: output/bench_rate_methods_YYYYMMDD.md
"""

import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import OUTPUT_DIR, OUTLIER_IQR_FACTOR, INPUT_FILE
from load_data import load_all


# ─── Rate-method implementations ────────────────────────────────────────────
# Each takes a sorted Series of (rate_per_delivery) and returns a single rate
# (or None if the series is too sparse). The IQR outlier filter is applied
# before any of these see the data.

def method_mean(rates: pd.Series) -> Optional[float]:
    if rates.empty:
        return None
    return float(rates.mean())


def method_median(rates: pd.Series) -> Optional[float]:
    if rates.empty:
        return None
    return float(rates.median())


def method_last(rates: pd.Series) -> Optional[float]:
    if rates.empty:
        return None
    return float(rates.iloc[-1])


def method_ewma_a30(rates: pd.Series) -> Optional[float]:
    if rates.empty:
        return None
    return float(rates.ewm(alpha=0.30, adjust=False).mean().iloc[-1])


def method_ewma_a50(rates: pd.Series) -> Optional[float]:
    if rates.empty:
        return None
    return float(rates.ewm(alpha=0.50, adjust=False).mean().iloc[-1])


def method_blended(rates: pd.Series) -> Optional[float]:
    if rates.empty:
        return None
    last = float(rates.iloc[-1])
    mean = float(rates.mean())
    return 0.6 * last + 0.4 * mean


def method_trailing_3(rates: pd.Series) -> Optional[float]:
    if rates.empty:
        return None
    tail = rates.tail(3)
    return float(tail.mean())


METHODS = {
    'mean_of_gaps':    method_mean,
    'median_of_gaps':  method_median,
    'last_gap':        method_last,      # current system
    'ewma_a30':        method_ewma_a30,
    'ewma_a50':        method_ewma_a50,
    'blended_60_40':   method_blended,
    'trailing_3':      method_trailing_3,
}


# ─── Helpers ────────────────────────────────────────────────────────────────

def _remove_outliers(rates: pd.Series, factor: float = OUTLIER_IQR_FACTOR) -> pd.Series:
    """Same IQR gate as forecast_consumption.py (upper only, no lower)."""
    rates = rates.dropna()
    if len(rates) < 3:
        return rates
    q1, q3 = rates.quantile([0.25, 0.75])
    upper = q3 + factor * (q3 - q1)
    return rates[rates <= upper]


def _build_per_client_history(deliveries: pd.DataFrame) -> dict:
    """
    Returns: { customer: DataFrame sorted by Date with columns
               [Date, Qty_lbs, Gap_days, Rate] }
    """
    d = deliveries.sort_values(['Customer', 'Date']).copy()
    d['Prev_Date'] = d.groupby('Customer')['Date'].shift(1)
    d['Gap_days'] = (d['Date'] - d['Prev_Date']).dt.days
    d['Rate'] = np.where(
        (d['Gap_days'] > 0) & d['Gap_days'].notna(),
        d['Qty_lbs'] / d['Gap_days'],
        np.nan,
    )
    out = {}
    for cust, grp in d.groupby('Customer'):
        out[cust] = grp.reset_index(drop=True)
    return out


def _backtest_client(hist: pd.DataFrame) -> Optional[dict]:
    """
    Hold out the last delivery. Use all prior deliveries to predict the rate,
    then compare to the rate actually realized at the held-out delivery.

    Returns a dict with per-method predictions and the actual, or None if
    the client has too few deliveries to backtest.
    """
    # Need at least 3 deliveries: 2 to form a prior rate + 1 to hold out
    if len(hist) < 3:
        return None

    train = hist.iloc[:-1]           # all but the last delivery
    test  = hist.iloc[-1]            # the one we predict

    # The actual rate at the held-out delivery — only valid if it has a gap
    if pd.isna(test['Rate']):
        return None
    actual = float(test['Rate'])

    # Gap rates from training set (last row of train has a valid gap if prev exists)
    train_rates = train['Rate'].dropna()
    if train_rates.empty:
        return None

    # Apply IQR filter, same as production code
    train_rates = _remove_outliers(train_rates)
    if train_rates.empty:
        return None

    preds = {}
    for name, fn in METHODS.items():
        try:
            p = fn(train_rates)
        except Exception:
            p = None
        preds[name] = p

    return {
        'Customer': hist['Customer'].iloc[0],
        'n_deliveries': int(len(hist)),
        'n_train_rates': int(len(train_rates)),
        'actual_rate': actual,
        'held_out_qty': float(test['Qty_lbs']),
        'held_out_gap_days': float(test['Gap_days']),
        **{f'pred_{k}': v for k, v in preds.items()},
    }


def _score_method(df: pd.DataFrame, method: str) -> dict:
    """Compute error metrics for one method across all backtested clients."""
    col = f'pred_{method}'
    sub = df[['actual_rate', col]].dropna()
    if sub.empty:
        return {'method': method, 'n': 0}

    y = sub['actual_rate'].values
    yh = sub[col].values
    err = yh - y
    abs_err = np.abs(err)
    pct_err = abs_err / np.maximum(y, 1e-6)

    within_25 = (pct_err <= 0.25).mean() * 100.0
    within_50 = (pct_err <= 0.50).mean() * 100.0

    return {
        'method':     method,
        'n':          int(len(sub)),
        'MAE':        float(abs_err.mean()),
        'MedianAE':   float(np.median(abs_err)),
        'MAPE_pct':   float(pct_err.mean() * 100),
        'MedianAPE_pct': float(np.median(pct_err) * 100),
        'Bias':       float(err.mean()),
        'MaxAbsErr':  float(abs_err.max()),
        'Within_25_pct': float(within_25),
        'Within_50_pct': float(within_50),
    }


# ─── Main ───────────────────────────────────────────────────────────────────

def run_benchmark(input_file: Optional[Path] = None, min_deliveries: int = 3) -> int:
    print('═' * 78)
    print('  RATE-METHOD BACKTEST — Real SK Delivery Data')
    print('═' * 78)

    print('\n  Loading delivery history...')
    clients_raw, deliveries = load_all(input_file or INPUT_FILE)
    print(f'  Loaded {len(deliveries):,} deliveries across {deliveries["Customer"].nunique()} customers')

    print(f'\n  Building per-client histories (need >= {min_deliveries} deliveries)...')
    histories = _build_per_client_history(deliveries)

    rows = []
    skipped_too_few = 0
    skipped_bad_gap = 0
    for cust, hist in histories.items():
        if len(hist) < min_deliveries:
            skipped_too_few += 1
            continue
        row = _backtest_client(hist)
        if row is None:
            skipped_bad_gap += 1
            continue
        rows.append(row)

    df = pd.DataFrame(rows)
    print(f'  Backtested {len(df)} clients  |  skipped {skipped_too_few} (too few)  '
          f'+ {skipped_bad_gap} (no valid held-out gap)')

    if df.empty:
        print('  ✗ Nothing to score.')
        return 1

    # ── Score each method ───────────────────────────────────────────────────
    print('\n' + '─' * 78)
    print('  METHOD COMPARISON (lower is better for MAE/MAPE/Bias; higher for Within_X)')
    print('─' * 78)
    scores = pd.DataFrame([_score_method(df, m) for m in METHODS])
    scores = scores.sort_values('MAE')

    # Pretty table
    print(f'\n  {"Method":<18s} {"n":>4s} {"MAE":>7s} {"MedAE":>7s} {"MAPE%":>7s} '
          f'{"MedAPE%":>8s} {"Bias":>8s} {"MaxErr":>8s} {"≤25%":>6s} {"≤50%":>6s}')
    print('  ' + '-' * 76)
    for _, r in scores.iterrows():
        print(f'  {r["method"]:<18s} {int(r["n"]):>4d} '
              f'{r["MAE"]:>7.2f} {r["MedianAE"]:>7.2f} '
              f'{r["MAPE_pct"]:>7.1f} {r["MedianAPE_pct"]:>8.1f} '
              f'{r["Bias"]:>+8.2f} {r["MaxAbsErr"]:>8.1f} '
              f'{r["Within_25_pct"]:>5.1f}% {r["Within_50_pct"]:>5.1f}%')

    # ── Rank methods on multiple criteria ───────────────────────────────────
    print('\n  Winners by criterion:')
    for col, label, smaller_better in [
        ('MAE',            'Mean absolute error',          True),
        ('MedianAE',       'Median absolute error',        True),
        ('MAPE_pct',       'Mean APE',                     True),
        ('MedianAPE_pct',  'Median APE (robust)',          True),
        ('Within_25_pct',  'Within 25% of actual',         False),
        ('Within_50_pct',  'Within 50% of actual',         False),
    ]:
        if smaller_better:
            winner = scores.loc[scores[col].idxmin()]
        else:
            winner = scores.loc[scores[col].idxmax()]
        print(f'    {label:<35s} → {winner["method"]:<18s} ({col}={winner[col]:.2f})')

    # ── Save outputs ────────────────────────────────────────────────────────
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    csv_path = OUTPUT_DIR / f'bench_rate_methods_{ts}.csv'
    md_path  = OUTPUT_DIR / f'bench_rate_methods_{ts}.md'

    df.to_csv(csv_path, index=False)
    print(f'\n  Per-client errors  → {csv_path.name}')

    with md_path.open('w') as f:
        f.write(f'# Rate-Method Backtest — {ts}\n\n')
        f.write(f'Backtested {len(df)} clients from real SK delivery data.\n\n')
        f.write('## Scoreboard (sorted by MAE)\n\n')
        f.write(scores.round(2).to_markdown(index=False))
        f.write('\n\n## Winners by criterion\n\n')
        for col, label, smaller in [
            ('MAE', 'Mean absolute error', True),
            ('MedianAE', 'Median absolute error', True),
            ('MAPE_pct', 'Mean APE', True),
            ('MedianAPE_pct', 'Median APE', True),
            ('Within_25_pct', 'Within 25%', False),
            ('Within_50_pct', 'Within 50%', False),
        ]:
            w = scores.loc[(scores[col].idxmin() if smaller else scores[col].idxmax())]
            f.write(f'- **{label}** → `{w["method"]}` ({col}={w[col]:.2f})\n')
    print(f'  Markdown report    → {md_path.name}')

    # ── Recommendation ──────────────────────────────────────────────────────
    # "Best" = lowest MedianAPE (robust) AND highest Within_25 — a combined test
    best_medape = scores.loc[scores['MedianAPE_pct'].idxmin(), 'method']
    best_within25 = scores.loc[scores['Within_25_pct'].idxmax(), 'method']
    current_method = 'last_gap'
    print('\n' + '═' * 78)
    if best_medape == best_within25:
        print(f'  RECOMMENDATION: Use `{best_medape}` — wins on robustness AND hit-rate.')
    else:
        print(f'  Median APE favors: `{best_medape}`')
        print(f'  Hit-rate  favors: `{best_within25}`')
    print(f'  Current system uses: `{current_method}`')
    cur_row = scores[scores['method'] == current_method].iloc[0]
    print(f'    current MedAPE={cur_row["MedianAPE_pct"]:.1f}%  '
          f'Within25={cur_row["Within_25_pct"]:.1f}%  '
          f'MAE={cur_row["MAE"]:.1f}')
    print('═' * 78)

    return 0


if __name__ == '__main__':
    sys.exit(run_benchmark())
