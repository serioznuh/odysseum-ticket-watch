"""Aggregate time budgets for the polling jobs (OTW-19).

The failure these exist for: Pathé retries every request three times against a
20 s timeout, over several endpoints, and the reminder ladder used to wait
behind all of it. No single timeout was ever exceeded, yet a bad connection
could spend a whole 15-minute warning window before the ladder was consulted.
"""

from __future__ import annotations

import base64
import json
import time
from typing import ClassVar

import httpx
import pytest

from watcher import cdp, cinesa, detect, jobs, news, pathe
from watcher import state as state_mod
from watcher.budget import MIN_REQUEST_TIMEOUT_SECONDS, Budget

PRIMARY = "dune-troisieme-partie-50828"
CINEMA = "cinema-pathe-odysseum"


def make_jwt(exp: float) -> str:
    """A token whose only interesting claim is `exp` (never verified)."""
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": int(exp)}).encode()
    ).decode().rstrip("=")
    return f"header.{payload}.signature"


class TokenCfg:
    """Only the fields get_token touches."""

    cinesa_token_url = "https://www.cinesa.es/"
    cinesa_chrome_path = "/nonexistent/Chrome"
    cinesa_chrome_profile = "/tmp/profile"
    cinesa_token_refresh_before_hours = 3.0

    def __init__(self, cache):
        self.cinesa_token_cache = str(cache)


