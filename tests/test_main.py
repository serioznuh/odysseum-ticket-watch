"""Tests for CLI alert construction."""

from __future__ import annotations

from datetime import datetime

from watcher import __main__ as cli
from watcher.detect import TZ_PARIS


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
