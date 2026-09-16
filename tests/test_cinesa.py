"""Unit tests for the Cinesa (Diagonal Mar) half: parsing, detection, state."""

from __future__ import annotations

import base64
import json
import logging
import time
from datetime import datetime, timedelta
from typing import ClassVar

import httpx
import pytest

from watcher import __main__ as main
from watcher import cdp, cinesa, detect, jobs, notify
from watcher import state as state_mod
from watcher.budget import Budget
from watcher.detect import CinesaSnapshot
from watcher.state import DEFAULT_STATE

PARIS = detect.TZ_PARIS
NOW = datetime(2026, 7, 29, 9, 0, tzinfo=PARIS)

IMAX = "0000000086"
VOSE = "0000000068"


class Cfg:
    cinesa_enabled = True
    cinesa_film_id = "HO00003228"
    cinesa_film_title = "La odisea"
    cinesa_page_url = "https://www.cinesa.es/peliculas/la-odisea/HO00003228/"
    cinesa_site_id = "032"
    cinesa_site_name = "Cinesa Diagonal Mar"
    cinesa_site_city = "Barcelona"
    cinesa_imax_attribute_id = IMAX
    cinesa_target_dates: ClassVar = ["2026-08-26", "2026-08-27"]


def fresh_state() -> dict:
    return json.loads(json.dumps(DEFAULT_STATE))


def days(*specs) -> list[dict]:
    """('2026-08-26', True) -> that date, with or without the IMAX attribute."""
    return [
        {"date": d, "attributes": sorted([VOSE] + ([IMAX] if imax else []))}
        for d, imax in specs
    ]


def kinds(findings) -> list[str]:
    return [f.kind for f in findings]


# --------------------------------------------------------------------- parsing

def ocapi_payload(*entries) -> dict:
    """Shape returned by /ocapi/v1/film-screening-dates (verified 2026-07)."""
    return {
        "filmScreeningDates": [
            {
                "businessDate": date,
                "filmScreenings": [
                    {
                        "filmId": film_id,
                        "sites": [{"siteId": site_id, "showtimeAttributeIds": attrs}],
                    }
                ],
            }
            for date, film_id, site_id, attrs in entries
        ]
    }


def test_parse_extracts_dates_and_attributes():
    payload = ocapi_payload(
        ("2026-07-29", "HO00003228", "032", [VOSE, IMAX]),
        ("2026-07-30", "HO00003228", "032", [VOSE]),
    )
    parsed = cinesa.parse_screening_dates(payload, Cfg)
    assert parsed == [
        {"date": "2026-07-29", "attributes": sorted([VOSE, IMAX])},
        {"date": "2026-07-30", "attributes": [VOSE]},
    ]


def test_parse_ignores_other_films_and_sites():
    payload = ocapi_payload(
        ("2026-07-29", "HO99999999", "032", [IMAX]),   # different film
        ("2026-07-30", "HO00003228", "012", [IMAX]),   # different Cinesa site
    )
    assert cinesa.parse_screening_dates(payload, Cfg) == []


def test_parse_sorts_by_date_and_tolerates_empty():
    payload = ocapi_payload(
        ("2026-08-02", "HO00003228", "032", [IMAX]),
        ("2026-07-29", "HO00003228", "032", [IMAX]),
    )
    assert [d["date"] for d in cinesa.parse_screening_dates(payload, Cfg)] == [
        "2026-07-29",
        "2026-08-02",
    ]
    assert cinesa.parse_screening_dates({}, Cfg) == []
    assert cinesa.parse_screening_dates(None, Cfg) == []


# ----------------------------------------------------------------------- token

def make_jwt(exp: float) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"exp": int(exp)}).encode()).decode()
    return "header." + payload.rstrip("=") + ".signature"


def test_token_expiry_reads_exp_and_survives_garbage():
    assert cinesa.token_expiry(make_jwt(1785438012)) == 1785438012
    assert cinesa.token_expiry("not-a-jwt") is None
    assert cinesa.token_expiry("") is None


def test_cached_token_used_while_fresh_and_dropped_near_expiry(tmp_path):
    path = tmp_path / "token.json"
    now = time.time()

    cinesa.save_token(path, make_jwt(now + 6 * 3600))
    assert cinesa.load_cached_token(path, now) is not None

    # Inside the refresh skew: treat as unusable so a run never starts with a
    # token that dies mid-flight.
    cinesa.save_token(path, make_jwt(now + 60))
    assert cinesa.load_cached_token(path, now) is None

    cinesa.save_token(path, make_jwt(now - 3600))
    assert cinesa.load_cached_token(path, now) is None


def test_missing_or_corrupt_token_cache_is_not_fatal(tmp_path):
    assert cinesa.load_cached_token(tmp_path / "nope.json", time.time()) is None
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert cinesa.load_cached_token(bad, time.time()) is None


def test_saved_token_is_not_world_readable(tmp_path):
    path = tmp_path / "token.json"
    cinesa.save_token(path, make_jwt(time.time() + 3600))
    assert path.stat().st_mode & 0o077 == 0


# ------------------------------------------------- proactive refresh (OTW-05)

class TokenCfg:
    """Only the fields get_token touches."""

    cinesa_token_url = "https://www.cinesa.es/"
    cinesa_chrome_path = "/nonexistent/Chrome"
    cinesa_chrome_profile = "/tmp/profile"
    cinesa_token_refresh_before_hours = 3.0

    def __init__(self, cache):
        self.cinesa_token_cache = str(cache)