class FakeClock:
    """A monotonic clock that only moves when the test says so — including
    when the code under test sleeps, which is most of a slow outage."""

    def __init__(self) -> None:
        self.t = 0.0

    def monotonic(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


def budget_for(clock: FakeClock, seconds: float, label: str = "job") -> Budget:
    return Budget(seconds, monotonic=clock.monotonic, sleep=clock.sleep, label=label)


# ------------------------------------------------------------- the primitive

def test_the_budget_covers_the_whole_job_not_one_call():
    clock = FakeClock()
    budget = budget_for(clock, 10.0)

    clock.t += 4.0
    assert budget.remaining() == pytest.approx(6.0)
    assert not budget.expired()

    clock.t += 6.0
    assert budget.expired()
    assert budget.remaining() == 0.0


def test_a_request_timeout_never_outlives_the_budget():
    clock = FakeClock()
    budget = budget_for(clock, 10.0)

    assert budget.timeout(20.0) == pytest.approx(10.0)  # capped by the budget
    assert budget.timeout(4.0) == pytest.approx(4.0)    # capped by the client
    clock.t += 9.8
    # Floored rather than handed a 0.2 s timeout: a request already in flight
    # should be allowed to land, not failed on arrival.
    assert budget.timeout(20.0) == pytest.approx(MIN_REQUEST_TIMEOUT_SECONDS)


def test_a_sleep_cannot_spend_more_than_is_left():
    clock = FakeClock()
    budget = budget_for(clock, 2.0)

    budget.sleep(30.0)

    assert clock.t == pytest.approx(2.0)
    assert budget.expired()


def test_a_child_budget_cannot_outlive_its_parent():
    clock = FakeClock()
    parent = budget_for(clock, 100.0, "polling")
    clock.t += 90.0

    generous = parent.child(60.0, "Cinesa check")
    assert generous.seconds == pytest.approx(10.0)  # what the parent has left

    modest = parent.child(3.0, "news check")
    assert modest.seconds == pytest.approx(3.0)


def test_the_polling_budgets_fit_inside_one_local_firing_interval():
    """The ladder's owner fires every LOCAL_FIRING_INTERVAL_MINUTES. If one
    pass could still be polling when the next firing is due, the budgets would
    not be protecting the thing they exist to protect."""
    interval = state_mod.LOCAL_FIRING_INTERVAL_MINUTES * 60

    assert jobs.POLLING_BUDGET_SECONDS < interval
    assert (
        jobs.PATHE_BUDGET_SECONDS
        + jobs.NEWS_BUDGET_SECONDS
        + jobs.CINESA_BUDGET_SECONDS
    ) <= jobs.POLLING_BUDGET_SECONDS


# ------------------------------------------------------------------- pathe

class Cfg:
    primary_slug = PRIMARY
    match_patterns: ClassVar[list[str]] = ["dune.{0,16}troisieme"]
    cinema_slug = CINEMA


def slow_client(clock: FakeClock, route, cost: float = 0.0) -> httpx.Client:
    """A client whose every response costs `cost` seconds of the fake clock."""

    def handler(request):
        clock.t += cost
        return route(request)

    return httpx.Client(transport=httpx.MockTransport(handler), base_url=pathe.BASE)


def test_an_exhausted_budget_stops_the_retry_sequence(monkeypatch):
    """Three attempts plus their backoff is the sequence that eats the window,
    so the budget has to be checked between attempts, not only per request."""
    monkeypatch.setattr(pathe.time, "sleep", lambda _s: None)
    calls = []

    def route(request):
        calls.append(request.url.path)
        return httpx.Response(500)

    unbudgeted = pathe._get_json_result(slow_client(FakeClock(), route), "/shows")
    assert not unbudgeted.healthy
    assert len(calls) == 3  # unchanged when no budget is given

    calls.clear()
    clock = FakeClock()
    result = pathe._get_json_result(
        slow_client(clock, route, cost=0.9),
        "/shows",
        budget=budget_for(clock, 2.0, "Pathé check"),
    )

    assert len(calls) == 1
    assert "time budget" in (result.diagnostic or "")


def test_the_budget_diagnostic_is_stable_across_endpoints():
    """It is stored in `last_error` for the cloud pass to read back. A URL in
    there would rewrite — and commit and push — state on every firing of an
    outage that flapped between endpoints."""
    clock = FakeClock()
    budget = budget_for(clock, 5.0, "Pathé check")
    clock.t += 5.0

    def route(_request):
        return httpx.Response(200, json={})

    first = pathe._get_json_result(slow_client(clock, route), "/shows", budget=budget)
    second = pathe._get_json_result(
        slow_client(clock, route), f"/show/{PRIMARY}/showtimes/{CINEMA}", budget=budget
    )

    assert first.diagnostic == second.diagnostic
    assert "http" not in (first.diagnostic or "").lower()


def test_a_listing_that_runs_out_of_budget_is_degradation_not_blindness():
    """The OTW-13 health model decides this, not the budget: catalogue data is
    still authoritative, so the run keeps it and reports partial degradation."""
    clock = FakeClock()

    def route(request):
        path = request.url.path
        if path.endswith("/api/shows"):
            return httpx.Response(200, json={"shows": [{"slug": PRIMARY}]})
        if path.endswith(f"/cinema/{CINEMA}/shows"):
            return httpx.Response(200, json={"shows": {}})
        clock.t += 60.0  # the listing call hangs
        return httpx.Response(500)

    snap = pathe.fetch_snapshot(
        slow_client(clock, route), Cfg, budget=budget_for(clock, 30.0, "Pathé check")
    )

    assert [s["slug"] for s in snap.matched_shows] == [PRIMARY]
    assert not snap.healthy
    assert snap.listing_results[PRIMARY]["showtimes"].health is (
        detect.FetchHealth.UNEXPECTED_FAILURE
    )
    assert detect.PARTIAL_PATHE_FAILURE in (snap.degradation_summary() or "")


def test_a_catalogue_call_that_runs_out_of_budget_still_fails_the_check():
    """The health signal must stay sharp: /shows not answering is a real
    outage, whatever stopped it."""
    clock = FakeClock()

    def route(_request):
        clock.t += 60.0
        return httpx.Response(500)

    with pytest.raises(RuntimeError, match="time budget"):
        pathe.fetch_snapshot(
            slow_client(clock, route), Cfg, budget=budget_for(clock, 10.0, "Pathé check")
        )


# -------------------------------------------------------------------- news

class NewsCfg:
    google_news_queries: ClassVar[list[str]] = ["https://feed/1", "https://feed/2"]
    extra_pages: ClassVar[list[str]] = ["https://page/1"]


def test_news_stops_fetching_once_the_budget_is_gone():
    """News is the least time-critical signal here. Whatever arrived before the
    budget ran out is still analysed; the rest waits for the next firing."""
    clock = FakeClock()
    fetched = []

    def handler(request):
        fetched.append(str(request.url))
        clock.t += 30.0
        return httpx.Response(
            200,
            text='<rss><channel><item><title>Dune</title></item></channel></rss>',
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))

    items = news.fetch_news_items(
        client, NewsCfg, budget=budget_for(clock, 25.0, "news check")
    )

    assert fetched == ["https://feed/1"]  # the second feed and the page are dropped
    assert [i["title"] for i in items] == ["Dune"]


