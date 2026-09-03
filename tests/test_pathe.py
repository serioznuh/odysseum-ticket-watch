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
# The Akamai block this project has actually observed (2026-09-02): also 403,
# also JSON — a bare `text/html` stand-in would let a real regression through.
BOT_BLOCK = httpx.Response(403, json={"error": "Error from IP 79.116.217.215"})
# A bot 403 carrying a bare JSON *string* must stay fatal too: matching on
# "any JSON string" would read a block as "no sessions" and go quiet.
BOT_BLOCK_STRING = httpx.Response(
    403, headers={"content-type": "application/json"}, text='"Error from IP 79.116.217.215"'
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
    # non-movie listing, the other is the IP being blocked. Only the body says
    # which, so the match is on the message — not on "403 with a JSON body".
    assert pathe.origin_refusal(BOT_BLOCK) is None
    assert pathe.origin_refusal(BOT_BLOCK_STRING) is None
    assert pathe.origin_refusal(httpx.Response(200, json={"shows": []})) is None
    assert pathe.origin_refusal(httpx.Response(403, text="Access Denied")) is None


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


@pytest.mark.parametrize("blocked", [BOT_BLOCK, BOT_BLOCK_STRING])
def test_a_real_bot_block_still_fails_loudly_after_retrying(blocked):
    """A block must never be mistaken for "no sessions" — including the
    string-bodied variant, which `allow_refusal` would otherwise swallow."""
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return blocked

    with pytest.raises(RuntimeError) as excinfo:
        pathe.get_json(client_for(handler), "/shows", allow_refusal=True)

    assert "refused by origin" not in str(excinfo.value)
    assert "403" in str(excinfo.value)
    assert len(calls) == 3


def test_a_failing_detail_call_does_not_blind_the_snapshot():
    """The other half of the 2026-09-02 bug: `/show/{slug}` was unguarded, so a
    listing visible only on the cinema programme could still abort everything."""
    programme = {"shows": {"dune-troisieme-partie-imax-70mm-9999": {"isBookable": True}}}

    def handler(request):
        path = request.url.path
        if path.endswith("/api/shows"):
            return httpx.Response(200, json={"shows": []})
        if path.endswith(f"/cinema/{CINEMA}/shows"):
            return httpx.Response(200, json=programme)
        return httpx.Response(500)

    snap = pathe.fetch_snapshot(client_for(handler), Cfg)

    # Falls back to the slug the programme already gave us, rather than dying.
    assert [s["slug"] for s in snap.matched_shows] == [
        "dune-troisieme-partie-imax-70mm-9999"
    ]
    assert snap.cinema_entries["dune-troisieme-partie-imax-70mm-9999"]["isBookable"]


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
