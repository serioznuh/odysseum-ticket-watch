"""Tests for notification loudness tiers."""

from __future__ import annotations

from watcher import notify


class Cfg:
    silent_kinds = ["HEARTBEAT", "NEWS_LEAD", "RECOVERED"]
    telegram_token = None
    telegram_chat_id = None


def test_silent_kinds_default_split():
    for kind in ("HEARTBEAT", "NEWS_LEAD", "RECOVERED"):
        assert notify.is_silent(Cfg, kind)
    for kind in ("SALE_DATE", "SALE_DATE_CHANGED", "TICKETS_AVAILABLE", "NEW_LISTING", "CINEMA_LISTED", "WATCHER_ERROR"):
        assert not notify.is_silent(Cfg, kind)


def test_silent_kinds_configurable():
    class QuietCfg(Cfg):
        silent_kinds = ["HEARTBEAT", "NEWS_LEAD", "RECOVERED", "NEW_LISTING"]

    assert notify.is_silent(QuietCfg, "NEW_LISTING")
    assert not notify.is_silent(QuietCfg, "SALE_DATE")


def test_dry_run_send_accepts_silent_flag():
    assert notify.send_telegram(Cfg, "hello", dry_run=True, silent=True) is True
    assert notify.send_telegram(Cfg, "hello", dry_run=True) is True
