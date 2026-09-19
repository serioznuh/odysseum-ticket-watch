"""Tests for CLI alert construction."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from watcher import __main__ as cli
from watcher import cinesa, detect, news, notify, pathe
from watcher.detect import TZ_PARIS, Snapshot
from watcher.state import CURRENT_STATE_VERSION, DEFAULT_STATE

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
        "Retrying every 5 min — usually clears by itself.",
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
        "Retrying every 5 min; check the logs if it persists.",
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

    assert "Retrying every 5 min" in "\n".join(local.lines)
    assert "datacenter" not in "\n".join(local.lines)

    assert "GitHub datacenter IPs" in "\n".join(in_ci.lines)
    assert "run the check locally" in "\n".join(in_ci.lines)
    assert "Retrying every 5 min" not in "\n".join(in_ci.lines)


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


def _write_cli_config(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(CONFIG_TOML, encoding="utf-8")
    return config


def test_invalid_state_stops_before_network_or_telegram_and_is_not_overwritten(
    tmp_path, monkeypatch
):
    config = _write_cli_config(tmp_path)
    state = tmp_path / "state.json"
    evidence = b"{ broken json"
    state.write_bytes(evidence)
    calls = []
    monkeypatch.setattr(notify, "send_telegram", lambda *args, **kwargs: calls.append("send"))
    monkeypatch.setattr(pathe, "make_client", lambda: calls.append("source"))

    result = cli.run(["--config", str(config), "--state", str(state), "--mode", "check"])

    assert result == 2
    assert calls == []
    assert state.read_bytes() == evidence
    assert sorted(p.name for p in tmp_path.iterdir()) == ["config.toml", "state.json"]


def test_missing_production_state_stops_without_sending_or_creating_it(
    tmp_path, monkeypatch
):
    config = _write_cli_config(tmp_path)
    state = tmp_path / "missing-state.json"
    calls = []
    monkeypatch.setattr(notify, "send_telegram", lambda *args, **kwargs: calls.append("send"))

    result = cli.run(["--config", str(config), "--state", str(state), "--mode", "remind"])

    assert result == 2
    assert calls == []
    assert not state.exists()


def test_bootstrap_state_is_an_explicit_first_use_action(tmp_path, monkeypatch):
    config = _write_cli_config(tmp_path)
    state = tmp_path / "first-use" / "state.json"
    calls = []
    monkeypatch.setattr(notify, "send_telegram", lambda *args, **kwargs: calls.append("send"))

    result = cli.run(
        ["--config", str(config), "--state", str(state), "--bootstrap-state"]
    )

    assert result == 0
    assert calls == []
    assert json.loads(state.read_text(encoding="utf-8")) == DEFAULT_STATE


def test_dry_run_migrates_only_in_memory_and_leaves_state_bytes_unchanged(tmp_path):
    config = _write_cli_config(tmp_path)
    state = tmp_path / "state.json"
    old = json.loads(json.dumps(DEFAULT_STATE))
    old["version"] = 1
    old.pop("cinesa")
    before = (json.dumps(old, indent=4) + "\n").encode()
    state.write_bytes(before)

    result = cli.run(
        ["--config", str(config), "--state", str(state), "--mode", "remind", "--dry-run"]
    )

    assert result == 0
    assert state.read_bytes() == before
    assert json.loads(state.read_text())["version"] == 1
    assert CURRENT_STATE_VERSION == 4


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

        def fake_fetch(client, cfg, **_budget):
            if isinstance(result, Exception):
                raise result
            return result

        self.monkeypatch.setattr(pathe, "fetch_snapshot", fake_fetch)
        self.sent = []

        def fake_send(cfg, text, **kw):
            self.sent.append(text)
            return delivered(text) if callable(delivered) else delivered

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


def test_persistent_listing_failure_uses_supervision_and_never_reports_recovery(
    tmp_path, monkeypatch
):
    """Healthy catalogues plus one permanently failing listing are degraded,
    not a successful check. The existing streak policy alerts once and settles."""
    runner = PatheCheckRunner(tmp_path, monkeypatch)
    slug = "dune-troisieme-partie"
    snap = Snapshot(
        matched_shows=[{"slug": slug, "title": "Dune : Troisième partie"}],
        listing_results={
            slug: {"showtimes": detect.FetchResult.failed("HTTP 500 from showtimes")}
        },
    )

    for _ in range(2):
        st = runner.run(snap, delivered=True)
        assert runner.sent == []
    st = runner.run(snap, delivered=True)

    assert len(runner.sent) == 1
    assert "Pathé watch is DEGRADED" in runner.sent[0]
    assert "Checks are running normally" not in runner.sent[0]
    assert st["last_check_ok"] is None
    assert st["failure_streak"] == 3
    assert st["error_alerted"] is True

    settled = runner.state.read_bytes()
    runner.run(snap, delivered=True)
    assert runner.sent == []
    assert runner.state.read_bytes() == settled

    healthy = Snapshot(matched_shows=[{"slug": slug, "title": "Dune"}])
    st = runner.run(healthy, delivered=True)
    assert len(runner.sent) == 1
    assert "Pathé watch is back" in runner.sent[0]
    assert "Degraded state cleared" in runner.sent[0]
    assert st["failure_streak"] == 0
    assert st["error_alerted"] is False
    assert st["last_check_ok"] is not None


def test_degraded_watch_escalating_to_catalogue_blindness_alerts_again(
    tmp_path, monkeypatch
):
    """Acknowledging partial degradation must never suppress a later, strictly
    worse catalogue outage. Unchanged blindness still settles after that alert."""
    runner = PatheCheckRunner(tmp_path, monkeypatch)
    slug = "dune-troisieme-partie"
    degraded = Snapshot(
        matched_shows=[{"slug": slug, "title": "Dune : Troisième partie"}],
        listing_results={
            slug: {"showtimes": detect.FetchResult.failed("HTTP 500 from showtimes")}
        },
    )
    for _ in range(3):
        st = runner.run(degraded, delivered=True)
    assert st["error_alerted"] is True
    assert "Pathé watch is DEGRADED" in runner.sent[0]

    blind = RuntimeError("HTTP 403 from Pathé catalogue")
    st = runner.run(blind, delivered=True)

    assert len(runner.sent) == 1
    assert "Pathé watch is BLIND" in runner.sent[0]
    assert st["error_alerted"] is True
    assert st["last_error"] == "HTTP 403"

    settled = runner.state.read_bytes()
    runner.run(blind, delivered=True)
    assert runner.sent == []
    assert runner.state.read_bytes() == settled


def test_cloud_reports_a_degraded_then_dark_mac_as_stopped_not_merely_degraded():
    st = {
        "last_check_ok": "2026-07-17T07:11:00+02:00",
        "last_catalogue_ok": "2026-07-18T07:11:00+02:00",
        "last_error": f"{detect.PARTIAL_PATHE_FAILURE} showtimes: dune",
        "error_alerted": True,
    }
    blind = NOW - datetime.fromisoformat(st["last_catalogue_ok"])

    finding = cli.build_stale_finding(Cfg, st, blind, "stale:x:0", 1)
    text = "\n".join([finding.title, *finding.lines])

    assert finding.title == "Local checks have stopped — 7 h 42 m"
    assert "Last catalogue check: Sat 18 Jul, 07:11." in text
    assert "stopped checking after reporting degraded listing data" in text
    assert "are dark — cloud reminders still run" in text
    assert "Catalogue checks still work" not in text
    assert "listing checks degraded" not in text


def test_cloud_supervision_uses_catalogue_liveness_for_degraded_local_half(
    tmp_path, monkeypatch
):
    config = tmp_path / "config.toml"
    config.write_text(
        CONFIG_TOML.replace("stale_check_hours = 0", "stale_check_hours = 18"),
        encoding="utf-8",
    )
    now = datetime.now(TZ_PARIS)
    st = json.loads(json.dumps(DEFAULT_STATE))
    st.update(
        last_check_ok=(now - timedelta(days=2)).isoformat(),
        last_catalogue_ok=(now - timedelta(hours=1)).isoformat(),
        last_error=f"{detect.PARTIAL_PATHE_FAILURE} showtimes: dune",
        error_alerted=True,
    )
    state = tmp_path / "state.json"
    state.write_text(json.dumps(st), encoding="utf-8")
    sent = []
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")
    monkeypatch.setattr(
        notify, "send_telegram", lambda cfg, text, **kw: sent.append(text) or True
    )

    assert cli.run(
        ["--config", str(config), "--state", str(state), "--mode", "remind"]
    ) == 0
    assert sent == []  # degraded but demonstrably alive

    st["last_catalogue_ok"] = (now - timedelta(hours=19)).isoformat()
    state.write_text(json.dumps(st), encoding="utf-8")
    assert cli.run(
        ["--config", str(config), "--state", str(state), "--mode", "remind"]
    ) == 0

    assert len(sent) == 1
    assert "Local checks have stopped" in sent[0]
    assert "Catalogue checks still work" not in sent[0]


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
    # The delivered `sales` baseline stays frozen so SALE_DATE remains
    # eligible, while current observation still arms the independent ladder.
    assert st["sales"] == {}
    assert st["sale_target"] == sale
    assert st["tickets_available"] is True  # current session truth is still ungated

    st = runner.run(snap, delivered=True)
    assert st["shows_seen"] == [show["slug"]]
    assert st["formats_seen"] == {show["slug"]: ["imax70"]}
    assert st["sales"] == {show["slug"]: sale}
    assert st["sale_target"] == sale
    assert set(st["alerts"]) == {
        f"new_show:{show['slug']}",
        f"tickets:{show['slug']}:imax70",
        f"sale:{show['slug']}:{sale}",
    }


def test_uncertain_sale_alert_still_arms_and_delivers_the_reminder_ladder(
    tmp_path, monkeypatch
):
    """A lost Telegram response quarantines only that message, not the fresh
    Pathé observation which drives the independent reminder ladder."""
    runner = PatheCheckRunner(tmp_path, monkeypatch)
    sale = (datetime.now(TZ_PARIS) + timedelta(minutes=10)).isoformat()
    show = {
        "slug": "dune-troisieme-partie",
        "title": "Dune : Troisième partie",
        "salesOpeningDatetime": sale,
        "isMovie": True,
    }
    outcomes = iter(
        (
            notify.SendResult("uncertain"),
            notify.SendResult("confirmed", 314),
        )
    )

    st = runner.run(
        Snapshot(matched_shows=[show]), delivered=lambda _text: next(outcomes)
    )

    assert len(runner.sent) == 2
    assert st["sales"] == {}
    assert st["sale_target"] == sale
    assert "15" in st["reminders_sent"][sale]
    assert any(
        record["status"] == "uncertain" and f"sale:{show['slug']}:{sale}" in record["keys"]
        for record in st["outbox"].values()
    )


def test_fresh_opening_supersedes_pending_alert_before_outbox_recovery(
    tmp_path, monkeypatch
):
    """A failed old alert must not be replayed before polling discovers that
    Pathé moved the opening and enqueues the corrected message."""
    runner = PatheCheckRunner(tmp_path, monkeypatch)
    slug = "dune-troisieme-partie"
    old = "2026-10-01T09:00:00+02:00"
    moved = "2026-10-02T09:00:00+02:00"

    def snapshot(sale):
        return Snapshot(
            matched_shows=[
                    {
                        "slug": slug,
                        "title": "Dune : Troisième partie",
                        "salesOpeningDatetime": sale,
                        "isMovie": True,
                }
            ]
        )

    first = runner.run(snapshot(old), delivered=False)
    assert first["outbox"]
    assert first["alerts"] == {}

    second = runner.run(snapshot(moved), delivered=True)

    assert len(runner.sent) == 1
    assert f"sale:{slug}:{old}" not in second["alerts"]
    assert f"sale:{slug}:{moved}" in second["alerts"]
    assert second["outbox"] == {}


def test_pending_reminder_waits_for_polling_and_is_retired_when_opening_moves(
    tmp_path, monkeypatch
):
    """Newly due reminders still lead the run, but a prior failed attempt
    must wait for Pathé to confirm that its target is still current."""
    runner = PatheCheckRunner(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "datetime", _scripted_clock(NOW, NOW))
    old = (NOW + timedelta(minutes=10)).isoformat()
    moved = (NOW + timedelta(days=2)).isoformat()

    def snapshot(sale):
        return Snapshot(
            matched_shows=[
                {
                    "slug": "dune-troisieme-partie",
                    "title": "Dune : Troisième partie",
                    "salesOpeningDatetime": sale,
                    "isMovie": True,
                }
            ]
        )

    outcomes = iter((notify.SendResult("confirmed", 10), notify.SendResult("failed")))
    first = runner.run(snapshot(old), delivered=lambda _text: next(outcomes))
    pending = list(first["outbox"].values())
    assert len(pending) == 1
    assert pending[0]["ack"]["type"] == "reminder"
    assert pending[0]["ack"]["target"] == old

    second = runner.run(snapshot(moved), delivered=True)

    assert len(runner.sent) == 1
    assert "Sale time CHANGED" in runner.sent[0]
    assert second["sale_target"] == moved
    assert all(
        record["ack"].get("target") != old
        for record in second["outbox"].values()
        if record["ack"]["type"] == "reminder"
    )


def test_healthy_poll_retires_failed_blind_alert_before_recovery(
    tmp_path, monkeypatch
):
    """A definite failure leaves the BLIND alert pending, but a later healthy
    observation makes it obsolete before outbox recovery can send it."""
    runner = PatheCheckRunner(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "datetime", _scripted_clock(NOW, NOW))
    state = json.loads(runner.state.read_text(encoding="utf-8"))
    stale = (NOW - timedelta(days=1)).isoformat()
    state.update(
        failure_streak=2,
        last_check_ok=stale,
        last_catalogue_ok=stale,
    )
    runner.state.write_text(json.dumps(state), encoding="utf-8")

    failed = runner.run(RuntimeError("HTTP 500"), delivered=False)
    assert len(failed["outbox"]) == 1
    blind = next(iter(failed["outbox"].values()))
    assert blind["kinds"] == ["WATCHER_ERROR"]
    assert blind["topics"] == ["pathe-health"]

    healthy = Snapshot(
        matched_shows=[
            {
                "slug": "dune-troisieme-partie",
                "title": "Dune : Troisième partie",
                "isMovie": True,
            }
        ]
    )
    recovered = runner.run(healthy, delivered=True)

    assert runner.sent == []
    assert recovered["outbox"] == {}


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


# ------------------------------------------------- reminder ownership (OTW-15)

REMINDER_CONFIG_TOML = """
[film]
primary_slug = "dune-troisieme-partie"