class FetchCfg(Cfg):
    """Cinesa fields needed by fetch_snapshot, with a test cache path."""

    cinesa_api_base = "https://vwc.cinesa.es/WSVistaWebClient"
    cinesa_token_url = "https://www.cinesa.es/"
    cinesa_chrome_path = "/nonexistent/Chrome"
    cinesa_chrome_profile = "/tmp/profile"
    cinesa_token_refresh_before_hours = 3.0

    def __init__(self, cache):
        self.cinesa_token_cache = str(cache)


def mock_cinesa_api(monkeypatch, route):
    requests = []

    def handler(request):
        requests.append(request)
        result = route(request)
        if isinstance(result, int):
            return httpx.Response(result, request=request)
        return httpx.Response(200, json=result, request=request)

    monkeypatch.setattr(
        cinesa,
        "make_client",
        lambda: httpx.Client(transport=httpx.MockTransport(handler)),
    )
    return requests


def test_fresh_token_is_reused_without_launching_chrome(tmp_path, monkeypatch):
    cfg = TokenCfg(tmp_path / "t.json")
    token = make_jwt(time.time() + 10 * 3600)
    cinesa.save_token(cfg.cinesa_token_cache, token)
    monkeypatch.setattr(
        cinesa, "mint_token", lambda c, budget=None: pytest.fail("must not launch Chrome")
    )
    assert cinesa.get_token(cfg) == token


def test_refresh_starts_before_expiry_not_at_it(tmp_path, monkeypatch):
    """The whole point of OTW-05: start trying while hours of life remain."""
    cfg = TokenCfg(tmp_path / "t.json")
    old = make_jwt(time.time() + 2 * 3600)          # inside the 3 h window
    cinesa.save_token(cfg.cinesa_token_cache, old, last_attempt=0)
    new = make_jwt(time.time() + 12 * 3600)
    monkeypatch.setattr(cinesa, "mint_token", lambda c, budget=None: new)

    assert cinesa.get_token(cfg) == new
    assert cinesa.load_cached_token(cfg.cinesa_token_cache, time.time()) == new


def test_failed_refresh_falls_back_to_the_valid_cached_token(tmp_path, monkeypatch):
    """A locked Mac must not take the Cinesa half down while a good token is
    still in hand — this is the bug the proactive window exists to remove."""
    cfg = TokenCfg(tmp_path / "t.json")
    good = make_jwt(time.time() + 2 * 3600)
    cinesa.save_token(cfg.cinesa_token_cache, good, last_attempt=0)

    def boom(c, budget=None):
        raise cdp.CDPError("Chrome DevTools endpoint never came up")

    monkeypatch.setattr(cinesa, "mint_token", boom)
    assert cinesa.get_token(cfg) == good          # still works
    # The attempt is recorded, so the next firing backs off instead of
    # launching Chrome again 15 minutes later.
    assert cinesa.read_cache(cfg.cinesa_token_cache)["last_refresh_attempt"] > 0


def test_backoff_prevents_a_chrome_launch_every_firing(tmp_path, monkeypatch):
    cfg = TokenCfg(tmp_path / "t.json")
    good = make_jwt(time.time() + 2 * 3600)
    cinesa.save_token(cfg.cinesa_token_cache, good, last_attempt=time.time() - 60)
    monkeypatch.setattr(
        cinesa, "mint_token", lambda c, budget=None: pytest.fail("should have backed off")
    )
    assert cinesa.get_token(cfg) == good


def test_a_proactive_refresh_defers_when_the_budget_cannot_afford_chrome(
    tmp_path, monkeypatch
):
    """OTW-19: the token step is the one part that needs a GUI, so it is the
    one part that can hang. A refresh that no longer fits in the run's budget
    waits for a later firing rather than holding the ladder open — the token in
    hand is still valid, which is exactly what the early window is for."""
    cfg = TokenCfg(tmp_path / "t.json")
    good = make_jwt(time.time() + 2 * 3600)  # inside the 3 h refresh window
    cinesa.save_token(cfg.cinesa_token_cache, good, last_attempt=0)
    monkeypatch.setattr(
        cinesa, "mint_token", lambda c, budget=None: pytest.fail("must not launch Chrome")
    )
    spent = Budget(5.0, label="Cinesa check")

    assert cinesa.get_token(cfg, budget=spent) == good

    # With room for Chrome the refresh happens as it always did.
    new = make_jwt(time.time() + 12 * 3600)
    monkeypatch.setattr(cinesa, "mint_token", lambda c, budget=None: new)
    assert cinesa.get_token(cfg, budget=Budget(600.0, label="Cinesa check")) == new


def test_dead_token_still_fails_loudly(tmp_path, monkeypatch):
    """No fallback once the token is actually unusable — going quietly blind
    would be worse than a ⚠️."""
    cfg = TokenCfg(tmp_path / "t.json")
    cinesa.save_token(cfg.cinesa_token_cache, make_jwt(time.time() - 3600))

    def boom(c, budget=None):
        raise cdp.CDPError("Chrome not found")

    monkeypatch.setattr(cinesa, "mint_token", boom)
    with pytest.raises(cdp.CDPError):
        cinesa.get_token(cfg)


