"""Tests for CLI alert construction."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from watcher import __main__ as cli
from watcher import notify, pathe
from watcher.detect import TZ_PARIS, Snapshot
from watcher.state import DEFAULT_STATE

NOW = datetime(2026, 7, 18, 14, 53, tzinfo=TZ_PARIS)


class Cfg:
    film_title = "Dune : Troisième partie"
    cinema_name = "Pathé Odysseum"
    cinema_city = "Montpellier"
    film_page_url = "https://www.pathe.fr/films/dune-troisieme-partie-50828"
    stale_check_hours = 18


BLIND_STATE = {"last_check_ok": "2026-07-18T07:11:00+02:00"}


def test_error_finding_names_the_watch_and_the_ip_block():
    error = (
        "Pathé API request failed for https://www.pathe.fr/api/shows: "
        "Client error '403 Forbidden' for url 'https://www.pathe.fr/api/shows'\n"
        "For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/403"
    )

    finding = cli.build_error_finding(Cfg, BLIND_STATE, error, NOW)

    assert finding.kind == "WATCHER_ERROR"
    assert finding.title == "Pathé watch is BLIND"
    assert finding.lines == [
        "Dune : Troisième partie · Pathé Odysseum",
        "No sale detection since Sat 18 Jul, 07:11 (7 h 42 m).",
        "Cause: Pathé is blocking your IP (403).",
        "Retrying every 15 min — usually clears by itself.",
    ]
    # The old copy said "disable any VPN or proxy". When the block is the ISP's
    # own IP — as it was on 2 Sep 2026 — that sends the user after a VPN they
    # do not have.
    assert "VPN" not in "\n".join(finding.lines)


def test_error_finding_keeps_generic_error_actionable_and_single_line():
    finding = cli.build_error_finding(
        Cfg, BLIND_STATE, "temporary DNS failure\nresolver unavailable", NOW
    )

    assert finding.lines == [
        "Dune : Troisième partie · Pathé Odysseum",
        "No sale detection since Sat 18 Jul, 07:11 (7 h 42 m).",
        "Cause: temporary DNS failure resolver unavailable",
        "Retrying every 15 min; check the logs if it persists.",
    ]


def test_error_finding_survives_a_watcher_that_never_succeeded():
    """No last_check_ok yet (first-run setup): no duration to report, but the
    message must still be sendable rather than crashing on None."""
    finding = cli.build_error_finding(Cfg, {}, "boom", NOW)

    assert finding.lines[1] == "No sale detection since the watcher started."


def test_recovered_finding_reports_how_long_it_was_blind():
    finding = cli.build_recovered_finding(Cfg, BLIND_STATE, NOW)

    assert finding.kind == "RECOVERED"
    assert finding.kind in notify.DEFAULT_SILENT_KINDS
    assert finding.lines == [
        "Dune : Troisième partie · Pathé Odysseum",
        "Blind for 7 h 42 m. Checks are running normally.",
    ]


def test_stale_finding_first_alert_buzzes_then_repeats_go_silent():
    """A blind spell that reports once and then goes quiet is the failure this
    guards against: day 1 buzzes, every later day is a silent reminder."""
    blind = timedelta(hours=18)
    first = cli.build_stale_finding(Cfg, BLIND_STATE, blind, "stale:x:0", 1)
    later = cli.build_stale_finding(Cfg, BLIND_STATE, timedelta(days=3), "stale:x:2", 3)

    assert first.kind == "WATCHER_ERROR"
    assert first.kind not in notify.DEFAULT_SILENT_KINDS
    assert first.title == "Local checks have stopped — 18 h"

    assert later.kind == "WATCHER_STILL_BLIND"
    assert later.kind in notify.DEFAULT_SILENT_KINDS
    assert later.title == "Still blind — day 3"
    # Both name the watch and say what the silence is costing.
    for f in (first, later):
        assert f.lines[0] == "Dune : Troisième partie · Pathé Odysseum"
        assert "are dark — cloud reminders still run." in f.lines[-1]


def test_stale_finding_names_every_half_the_local_script_owns():
    """OTW-07: last_check_ok goes stale when the whole local half stops, which
    takes news and Cinesa down with Pathé — naming only Pathé understates it."""

    class WithCinesa(Cfg):
        cinesa_enabled = True

    class WithoutCinesa(Cfg):
        cinesa_enabled = False

    on = cli.build_stale_finding(WithCinesa, BLIND_STATE, timedelta(hours=18), "k", 1)
    off = cli.build_stale_finding(WithoutCinesa, BLIND_STATE, timedelta(hours=18), "k", 1)

    assert "Pathé, news and Cinesa checks are dark" in on.lines[-1]
    assert "Pathé and news checks are dark" in off.lines[-1]
    assert "Cinesa" not in off.lines[-1]
    # The reassurance that the cloud half is still alive must survive.
    assert "cloud reminders still run" in on.lines[-1]


def test_error_finding_swaps_the_403_hint_in_ci(monkeypatch):
    """OTW-03: a manual CI dispatch is 403'd by the datacenter IP. There the
    local advice is wrong — nothing retries and there is no launchd log."""
    error = "Client error '403 Forbidden' for url 'https://www.pathe.fr/api/shows'"

    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    local = cli.build_error_finding(Cfg, BLIND_STATE, error, NOW)

    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    in_ci = cli.build_error_finding(Cfg, BLIND_STATE, error, NOW)

    assert "Retrying every 15 min" in "\n".join(local.lines)
    assert "datacenter" not in "\n".join(local.lines)

    assert "GitHub datacenter IPs" in "\n".join(in_ci.lines)
    assert "run the check locally" in "\n".join(in_ci.lines)
    assert "Retrying every 15 min" not in "\n".join(in_ci.lines)


def test_stale_finding_distinguishes_ip_block_from_a_silent_mac():
    """The cloud pass never calls Pathé, so it reads the cause the local half
    recorded. The two cases need opposite responses: wait, or go switch the
    Mac on."""
    blocked = dict(BLIND_STATE, error_alerted=True, last_error="HTTP 403 Forbidden from x")
    quiet = dict(BLIND_STATE)

    blocked_msg = cli.build_stale_finding(Cfg, blocked, timedelta(days=2), "k", 2)
    quiet_msg = cli.build_stale_finding(Cfg, quiet, timedelta(days=2), "k", 2)

    assert "Cause: Pathé is still blocking your IP (403)." in blocked_msg.lines
    assert "Cause: the Mac hasn't completed a check — off, asleep, or can't push." in quiet_msg.lines


def test_error_finding_does_not_blame_the_ip_for_an_origin_refusal():
    """On 2026-09-02 a listing Pathé declined to serve was reported as an IP
    block, which sent the owner after a network problem that did not exist."""
    error = (
        "HTTP 403 refused by origin from "
        f"https://www.pathe.fr/api/show/{'dune-x-55289'}/showtimes/cinema-pathe-odysseum"
        " — 'No movie allowed !'"
    )

    finding = cli.build_error_finding(Cfg, BLIND_STATE, error, NOW)

    assert "Cause: Pathé is refusing a listing (403), not your IP." in finding.lines
    assert "blocking your IP" not in "\n".join(finding.lines)


def test_origin_refusal_keeps_its_cause_in_ci_and_across_the_stale_repeat():
    """CI must not relabel it a datacenter block, and the 24 h repeat has to
    stay grammatical after the 'still' rewrite."""
    st = dict(BLIND_STATE, error_alerted=True, last_error="HTTP 403 refused by origin")

    ci_cause, _ = cli.pathe_cause("HTTP 403 refused by origin", ci=True)
    repeat = cli.build_stale_finding(Cfg, st, timedelta(days=2), "k", 2)

    assert ci_cause == "Cause: Pathé is refusing a listing (403), not your IP."
    assert "Cause: Pathé is still refusing a listing (403), not your IP." in repeat.lines


def test_stale_finding_ignores_a_stale_cause_from_a_finished_outage():
    """last_error without error_alerted means the local half is not currently
    failing — the recorded cause is from an outage that already recovered."""
    st = dict(BLIND_STATE, error_alerted=False, last_error="HTTP 403 Forbidden from x")

    msg = cli.build_stale_finding(Cfg, st, timedelta(days=2), "k", 2)

    assert "the Mac hasn't completed a check" in "\n".join(msg.lines)


def test_error_summary_is_idempotent():
    """The summary is stored in state and re-parsed by the cloud pass, so
    summarising it twice must not lose the status code."""
    raw = "Client error '403 Forbidden' for url 'https://www.pathe.fr/api/shows'"
    once = cli.summarize_pathe_error(raw)

    assert once == (
        "HTTP 403 Forbidden from https://www.pathe.fr/api/shows",
        403,
    )
    assert cli.summarize_pathe_error(once[0]) == once


def test_fmt_duration_reads_naturally_at_every_scale():
    assert cli.fmt_duration(timedelta(minutes=45)) == "45 min"
    assert cli.fmt_duration(timedelta(hours=6, minutes=20)) == "6 h 20 m"
    assert cli.fmt_duration(timedelta(hours=18)) == "18 h"
    assert cli.fmt_duration(timedelta(days=3)) == "3 days"


CONFIG_TOML = """
[film]
primary_slug = "dune-troisieme-partie"

