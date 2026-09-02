"""Unit tests for state persistence and the reminder ladder."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from watcher import detect
from watcher import state as state_mod
from watcher.detect import Snapshot
from watcher.state import (
    DEFAULT_STATE,
    already_sent,
    due_reminders,
    load_state,
    mark_reminder,
    mark_sent,
    migrate_stale_keys,
    save_state,
    update_from_snapshot,
)

PARIS = detect.TZ_PARIS
OFFSETS = [1440, 120, 15]
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


def test_undelivered_one_shot_alerts_leave_their_baselines_alone():
    """Failed NEW_LISTING/TICKETS_AVAILABLE sends must retry, while current
    sale and ticket facts still move forward."""

    class Cfg:
        primary_slug = "primary"
        film_title = "Dune : Troisième partie"
        cinema_name = "Pathé Odysseum"
        cinema_city = "Montpellier"
        reminder_offsets_minutes = OFFSETS

    sale = iso_in(timedelta(days=30))
    show = {
        "slug": "dune-imax-70mm",
        "title": "Dune : Projection IMAX 70mm",
        "salesOpeningDatetime": sale,
        "isMovie": False,
    }
    snap = Snapshot(
        matched_shows=[show],
        cinema_entries={show["slug"]: {"isBookable": True}},
        showtimes={
            show["slug"]: {
                "2026-12-16": [{"tags": ["imax"], "refCmd": "https://booking"}]
            }
        },
    )
    st = fresh_state()

    update_from_snapshot(st, snap, Cfg, NOW, advance_one_shot=False)

    assert st["shows_seen"] == []
    assert st["formats_seen"] == {}
    assert st["sales"] == {show["slug"]: sale}
    assert st["sale_target"] == sale
    assert st["tickets_available"] is True
    assert [f.kind for f in detect.analyze_pathe(snap, st, Cfg, NOW)] == [
        "NEW_LISTING",
        "TICKETS_AVAILABLE",
    ]


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


class CadenceCfg:
    cadence_baseline_hours = 4.0
    cadence_within_week_hours = 2.0
    cadence_final_48h_hours = 0.5
    cadence_opening_window_minutes = 15
    cadence_after_tickets_hours = 6.0


def test_adaptive_staleness_tiers():
    st = fresh_state()
    assert state_mod.adaptive_staleness_hours(st, CadenceCfg, NOW) == 4.0  # nothing known

    st["sale_target"] = iso_in(timedelta(days=10))
    assert state_mod.adaptive_staleness_hours(st, CadenceCfg, NOW) == 4.0  # too far out

    st["sale_target"] = iso_in(timedelta(days=3))
    assert state_mod.adaptive_staleness_hours(st, CadenceCfg, NOW) == 2.0

    st["sale_target"] = iso_in(timedelta(hours=20))
    assert state_mod.adaptive_staleness_hours(st, CadenceCfg, NOW) == 0.5

    st["sale_target"] = iso_in(timedelta(hours=2))
    assert state_mod.adaptive_staleness_hours(st, CadenceCfg, NOW) == 0.25  # opening window

    st["sale_target"] = iso_in(timedelta(hours=-3))
    assert state_mod.adaptive_staleness_hours(st, CadenceCfg, NOW) == 0.25  # sessions appear now

    st["sale_target"] = iso_in(timedelta(hours=-10))
    assert state_mod.adaptive_staleness_hours(st, CadenceCfg, NOW) == 4.0  # window over, no tickets

    st["tickets_available"] = True
    assert state_mod.adaptive_staleness_hours(st, CadenceCfg, NOW) == 6.0  # relaxed

    st["sale_target"] = iso_in(timedelta(hours=2))
    assert state_mod.adaptive_staleness_hours(st, CadenceCfg, NOW) == 0.25  # proximity wins


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


def test_stale_key_migration_does_not_re_alert_a_currently_blind_watcher():
    """The stale key gained a ':{period}' suffix so the alert can repeat daily.
    A machine that is blind right now must not get a duplicate on upgrade."""
    old_key = "stale:2026-08-07T09:27:48+02:00"
    st = {"alerts": {old_key: "2026-08-08T03:00:00+02:00", "error:2026-09-02": "x"}}

    migrate_stale_keys(st)

    assert old_key not in st["alerts"]
    assert st["alerts"][f"{old_key}:0"] == "2026-08-08T03:00:00+02:00"
    # Unrelated dedup memory is untouched.
    assert st["alerts"]["error:2026-09-02"] == "x"


def test_stale_key_migration_is_idempotent_and_leaves_new_keys_alone():
    already = {"alerts": {"stale:2026-08-07T09:27:48+02:00:0": "x"}}

    migrate_stale_keys(already)
    migrate_stale_keys(already)

    assert already["alerts"] == {"stale:2026-08-07T09:27:48+02:00:0": "x"}


def test_load_state_migrates_on_read(tmp_path):
    p = tmp_path / "state.json"
    p.write_text(
        json.dumps({"alerts": {"stale:2026-08-07T09:27:48+02:00": "x"}}), encoding="utf-8"
    )

    st = load_state(p)

    assert "stale:2026-08-07T09:27:48+02:00:0" in st["alerts"]


def test_stale_key_migration_leaves_two_digit_periods_alone():
    """Regression: `fromisoformat` accepts sub-minute UTC offsets, so
    'stale:<iso>:37' parses as a timestamp with a +HH:MM:SS offset. Treating
    "does it parse" as the old-format test renamed every period from 10 to 99
    after it was sent, re-alerting on every cloud pass — 96 messages a day
    through days 11-100 of an outage."""
    iso = "2026-09-02T07:11:00+02:00"

    for period in (0, 1, 9, 10, 37, 99, 100):
        key = f"stale:{iso}:{period}"
        st = {"alerts": {key: "x"}}

        migrate_stale_keys(st)

        assert list(st["alerts"]) == [key], f"period {period} was rewritten"


def test_stale_key_migration_still_converts_the_real_legacy_shapes():
    for iso in (
        "2026-09-02T07:11:00+02:00",
        "2026-09-02T07:11:00.123456+02:00",
        "2026-09-02T07:11:00Z",
        "2026-09-02T07:11:00",
    ):
        st = {"alerts": {f"stale:{iso}": "x"}}

        migrate_stale_keys(st)

        assert list(st["alerts"]) == [f"stale:{iso}:0"], iso
