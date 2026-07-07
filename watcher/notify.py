"""Telegram notifications (HTML parse mode) with dry-run support."""

from __future__ import annotations

import html
import logging
import time
from typing import Any

import httpx

from . import detect
from .detect import Finding

log = logging.getLogger(__name__)

ICONS = {
    "SALE_DATE": "🎟️",
    "SALE_DATE_CHANGED": "🔁",
    "TICKETS_AVAILABLE": "🚨",
    "NEW_LISTING": "🆕",
    "CINEMA_LISTED": "📍",
    "NEWS_LEAD": "📰",
    "WATCHER_ERROR": "⚠️",
    "RECOVERED": "✅",
    "HEARTBEAT": "💤",
}

OFFSET_LABELS = {1440: "24 hours", 120: "2 hours", 15: "15 minutes"}

# Kinds delivered without sound/vibration by default; the phone buzzes for
# everything else (sale dates, tickets, reminders, failures). Reminders and
# the "open now" ping are always loud. Override via [alerts] silent_kinds.
DEFAULT_SILENT_KINDS = ["HEARTBEAT", "NEWS_LEAD", "RECOVERED"]


def is_silent(cfg: Any, kind: str) -> bool:
    return kind in getattr(cfg, "silent_kinds", DEFAULT_SILENT_KINDS)


def render_finding(f: Finding) -> str:
    icon = ICONS.get(f.kind, "ℹ️")
    body = "\n".join(html.escape(line) for line in f.lines)
    text = f"{icon} <b>{html.escape(f.title)}</b>\n{body}"
    if f.url:
        text += f"\n🔗 {html.escape(f.url)}"
    return text


def render_reminder(offset: int | str, target_iso: str, cfg: Any) -> str:
    when = detect.fmt_dt(detect.parse_iso(target_iso))
    where = f"{html.escape(cfg.cinema_name)}, {html.escape(cfg.cinema_city)}"
    film = html.escape(cfg.film_title)
    if offset == "open":
        return (
            "🟢 <b>Ticket sales should be OPEN NOW</b>\n"
            f"🎬 {film}\n"
            f"🏛️ {where}\n"
            f"🗓️ Opening was scheduled for: {html.escape(when)}\n"
            f"👉 Book: {html.escape(cfg.film_page_url)}\n"
            f"🏟️ Cinema page: {html.escape(cfg.cinema_page_url)}"
        )
    label = OFFSET_LABELS.get(int(offset), f"{offset} minutes")
    return (
        f"⏰ <b>Reminder: ticket sale opens in ~{label}</b>\n"
        f"🎬 {film}\n"
        f"🗓️ Opening: {html.escape(when)}\n"
        f"🏛️ {where}\n"
        "Be ready: sign in on pathe.fr, save a payment method.\n"
        f"👉 {html.escape(cfg.film_page_url)}"
    )


def send_telegram(cfg: Any, text: str, *, dry_run: bool, silent: bool = False) -> bool:
    """Send one message. Returns True on success (always True in dry-run)."""
    if dry_run:
        log.info(
            "[dry-run] would send Telegram message%s:\n%s\n%s\n%s",
            " (silent)" if silent else "",
            "-" * 60,
            text,
            "-" * 60,
        )
        return True
    if not (cfg.telegram_token and cfg.telegram_chat_id):
        log.error("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set — cannot send")
        return False

    url = f"https://api.telegram.org/bot{cfg.telegram_token}/sendMessage"
    payload = {
        "chat_id": cfg.telegram_chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "disable_notification": silent,
    }
    for attempt in range(2):
        try:
            r = httpx.post(url, json=payload, timeout=20.0)
            if r.status_code == 429:
                retry_after = int(r.json().get("parameters", {}).get("retry_after", 3))
                log.warning("telegram rate-limited, retrying in %ds", retry_after)
                time.sleep(retry_after)
                continue
            r.raise_for_status()
            if r.json().get("ok"):
                log.info("telegram message sent")
                return True
            log.error("telegram API returned not-ok: %s", r.text[:300])
            return False
        except httpx.HTTPError as e:
            # httpx exception messages include the URL — redact the token.
            msg = str(e).replace(cfg.telegram_token, "***")
            log.error("telegram send failed (attempt %d/2): %s", attempt + 1, msg)
            time.sleep(2)
    return False
