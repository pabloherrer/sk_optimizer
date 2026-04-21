"""
run_all.py — Master test runner.

Orchestrates all S&K optimizer test suites, times each, prints a summary, and
writes a consolidated markdown report to output/test_report.md. CI can attach
the report as a build artifact.

Usage:
    python tests/run_all.py                  # fast suites only (default)
    python tests/run_all.py --with-scale     # + scale / performance (30-60s)
    python tests/run_all.py --with-real      # + real-data integration (~20s)
    python tests/run_all.py --with-bench     # + A/B benchmark (minutes)
    python tests/run_all.py --all            # everything
    python tests/run_all.py --quiet          # suppress per-suite details

Exit code: 0 if all run suites pass, non-zero otherwise.
"""

import argparse
import io
import sys
import time
import contextlib
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import OUTPUT_DIR


# ─── Suite registry ─────────────────────────────────────────────────────────
#
# (key, module_path, public_fn_name, label, tier)
#   tier: 'fast' (always), 'scale', 'real', 'bench'

SUITES = [
    # Fast suites — always run
    ('unit_inventory',
     'tests.test_unit_inventory', 'run_all_tests',
     'Unit — inventory math', 'fast'),
    ('unit_helpers',
     'tests.test_unit_helpers', 'run_all_tests',
     'Unit — solver helpers', 'fast'),
    ('solver_invariants',
     'tests.test_solver_invariants', 'run_all_tests',
     'Solver invariants (property tests)', 'fast'),
    ('feature_contract',
     'tests.test_feature_contract', 'run_all_tests',
     'Feature — 14-day contract (Aksen 2012)', 'fast'),
    ('feature_windows',
     'tests.test_feature_windows', 'run_all_tests',
     'Feature — time windows (Cornillier 2009)', 'fast'),
    ('feature_efficiency',
     'tests.test_feature_efficiency', 'run_all_tests',
     'Feature — fill efficiency (Cornillier/Archetti)', 'fast'),
    ('feature_forward_refills',
     'tests.test_feature_forward_refills', 'run_all_tests',
     'Feature — forward refills (Coelho 2014)', 'fast'),
    ('feature_overtime',
     'tests.test_feature_overtime', 'run_all_tests',
     'Feature — overtime labor model', 'fast'),
    ('determinism',
     'tests.test_determinism', 'run_all_tests',
     'Determinism & input immutability', 'fast'),

    # Slow suites — opt-in
    ('scale',
     'tests.test_scale', 'run_all_tests',
     'Scale / performance (50 & 150 clients)', 'scale'),
    ('real_data',
     'tests.test_real_data', 'run_all_tests',
     'Real-data integration', 'real'),
]


# ─── Runner ─────────────────────────────────────────────────────────────────

def _run_suite(mod_path, fn_name, capture_output=False):
    import importlib
    mod = importlib.import_module(mod_path)
    fn  = getattr(mod, fn_name)

    t0 = time.time()
    buf = io.StringIO()
    if capture_output:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            rc = fn()
    else:
        rc = fn()
    elapsed = time.time() - t0
    return rc, elapsed, buf.getvalue()


def _print_banner(text, char='═'):
    print(char * 78)
    print(f'  {text}')
    print(char * 78)