def test_rejected_token_never_falls_back_to_itself(tmp_path, monkeypatch):
    """force=True follows a 401: the cached token is known bad, so reusing it
    would just loop."""
    cfg = TokenCfg(tmp_path / "t.json")
    cinesa.save_token(cfg.cinesa_token_cache, make_jwt(time.time() + 6 * 3600))

    def boom(c, budget=None):
        raise cdp.CDPError("no")

    monkeypatch.setattr(cinesa, "mint_token", boom)
    with pytest.raises(cdp.CDPError):
        cinesa.get_token(cfg, force=True)


def test_403_mint_failure_cools_down_chrome_and_cached_token_recovers(
    tmp_path, monkeypatch
):
    """A VPN 403 retries the API every firing but mints at most once per hour."""
    clock = [1_800_000_000.0]
    monkeypatch.setattr(cinesa.time, "time", lambda: clock[0])
    cfg = FetchCfg(tmp_path / "token.json")
    old = make_jwt(clock[0] + 12 * 3600)
    cinesa.save_token(cfg.cinesa_token_cache, old, last_attempt=0)

    launches = []

    def blocked_mint(_cfg, budget=None):
        launches.append(clock[0])
        raise cdp.CDPError("Cloudflare challenge did not clear")

    monkeypatch.setattr(cinesa, "mint_token", blocked_mint)
    recovered = [False]

    def route(_request):
        if recovered[0]:
            return ocapi_payload(("2026-08-26", Cfg.cinesa_film_id, "032", [IMAX]))
        return 403

    mock_cinesa_api(monkeypatch, route)

    for _ in range(4):
        with pytest.raises(cinesa.TokenRejected) as rejected:
            cinesa.fetch_snapshot(cfg)
        assert rejected.value.status_code == 403
        clock[0] += 15 * 60

    assert len(launches) == 1
    cache = cinesa.read_cache(cfg.cinesa_token_cache)
    assert cache["token"] == old
    assert cache["mint_cooldown_until"] == pytest.approx(
        1_800_000_000.0 + cinesa.NETWORK_REJECTION_COOLDOWN_SECONDS
    )

    # Once the network is usable again, the original token succeeds without a
    # second headed Chrome launch, and the cache-only incident disappears.
    recovered[0] = True
    snap = cinesa.fetch_snapshot(cfg)
    assert snap.days == [{"date": "2026-08-26", "attributes": [IMAX]}]
    assert len(launches) == 1
    cache = cinesa.read_cache(cfg.cinesa_token_cache)
    assert cache["token"] == old
    assert "mint_cooldown_until" not in cache


def test_403_gets_one_new_mint_after_cooldown_window(tmp_path, monkeypatch):
    """A persistent block gets another mint chance only after the window."""
    clock = [1_800_000_000.0]
    monkeypatch.setattr(cinesa.time, "time", lambda: clock[0])
    cfg = FetchCfg(tmp_path / "token.json")
    cinesa.save_token(
        cfg.cinesa_token_cache,
        make_jwt(clock[0] + 12 * 3600),
        last_attempt=0,
    )
    launches = []

    def blocked_mint(_cfg, budget=None):
        launches.append(clock[0])
        raise cdp.CDPError("blocked")

    monkeypatch.setattr(cinesa, "mint_token", blocked_mint)
    mock_cinesa_api(monkeypatch, lambda _request: 403)

    with pytest.raises(cinesa.TokenRejected):
        cinesa.fetch_snapshot(cfg)
    clock[0] += 15 * 60
    with pytest.raises(cinesa.TokenRejected):
        cinesa.fetch_snapshot(cfg)
    assert len(launches) == 1

    clock[0] += cinesa.NETWORK_REJECTION_COOLDOWN_SECONDS
    with pytest.raises(cinesa.TokenRejected):
        cinesa.fetch_snapshot(cfg)
    assert len(launches) == 2


def test_401_forces_token_renewal(tmp_path, monkeypatch):
    cfg = FetchCfg(tmp_path / "token.json")
    old = make_jwt(time.time() + 6 * 3600)
    new = make_jwt(time.time() + 12 * 3600)
    cinesa.save_token(cfg.cinesa_token_cache, old)
    monkeypatch.setattr(cinesa, "mint_token", lambda _cfg, budget=None: new)

    requests = mock_cinesa_api(
        monkeypatch,
        lambda _request: 401 if len(requests) == 1 else ocapi_payload(
            ("2026-08-26", Cfg.cinesa_film_id, "032", [IMAX])
        ),
    )

    snap = cinesa.fetch_snapshot(cfg)
    assert snap.days == [{"date": "2026-08-26", "attributes": [IMAX]}]
    assert len(requests) == 2
    assert requests[0].headers["Authorization"] == f"Bearer {old}"
    assert requests[1].headers["Authorization"] == f"Bearer {new}"


# ------------------------------------------------------------------- detection

def test_target_date_with_imax_alerts_loudly():
    snap = CinesaSnapshot(days=days(("2026-08-25", True), ("2026-08-26", True)))
    findings = detect.analyze_cinesa(snap, fresh_state(), Cfg, NOW)
    assert kinds(findings) == ["CINESA_TARGET_DATE"]
    assert findings[0].key == "cinesa_target:032:HO00003228:2026-08-26"
    assert "2026-08-26" in "\n".join(findings[0].lines)


def test_target_date_without_imax_is_reported_separately():
    snap = CinesaSnapshot(days=days(("2026-08-26", False)))
    findings = detect.analyze_cinesa(snap, fresh_state(), Cfg, NOW)
    assert kinds(findings) == ["CINESA_TARGET_NO_IMAX"]
    # Must be a distinct key, so the loud alert can still fire for the same
    # date once IMAX shows up on it.
    assert findings[0].key == "cinesa_target_noimax:032:HO00003228:2026-08-26"


