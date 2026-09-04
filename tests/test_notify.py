"""Tests for notification loudness tiers and reminder wording."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import ClassVar

from watcher import notify
from watcher.detect import TZ_PARIS

TARGET = datetime(2026, 9, 9, 9, 0, tzinfo=TZ_PARIS)
TARGET_ISO = TARGET.isoformat()


class Cfg:
    silent_kinds: ClassVar = ["HEARTBEAT", "NEWS_LEAD", "RECOVERED"]
    telegram_token = None
    telegram_chat_id = None
    cinema_name = "Pathé Odysseum"
    cinema_city = "Montpellier"
    film_title = "Dune : Troisième partie"
    film_page_url = "https://www.pathe.fr/films/dune-troisieme-partie-50828"


def test_silent_kinds_default_split():
    for kind in ("HEARTBEAT", "NEWS_LEAD", "RECOVERED"):
        assert notify.is_silent(Cfg, kind)
    for kind in ("SALE_DATE", "SALE_DATE_CHANGED", "TICKETS_AVAILABLE", "NEW_LISTING", "CINEMA_LISTED", "WATCHER_ERROR"):
        assert not notify.is_silent(Cfg, kind)


def test_silent_kinds_configurable():
    class QuietCfg(Cfg):
        silent_kinds: ClassVar = ["HEARTBEAT", "NEWS_LEAD", "RECOVERED", "NEW_LISTING"]

    assert notify.is_silent(QuietCfg, "NEW_LISTING")
    assert not notify.is_silent(QuietCfg, "SALE_DATE")


def test_dry_run_send_accepts_silent_flag():
    assert notify.send_telegram(Cfg, "hello", dry_run=True, silent=True) is True
    assert notify.send_telegram(Cfg, "hello", dry_run=True) is True


def test_escaping_keeps_apostrophes_literal_but_neutralises_markup():
    """Telegram HTML needs & < > escaped and nothing else. Escaping quotes as
    well turned every apostrophe into &#x27; in the rendered message."""
    from watcher.detect import Finding

    f = Finding(
        kind="WATCHER_ERROR",
        key="k",
        confidence="high",
        title="The Mac hasn't checked in",
        lines=["a & b <script>alert(1)</script>", "the cinema's feed"],
        url=None,
    )

    out = notify.render_finding(f)

    assert "&#x27;" not in out
    assert "hasn't checked in" in out
    assert "the cinema's feed" in out
    assert "&amp;" in out and "&lt;script&gt;" in out
    assert "<script>" not in out


# ------------------------------------------------- reminder countdown (OTW-15)
# A reminder can be delivered long after its window opened — the cloud pass only
# steps in once the local half has missed one — so the message has to report the
# time actually left, not the offset it was scheduled at.

def test_late_two_hour_reminder_reports_the_real_time_left():
    out = notify.render_reminder(120, TARGET_ISO, Cfg, TARGET - timedelta(minutes=20))
    assert "Sale opens in 20 minutes" in out
    assert "2 hours" not in out
    assert "— 09:00" in out  # the absolute clock time is unchanged


def test_on_time_reminders_still_read_as_their_offset():
    two_h = notify.render_reminder(120, TARGET_ISO, Cfg, TARGET - timedelta(minutes=120))
    assert "Sale opens in 2 hours — 09:00" in two_h

    quarter = notify.render_reminder(15, TARGET_ISO, Cfg, TARGET - timedelta(minutes=15))
    assert "Sale opens in 15 minutes — 09:00" in quarter


def test_reminder_without_a_clock_falls_back_to_its_offset_label():
    """`now` is optional on render_reminder; the label is all there is then."""
    assert "Sale opens in 15 minutes" in notify.render_reminder(15, TARGET_ISO, Cfg)


def test_countdown_granularity():
    def left(**kw):
        return notify._countdown(TARGET_ISO, TARGET - timedelta(**kw), "fallback")

    assert left(minutes=1) == "1 minute"
    assert left(minutes=59) == "59 minutes"
    assert left(minutes=60) == "1 hour"
    # Below a day the leftover minutes are spelled out: the 2 h rung lands at
    # T-105…T-120 on the 15-min grid, and a bare "1 hour" understates it badly.
    assert left(minutes=105) == "1 hour 45 min"
    # Floored, never rounded up: the phrase must not promise time that is gone.
    assert left(minutes=119) == "1 hour 59 min"
    assert left(hours=23) == "23 hours"
    # Above a day the remainder is dropped — the absolute date is printed beside
    # the phrase and already carries that precision.
    assert left(hours=25) == "1 day"
    assert left(hours=47) == "1 day"
    assert left(days=6) == "6 days"
    # Past the opening a numeric offset must not read "0 minutes" or negative.
    assert left(minutes=-5) == "moments"
    assert left(minutes=0) == "moments"


def test_countdown_falls_back_when_the_target_is_unparseable():
    assert notify._countdown("not-a-date", TARGET, "2 hours") == "2 hours"
    assert notify._countdown(TARGET_ISO, None, "2 hours") == "2 hours"