[cinema]
slug = "montpellier-multiplexe-odysseum"

[news]
enabled = false

[cinesa]
enabled = false
"""


def _reminder_fixture(tmp_path, monkeypatch, sent: list, minutes: int = 10):
    """State on the eve of the sale: 24h and 2h reminders already delivered, the
    15-min one still owed, and a Pathé check fresh enough for the cadence guard
    to skip. Cinesa is off, the default."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")
    monkeypatch.setattr(
        notify, "send_telegram", lambda cfg, text, **kw: sent.append(text) or True
    )
    config = tmp_path / "config.toml"
    config.write_text(REMINDER_CONFIG_TOML, encoding="utf-8")

    now = datetime.now(TZ_PARIS)
    # The extra 30 s keeps the floored countdown on "10 minutes" for the whole
    # test rather than tipping to 9 on the clock ticking between here and run().
    target = (now + timedelta(minutes=minutes, seconds=30)).isoformat()
    st = json.loads(json.dumps(DEFAULT_STATE))
    st["last_check_ok"] = now.isoformat()
    st["sale_target"] = target
    st["reminders_sent"] = {target: ["120", "1440"]}
    state = tmp_path / "state.json"
    state.write_text(json.dumps(st), encoding="utf-8")
    return config, state, target


def test_local_half_sends_the_reminder_the_cadence_guard_used_to_swallow(
    tmp_path, monkeypatch
):
    """Reminders were cloud-only, and the cloud cron ran ~11% of its schedule.
    The local half owns them now — including on a firing where the adaptive
    guard skips Pathé, which with Cinesa off used to return before the ladder."""
    sent: list[str] = []
    config, state, target = _reminder_fixture(tmp_path, monkeypatch, sent)

    assert cli.run(
        ["--config", str(config), "--state", str(state),
         "--mode", "check", "--adaptive-cadence"]
    ) == 0

    assert len(sent) == 1
    assert "Sale opens in 10 minutes" in sent[0]  # actual time left...
    assert "in 15 minutes" not in sent[0]         # ...not the offset's label
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert "15" in saved["reminders_sent"][target]