def test_both_target_dates_alert_independently():
    snap = CinesaSnapshot(days=days(("2026-08-26", True), ("2026-08-27", True)))
    findings = detect.analyze_cinesa(snap, fresh_state(), Cfg, NOW)
    assert kinds(findings) == ["CINESA_TARGET_DATE", "CINESA_TARGET_DATE"]
    assert {f.key.rsplit(":", 1)[1] for f in findings} == {"2026-08-26", "2026-08-27"}


def test_horizon_short_of_the_targets_is_silent():
    """The everyday case: the window rolls forward but has not reached them."""
    snap = CinesaSnapshot(days=days(("2026-08-24", True), ("2026-08-25", True)))
    assert detect.analyze_cinesa(snap, fresh_state(), Cfg, NOW) == []


def test_imax_gone_needs_two_consecutive_observations():
    st = fresh_state()
    st["cinesa"]["imax_present"] = True
    snap = CinesaSnapshot(days=days(("2026-08-01", False), ("2026-08-02", False)))

    # First absence: recorded, not alerted.
    assert detect.analyze_cinesa(snap, st, Cfg, NOW) == []
    state_mod.update_from_cinesa(st, snap, Cfg, NOW)
    assert st["cinesa"]["imax_absent_streak"] == 1
    assert st["cinesa"]["imax_present"] is True  # not flipped yet

    # Second absence: now it is real.
    assert kinds(detect.analyze_cinesa(snap, st, Cfg, NOW)) == ["CINESA_IMAX_GONE"]
    state_mod.update_from_cinesa(st, snap, Cfg, NOW)
    assert st["cinesa"]["imax_present"] is False


def test_empty_snapshot_never_fakes_an_imax_drop():
    """A transient API blip returning nothing must not manufacture an alert."""
    st = fresh_state()
    st["cinesa"]["imax_present"] = True
    st["cinesa"]["imax_absent_streak"] = 5
    empty = CinesaSnapshot(days=[])

    assert detect.analyze_cinesa(empty, st, Cfg, NOW) == []
    state_mod.update_from_cinesa(st, empty, Cfg, NOW)
    assert st["cinesa"]["imax_present"] is True
    assert st["cinesa"]["horizon"] is None


def test_imax_return_alerts_immediately():
    st = fresh_state()
    st["cinesa"]["imax_present"] = False
    snap = CinesaSnapshot(days=days(("2026-08-01", True)))
    assert kinds(detect.analyze_cinesa(snap, st, Cfg, NOW)) == ["CINESA_IMAX_BACK"]


def test_first_ever_check_sets_baseline_without_alerting():
    st = fresh_state()
    assert st["cinesa"]["imax_present"] is None
    snap = CinesaSnapshot(days=days(("2026-08-01", True)))
    assert detect.analyze_cinesa(snap, st, Cfg, NOW) == []
    state_mod.update_from_cinesa(st, snap, Cfg, NOW)
    assert st["cinesa"]["imax_present"] is True


# ----------------------------------------------------------------------- state

def test_unchanged_schedule_leaves_state_untouched():
    """Guards the 5-min cadence: no spurious diff means no commit/push churn."""
    st = fresh_state()
    snap = CinesaSnapshot(days=days(("2026-08-01", True), ("2026-08-25", True)))
    state_mod.update_from_cinesa(st, snap, Cfg, NOW)
    first = json.dumps(st, sort_keys=True)

    later = datetime(2026, 7, 29, 9, 15, tzinfo=PARIS)
    state_mod.update_from_cinesa(st, snap, Cfg, later)
    assert json.dumps(st, sort_keys=True) == first


def test_confirmed_absence_stops_changing_the_state_file():
    """The streak must not keep counting: it is only ever read as ">= 2", and a
    growing number would diff (and commit) state.json every 5 minutes for as
    long as IMAX stays away."""
    st = fresh_state()
    st["cinesa"]["imax_present"] = True
    snap = CinesaSnapshot(days=days(("2026-08-01", False), ("2026-08-02", False)))

    state_mod.update_from_cinesa(st, snap, Cfg, NOW)          # absence recorded
    state_mod.update_from_cinesa(st, snap, Cfg, NOW)          # absence confirmed
    confirmed = json.dumps(st, sort_keys=True)
    assert st["cinesa"]["imax_present"] is False

    for hour in (10, 11, 12):
        later = datetime(2026, 7, 29, hour, 0, tzinfo=PARIS)
        state_mod.update_from_cinesa(st, snap, Cfg, later)
        assert json.dumps(st, sort_keys=True) == confirmed


def test_undelivered_imax_alert_leaves_the_baseline_alone():
    """Advancing the baseline after a failed send would make the next run agree
    with reality and never re-raise the transition."""
    st = fresh_state()
    st["cinesa"]["imax_present"] = True
    st["cinesa"]["imax_absent_streak"] = 1
    snap = CinesaSnapshot(days=days(("2026-08-01", False), ("2026-08-02", False)))

    state_mod.update_from_cinesa(st, snap, Cfg, NOW, advance_imax=False)
    assert st["cinesa"]["imax_present"] is True
    assert st["cinesa"]["imax_absent_streak"] == 1
    assert st["cinesa"]["horizon"] == "2026-08-02"  # non-alerting fields still move

    # The alert is therefore still pending on the next check.
    assert kinds(detect.analyze_cinesa(snap, st, Cfg, NOW)) == ["CINESA_IMAX_GONE"]


