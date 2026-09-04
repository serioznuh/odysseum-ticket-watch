"""Unit tests for state persistence and the reminder ladder."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

from watcher import detect
from watcher import state as state_mod
from watcher.config import load_config
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


# --------------------------------------------------- reminder failover (OTW-15)
# `grace_minutes` is what lets the cloud pass be a failover rather than a second
# owner of the ladder: it only reports a reminder the local half already missed.

def test_grace_zero_matches_the_no_grace_call():
    """The local half owns the ladder and passes no grace — that path must stay
    byte-for-byte the behaviour the ladder had before OTW-15."""
    for delta in (timedelta(hours=23), timedelta(minutes=90), timedelta(minutes=10),
                  timedelta(hours=-1), timedelta(hours=-7)):
        st = fresh_state()
        st["sale_target"] = iso_in(delta)
        assert due_reminders(st, OFFSETS, NOW, 0.0) == due_reminders(st, OFFSETS, NOW)


def test_grace_holds_a_freshly_opened_window_back():
    st = fresh_state()
    target = iso_in(timedelta(minutes=118))  # the 2h window opened 2 min ago
    st["sale_target"] = target
    mark_reminder(st, target, 1440, OFFSETS)  # the owner sent the 24h one yesterday

    # The owner fires as soon as the window opens...
    assert [d["offset"] for d in due_reminders(st, OFFSETS, NOW, 0)] == [120]
    # ...the failover stays quiet until its grace has elapsed.
    assert due_reminders(st, OFFSETS, NOW, 25) == []
    late = NOW + timedelta(minutes=23)  # window opened 25 min ago; nobody sent it
    assert [d["offset"] for d in due_reminders(st, OFFSETS, late, 25)] == [120]


def test_grace_falls_back_to_an_older_offset_the_owner_never_sent():
    """Grace narrows what the failover may send, so the most imminent offset can
    be held back while an older unsent one is not. Sending that older reminder
    is right: the Mac slept through it, so the user never got it — and its
    wording is time-aware, so it does not misstate the countdown."""
    st = fresh_state()
    st["sale_target"] = iso_in(timedelta(minutes=118))  # 24h reminder never sent
    assert [d["offset"] for d in due_reminders(st, OFFSETS, NOW, 25)] == [1440]


def test_grace_never_widens_the_open_pings_six_hour_cutoff():
    """Grace delays eligibility; it must not push the 'open' ping's absolute
    deadline out, or a failover could shout GO long after the sale opened."""
    st = fresh_state()
    st["sale_target"] = iso_in(timedelta(hours=-1))
    assert due_reminders(st, OFFSETS, NOW, 25) == [{"offset": "open", "target": st["sale_target"]}]

    just_opened = fresh_state()
    just_opened["sale_target"] = iso_in(timedelta(minutes=-10))
    assert due_reminders(just_opened, OFFSETS, NOW, 25) == []   # owner's turn, not ours

    # 6h12m past opening: still shut at grace 0, and must stay shut at grace 25.
    # Subtracting the grace from the age instead of the eligibility time would
    # read this as 5h47m and re-open the window.
    stale = fresh_state()
    stale["sale_target"] = iso_in(timedelta(hours=-6, minutes=-12))
    assert due_reminders(stale, OFFSETS, NOW, 0) == []
    assert due_reminders(stale, OFFSETS, NOW, 25) == []


def test_grace_never_runs_ahead_of_the_owner_on_a_negative_value():
    """A negative grace would make the failover *earlier* than the owner rather
    than later — opening the 2 h rung before its window does. It clamps to 0."""
    st = fresh_state()
    target = iso_in(timedelta(minutes=130))  # the 2 h window opens in 10 min
    st["sale_target"] = target
    mark_reminder(st, target, 1440, OFFSETS)  # the 24 h rung is already spent

    assert due_reminders(st, OFFSETS, NOW) == []  # nothing is due for the owner
    # ...and no negative grace may drag the 2 h rung forward for the failover.
    assert due_reminders(st, OFFSETS, NOW, -20) == []
    assert due_reminders(st, OFFSETS, NOW, -60) == []


def test_grace_is_capped_at_half_a_rungs_window():
    """A flat grace wider than a rung swallows it whole: at grace 25 the 15-min
    warning became eligible at dt+10, past the opening, where the 'open' branch
    takes over — so the cloud could never deliver it (OTW-15 round 2). The wait
    is capped at half the window, and rungs wider than twice the grace keep the
    full margin."""
    target = iso_in(timedelta(minutes=15))  # the 15-min window opens right now
    st = fresh_state()
    st["sale_target"] = target
    for spent in (1440, 120):
        mark_reminder(st, target, spent, OFFSETS)

    assert [d["offset"] for d in due_reminders(st, OFFSETS, NOW, 0)] == [15]  # owner
    assert due_reminders(st, OFFSETS, NOW + timedelta(minutes=7), 25) == []
    late = NOW + timedelta(minutes=8)  # past the half-window cap of 7.5 min
    assert [d["offset"] for d in due_reminders(st, OFFSETS, late, 25)] == [15]

    # The 2 h rung is wide enough for the whole 25 min, so it is unaffected.
    wide = fresh_state()
    wide_target = iso_in(timedelta(minutes=120))
    wide["sale_target"] = wide_target
    mark_reminder(wide, wide_target, 1440, OFFSETS)
    assert due_reminders(wide, OFFSETS, NOW + timedelta(minutes=24), 25) == []
    at_grace = NOW + timedelta(minutes=25)
    assert [d["offset"] for d in due_reminders(wide, OFFSETS, at_grace, 25)] == [120]


def test_shipped_grace_leaves_every_configured_offset_deliverable():
    """Production invariant, read from production: the grace the cloud pass
    actually passes must leave every offset in config.toml reachable — at
    least over the second half of its window — so a change to either number
    fails here instead of silently dropping a rung's failover."""
    root = Path(__file__).resolve().parent.parent
    offsets = load_config(root / "config.toml").reminder_offsets_minutes
    workflow = (root / ".github" / "workflows" / "watch.yml").read_text(encoding="utf-8")
    match = re.search(r"--reminder-grace-minutes\s+([0-9.]+)", workflow)
    assert match, "the cloud pass no longer passes --reminder-grace-minutes"
    grace = float(match.group(1))

    for offset in sorted(offsets):
        target = iso_in(timedelta(minutes=offset))  # this rung's window opens now
        st = fresh_state()
        st["sale_target"] = target
        for spent in (o for o in offsets if o > offset):
            mark_reminder(st, target, spent, offsets)
        halfway = NOW + timedelta(minutes=offset / 2)
        assert [d["offset"] for d in due_reminders(st, offsets, halfway, grace)] == [offset], (
            f"the {offset}-minute reminder is undeliverable by the cloud failover "
            f"at --reminder-grace-minutes {grace:g}"
        )


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