def test_cloud_grace_stays_out_of_the_owners_way(tmp_path, monkeypatch):
    """Same state, the cloud pass: the 15-min window opened 5 min ago, inside
    the 25-min grace, so the failover leaves it to the Mac."""
    sent: list[str] = []
    config, state, target = _reminder_fixture(tmp_path, monkeypatch, sent)

    assert cli.run(
        ["--config", str(config), "--state", str(state),
         "--mode", "remind", "--reminder-grace-minutes", "25"]
    ) == 0

    assert sent == []
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["reminders_sent"][target] == ["120", "1440"]


def _scripted_clock(*readings: datetime):
    """A stand-in for `datetime` whose `now()` returns `readings` in order, the
    last one repeating. Subclassing keeps every other use of the name working.

    A run reads the clock twice — once at the top, which is also what the
    pre-polling reminder pass sees, and once after the sources have been
    polled, for the second pass over the ladder.
    """
    queue = list(readings)

    class ScriptedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return queue.pop(0) if len(queue) > 1 else queue[0]

    return ScriptedClock


def test_a_run_that_overruns_the_opening_still_gets_both_rungs_out(
    tmp_path, monkeypatch
):
    """A run that starts at T-14 and only finishes at T+1 owes the user two
    different things, and used to deliver neither correctly.

    The ladder used to sit behind all polling and retries: it woke at T+1, so
    the 15-min warning was gone for good, and wording it from the run-start
    clock would have announced a warning for a sale that was already open. It
    is now checked before any request — the warning goes out at T-14, on time —
    and again afterwards, where the later clock correctly picks the GO ping
    (OTW-19)."""
    sent: list[str] = []
    config, state, target = _reminder_fixture(tmp_path, monkeypatch, sent)
    opening = datetime.fromisoformat(target)
    monkeypatch.setattr(
        cli,
        "datetime",
        _scripted_clock(opening - timedelta(minutes=14), opening + timedelta(minutes=1)),
    )

    assert cli.run(
        ["--config", str(config), "--state", str(state),
         "--mode", "check", "--adaptive-cadence"]
    ) == 0

    assert len(sent) == 2
    assert "Sale opens in 14 minutes" in sent[0]      # recovered, not swallowed
    assert "Scheduled sale time reached" in sent[1]   # worded from the later clock
    assert "Sale opens in" not in sent[1]
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["reminders_sent"][target] == ["120", "1440", "15", "open"]