def test_horizon_move_is_recorded_and_timestamped():
    st = fresh_state()
    state_mod.update_from_cinesa(
        st, CinesaSnapshot(days=days(("2026-08-25", True))), Cfg, NOW
    )
    assert st["cinesa"]["horizon"] == "2026-08-25"
    stamped = st["cinesa"]["last_change"]

    later = datetime(2026, 7, 30, 9, 0, tzinfo=PARIS)
    state_mod.update_from_cinesa(
        st, CinesaSnapshot(days=days(("2026-08-25", True), ("2026-08-26", True))), Cfg, later
    )
    assert st["cinesa"]["horizon"] == "2026-08-26"
    assert st["cinesa"]["day_count"] == 2
    assert st["cinesa"]["last_change"] != stamped


def test_state_upgrade_fills_in_new_cinesa_keys(tmp_path):
    """A state file written before this feature must load with defaults."""
    path = tmp_path / "state.json"
    old = fresh_state()
    old["version"] = 1
    old.pop("cinesa")
    path.write_text(json.dumps(old), encoding="utf-8")
    loaded = state_mod.load_state(path)
    assert loaded["cinesa"]["imax_present"] is None
    assert loaded["cinesa"]["imax_absent_streak"] == 0


def test_cinesa_failure_alert_buzzes_and_names_the_cause():
    """The safety net: a blind Cinesa half must reach the phone, loudly, and
    must not be mistaken for the Pathé half failing."""
    chrome = main.build_cinesa_error_finding(
        Cfg, "CDPError: Chrome DevTools endpoint never came up", "cinesa_error:2026-08-01"
    )
    assert chrome.kind == "WATCHER_ERROR"
    assert chrome.kind not in notify.DEFAULT_SILENT_KINDS  # i.e. it buzzes
    body = "\n".join(chrome.lines)
    # A locked screen is verified-fine (OTW-05), so the hint must not send the
    # user off to unlock the Mac; logged-in + awake is the real requirement.
    assert "Chrome" in body and "logged in and awake" in body
    assert "unlocked" not in body
    assert "Pathé half is unaffected" in body
    # Every alert names its target, so a second watch can never be confused
    # with this one.
    assert Cfg.cinesa_film_title in body and Cfg.cinesa_site_name in body

    generic = main.build_cinesa_error_finding(
        Cfg, "HTTP 500 from vwc.cinesa.es", "cinesa_error:2026-08-01"
    )
    assert "500" in "\n".join(generic.lines)

    blocked = main.build_cinesa_error_finding(
        Cfg,
        cinesa.TokenRejected(403, "https://vwc.cinesa.es/WSVistaWebClient"),
        "cinesa_error:2026-08-01",
    )
    blocked_body = "\n".join(blocked.lines)
    # The old copy told the user to disable a VPN. That advice was wrong
    # whenever they had none — an IP block is the fact; the remedy is not.
    assert "blocking your IP (403)" in blocked_body
    assert "VPN" not in blocked_body

    quoted = main.build_cinesa_error_finding(
        Cfg,
        "Client error '403 Forbidden' for url 'https://vwc.cinesa.es/WSVistaWebClient'",
        "cinesa_error:2026-08-01",
    )
    assert "blocking your IP (403)" in "\n".join(quoted.lines)


def test_cinesa_repeat_after_day_one_is_silent():
    """A long Cinesa outage keeps reporting, but only the first day buzzes."""
    first = main.build_cinesa_error_finding(Cfg, "boom", "cinesa_error:2026-08-01")
    later = main.build_cinesa_error_finding(
        Cfg, "boom", "cinesa_error:2026-08-04", day=4, since="Sat 1 Aug, 09:00"
    )

    assert first.kind == "WATCHER_ERROR"
    assert first.kind not in notify.DEFAULT_SILENT_KINDS
    assert later.kind == "WATCHER_STILL_BLIND"
    assert later.kind in notify.DEFAULT_SILENT_KINDS
    assert "day 4" in later.title
    assert "Sat 1 Aug, 09:00" in "\n".join(later.lines)


def test_cinesa_error_and_recovery_keys_allow_repeat_incidents():
    """Daily-granular keys: one alert per incident per day, but a later outage
    is not silenced forever by the dedup store."""
    day1 = main.build_cinesa_error_finding(Cfg, "boom", "cinesa_error:2026-08-01")
    day2 = main.build_cinesa_error_finding(Cfg, "boom", "cinesa_error:2026-08-02")
    assert day1.key != day2.key

    rec = main.build_cinesa_recovered_finding(Cfg, NOW)
    assert rec.kind == "RECOVERED"
    assert rec.key.startswith("cinesa_recovered:")
    assert Cfg.cinesa_film_title in "\n".join(rec.lines)


def test_state_upgrade_preserves_existing_cinesa_values(tmp_path):
    path = tmp_path / "state.json"
    old = fresh_state()
    old["version"] = 1
    old["cinesa"] = {"imax_present": True, "horizon": "2026-08-25"}
    path.write_text(json.dumps(old), encoding="utf-8")
    loaded = state_mod.load_state(path)
    assert loaded["cinesa"]["imax_present"] is True
    assert loaded["cinesa"]["horizon"] == "2026-08-25"
    assert loaded["cinesa"]["failure_streak"] == 0  # new key still initialised


# ------------------------------------------------------------ check-mode wiring

