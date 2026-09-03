"""Unit tests for the Pathé HTTP client.

The case these exist for: on 2026-09-02 a new IMAX 70 mm listing appeared whose
showtimes endpoint answers 403 `"No movie allowed !"` from Pathé's own nginx.
That aborted the whole snapshot, and the watcher stayed blind for 38 h across
the window in which the sale date for 9 Sep was published.
"""

from __future__ import annotations

from typing import ClassVar

import httpx
import pytest

from watcher import pathe

PRIMARY = "dune-troisieme-partie-50828"
EVENT = "dune-troisieme-partie-projection-imax-70mm-55289"
CINEMA = "cinema-pathe-odysseum"

# Pathé's origin refusal, byte for byte as observed 2026-09-03.
REFUSAL = httpx.Response(
    403, headers={"content-type": "application/json"}, text='"No movie allowed !"'
)
# What Akamai's bot manager returns instead — same status, nothing else alike.
BOT_BLOCK = httpx.Response(
    403, headers={"content-type": "text/html"}, text="<html>Access Denied</html>"
)


class Cfg:
    primary_slug = PRIMARY
    match_patterns: ClassVar[list[str]] = ['dune.{0,16}troisieme']
    cinema_slug = CINEMA


@pytest.fixture(autouse=True)
def _no_backoff_sleeps(monkeypatch):
    """Retry backoff and the politeness delay are real seconds otherwise."""
    monkeypatch.setattr(pathe.time, "sleep", lambda _s: None)


def client_for(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), base_url=pathe.BASE)


def test_origin_refusal_reads_the_message_out_of_a_json_403():
    assert pathe.origin_refusal(REFUSAL) == "No movie allowed !"


def test_origin_refusal_ignores_a_bot_block_and_healthy_responses():
    # The whole point is telling these two 403s apart: one is Pathé declining a
    # listing, the other is the IP being blocked. Only the body separates them.
    assert pathe.origin_refusal(BOT_BLOCK) is None
    assert pathe.origin_refusal(httpx.Response(200, json={"shows": []})) is None
    # A JSON *object* is some other API error; not our narrow case.
    assert pathe.origin_refusal(
        httpx.Response(403, json={"error": "forbidden"})
    ) is None


def test_allow_refusal_reads_a_refused_listing_as_no_data():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return REFUSAL

    got = pathe.get_json(
        client_for(handler), f"/show/{EVENT}/showtimes/{CINEMA}", allow_refusal=True
    )

    assert got is None
    # Deterministic: retrying a refusal three times only slows the check down.
    assert len(calls) == 1


def test_refusal_without_allow_refusal_is_marked_and_not_retried():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return REFUSAL

    with pytest.raises(RuntimeError) as excinfo:
        pathe.get_json(client_for(handler), "/shows")

    # The marker is what keeps the outage alert from blaming the user's IP.
    assert "refused by origin" in str(excinfo.value)
    assert "No movie allowed !" in str(excinfo.value)
    assert len(calls) == 1


def test_a_real_bot_block_still_fails_loudly_after_retrying():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return BOT_BLOCK

    with pytest.raises(RuntimeError) as excinfo:
        pathe.get_json(client_for(handler), "/shows")

    assert "refused by origin" not in str(excinfo.value)
    assert "403" in str(excinfo.value)
    assert len(calls) == 3


def test_one_refused_listing_does_not_blind_the_whole_snapshot():
    """The 2026-09-02 regression, end to end."""
    shows = {
        "shows": [
            {"slug": PRIMARY, "title": "Dune : Troisième partie"},
            {"slug": EVENT, "title": "Dune - Troisième partie : Projection IMAX 70mm"},
        ]
    }

    def handler(request):
        path = request.url.path
        if path.endswith("/api/shows"):
            return httpx.Response(200, json=shows)
        if path.endswith(f"/cinema/{CINEMA}/shows"):
            return httpx.Response(200, json={"shows": {PRIMARY: {"bookable": False}}})
        if EVENT in path and "/showtimes/" in path:
            return REFUSAL
        if "/showtimes/" in path:
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={"slug": path.rsplit("/", 1)[-1]})

    snap = pathe.fetch_snapshot(client_for(handler), Cfg)

    # Both listings survive to detection — including the refused one, which is
    # where the sale date and the new-listing alert actually live.
    assert sorted(s["slug"] for s in snap.matched_shows) == sorted([PRIMARY, EVENT])
    assert snap.showtimes == {}
    assert PRIMARY in snap.cinema_entries


def test_a_transport_failure_on_one_listing_is_also_survivable():
    """`allow_refusal` covers the known 403; nothing else may blind the run."""

    def handler(request):
        path = request.url.path
        if path.endswith("/api/shows"):
            return httpx.Response(200, json={"shows": [{"slug": PRIMARY}]})
        if path.endswith(f"/cinema/{CINEMA}/shows"):
            return httpx.Response(200, json={"shows": {}})
        if "/showtimes/" in path:
            return httpx.Response(500)
        return httpx.Response(200, json={"slug": PRIMARY})

    snap = pathe.fetch_snapshot(client_for(handler), Cfg)

    assert [s["slug"] for s in snap.matched_shows] == [PRIMARY]
    assert snap.showtimes == {}


def test_a_broken_catalogue_call_still_fails_the_check():
    """The health signal must stay sharp: /shows failing is a real outage."""

    def handler(request):
        return httpx.Response(500)

    with pytest.raises(RuntimeError):
        pathe.fetch_snapshot(client_for(handler), Cfg)