def test_a_slow_run_counts_down_from_where_it_finished(tmp_path, monkeypatch):
    """A rung whose window opens *during* the run belongs to the second ladder
    pass, and must be worded from the clock that pass reads. Here the 15-min
    window is still shut at run-start (T-20) and open by the time the sources
    are done (T-2): two minutes to announce, not twenty — the countdown is what
    the user acts on."""
    sent: list[str] = []
    config, state, target = _reminder_fixture(tmp_path, monkeypatch, sent, minutes=20)
    opening = datetime.fromisoformat(target)
    monkeypatch.setattr(
        cli,
        "datetime",
        _scripted_clock(opening - timedelta(minutes=20), opening - timedelta(minutes=2)),
    )

    assert cli.run(
        ["--config", str(config), "--state", str(state),
         "--mode", "check", "--adaptive-cadence"]
    ) == 0

    assert len(sent) == 1
    assert "Sale opens in 2 minutes" in sent[0]
    assert "20 minutes" not in sent[0]  # the run-start clock's countdown
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert "15" in saved["reminders_sent"][target]


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


def test_wanted_pathe_dates_retry_together_then_stay_quiet(tmp_path, monkeypatch):
    runner = PatheCheckRunner(tmp_path, monkeypatch)
    runner.config.write_text(CONFIG_TOML.replace(
        '[film]', '[film]\ntarget_format="imax70"\n'
        'target_dates=["2026-12-19", "2026-12-20"]'))
    slug = "dune-troisieme-partie-projection-imax-70mm-55289"
    snap = Snapshot(
        matched_shows=[{"slug": slug, "title": "Dune IMAX 70mm", "isMovie": False}],
        cinema_entries={slug: {"bookable": True, "days": {
            "2026-12-19": {"bookable": True}, "2026-12-20": {"bookable": True}}}},
    )
    # Stable clock: test still exercises wanted dates after calendar December.
    monkeypatch.setattr(cli, "datetime", _scripted_clock(NOW, NOW))
    initial = json.loads(runner.state.read_text())
    initial['shows_seen'] = [slug]
    initial['formats_seen'] = {slug: ['imax70']}
    runner.state.write_text(json.dumps(initial))
    st = runner.run(snap, delivered=False)
    assert len(runner.sent) == 1
    assert st['alerts'] == {}
    monkeypatch.setattr(cli, "datetime", _scripted_clock(NOW, NOW))
    st = runner.run(snap, delivered=True)
    assert len(runner.sent) == 1
    assert '19 Dec' in runner.sent[0] and '20 Dec' in runner.sent[0]
    assert len([k for k in st['alerts'] if k.startswith('pathe_target:')]) == 2
    monkeypatch.setattr(cli, "datetime", _scripted_clock(NOW, NOW))
    runner.run(snap, delivered=True)
    assert runner.sent == []