CONFIG_TOML = """
[film]
primary_slug = "dune-troisieme-partie"

[cinema]
slug = "montpellier-multiplexe-odysseum"

[news]
enabled = false

[cinesa]
enabled = true
film_id = "HO00003228"
film_title = "La odisea"
site_id = "032"
site_name = "Cinesa Diagonal Mar"
site_city = "Barcelona"
imax_attribute_id = "0000000086"
"""


class CheckRunner:
    """Drives `run --mode check` with the Pathé half asleep and Cinesa faked.

    Nothing here touches the network: Pathé is skipped via the freshness guard,
    `cinesa.fetch_snapshot` and `notify.send_telegram` are replaced.
    """

    def __init__(self, tmp_path, monkeypatch, initial_cinesa: dict):
        self.monkeypatch = monkeypatch
        self.config = tmp_path / "config.toml"
        self.config.write_text(CONFIG_TOML, encoding="utf-8")
        self.state = tmp_path / "state.json"
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "chat")
        start = fresh_state()
        start["cinesa"].update(initial_cinesa)
        start["last_check_ok"] = datetime.now(PARIS).isoformat()
        self.state.write_text(json.dumps(start), encoding="utf-8")

    def run(self, result, *, delivered: bool) -> dict:
        """One firing. `result` is a snapshot to return or an exception to raise."""
        def fake_fetch(cfg, **_budget):
            if isinstance(result, Exception):
                raise result
            return result

        self.monkeypatch.setattr(cinesa, "fetch_snapshot", fake_fetch)
        self.monkeypatch.setattr(
            notify, "send_telegram", lambda *a, **kw: delivered
        )
        assert main.run(
            ["--config", str(self.config), "--state", str(self.state),
             "--mode", "check", "--skip-if-checked-within", "6"]
        ) == 0
        return json.loads(self.state.read_text(encoding="utf-8"))


def test_failed_imax_alert_is_retried_on_the_next_run(tmp_path, monkeypatch):
    """A CINESA_IMAX_GONE that never reached the phone must not be swallowed by
    the baseline advancing behind it."""
    runner = CheckRunner(
        tmp_path, monkeypatch, {"imax_present": True, "imax_absent_streak": 1}
    )
    gone = CinesaSnapshot(days=days(("2026-08-01", False), ("2026-08-02", False)))

    st = runner.run(gone, delivered=False)
    assert st["cinesa"]["imax_present"] is True       # baseline held back
    assert st["cinesa"]["imax_absent_streak"] == 1
    assert st["cinesa"]["horizon"] == "2026-08-02"    # horizon still recorded

    st = runner.run(gone, delivered=True)
    assert st["cinesa"]["imax_present"] is False      # delivered — baseline moves


def test_cinesa_outage_stops_rewriting_state_once_capped(tmp_path, monkeypatch):
    """A dead Cinesa half must not commit and push a new failure count every
    15 minutes for the whole outage."""
    runner = CheckRunner(tmp_path, monkeypatch, {"imax_present": True})
    boom = RuntimeError("HTTP 500 from vwc.cinesa.es")

    for _ in range(3):  # failure_streak_threshold
        runner.run(boom, delivered=False)
    assert json.loads(runner.state.read_text())["cinesa"]["failure_streak"] == 3

    settled = runner.state.read_bytes()
    runner.run(boom, delivered=False)
    runner.run(boom, delivered=False)
    assert runner.state.read_bytes() == settled


# ------------------------- cleanup integrity must not be absorbed (OTW-19 r5)

def _leak(_cfg, _budget=None):
    raise cdp.ChromeLeakError(
        "read the value, but Chrome on watcher profile /p could not be confirmed"
        " terminated"
    )


def test_a_leaked_chrome_is_not_absorbed_by_the_cached_token_fallback(
    tmp_path, monkeypatch, caplog
):
    """The proactive-refresh fallback is right for a mint that simply failed —
    nothing was left behind, so the token in hand is still the best move. A
    cleanup-integrity failure is the opposite: continuing on the cached token
    passes the API check, clears the health state and exits 0 while the next
    mint is already doomed by the profile lock."""
    cfg = TokenCfg(tmp_path / "t.json")
    good = make_jwt(time.time() + 2 * 3600)  # inside the 3 h refresh window
    cinesa.save_token(cfg.cinesa_token_cache, good, last_attempt=0)
    monkeypatch.setattr(cinesa, "mint_token", _leak)

    with caplog.at_level(logging.ERROR, logger=cinesa.log.name), pytest.raises(
        cdp.ChromeLeakError
    ):
        cinesa.get_token(cfg)

    assert "could not confirm Chrome was terminated" in caplog.text
    # The attempt is still recorded, so a leak that takes a while to fix does
    # not mean a Chrome launch on every 5-min firing meanwhile.
    assert cinesa.read_cache(cfg.cinesa_token_cache)["last_refresh_attempt"] > 0

    # An ordinary mint failure still falls back exactly as before.
    def boom(_cfg, _budget=None):
        raise cdp.CDPError("Cloudflare challenge did not clear")

    monkeypatch.setattr(cinesa, "mint_token", boom)
    cinesa.save_token(cfg.cinesa_token_cache, good, last_attempt=0)
    assert cinesa.get_token(cfg) == good


