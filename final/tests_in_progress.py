"""
Unit tests for the in-progress / assumed-delivered reconciliation layer.

Run:  python -m final.tests_in_progress   (from the sk_optimizer repo root)
"""
from __future__ import annotations

import json
import tempfile
from datetime import date, datetime
from pathlib import Path

from v2.domain.client import TankState

from final.app.in_progress_store import (
    EXPIRY_DAYS,
    InProgressEntry,
    apply_assumed_deliveries,
    load_in_progress,
    save_in_progress,
)

PLAN_TODAY = date(2026, 7, 3)          # planning "tomorrow"
YESTERDAY = date(2026, 7, 2)           # deliveries dispatched today


def _tank(cid='C1', lbs=40.0, source='estimated', rate=25.0,
          last_delivery=None, as_of=None) -> TankState:
    return TankState(
        client_id=cid, current_lbs=lbs,
        as_of=as_of or datetime(2026, 7, 2, 12, 0),
        source=source, rate_lbs_per_day=rate,
        last_delivery_date=last_delivery,
    )


CAPS = {'C1': 500.0}


def _apply(tank, entry, manual_ids=frozenset(), plan_today=PLAN_TODAY):
    tanks, log = apply_assumed_deliveries(
        {'C1': tank}, CAPS, (entry,), manual_ids, plan_today)
    return tanks['C1'], log


def test_basic_assumption():
    """Unlogged delivery yesterday → tank assumed full, decayed 1 day."""
    ts, _ = _apply(_tank(), InProgressEntry('C1', YESTERDAY))
    assert ts.source == 'assumed', ts.source
    assert ts.current_lbs == 500.0 - 25.0 * 1, ts.current_lbs   # cap − 1d rate


def test_qty_partial_fill():
    """Recorded qty → pre_level + qty (capped), not full tank."""
    ts, _ = _apply(_tank(lbs=40.0), InProgressEntry('C1', YESTERDAY, qty_lbs=200.0))
    assert ts.current_lbs == (40.0 + 200.0) - 25.0, ts.current_lbs


def test_future_entry_ignored():
    """Entry dated on/after plan day 0 → not yet delivered → ignored."""
    ts, _ = _apply(_tank(), InProgressEntry('C1', PLAN_TODAY))
    assert ts.source == 'estimated'


def test_expired_entry_ignored():
    old = date(2026, 6, 25)
    assert (PLAN_TODAY - old).days > EXPIRY_DAYS
    ts, log = _apply(_tank(), InProgressEntry('C1', old))
    assert ts.source == 'estimated'
    assert any('EXPIRED' in l for l in log)


def test_confirmed_by_log_ignored():
    """Delivery_Log already has an entry on/after the assumed date → real wins."""
    ts, _ = _apply(_tank(last_delivery='2026-07-02'),
                   InProgressEntry('C1', YESTERDAY))
    assert ts.source == 'estimated'


def test_older_log_entry_does_not_confirm():
    ts, _ = _apply(_tank(last_delivery='2026-06-20'),
                   InProgressEntry('C1', YESTERDAY))
    assert ts.source == 'assumed'


def test_manual_reading_wins():
    ts, _ = _apply(_tank(source='manual'), InProgressEntry('C1', YESTERDAY),
                   manual_ids=frozenset({'C1'}))
    assert ts.source == 'manual'


def test_post_fill_sensor_wins():
    """Sensor reading strictly after the delivery day → trust the sensor."""
    ts, _ = _apply(_tank(source='sensor', as_of=datetime(2026, 7, 3, 6, 0)),
                   InProgressEntry('C1', YESTERDAY))
    assert ts.source == 'sensor'


def test_pre_fill_sensor_overridden():
    """Sensor reading ON the delivery day may be pre-fill → assumption wins."""
    ts, _ = _apply(_tank(source='sensor', lbs=30.0,
                         as_of=datetime(2026, 7, 2, 9, 0)),
                   InProgressEntry('C1', YESTERDAY))
    assert ts.source == 'assumed'
    assert ts.current_lbs == 500.0 - 25.0


def test_latest_entry_per_client_wins():
    tanks, _ = apply_assumed_deliveries(
        {'C1': _tank(rate=25.0)}, CAPS,
        (InProgressEntry('C1', date(2026, 6, 30)),
         InProgressEntry('C1', YESTERDAY)),
        frozenset(), PLAN_TODAY)
    assert tanks['C1'].current_lbs == 500.0 - 25.0   # decayed from Jul 2, not Jun 30


def test_unknown_client_skipped():
    tanks, log = apply_assumed_deliveries(
        {'C1': _tank()}, CAPS, (InProgressEntry('ZZZ', YESTERDAY),),
        frozenset(), PLAN_TODAY)
    assert tanks['C1'].source == 'estimated'
    assert any('unknown client' in l for l in log)


def test_roundtrip_store():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / 'in_progress.json'
        save_in_progress(p, [
            {'client_id': 'C1', 'date': '2026-07-02', 'qty_lbs': None,
             'note': 'route T2', 'created_at': '2026-07-02T08:00:00'},
            {'client_id': 'BAD'},                      # malformed → skipped
        ])
        entries = load_in_progress(p)
        assert len(entries) == 1
        assert entries[0].client_id == 'C1'
        assert entries[0].qty_lbs is None
        assert entries[0].date == date(2026, 7, 2)


def test_load_missing_file():
    assert load_in_progress(Path('/nonexistent/in_progress.json')) == ()


if __name__ == '__main__':
    fns = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f'  ✓ {fn.__name__}')
        except AssertionError as e:
            failed += 1
            print(f'  ✗ {fn.__name__}: {e}')
    print(f'\n{len(fns) - failed}/{len(fns)} passed')
    raise SystemExit(1 if failed else 0)