def main():
    p = argparse.ArgumentParser(description='Master S&K test runner.')
    p.add_argument('--with-scale', action='store_true',
                   help='Include scale / performance tests')
    p.add_argument('--with-real', action='store_true',
                   help='Include real-data integration tests')
    p.add_argument('--with-bench', action='store_true',
                   help='Include A/B benchmark')
    p.add_argument('--all', action='store_true',
                   help='Run every available suite')
    p.add_argument('--quiet', action='store_true',
                   help='Suppress per-suite verbose output')
    p.add_argument('--bench-mode', default='minimal',
                   choices=['minimal', 'fast', 'full'],
                   help='Benchmark breadth if --with-bench is set')
    p.add_argument('--bench-solve-sec', type=int, default=6,
                   help='Per-config solve budget for benchmark')
    p.add_argument('--output', default=None,
                   help='Override output markdown path')
    args = p.parse_args()

    if args.all:
        args.with_scale = args.with_real = args.with_bench = True

    selected_tiers = {'fast'}
    if args.with_scale:  selected_tiers.add('scale')
    if args.with_real:   selected_tiers.add('real')

    suites_to_run = [s for s in SUITES if s[4] in selected_tiers]

    print()
    _print_banner(f'S&K Optimizer — Master Test Runner  '
                  f'({datetime.now():%Y-%m-%d %H:%M})')
    print(f'  Suites selected: {len(suites_to_run)}  '
          f'({", ".join(sorted(selected_tiers))})')
    if args.with_bench:
        print(f'  + A/B benchmark ({args.bench_mode}, {args.bench_solve_sec}s/run)')
    print()

    results = []
    total_t0 = time.time()
    for key, mod, fn, label, tier in suites_to_run:
        print(f'─── {label} ' + '─' * max(0, 60 - len(label)))
        try:
            rc, elapsed, captured = _run_suite(mod, fn, capture_output=args.quiet)
        except Exception as e:
            rc = 2
            elapsed = 0.0
            captured = f'CRASH: {type(e).__name__}: {e}'
            print(f'  ✗ CRASHED: {type(e).__name__}: {e}')
        results.append({
            'key': key, 'label': label, 'tier': tier,
            'rc': rc, 'elapsed': elapsed, 'output': captured,
        })
        if args.quiet:
            status = '✓' if rc == 0 else '✗'
            print(f'  {status} {label}  ({elapsed:.1f}s)  rc={rc}')
        print()

    # Optional benchmark
    bench_path = None
    if args.with_bench:
        print(f'─── A/B Benchmark ({args.bench_mode}) ───')
        try:
            from tests.bench_ab import run_benchmark
            bench_path = run_benchmark(
                mode=args.bench_mode,
                solve_sec=args.bench_solve_sec,
                use_real=False,
            )
        except Exception as e:
            print(f'  Benchmark crashed: {type(e).__name__}: {e}')
        print()

    total_elapsed = time.time() - total_t0

    # Summary
    _print_banner('Summary', '━')
    n_pass = sum(1 for r in results if r['rc'] == 0)
    n_fail = sum(1 for r in results if r['rc'] != 0)
    for r in results:
        icon = '✓' if r['rc'] == 0 else '✗'
        print(f'  {icon} {r["label"]:<52s} {r["elapsed"]:6.1f}s  rc={r["rc"]}')
    print()
    print(f'  {n_pass} suite(s) passed, {n_fail} failed  |  '
          f'total {total_elapsed:.1f}s')
    if bench_path:
        print(f'  + benchmark report → {bench_path}')
    print()

    # Write markdown report
    out_path = Path(args.output) if args.output else OUTPUT_DIR / 'test_report.md'
    out_path.parent.mkdir(parents=True, exist_ok=True)
    _write_report(results, total_elapsed, bench_path, out_path, selected_tiers,
                  bench_mode=args.bench_mode if args.with_bench else None)
    print(f'  ↓ Report → {out_path}')
    print()

    return 0 if n_fail == 0 else 1


def _write_report(results, total_elapsed, bench_path, out_path, tiers, bench_mode):
    lines = []
    lines.append(f'# S&K Optimizer — Test Report')
    lines.append(f'')
    lines.append(f'**Generated:** {datetime.now():%Y-%m-%d %H:%M:%S}  ')
    lines.append(f'**Tiers:** {", ".join(sorted(tiers))}')
    if bench_mode:
        lines.append(f'  |  **Bench mode:** {bench_mode}')
    lines.append(f'**Total elapsed:** {total_elapsed:.1f}s')
    lines.append(f'')
    lines.append(f'## Summary')
    lines.append(f'')
    lines.append('| Suite | Tier | Result | Elapsed |')
    lines.append('|-------|------|--------|---------|')
    for r in results:
        icon = '✓ PASS' if r['rc'] == 0 else '✗ FAIL'
        lines.append(f'| {r["label"]} | {r["tier"]} | {icon} | {r["elapsed"]:.1f}s |')
    lines.append('')
    n_pass = sum(1 for r in results if r['rc'] == 0)
    n_fail = sum(1 for r in results if r['rc'] != 0)
    lines.append(f'**{n_pass}** passed, **{n_fail}** failed ({len(results)} suites total)')
    lines.append(f'')

    if bench_path:
        lines.append(f'## Benchmark')
        lines.append(f'')
        lines.append(f'See [{bench_path.name}]({bench_path.name}) for A/B comparison.')
        lines.append(f'')

    # Per-suite details (captured stdout if quiet mode was used)
    details = [r for r in results if r['output']]
    if details:
        lines.append(f'## Suite output (captured)')
        lines.append(f'')
        for r in details:
            lines.append(f'### {r["label"]}')
            lines.append(f'')
            lines.append(f'```')
            # Trim extremely long captures
            txt = r['output']
            if len(txt) > 6000:
                txt = txt[:3000] + '\n... (truncated) ...\n' + txt[-3000:]
            lines.append(txt)
            lines.append(f'```')
            lines.append(f'')

    out_path.write_text('\n'.join(lines))


if __name__ == '__main__':
    sys.exit(main())