def test_a_leaked_chrome_is_not_absorbed_by_the_403_fallback(tmp_path, monkeypatch):
    """The other broad catch, on the 403 path: it keeps the old token for a
    network rejection and must not swallow a leak either. No mint cooldown
    either — that mechanism arranges quiet retries, which is the opposite of
    what a leak needs."""
    cfg = FetchCfg(tmp_path / "t.json")
    cinesa.save_token(cfg.cinesa_token_cache, make_jwt(time.time() + 6 * 3600))
    monkeypatch.setattr(cinesa, "mint_token", _leak)
    mock_cinesa_api(monkeypatch, lambda _request: 403)

    with pytest.raises(cdp.ChromeLeakError):
        cinesa.fetch_snapshot(cfg)

    assert "mint_cooldown_until" not in cinesa.read_cache(cfg.cinesa_token_cache)


def test_a_forced_renewal_after_a_401_also_surfaces_a_leak(tmp_path, monkeypatch):
    """force=True skips the proactive fallback, so this reaches the caller via
    fetch_snapshot's 401 branch. Checked because it is a third route into the
    same mint."""
    cfg = FetchCfg(tmp_path / "t.json")
    cinesa.save_token(cfg.cinesa_token_cache, make_jwt(time.time() + 6 * 3600))
    monkeypatch.setattr(cinesa, "mint_token", _leak)
    mock_cinesa_api(monkeypatch, lambda _request: 401)

    with pytest.raises(cdp.ChromeLeakError):
        cinesa.fetch_snapshot(cfg)


def test_the_leak_alert_says_what_to_do_about_it():
    """"check Chrome is installed and the Mac is awake" would send the owner
    after the wrong thing entirely."""
    leak = main.build_cinesa_error_finding(
        Cfg, cdp.ChromeLeakError("could not be confirmed terminated"), "k"
    )

    assert "Cause: a leftover Chrome may still hold the watcher profile." in leak.lines
    assert "Needs you: quit Chrome" in leak.lines[-1]
    assert "check Chrome is installed" not in "\n".join(leak.lines)
    # Still the ordinary loud Cinesa error, keyed and shaped like the others.
    assert leak.kind == "WATCHER_ERROR"
    assert leak.kind not in notify.DEFAULT_SILENT_KINDS


def test_a_leaked_chrome_makes_the_run_exit_non_zero(tmp_path, monkeypatch):
    """It still feeds the capped streak and its alert like any Cinesa failure,
    but it must not pass for a healthy run in launchd's log or in Actions — a
    locked profile needs the owner, and nothing else will clear it."""
    runner = CheckRunner(tmp_path, monkeypatch, {"imax_present": True})

    def leaking_fetch(cfg, **_budget):
        raise cdp.ChromeLeakError("could not be confirmed terminated")

    monkeypatch.setattr(cinesa, "fetch_snapshot", leaking_fetch)
    monkeypatch.setattr(cdp, "profile_lock_status", lambda *_a, **_kw: True)
    monkeypatch.setattr(notify, "send_telegram", lambda *a, **kw: True)

    assert main.run(
        ["--config", str(runner.config), "--state", str(runner.state),
         "--mode", "check", "--skip-if-checked-within", "6"]
    ) == 1

    # The streak still advanced, so the existing threshold alert still arrives.
    assert json.loads(runner.state.read_text())["cinesa"]["failure_streak"] == 1

    # An ordinary Cinesa outage stays exit 0: it is a source being down, not a
    # local integrity problem, and the streak alert is the right channel.
    def plain_outage(cfg, **_budget):
        raise RuntimeError("HTTP 500 from vwc.cinesa.es")

    monkeypatch.setattr(cinesa, "fetch_snapshot", plain_outage)
    monkeypatch.setattr(cdp, "profile_lock_status", lambda *_a, **_kw: False)
    assert main.run(
        ["--config", str(runner.config), "--state", str(runner.state),
         "--mode", "check", "--skip-if-checked-within", "6"]
    ) == 0


def test_a_recurring_leak_is_not_starved_by_cached_token_runs(tmp_path, monkeypatch):
    """The starvation hole: after a leak, the mint backs off for 30 min, the
    cached token keeps working, and `update_from_cinesa` resets `failure_streak`
    to zero on every one of those successful runs. The ordinary streak therefore
    never reaches its threshold, so the alert never fired and most runs exited 0
    while Chrome still held the profile (round-6 review).

    `leak_since` is re-tested against the profile instead of counted, so it
    survives those successes and alerts on schedule.
    """
    runner = CheckRunner(tmp_path, monkeypatch, {"imax_present": True})
    sent: list[str] = []
    monkeypatch.setattr(
        notify, "send_telegram", lambda cfg, text, **kw: sent.append(text) or True
    )
    # Chrome is still holding the profile on every firing from here on.
    monkeypatch.setattr(cdp, "profile_lock_status", lambda *_a, **_kw: True)

    def leak(cfg, **_budget):
        raise cdp.ChromeLeakError("could not be confirmed terminated")

    def cached_ok(cfg, **_budget):
        return CinesaSnapshot(days=days(("2026-08-01", True), ("2026-08-02", True)))

    argv = ["--config", str(runner.config), "--state", str(runner.state),
            "--mode", "check", "--skip-if-checked-within", "6"]

    monkeypatch.setattr(cinesa, "fetch_snapshot", leak)
    assert main.run(argv) == 1
    st = json.loads(runner.state.read_text())
    assert st["cinesa"]["leak_since"], "the leak must be remembered"

    # Now the runs that used to bury it: the cached token works, the poll
    # succeeds, and the ordinary streak goes back to zero.
    monkeypatch.setattr(cinesa, "fetch_snapshot", cached_ok)
    assert main.run(argv) == 1  # still not a healthy run
    st = json.loads(runner.state.read_text())
    assert st["cinesa"]["failure_streak"] == 0  # reset, exactly as before
    assert st["cinesa"]["leak_since"]           # but the leak is still known

    # Once it has outlasted what the failure threshold means in wall-clock time,
    # it alerts on its own rather than waiting for a streak that never comes.
    stamped = datetime.fromisoformat(st["cinesa"]["leak_since"])
    st["cinesa"]["leak_since"] = (stamped - timedelta(minutes=20)).isoformat()
    runner.state.write_text(json.dumps(st), encoding="utf-8")
    sent.clear()
    assert main.run(argv) == 1

    assert len(sent) == 1
    assert "Cinesa token step needs you" in sent[0]
    assert "quit Chrome" in sent[0]


