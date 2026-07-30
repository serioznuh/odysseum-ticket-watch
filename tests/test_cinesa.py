"""Unit tests for the Cinesa (Diagonal Mar) half: parsing, detection, state."""

from __future__ import annotations

import base64
import json
import time
from datetime import datetime
from typing import ClassVar

import pytest

from watcher import __main__ as main
from watcher import cdp, cinesa, detect, notify
from watcher import state as state_mod
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


def test_fresh_token_is_reused_without_launching_chrome(tmp_path, monkeypatch):
    cfg = TokenCfg(tmp_path / "t.json")
    token = make_jwt(time.time() + 10 * 3600)
    cinesa.save_token(cfg.cinesa_token_cache, token)
    monkeypatch.setattr(
        cinesa, "mint_token", lambda c: pytest.fail("must not launch Chrome")
    )
    assert cinesa.get_token(cfg) == token


def test_refresh_starts_before_expiry_not_at_it(tmp_path, monkeypatch):
    """The whole point of OTW-05: start trying while hours of life remain."""
    cfg = TokenCfg(tmp_path / "t.json")
    old = make_jwt(time.time() + 2 * 3600)          # inside the 3 h window
    cinesa.save_token(cfg.cinesa_token_cache, old, last_attempt=0)
    new = make_jwt(time.time() + 12 * 3600)
    monkeypatch.setattr(cinesa, "mint_token", lambda c: new)

    assert cinesa.get_token(cfg) == new
    assert cinesa.load_cached_token(cfg.cinesa_token_cache, time.time()) == new


def test_failed_refresh_falls_back_to_the_valid_cached_token(tmp_path, monkeypatch):
    """A locked Mac must not take the Cinesa half down while a good token is
    still in hand — this is the bug the proactive window exists to remove."""
    cfg = TokenCfg(tmp_path / "t.json")
    good = make_jwt(time.time() + 2 * 3600)
    cinesa.save_token(cfg.cinesa_token_cache, good, last_attempt=0)

    def boom(c):
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
        cinesa, "mint_token", lambda c: pytest.fail("should have backed off")
    )
    assert cinesa.get_token(cfg) == good


def test_dead_token_still_fails_loudly(tmp_path, monkeypatch):
    """No fallback once the token is actually unusable — going quietly blind
    would be worse than a ⚠️."""
    cfg = TokenCfg(tmp_path / "t.json")
    cinesa.save_token(cfg.cinesa_token_cache, make_jwt(time.time() - 3600))

    def boom(c):
        raise cdp.CDPError("Chrome not found")

    monkeypatch.setattr(cinesa, "mint_token", boom)
    with pytest.raises(cdp.CDPError):
        cinesa.get_token(cfg)


def test_rejected_token_never_falls_back_to_itself(tmp_path, monkeypatch):
    """force=True follows a 401: the cached token is known bad, so reusing it
    would just loop."""
    cfg = TokenCfg(tmp_path / "t.json")
    cinesa.save_token(cfg.cinesa_token_cache, make_jwt(time.time() + 6 * 3600))

    def boom(c):
        raise cdp.CDPError("no")

    monkeypatch.setattr(cinesa, "mint_token", boom)
    with pytest.raises(cdp.CDPError):
        cinesa.get_token(cfg, force=True)


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
    """Guards the 15-min cadence: no spurious diff means no commit/push churn."""
    st = fresh_state()
    snap = CinesaSnapshot(days=days(("2026-08-01", True), ("2026-08-25", True)))
    state_mod.update_from_cinesa(st, snap, Cfg, NOW)
    first = json.dumps(st, sort_keys=True)

    later = datetime(2026, 7, 29, 9, 15, tzinfo=PARIS)
    state_mod.update_from_cinesa(st, snap, Cfg, later)
    assert json.dumps(st, sort_keys=True) == first


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
    path.write_text(json.dumps({"version": 1, "alerts": {}}), encoding="utf-8")
    loaded = state_mod.load_state(path)
    assert loaded["cinesa"]["imax_present"] is None
    assert loaded["cinesa"]["imax_absent_streak"] == 0


def test_cinesa_failure_alert_buzzes_and_names_the_cause():
    """The safety net: a blind Cinesa half must reach the phone, loudly, and
    must not be mistaken for the Pathé half failing."""
    chrome = main.build_cinesa_error_finding(
        Cfg, 3, "CDPError: Chrome DevTools endpoint never came up", "cinesa_error:2026-08-01"
    )
    assert chrome.kind == "WATCHER_ERROR"
    assert chrome.kind not in notify.DEFAULT_SILENT_KINDS  # i.e. it buzzes
    body = "\n".join(chrome.lines)
    assert "Chrome" in body and "unlocked" in body
    assert "Pathé half is unaffected" in body

    generic = main.build_cinesa_error_finding(
        Cfg, 3, "HTTP 500 from vwc.cinesa.es", "cinesa_error:2026-08-01"
    )
    assert "Cinesa API change" in "\n".join(generic.lines)


def test_cinesa_error_and_recovery_keys_allow_repeat_incidents():
    """Daily-granular keys: one alert per incident per day, but a later outage
    is not silenced forever by the dedup store."""
    day1 = main.build_cinesa_error_finding(Cfg, 3, "boom", "cinesa_error:2026-08-01")
    day2 = main.build_cinesa_error_finding(Cfg, 3, "boom", "cinesa_error:2026-08-02")
    assert day1.key != day2.key

    rec = main.build_cinesa_recovered_finding(Cfg, NOW)
    assert rec.kind == "RECOVERED"
    assert rec.key.startswith("cinesa_recovered:")


def test_state_upgrade_preserves_existing_cinesa_values(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps({"version": 1, "cinesa": {"imax_present": True, "horizon": "2026-08-25"}}),
        encoding="utf-8",
    )
    loaded = state_mod.load_state(path)
    assert loaded["cinesa"]["imax_present"] is True
    assert loaded["cinesa"]["horizon"] == "2026-08-25"
    assert loaded["cinesa"]["failure_streak"] == 0  # new key still initialised
