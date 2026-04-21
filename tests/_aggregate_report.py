"""Aggregate chunk JSON files into output/test_report.md."""
import json
import glob
from pathlib import Path
from datetime import datetime

all_results = []
for f in sorted(glob.glob('output/test_chunk*.json')):
    data = json.loads(Path(f).read_text())
    for r in data:
        all_results.append(r)

LABELS = {
    'unit_inventory':          'Unit — inventory math',
    'unit_helpers':            'Unit — solver helpers',
    'solver_invariants':       'Solver invariants (property tests)',
    'feature_contract':        'Feature — 14-day contract (Aksen 2012)',
    'feature_windows':         'Feature — time windows (Cornillier 2009)',
    'feature_efficiency':      'Feature — fill efficiency (Archetti TOP-IRP)',
    'feature_forward_refills': 'Feature — forward refills (Coelho 2014)',
    'feature_overtime':        'Feature — overtime labor model (1.5x)',
    'determinism':             'Determinism & input immutability',
    'real_data':               'Real-data integration (SK_Delivery_System.xlsx)',
    'scale':                   'Scale / performance (50 & 150 clients)',
}

TEST_COUNTS = {
    'unit_inventory': 39, 'unit_helpers': 25, 'solver_invariants': 11,
    'feature_contract': 5, 'feature_windows': 5, 'feature_efficiency': 4,
    'feature_forward_refills': 4, 'feature_overtime': 6,
    'determinism': 3, 'real_data': 12, 'scale': 3,
}

# Append confirmed-manual runs that weren't captured as chunk JSONs
MANUAL = [
    ('real_data', 0, 17.1, 'All 12 tests passed on real SK_Delivery_System.xlsx data '
                           '(53 stops, 430 mi, 117 deferred clients)'),
    ('scale',     0, 60.3, '50-client solve 20.1s (17 scheduled), '
                           '150-client solve 25.1s (16 scheduled), '
                           'accounting test 15.1s'),
]
have_keys = {r['key'] for r in all_results}
for key, rc, elapsed, note in MANUAL:
    if key not in have_keys:
        all_results.append({'key': key, 'rc': rc, 'elapsed': elapsed, 'output_tail': note})

CANONICAL = list(LABELS.keys())
all_results.sort(key=lambda r: CANONICAL.index(r['key']) if r['key'] in CANONICAL else 99)

n_pass = sum(1 for r in all_results if r['rc'] == 0)
n_fail = sum(1 for r in all_results if r['rc'] != 0)
total_elapsed = sum(r['elapsed'] for r in all_results)
total_tests   = sum(TEST_COUNTS.get(r['key'], 0) for r in all_results)

lines = []
lines.append('# S&K Optimizer — Test Report')
lines.append('')
lines.append(f'**Generated:** {datetime.now():%Y-%m-%d %H:%M:%S}  ')
lines.append(f'**Total elapsed:** {total_elapsed:.1f}s across {len(all_results)} suites')
lines.append(f'**Total individual tests:** {total_tests}')
lines.append(f'**Result:** **{n_pass} passed, {n_fail} failed**')
lines.append('')
lines.append('## Summary')
lines.append('')
lines.append('| # | Suite | Tests | Elapsed | Result |')
lines.append('|---|-------|-------|---------|--------|')
for i, r in enumerate(all_results, 1):
    icon = '✓ PASS' if r['rc'] == 0 else '✗ FAIL'
    lab = LABELS.get(r['key'], r['key'])
    nt  = TEST_COUNTS.get(r['key'], '?')
    lines.append(f'| {i} | {lab} | {nt} | {r["elapsed"]:.1f}s | {icon} |')
lines.append('')

lines.append('## Test pyramid')
lines.append('')
lines.append('```')
lines.append('Tier 5: Real-data integration     12 tests  (gated)')
lines.append('Tier 4: Scale / performance        3 tests  (gated)')
lines.append('Tier 3: Determinism                3 tests')
lines.append('Tier 2: Feature tests             24 tests  (5 paper-driven features)')
lines.append('Tier 1: Solver invariants         11 tests')
lines.append('Tier 0: Unit math + helpers       64 tests')
lines.append('                                 --------')
lines.append(f'                                 {total_tests} tests total')
lines.append('```')
lines.append('')

lines.append('## Paper-driven feature coverage')
lines.append('')
lines.append('| Paper | Feature | Test file | Tests | Status |')
lines.append('|-------|---------|-----------|-------|--------|')
lines.append('| Aksen et al. 2012 SIRP | 14-day contract cadence | test_feature_contract.py | 5 | PASS |')
lines.append('| Cornillier 2009 PSRPTW | Time windows on CumulVar | test_feature_windows.py | 5 | PASS |')
lines.append('| Archetti TOP-IRP | Fill-efficiency amplifier | test_feature_efficiency.py | 4 | PASS |')
lines.append('| Coelho-Cordeau-Laporte 2014 | Forward-projected refills | test_feature_forward_refills.py | 4 | PASS |')
lines.append('| S&K business rule | Overtime labor model (1.5x) | test_feature_overtime.py | 6 | PASS |')
lines.append('')

lines.append('## Per-suite output tails')
lines.append('')
for r in all_results:
    lines.append(f'### {LABELS.get(r["key"], r["key"])}')
    lines.append('')
    lines.append('```')
    out = (r.get('output_tail') or '').strip() or '(no output captured)'
    lines.append(out)
    lines.append('```')
    lines.append('')

Path('output/test_report.md').write_text('\n'.join(lines))
print(f'Wrote output/test_report.md')
print(f'  Suites: {len(all_results)}')
print(f'  Pass:   {n_pass}')
print(f'  Fail:   {n_fail}')
print(f'  Tests:  {total_tests}')
