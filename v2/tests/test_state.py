"""
Tests for v2.state.store.StateStore.

Run: ./sk_venv/bin/python3 -m pytest v2/tests/test_state.py -v
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from v2.state.store import StateStore  # noqa: E402


def test_load_missing_file_returns_empty(tmp_path: Path):
    store = StateStore(tmp_path / 'state.json')
    assert store.load() == {}


def test_load_empty_file_returns_empty(tmp_path: Path):
    p = tmp_path / 'state.json'
    p.write_text('')
    store = StateStore(p)
    assert store.load() == {}


def test_load_corrupt_file_returns_empty(tmp_path: Path):
    p = tmp_path / 'state.json'
    p.write_text('{not valid json')
    store = StateStore(p)
    assert store.load() == {}


def test_save_then_load_round_trip(tmp_path: Path):
    p = tmp_path / 'state.json'
    store = StateStore(p)
    state = {
        '1054': {
            'id': '1054', 'current_lbs': 326.0,
            'confidence': 'sensor', 'updated_at': '2026-05-21T08:00:00',
        },
        '2017': {
            'id': '2017', 'current_lbs': 120.5,
            'confidence': 'estimated', 'updated_at': '2026-05-21T08:00:00',
        },
    }
    store.save(state)
    loaded = store.load()
    assert set(loaded.keys()) == {'1054', '2017'}
    assert loaded['1054']['current_lbs'] == 326.0
    assert loaded['1054']['confidence'] == 'sensor'
    assert loaded['2017']['current_lbs'] == 120.5


def test_save_creates_backup_before_overwrite(tmp_path: Path):
    p = tmp_path / 'state.json'
    store = StateStore(p)
    # First save establishes the file
    store.save({'1054': {'id': '1054', 'current_lbs': 100.0}})
    assert not store.backup_file.exists()  # no prior file to back up
    # Second save should create a backup of the first file
    store.save({'1054': {'id': '1054', 'current_lbs': 200.0}})
    assert store.backup_file.exists()
    backup = json.loads(store.backup_file.read_text())
    # Backup should contain the OLD value (100.0), not the new one
    assert backup['clients']['1054']['current_lbs'] == 100.0
    # Main file should have the new value
    current = json.loads(p.read_text())
    assert current['clients']['1054']['current_lbs'] == 200.0


def test_atomic_write_keeps_old_state_on_crash(tmp_path: Path, monkeypatch):
    """If os.replace fails mid-write, the tmp file should be left and the
    original file should be untouched."""
    p = tmp_path / 'state.json'
    store = StateStore(p)
    # Establish a known-good state file
    store.save({'1054': {'id': '1054', 'current_lbs': 100.0}})
    original_payload = p.read_text()

    # Now simulate a crash during the next save by making os.replace raise
    import os
    real_replace = os.replace

    def boom(src, dst):
        raise OSError('simulated crash')

    monkeypatch.setattr('v2.state.store.os.replace', boom)
    with pytest.raises(OSError, match='simulated crash'):
        store.save({'1054': {'id': '1054', 'current_lbs': 999.0}})

    monkeypatch.setattr('v2.state.store.os.replace', real_replace)
    # The main file must be unchanged (the new value 999 must NOT be there)
    assert p.read_text() == original_payload
    loaded = store.load()
    assert loaded['1054']['current_lbs'] == 100.0


def test_record_run_appends_to_jsonl(tmp_path: Path):
    p = tmp_path / 'state.json'
    store = StateStore(p)
    store.record_run(run_id='r1', metadata={'today': '2026-05-21', 'stops': 12})
    store.record_run(run_id='r2', metadata={'today': '2026-05-22', 'stops': 14})
    log_path = tmp_path / 'runs.jsonl'
    assert log_path.exists()
    lines = log_path.read_text().strip().split('\n')
    assert len(lines) == 2
    r1 = json.loads(lines[0])
    r2 = json.loads(lines[1])
    assert r1['run_id'] == 'r1'
    assert r1['metadata']['stops'] == 12
    assert r2['run_id'] == 'r2'
    assert r2['metadata']['today'] == '2026-05-22'


def test_load_handles_v1_legacy_flat_dict(tmp_path: Path):
    """A v1-style file (flat {id: lbs}) should be loadable as v2 dict."""
    p = tmp_path / 'state.json'
    p.write_text(json.dumps({'1054': 326.0, '2017': 120.5}))
    store = StateStore(p)
    loaded = store.load()
    assert loaded['1054']['current_lbs'] == 326.0
    assert loaded['2017']['current_lbs'] == 120.5


def test_save_returns_immutable_loaded_view(tmp_path: Path):
    """Mutating the loaded dict should not affect a subsequent load."""
    p = tmp_path / 'state.json'
    store = StateStore(p)
    store.save({'1054': {'id': '1054', 'current_lbs': 100.0}})
    a = store.load()
    a['1054']['current_lbs'] = 9999.0  # mutate
    b = store.load()
    assert b['1054']['current_lbs'] == 100.0