# ---------------------------------------------- the Cinesa token step (round 1)

def test_mint_waits_shrink_to_what_the_budget_leaves():
    """Chrome's own worst case (30 s startup + 60 s page poll + cleanup) is
    longer than the whole Cinesa job budget, so an unbudgeted mint could
    overrun both it and the aggregate polling budget on its own."""
    assert cinesa._mint_waits(None) == (
        cinesa.CHROME_STARTUP_SECONDS,
        cinesa.CHROME_PAGE_WAIT_SECONDS,
    )

    clock = FakeClock()
    startup, page = cinesa._mint_waits(budget_for(clock, 60.0, "Cinesa check"))
    spent = startup + page + cinesa.CHROME_CLEANUP_RESERVE_SECONDS

    assert spent <= 60.0                     # cleanup is reserved, never shared
    assert startup < cinesa.CHROME_STARTUP_SECONDS
    assert page < cinesa.CHROME_PAGE_WAIT_SECONDS
    assert startup < page                    # same 1:2 shape as the defaults


def test_a_required_mint_refuses_to_launch_chrome_it_cannot_see_through(
    tmp_path, monkeypatch
):
    """Cache miss and forced renewal both skip the proactive-refresh gate, so
    they need their own: a launch that could not finish would only spend the
    rest of the run's allowance. It fails loudly instead — which is the
    documented response to Chrome not clearing the challenge, and keeps the
    real headed browser exactly as it is."""
    cfg = TokenCfg(tmp_path / "t.json")
    monkeypatch.setattr(
        cdp, "evaluate_on_page", lambda *a, **kw: pytest.fail("Chrome was launched")
    )
    clock = FakeClock()
    spent = budget_for(clock, 8.0, "Cinesa check")

    with pytest.raises(cdp.CDPError, match="Chrome not launched"):
        cinesa.get_token(cfg, budget=spent)  # no cached token at all

    cinesa.save_token(cfg.cinesa_token_cache, make_jwt(time.time() + 6 * 3600))
    with pytest.raises(cdp.CDPError, match="Chrome not launched"):
        cinesa.get_token(cfg, force=True, budget=spent)


def test_a_budgeted_mint_hands_chrome_the_shortened_waits(tmp_path, monkeypatch):
    seen = {}
    cfg = TokenCfg(tmp_path / "t.json")

    def fake_page(url, expression, **kwargs):
        seen.update(kwargs)
        return "token-value"

    monkeypatch.setattr(cdp, "evaluate_on_page", fake_page)
    clock = FakeClock()

    assert cinesa.get_token(cfg, budget=budget_for(clock, 60.0, "Cinesa check")) == (
        "token-value"
    )
    assert seen["startup_seconds"] < cinesa.CHROME_STARTUP_SECONDS
    assert seen["wait_seconds"] < cinesa.CHROME_PAGE_WAIT_SECONDS