[cinema]
slug = "montpellier-multiplexe-odysseum"
name = "Pathé Odysseum"
city = "Montpellier"

[news]
enabled = false

[alerts]
heartbeat_days = 0
failure_streak_threshold = 3
stale_check_hours = 0
"""


class PatheCheckRunner:
    """Drive check mode with all Pathé I/O and Telegram delivery faked."""

    def __init__(self, tmp_path, monkeypatch):
        self.monkeypatch = monkeypatch
        self.config = tmp_path / "config.toml"
        self.config.write_text(CONFIG_TOML, encoding="utf-8")
        self.state = tmp_path / "state.json"
        self.state.write_text(json.dumps(DEFAULT_STATE), encoding="utf-8")
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")
        monkeypatch.setattr(pathe, "make_client", object)

    def run(self, result, *, delivered: bool) -> dict:
        """One firing. `result` is a snapshot to return or an exception to raise."""

        def fake_fetch(client, cfg):
            if isinstance(result, Exception):
                raise result
            return result

        self.monkeypatch.setattr(pathe, "fetch_snapshot", fake_fetch)
        self.sent = []

        def fake_send(cfg, text, **kw):
            self.sent.append(text)
            return delivered

        self.monkeypatch.setattr(notify, "send_telegram", fake_send)
        assert (
            cli.run(
                [
                    "--config",
                    str(self.config),
                    "--state",
                    str(self.state),
                    "--mode",
                    "check",
                ]
            )
            == 0
        )
        return json.loads(self.state.read_text(encoding="utf-8"))


def test_pathe_outage_stops_rewriting_state_once_capped(tmp_path, monkeypatch):
    """A prolonged outage must settle instead of committing a new count every firing."""
    runner = PatheCheckRunner(tmp_path, monkeypatch)
    boom = RuntimeError("HTTP 500 from www.pathe.fr")

    for _ in range(3):  # failure_streak_threshold
        runner.run(boom, delivered=False)
    assert json.loads(runner.state.read_text())["failure_streak"] == 3

    settled = runner.state.read_bytes()
    runner.run(boom, delivered=False)
    runner.run(boom, delivered=False)
    assert runner.state.read_bytes() == settled


def test_failed_pathe_one_shot_alerts_are_retried_on_the_next_run(
    tmp_path, monkeypatch
):
    """Every alert baseline moves only once the alert it gates was delivered."""
    runner = PatheCheckRunner(tmp_path, monkeypatch)
    sale = "2026-11-05T08:00:00+01:00"
    show = {
        "slug": "dune-troisieme-partie-imax-70mm",
        "title": "Dune : Troisième partie : Projection IMAX 70mm",
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

    st = runner.run(snap, delivered=False)
    assert st["shows_seen"] == []
    assert st["formats_seen"] == {}
    # `sales` used to be recorded here regardless ("sale truth is ungated"),
    # which armed the reminder ladder one run sooner — at the price of marking
    # the opening known, so SALE_DATE was never raised again and the single
    # alert this watcher exists for was lost to one failed send.
    assert st["sales"] == {}
    assert st["tickets_available"] is True  # current session truth is still ungated

    st = runner.run(snap, delivered=True)
    assert st["shows_seen"] == [show["slug"]]
    assert st["formats_seen"] == {show["slug"]: ["imax70"]}
    assert st["sales"] == {show["slug"]: sale}
    assert st["sale_target"] == sale  # the ladder is armed, one run later
    assert set(st["alerts"]) == {
        f"new_show:{show['slug']}",
        f"tickets:{show['slug']}:imax70",
        f"sale:{show['slug']}:{sale}",
    }


def test_stale_period_fires_once_at_the_threshold_then_daily():
    """Regression guard for the gap this feature closes: an outage used to
    alert once and then go quiet for as long as it lasted."""
    hours = 18
    fired = []
    # Walk a 5-day outage at the cloud pass's own 15-minute cadence.
    for step in range(1, 5 * 24 * 4):
        blind = timedelta(minutes=15 * step)
        if blind <= timedelta(hours=hours):
            continue
        period = cli.stale_period(blind, hours)
        if period not in [p for p, _ in fired]:
            fired.append((period, blind))

    assert [p for p, _ in fired] == [0, 1, 2, 3, 4]
    # First at the threshold, then every 24 h — same clock time each day.
    assert [round(b.total_seconds() / 3600) for _, b in fired] == [18, 42, 66, 90, 114]


def test_stale_period_is_zero_right_at_the_threshold():
    assert cli.stale_period(timedelta(hours=18, minutes=1), 18) == 0
    assert cli.stale_period(timedelta(hours=41), 18) == 0
    assert cli.stale_period(timedelta(hours=42), 18) == 1


def test_stale_finding_never_blames_ci_for_an_error_the_mac_recorded(monkeypatch):
    """The cloud supervision pass always runs inside Actions, but the cause it
    reports was recorded by the Mac. Branching on the *reader's* machine made
    every residential 403 read as the expected datacenter block — telling the
    user to dismiss a real outage."""
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    st = dict(BLIND_STATE, error_alerted=True, last_error="HTTP 403")

    stale = cli.build_stale_finding(Cfg, st, timedelta(days=2), "k", 2)

    assert "Cause: Pathé is still blocking your IP (403)." in stale.lines
    assert "datacenter" not in "\n".join(stale.lines)

    # The local builder still describes the machine it is running on.
    error = "Client error '403 Forbidden' for url 'https://www.pathe.fr/api/shows'"
    assert "GitHub datacenter IPs" in "\n".join(
        cli.build_error_finding(Cfg, BLIND_STATE, error, NOW).lines
    )


def test_recorded_cause_drops_the_failing_url():
    """fetch_snapshot hits several endpoints. Keeping the URL in `last_error`
    would rewrite — and commit and push — state on every 15-min firing of an
    outage that flapped between them."""
    a = cli.summarize_pathe_error(
        "Client error '403 Forbidden' for url 'https://www.pathe.fr/api/shows'"
    )
    b = cli.summarize_pathe_error(
        "Client error '403 Forbidden' for url 'https://www.pathe.fr/api/show/x/showtimes/y'"
    )

    assert a[1] == b[1] == 403
    assert f"HTTP {a[1]}" == f"HTTP {b[1]}" == "HTTP 403"
    # And the stored short form still yields its status when re-read.
    assert cli.summarize_pathe_error("HTTP 403")[1] == 403


# --------------------------------------------------------------- merged delivery

DUPLICATE_SALE = "2026-11-05T08:00:00+01:00"


def two_listings_one_opening() -> Snapshot:
    """What produced the 2026-09-03 burst: two listings, one opening time."""
    return Snapshot(
        matched_shows=[
            {
                "slug": "dune-troisieme-partie",
                "title": "Dune : Troisième partie",
                "salesOpeningDatetime": DUPLICATE_SALE,
                "isMovie": True,
            },
            {
                "slug": "dune-troisieme-partie-projection-imax-70mm",
                "title": "Dune - Troisième partie : Projection IMAX 70mm",
                "salesOpeningDatetime": DUPLICATE_SALE,
                "isMovie": False,
            },
        ]
    )


def test_one_opening_on_two_listings_sends_one_message_and_marks_both_keys(
    tmp_path, monkeypatch
):
    runner = PatheCheckRunner(tmp_path, monkeypatch)

    state = runner.run(two_listings_one_opening(), delivered=True)

    sale_keys = [k for k in state["alerts"] if k.startswith("sale:")]
    assert len(sale_keys) == 2  # dedup memory unchanged: still one key per listing
    # …but the user's phone buzzed once for the opening, not twice.
    openings = [t for t in runner.sent if "Sale opens" in t]
    assert len(openings) == 1
    assert "IMAX 70 mm (1.43:1), Standard / other" in openings[0]


def test_a_failed_merged_send_marks_no_key_and_retries_the_whole_group(
    tmp_path, monkeypatch
):
    """Half a group marked sent would leave the other half alone forever."""
    runner = PatheCheckRunner(tmp_path, monkeypatch)

    state = runner.run(two_listings_one_opening(), delivered=False)
    assert [k for k in state["alerts"] if k.startswith("sale:")] == []

    state = runner.run(two_listings_one_opening(), delivered=True)
    assert len([k for k in state["alerts"] if k.startswith("sale:")]) == 2
    assert len([t for t in runner.sent if "Sale opens" in t]) == 1


def test_an_already_announced_listing_does_not_rejoin_the_group(
    tmp_path, monkeypatch
):
    """Merging runs after the already-sent filter, so a second listing
    appearing later is announced on its own — not alongside old news."""
    runner = PatheCheckRunner(tmp_path, monkeypatch)
    first = Snapshot(matched_shows=[two_listings_one_opening().matched_shows[0]])

    runner.run(first, delivered=True)
    was = json.loads(runner.state.read_text())["alerts"]

    state = runner.run(two_listings_one_opening(), delivered=True)

    # The first listing's alert keeps its original timestamp — not re-sent.
    key = "sale:dune-troisieme-partie:" + DUPLICATE_SALE
    assert state["alerts"][key] == was[key]
    openings = [t for t in runner.sent if "Sale opens" in t]
    assert len(openings) == 1
    assert "IMAX 70 mm (1.43:1) ·" in openings[0]  # only the new listing


def test_a_listing_returned_twice_is_announced_once(tmp_path, monkeypatch):
    """A catalogue that repeats a slug must not repeat it inside a message."""
    runner = PatheCheckRunner(tmp_path, monkeypatch)
    show = two_listings_one_opening().matched_shows[1]

    runner.run(Snapshot(matched_shows=[show, dict(show)]), delivered=True)

    assert len([t for t in runner.sent if "Sale opens" in t]) == 1
    assert len([t for t in runner.sent if "New listing" in t]) == 1