def test_a_freed_profile_clears_the_leak_and_the_run_is_healthy_again(
    tmp_path, monkeypatch
):
    """Only evidence clears it: the profile being demonstrably free."""
    runner = CheckRunner(tmp_path, monkeypatch, {"imax_present": True})
    st = json.loads(runner.state.read_text())
    st["cinesa"]["leak_since"] = (datetime.now(PARIS) - timedelta(hours=2)).isoformat()
    runner.state.write_text(json.dumps(st), encoding="utf-8")
    monkeypatch.setattr(notify, "send_telegram", lambda *a, **kw: True)
    monkeypatch.setattr(
        cinesa,
        "fetch_snapshot",
        lambda cfg, **_kw: CinesaSnapshot(days=days(("2026-08-01", True))),
    )
    argv = ["--config", str(runner.config), "--state", str(runner.state),
            "--mode", "check", "--skip-if-checked-within", "6"]

    # An unknown answer is not resolution: the leak stays, the run stays unhealthy.
    monkeypatch.setattr(cdp, "profile_lock_status", lambda *_a, **_kw: None)
    assert main.run(argv) == 1
    assert json.loads(runner.state.read_text())["cinesa"]["leak_since"]

    monkeypatch.setattr(cdp, "profile_lock_status", lambda *_a, **_kw: False)
    assert main.run(argv) == 0
    assert "leak_since" not in json.loads(runner.state.read_text())["cinesa"]


def test_an_existing_leak_is_reconciled_after_an_unrelated_failure(
    tmp_path, monkeypatch
):
    """Leak truth is independent of the exception raised by this firing.

    An ordinary Cinesa failure must keep a live prior leak visible and make the
    run non-zero. The same ordinary failure must clear the episode once the
    profile is demonstrably free, even if required token mints keep failing.
    """
    runner = CheckRunner(tmp_path, monkeypatch, {"imax_present": True})
    st = json.loads(runner.state.read_text())
    st["cinesa"]["leak_since"] = (
        datetime.now(PARIS) - timedelta(hours=2)
    ).isoformat()
    runner.state.write_text(json.dumps(st), encoding="utf-8")
    monkeypatch.setattr(
        cinesa,
        "fetch_snapshot",
        lambda cfg, **_kw: (_ for _ in ()).throw(RuntimeError("token mint failed")),
    )
    monkeypatch.setattr(notify, "send_telegram", lambda *a, **kw: False)
    argv = [
        "--config",
        str(runner.config),
        "--state",
        str(runner.state),
        "--mode",
        "check",
        "--skip-if-checked-within",
        "6",
    ]

    monkeypatch.setattr(cdp, "profile_lock_status", lambda *_a, **_kw: True)
    assert main.run(argv) == 1
    assert json.loads(runner.state.read_text())["cinesa"]["leak_since"]

    monkeypatch.setattr(cdp, "profile_lock_status", lambda *_a, **_kw: False)
    assert main.run(argv) == 0
    assert "leak_since" not in json.loads(runner.state.read_text())["cinesa"]


def test_first_leak_alert_is_loud_even_when_episode_crossed_midnight(monkeypatch):
    """Loudness follows a receipt for this episode, not calendar age."""

    class LeakCfg(Cfg):
        failure_streak_threshold = 3
        cinesa_chrome_profile = "/tmp/watcher-profile"

    since = datetime(2026, 9, 15, 23, 50, tzinfo=PARIS)
    first_now = datetime(2026, 9, 16, 0, 10, tzinfo=PARIS)
    state = fresh_state()
    state["cinesa"]["leak_since"] = since.isoformat()
    ctx = jobs.RunContext(cfg=LeakCfg, state=state, clock=lambda: first_now)
    monkeypatch.setattr(cdp, "profile_lock_status", lambda *_a, **_kw: True)

    first = jobs.CinesaOutcome()
    jobs.track_profile_leak(ctx, first, first_now, False, None)

    assert len(first.findings) == 1
    assert first.findings[0].kind == "WATCHER_ERROR"
    assert first.findings[0].title == "Cinesa token step needs you"

    # Only a recorded successful delivery turns a later daily reminder silent.
    state_mod.mark_sent(state, first.findings[0].key, first_now)
    repeat_now = first_now + timedelta(days=1)
    repeat = jobs.CinesaOutcome()
    jobs.track_profile_leak(ctx, repeat, repeat_now, False, None)

    assert len(repeat.findings) == 1
    assert repeat.findings[0].kind == "WATCHER_STILL_BLIND"
    assert "day 3" in repeat.findings[0].title