# ------------------------------------------- bounded jobs, ordering (OTW-19)

REMIND_ONLY_CONFIG_TOML = REMINDER_CONFIG_TOML.replace(
    "[cinesa]\nenabled = false",
    '[cinesa]\nenabled = true\nfilm_id = "HO00003228"\nsite_id = "032"',
)


def test_a_slow_source_cannot_eat_the_warning_window(tmp_path, monkeypatch):
    """The reminder ladder used to wait behind every fetch and retry. A run
    that started at T-14 and did not reach the ladder until T+6 lost the 15-min
    warning outright — re-reading the clock could word the late message
    correctly, but could not give the window back.

    The ladder is now served before a source is contacted at all, so the
    warning goes out on time even though this run's Pathé call burns the whole
    window and then fails."""
    sent: list[str] = []
    trace: list[str] = []
    config, state, target = _reminder_fixture(tmp_path, monkeypatch, sent)
    opening = datetime.fromisoformat(target)

    def traced_send(cfg, text, **kw):
        trace.append("telegram")
        sent.append(text)
        return True

    def slow_then_broken(client, cfg, **_budget):
        trace.append("pathe")
        raise RuntimeError("HTTP 500 from www.pathe.fr")

    monkeypatch.setattr(notify, "send_telegram", traced_send)
    monkeypatch.setattr(pathe, "make_client", object)
    monkeypatch.setattr(pathe, "fetch_snapshot", slow_then_broken)
    monkeypatch.setattr(
        cli,
        "datetime",
        _scripted_clock(opening - timedelta(minutes=14), opening + timedelta(minutes=6)),
    )

    # No cadence flag: this firing really does poll Pathé.
    assert cli.run(
        ["--config", str(config), "--state", str(state), "--mode", "check"]
    ) == 0

    assert trace[0] == "telegram"  # the ladder went first, not last
    assert "pathe" in trace        # ...and the source was still polled
    assert "Sale opens in 14 minutes" in sent[0]
    assert "Scheduled sale time reached" in sent[-1]
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["reminders_sent"][target] == ["120", "1440", "15", "open"]


