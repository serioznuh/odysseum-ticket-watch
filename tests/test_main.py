"""Tests for CLI alert construction."""

from __future__ import annotations

import json
from datetime import datetime

from watcher import __main__ as cli
from watcher import notify, pathe
from watcher.detect import TZ_PARIS, Snapshot
from watcher.state import DEFAULT_STATE

NOW = datetime(2026, 7, 18, 14, 53, tzinfo=TZ_PARIS)


class Cfg:
    film_page_url = "https://www.pathe.fr/films/dune-troisieme-partie-50828"


def test_error_finding_explains_vpn_for_403():
    error = (
        "Pathé API request failed for https://www.pathe.fr/api/shows: "
        "Client error '403 Forbidden' for url 'https://www.pathe.fr/api/shows'\n"
        "For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/403"
    )

    finding = cli.build_error_finding(Cfg, 9, error, NOW)

    assert finding.kind == "WATCHER_ERROR"
    assert finding.lines == [
        "9 consecutive checks have failed.",
        "Last error: HTTP 403 Forbidden from https://www.pathe.fr/api/shows",
        "Pathé rejected the current network/IP. Disable any VPN or proxy, then wait for the next automatic retry.",
        "The watcher is currently BLIND. Local checks retry every 15 min; details: ~/.ticket-watch/logs/launchd.log",
    ]


def test_error_finding_keeps_generic_error_actionable_and_single_line():
    finding = cli.build_error_finding(Cfg, 3, "temporary DNS failure\nresolver unavailable", NOW)

    assert finding.lines == [
        "3 consecutive checks have failed.",
        "Last error: temporary DNS failure resolver unavailable",
        "Possible causes: Pathé API change or a local/network outage.",
        "The watcher is currently BLIND. Local checks retry every 15 min; details: ~/.ticket-watch/logs/launchd.log",
    ]


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
        self.monkeypatch.setattr(notify, "send_telegram", lambda *a, **kw: delivered)
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
    """One-shot baselines move only after NEW_LISTING/TICKETS_AVAILABLE delivery."""
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
    assert st["sales"] == {show["slug"]: sale}  # sale truth is deliberately ungated
    assert st["tickets_available"] is True      # current session truth is ungated

    st = runner.run(snap, delivered=True)
    assert st["shows_seen"] == [show["slug"]]
    assert st["formats_seen"] == {show["slug"]: ["imax70"]}
    assert set(st["alerts"]) == {
        f"new_show:{show['slug']}",
        f"tickets:{show['slug']}:imax70",
    }
