"""Size-based rotation for the shared event-log append helper (#499)."""

import json

import pytest

from agentwire.utils import event_log
from agentwire.utils.event_log import append_event


@pytest.fixture
def events_path(tmp_path):
    return tmp_path / "sub" / "demo-events.jsonl"


def _read_lines(path):
    return path.read_text().splitlines()


def test_appends_jsonl_line_unchanged(events_path):
    """Each event is one ``json.dumps(record) + "\\n"`` line — format intact."""
    append_event(events_path, {"ts": 1, "event": "a", "x": 2})
    append_event(events_path, {"ts": 3, "event": "b"})

    lines = _read_lines(events_path)
    assert lines == ['{"ts": 1, "event": "a", "x": 2}', '{"ts": 3, "event": "b"}']
    # Round-trips back to the original records.
    assert json.loads(lines[0]) == {"ts": 1, "event": "a", "x": 2}


def test_creates_parent_dir(events_path):
    assert not events_path.parent.exists()
    append_event(events_path, {"event": "a"})
    assert events_path.exists()


def test_rotation_triggers_past_threshold(events_path, monkeypatch):
    """Crossing the size cap rolls the active file to ``.1`` and starts fresh."""
    monkeypatch.setenv("AGENTWIRE_EVENT_LOG_MAX_BYTES", "1000")
    monkeypatch.setenv("AGENTWIRE_EVENT_LOG_BACKUPS", "3")

    rolled = events_path.with_name(events_path.name + ".1")
    payload = "x" * 40  # each line ~ 64 bytes; ~15 fill the 1000-byte cap

    # Below the cap: no rotation, everything stays in the active file.
    for i in range(10):
        append_event(events_path, {"event": "fill", "i": i, "pad": payload})
    assert not rolled.exists()
    assert len(_read_lines(events_path)) == 10

    # Keep going until the active file would cross the cap — it rolls to .1 and
    # the active file is reset to hold the new line(s).
    for i in range(10, 20):
        append_event(events_path, {"event": "fill", "i": i, "pad": payload})

    assert rolled.exists(), "expected the active log to roll to .1 past threshold"
    assert events_path.stat().st_size <= 1000
    # No events lost across the single rotation: active + .1 holds all 20.
    total = _read_lines(events_path) + _read_lines(rolled)
    assert len(total) == 20
    assert {json.loads(line)["i"] for line in total} == set(range(20))


def test_backup_count_is_bounded(events_path, monkeypatch):
    """Only ``AGENTWIRE_EVENT_LOG_BACKUPS`` rolled files are retained."""
    monkeypatch.setenv("AGENTWIRE_EVENT_LOG_MAX_BYTES", "120")
    monkeypatch.setenv("AGENTWIRE_EVENT_LOG_BACKUPS", "2")

    for i in range(60):
        append_event(events_path, {"event": "fill", "i": i, "pad": "y" * 40})

    # .1 and .2 may exist; .3 must never appear.
    assert not events_path.with_name(events_path.name + ".3").exists()


def test_rotation_disabled_when_max_bytes_zero(events_path, monkeypatch):
    monkeypatch.setenv("AGENTWIRE_EVENT_LOG_MAX_BYTES", "0")
    for i in range(50):
        append_event(events_path, {"event": "fill", "i": i, "pad": "z" * 40})

    assert not events_path.with_name(events_path.name + ".1").exists()
    assert len(_read_lines(events_path)) == 50


def test_default_threshold_is_five_mib():
    assert event_log.DEFAULT_MAX_BYTES == 5 * 1024 * 1024
    assert event_log.DEFAULT_BACKUPS == 3