def test_a_crashing_job_cannot_take_the_ladder_down_with_it(tmp_path, monkeypatch):
    """A bug in one job is reported by the exit code, but must not cost the run
    its reminders, its supervision or its state save."""
    sent: list[str] = []
    config, state, target = _reminder_fixture(tmp_path, monkeypatch, sent)

    def boom(*args, **kwargs):
        raise TypeError("analysis bug")

    monkeypatch.setattr(pathe, "make_client", object)
    monkeypatch.setattr(pathe, "fetch_snapshot", lambda client, cfg, **kw: Snapshot())
    monkeypatch.setattr(detect, "analyze_pathe", boom)

    assert cli.run(
        ["--config", str(config), "--state", str(state), "--mode", "check"]
    ) == 1

    assert len(sent) == 1
    assert "Sale opens in 10 minutes" in sent[0]
    # The receipt is persisted, so the next firing does not repeat the rung.
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert "15" in saved["reminders_sent"][target]


def test_default_remind_mode_makes_no_source_requests(tmp_path, monkeypatch):
    """What the cloud half runs every 15 min. Pathé 403s datacenter IPs and
    Cinesa is challenged from them, so this pass must stay reminders and
    supervision only — even with the Cinesa half enabled in config."""
    sent: list[str] = []
    config, state, _target = _reminder_fixture(tmp_path, monkeypatch, sent)
    config.write_text(REMIND_ONLY_CONFIG_TOML, encoding="utf-8")
    touched: list[str] = []

    monkeypatch.setattr(pathe, "make_client", lambda: touched.append("pathe client"))
    monkeypatch.setattr(pathe, "fetch_snapshot", lambda *a, **k: touched.append("pathe"))
    monkeypatch.setattr(news, "fetch_news_items", lambda *a, **k: touched.append("news"))
    monkeypatch.setattr(cinesa, "fetch_snapshot", lambda *a, **k: touched.append("cinesa"))

    assert cli.run(
        ["--config", str(config), "--state", str(state), "--mode", "remind"]
    ) == 0

    assert touched == []
    assert len(sent) == 1
    assert "Sale opens in 10 minutes" in sent[0]


