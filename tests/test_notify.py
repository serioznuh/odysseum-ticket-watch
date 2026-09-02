"""Tests for notification loudness tiers."""

from __future__ import annotations

from typing import ClassVar

from watcher import notify


class Cfg:
    silent_kinds: ClassVar = ["HEARTBEAT", "NEWS_LEAD", "RECOVERED"]
    telegram_token = None
    telegram_chat_id = None


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
