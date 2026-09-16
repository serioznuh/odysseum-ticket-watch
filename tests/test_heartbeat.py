"""The quiet summary must describe current evidence for the user's watch."""

from copy import deepcopy
from dataclasses import replace
from datetime import datetime

import pytest

from watcher import detect, notify, state
from watcher.__main__ import build_heartbeat
from watcher.config import load_config

NOW = datetime(2026, 9, 14, 23, 32, tzinfo=detect.TZ_PARIS)
OLD_SALE = "2026-09-09T09:00:00+02:00"
FUTURE_SALE = "2026-10-01T09:00:00+02:00"
EVENT = "dune-troisieme-partie-projection-imax-70mm-55289"
PRIMARY = "dune-troisieme-partie-50828"
OTHER = "la-seance-70mm-dune-troisieme-partie-55319"


@pytest.fixture
def cfg():
    return replace(
        load_config("config.toml"), cinesa_enabled=False,
        pathe_target_format="imax70", pathe_target_dates=["2026-12-19", "2026-12-20"],
    )


@pytest.fixture
def snap():
    # Live shape on 14–15 September: a national timestamp on three listings,
    # with only the December 15 preview bookable at the watched cinema.
    return detect.Snapshot(
        matched_shows=[
            {"slug": PRIMARY, "title": "Dune", "salesOpeningDatetime": OLD_SALE},
            {"slug": EVENT, "title": "Dune IMAX 70mm", "salesOpeningDatetime": OLD_SALE},
            {"slug": OTHER, "title": "Dune La Séance 70mm", "salesOpeningDatetime": OLD_SALE},
        ],
        cinema_entries={
            PRIMARY: {"isBookable": True, "days": {"2026-12-15": {"bookable": True}}},
            EVENT: {"isBookable": True, "days": {"2026-12-15": {"bookable": True}}},
        },
        showtimes={PRIMARY: {"2026-12-15": [{"status": "available", "tags": ["4dx"]}]}},
    )


def heartbeat(cfg, snap, st=None, now=NOW):
    return build_heartbeat(cfg, snap, st or deepcopy(state.DEFAULT_STATE), now)


def test_heartbeat_does_not_reuse_old_national_sale_or_preview_availability(cfg, snap):
    st = deepcopy(state.DEFAULT_STATE)
    st.update(sales={s["slug"]: OLD_SALE for s in snap.matched_shows},
              sale_target=OLD_SALE, tickets_available=True, formats_seen={EVENT: ["imax70"]})
    before = deepcopy(st)
    finding = heartbeat(cfg, snap, st)
    text = "\n".join(finding.lines)

    assert "9 Sep" not in text
    assert "No upcoming national sale opening published" in text
    assert "IMAX 70 mm" in text
    assert "19 Dec: booking not confirmed." in text
    assert "20 Dec: booking not confirmed." in text
    assert "sessions bookable" not in text
    assert all(slug not in text for slug in (PRIMARY, EVENT, OTHER))
    assert finding.url == cfg.pathe_page_url
    assert finding.kind == "HEARTBEAT"
    assert finding.key == "heartbeat:2026-09-14"
    assert notify.is_silent(cfg, finding.kind)
    assert finding.lines[0] == f"{cfg.film_title} · {cfg.cinema_name}"
    assert st == before


def test_heartbeat_confirms_each_date_from_current_programme_even_if_already_alerted(cfg, snap):
    snap.cinema_entries[EVENT]["days"]["2026-12-19"] = {"bookable": True}
    st = deepcopy(state.DEFAULT_STATE)
    state.mark_sent(st, detect.pathe_date_key(cfg, "2026-12-19"), NOW)
    text = "\n".join(heartbeat(cfg, snap, st).lines)
    assert "19 Dec: booking confirmed." in text
    assert "20 Dec: booking not confirmed." in text


@pytest.mark.parametrize("status, expected", [
    ("available", "booking confirmed"), ("soldOut", "booking not confirmed"),
    (None, "booking not confirmed"),
])
def test_heartbeat_uses_session_status_and_format_on_regular_listing(cfg, snap, status, expected):
    snap.showtimes[PRIMARY]["2026-12-19"] = [{"status": status, "tags": ["imax", "70mm"]}]
    text = "\n".join(heartbeat(cfg, snap).lines)
    assert f"19 Dec: {expected}." in text


