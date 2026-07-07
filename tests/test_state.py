"""Unit tests for state persistence and the reminder ladder."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from watcher import detect, state as state_mod
from watcher.detect import Snapshot
from watcher.state import (
    DEFAULT_STATE,
    already_sent,
    due_reminders,
    load_state,
    mark_reminder,
    mark_sent,
    save_state,
    update_from_snapshot,
)

PARIS = detect.TZ_PARIS
NOW = datetime(2026, 7, 6, 9, 0, tzinfo=PARIS)
OFFSETS = [1440, 120, 15]


def fresh_state() -> dict:
    return json.loads(json.dumps(DEFAULT_STATE))


def iso_in(delta: timedelta) -> str:
    return (NOW + delta).isoformat()


# ------------------------------------------------------------------ persistence

def test_state_roundtrip(tmp_path):
    path = tmp_path / "state.json"
    st = fresh_state()
    mark_sent(st, "sale:x:y", NOW)
    save_state(path, st)
    loaded = load_state(path)
    assert already_sent(loaded, "sale:x:y")
    assert loaded["version"] == 1


def test_corrupt_state_recovers(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{ not json !!!", encoding="utf-8")
    loaded = load_state(path)
    assert loaded["alerts"] == {}
    assert (tmp_path / "state.json.bak").exists()


def test_missing_state_is_default(tmp_path):
    loaded = load_state(tmp_path / "nope.json")
    assert loaded == DEFAULT_STATE


# ------------------------------------------------------------------ snapshot -> state

def test_update_from_snapshot_records_sales_and_target():
    st = fresh_state()
    near = iso_in(timedelta(days=30))
    far = iso_in(timedelta(days=60))
    past = iso_in(timedelta(days=-10))
    snap = Snapshot(
        matched_shows=[
            {"slug": "a", "title": "A", "salesOpeningDatetime": far},
            {"slug": "b", "title": "B", "salesOpeningDatetime": near},
            {"slug": "c", "title": "C", "salesOpeningDatetime": past},
        ]
    )
    update_from_snapshot(st, snap, None, NOW)
    assert st["sales"] == {"a": far, "b": near, "c": past}
    assert st["sale_target"] == near  # earliest FUTURE opening
    assert set(st["shows_seen"]) == {"a", "b", "c"}
    assert st["tickets_available"] is False


def test_update_from_snapshot_marks_tickets_available():
    st = fresh_state()
    snap = Snapshot(
        matched_shows=[{"slug": "a", "title": "A : Projection IMAX 70mm"}],
        showtimes={"a": {"2026-12-16": [{"tags": ["imax"], "refCmd": "x"}]}},
    )
    update_from_snapshot(st, snap, None, NOW)
    assert st["tickets_available"] is True
    assert st["formats_seen"]["a"] == ["imax70"]


# ------------------------------------------------------------------ reminders

def test_no_reminder_far_from_target():
    st = fresh_state()
    st["sale_target"] = iso_in(timedelta(hours=25))
    assert due_reminders(st, OFFSETS, NOW) == []


def test_reminder_ladder_in_order():
    st = fresh_state()
    target = iso_in(timedelta(hours=23))
    st["sale_target"] = target

    due = due_reminders(st, OFFSETS, NOW)
    assert [d["offset"] for d in due] == [1440]
    mark_reminder(st, target, 1440, OFFSETS)
    assert due_reminders(st, OFFSETS, NOW) == []

    later = NOW + timedelta(hours=21, minutes=30)  # T-90min
    due = due_reminders(st, OFFSETS, later)
    assert [d["offset"] for d in due] == [120]
    mark_reminder(st, target, 120, OFFSETS)

    at_t14 = NOW + timedelta(hours=22, minutes=50)  # T-10min
    due = due_reminders(st, OFFSETS, at_t14)
    assert [d["offset"] for d in due] == [15]
    mark_reminder(st, target, 15, OFFSETS)
    assert due_reminders(st, OFFSETS, at_t14) == []


def test_reminder_skips_ahead_when_late():
    st = fresh_state()
    target = iso_in(timedelta(minutes=10))
    st["sale_target"] = target
    due = due_reminders(st, OFFSETS, NOW)
    assert [d["offset"] for d in due] == [15]  # only the most imminent, not all three
    mark_reminder(st, target, 15, OFFSETS)
    assert st["reminders_sent"][target] == ["120", "1440", "15"]


def test_open_ping_after_target_once_with_grace():
    st = fresh_state()
    target = iso_in(timedelta(hours=-1))
    st["sale_target"] = target
    due = due_reminders(st, OFFSETS, NOW)
    assert [d["offset"] for d in due] == ["open"]
    mark_reminder(st, target, "open", OFFSETS)
    assert due_reminders(st, OFFSETS, NOW) == []

    st2 = fresh_state()
    st2["sale_target"] = iso_in(timedelta(hours=-7))  # beyond 6h grace
    assert due_reminders(st2, OFFSETS, NOW) == []


def test_is_check_fresh():
    st = fresh_state()
    assert state_mod.is_check_fresh(st, 5, NOW) is False  # never checked yet
    st["last_check_ok"] = iso_in(timedelta(hours=-2))
    assert state_mod.is_check_fresh(st, 5, NOW) is True   # morning run succeeded -> retry skips
    st["last_check_ok"] = iso_in(timedelta(hours=-6))
    assert state_mod.is_check_fresh(st, 5, NOW) is False  # morning run missed -> retry runs
    assert state_mod.is_check_fresh(st, 0, NOW) is False  # guard disabled


def test_is_check_stale():
    st = fresh_state()
    assert state_mod.is_check_stale(st, 72, NOW) is False  # never checked -> setup phase
    st["last_check_ok"] = iso_in(timedelta(hours=-10))
    assert state_mod.is_check_stale(st, 72, NOW) is False
    st["last_check_ok"] = iso_in(timedelta(hours=-80))
    assert state_mod.is_check_stale(st, 72, NOW) is True
    assert state_mod.is_check_stale(st, 0, NOW) is False  # disabled


def test_reminders_stop_when_tickets_available():
    st = fresh_state()
    st["sale_target"] = iso_in(timedelta(minutes=10))
    st["tickets_available"] = True
    assert due_reminders(st, OFFSETS, NOW) == []