def test_a_newly_published_opening_is_alerted_in_the_run_that_polls_it(
    tmp_path, monkeypatch
):
    """Splitting the pass into jobs must not put a run's lag between observing
    a sale date and announcing it — that single alert is the whole point."""
    runner = PatheCheckRunner(tmp_path, monkeypatch)
    sale = "2026-11-05T08:00:00+01:00"
    show = {
        "slug": "dune-troisieme-partie",
        "title": "Dune : Troisième partie",
        "salesOpeningDatetime": sale,
        "isMovie": True,
    }

    st = runner.run(Snapshot(matched_shows=[show]), delivered=True)

    assert [t for t in runner.sent if "Sale opens" in t]
    assert f"sale:{show['slug']}:{sale}" in st["alerts"]
    assert st["sale_target"] == sale  # and the ladder is armed by the same run


def test_a_crash_in_analysis_cannot_bank_a_recovery_it_threw_away(
    tmp_path, monkeypatch
):
    """The healthy branch clears `error_alerted` and refreshes the health
    timestamps; the recovery alert itself is one of the findings the job
    returns. If analysis then crashed and the coordinator discarded those
    findings, a saved `error_alerted=False` would mean the outage ended in
    silence and "Pathé watch is back" could never fire again (round-1 review).
    """
    runner = PatheCheckRunner(tmp_path, monkeypatch)
    blind = json.loads(runner.state.read_text())
    blind.update(
        error_alerted=True,
        failure_streak=3,
        last_error="HTTP 403",
        last_check_ok="2026-07-18T07:11:00+02:00",
        last_catalogue_ok="2026-07-18T07:11:00+02:00",
    )
    blind["alerts"]["stale:2026-07-18T07:11:00+02:00:0"] = "2026-07-19T07:11:00+02:00"
    runner.state.write_text(json.dumps(blind), encoding="utf-8")
    healthy = Snapshot(matched_shows=[{"slug": "dune-troisieme-partie", "title": "Dune"}])

    real_analyze = detect.analyze_pathe
    sent: list[str] = []

    def boom(*args, **kwargs):
        raise TypeError("analysis bug")

    monkeypatch.setattr(detect, "analyze_pathe", boom)
    monkeypatch.setattr(pathe, "make_client", object)
    monkeypatch.setattr(pathe, "fetch_snapshot", lambda client, cfg, **kw: healthy)
    monkeypatch.setattr(
        notify, "send_telegram", lambda cfg, text, **kw: sent.append(text) or True
    )
    argv = [
        "--config", str(runner.config), "--state", str(runner.state), "--mode", "check",
    ]

    assert cli.run(argv) == 1  # the bug is reported...

    # ...and nothing about the outage was banked, so the evidence the recovery
    # alert is built from survives it.
    stranded = json.loads(runner.state.read_text(encoding="utf-8"))
    assert stranded["error_alerted"] is True
    assert stranded["last_error"] == "HTTP 403"
    assert stranded["last_check_ok"] == "2026-07-18T07:11:00+02:00"
    assert "stale:2026-07-18T07:11:00+02:00:0" in stranded["alerts"]
    assert sent == []

    # The same state, once the bug is gone: recovery still reaches the phone.
    monkeypatch.setattr(detect, "analyze_pathe", real_analyze)
    assert cli.run(argv) == 0

    recovered = json.loads(runner.state.read_text(encoding="utf-8"))
    assert [t for t in sent if "Pathé watch is back" in t]
    assert recovered["error_alerted"] is False
    assert "last_error" not in recovered
    assert recovered["last_check_ok"] != "2026-07-18T07:11:00+02:00"