def test_other_format_on_wanted_date_does_not_confirm_imax70(cfg, snap):
    snap.cinema_entries[PRIMARY]["days"]["2026-12-19"] = {
        "bookable": True, "tags": ["imax", "70mm"],
    }
    snap.showtimes[PRIMARY]["2026-12-19"] = [{"status": "available", "tags": ["4dx"]}]
    assert "19 Dec: booking not confirmed." in heartbeat(cfg, snap).lines


def test_sale_summary_uses_live_selected_listings_and_deduplicates_instants(cfg, snap):
    snap.matched_shows[0]["salesOpeningDatetime"] = "2026-11-01T09:00:00+01:00"
    snap.matched_shows[1]["salesOpeningDatetime"] = FUTURE_SALE
    snap.matched_shows.append({"slug": "second-imax-70mm", "salesOpeningDatetime":
                               "2026-10-01T07:00:00+00:00"})
    st = deepcopy(state.DEFAULT_STATE)
    st["sales"] = {EVENT: OLD_SALE, "removed-imax-70mm": "2026-12-01T09:00:00+01:00"}
    text = "\n".join(heartbeat(cfg, snap, st).lines)

    assert text.count("Thu 1 Oct, 09:00") == 1
    assert "national sale opening" in text
    assert "1 Nov" not in text
    assert "1 Dec" not in text
    assert "9 Sep" not in text
    assert "19 Dec: booking not confirmed." in text


@pytest.mark.parametrize("value", [None, "", "invalid", OLD_SALE, NOW.isoformat()])
def test_missing_invalid_or_elapsed_live_sale_cannot_resurrect_stored_date(cfg, snap, value):
    snap.matched_shows[1]["salesOpeningDatetime"] = value
    st = deepcopy(state.DEFAULT_STATE)
    st["sales"][EVENT] = FUTURE_SALE
    text = "\n".join(heartbeat(cfg, snap, st).lines)
    assert "No upcoming national sale opening published" in text
    assert "1 Oct" not in text


def test_passed_wanted_dates_are_not_reported_as_waiting_for_booking(cfg, snap):
    text = "\n".join(heartbeat(cfg, snap, now=NOW.replace(month=12, day=20)).lines)
    assert "19 Dec: date has passed." in text
    assert "20 Dec: booking not confirmed." in text


def test_unrestricted_watch_keeps_live_sale_and_programme_availability(cfg, snap):
    cfg = replace(cfg, pathe_target_format="", pathe_target_dates=[])
    snap.matched_shows[0]["salesOpeningDatetime"] = FUTURE_SALE
    snap.showtimes = {}
    finding = heartbeat(cfg, snap)
    assert "Thu 1 Oct, 09:00" in "\n".join(finding.lines)
    assert "booking confirmed" in "\n".join(finding.lines)
    assert finding.url == cfg.film_page_url


def test_heartbeat_reports_unexpected_listing_failures_instead_of_full_health(cfg, snap):
    snap.listing_results = {
        PRIMARY: {"showtimes": detect.FetchResult.failed("HTTP 500")},
        OTHER: {"detail": detect.FetchResult.failed("timed out")},
        EVENT: {"showtimes": detect.FetchResult.refused("No movie allowed !")},
    }

    finding = heartbeat(cfg, snap)
    text = "\n".join(finding.lines)

    assert finding.title == "All quiet — Pathé partly degraded"
    assert "Pathé check degraded" in text
    assert f"showtimes: {PRIMARY}" in text
    assert f"detail: {OTHER}" in text
    assert EVENT not in text
    assert "All checks healthy." not in text


def test_format_only_watch_does_not_treat_other_or_sold_out_sessions_as_bookable(cfg, snap):
    cfg = replace(cfg, pathe_target_dates=[])
    snap.cinema_entries.pop(EVENT)
    snap.showtimes[PRIMARY]["2026-12-19"] = [{"status": "soldOut", "tags": ["imax", "70mm"]}]
    assert "booking not confirmed" in "\n".join(heartbeat(cfg, snap).lines)
    snap.showtimes[PRIMARY]["2026-12-19"][0]["status"] = "available"
    assert "booking confirmed" in "\n".join(heartbeat(cfg, snap).lines)


def test_format_only_heartbeat_does_not_confirm_booking_from_a_failed_call(cfg, snap):
    cfg = replace(cfg, pathe_target_dates=[])
    snap.showtimes = {}
    snap.listing_results = {
        EVENT: {"showtimes": detect.FetchResult.failed("HTTP 500")}
    }

    text = "\n".join(heartbeat(cfg, snap).lines)

    assert "booking not confirmed" in text
    assert "All checks healthy" not in text
