"""Telegram notifications (HTML parse mode) with dry-run support."""

from __future__ import annotations

import html
import logging
import time
from datetime import datetime
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
    "WATCHER_ERROR": "🔴",
    "WATCHER_STILL_BLIND": "🔴",
    "RECOVERED": "✅",
    "HEARTBEAT": "💤",
    "CINESA_TARGET_DATE": "🎫",
    "CINESA_TARGET_NO_IMAX": "🗓️",
    "CINESA_IMAX_GONE": "📉",
    "CINESA_IMAX_BACK": "📈",
}

OFFSET_LABELS = {1440: "24 hours", 120: "2 hours", 15: "15 minutes"}

# Kinds delivered without sound/vibration by default; the phone buzzes for
# everything else (sale dates, tickets, reminders, failures). Reminders and
# the "open now" ping are always loud. Override via [alerts] silent_kinds.
DEFAULT_SILENT_KINDS = [
    "HEARTBEAT",
    "NEWS_LEAD",
    "RECOVERED",
    "CINESA_TARGET_NO_IMAX",
    # Daily "still blind" repeats: the first outage alert buzzes, the drumbeat
    # after it must not — it is the same known problem, once a day.
    "WATCHER_STILL_BLIND",
]


def is_silent(cfg: Any, kind: str) -> bool:
    return kind in getattr(cfg, "silent_kinds", DEFAULT_SILENT_KINDS)


def esc(text: str) -> str:
    """Escape for Telegram HTML. Text content needs only & < > — escaping
    quotes as well (html.escape's default) turns every apostrophe into
    &#x27;, which is noise at best and visible at worst."""
    return html.escape(str(text), quote=False)


def render_finding(f: Finding) -> str:
    icon = ICONS.get(f.kind, "ℹ️")
    body = "\n".join(esc(line) for line in f.lines)
    text = f"{icon} <b>{esc(f.title)}</b>\n{body}"
    if f.url:
        text += f"\n🔗 {esc(f.url)}"
    return text


def _clock(target_iso: str) -> str:
    """Bare HH:MM (Paris). Used by the near offsets, where the day is obvious."""
    dt = detect.parse_iso(target_iso)
    if dt is None:
        return "unknown"
    return detect.as_aware(dt).astimezone(detect.TZ_PARIS).strftime("%H:%M")


def _when_phrase(target_iso: str, now: datetime | None = None) -> str:
    """'tomorrow, 10:00' when that is unambiguous, otherwise a dated form.

    The 24 h reminder is due any time between 24 h and 2 h before opening (a
    missed run pushes it later), so the day word has to be computed, not
    assumed.
    """
    dt = detect.parse_iso(target_iso)
    if dt is None:
        return "unknown"
    dt = detect.as_aware(dt).astimezone(detect.TZ_PARIS)
    clock = dt.strftime("%H:%M")
    if now is not None:
        today = detect.as_aware(now).astimezone(detect.TZ_PARIS).date()
        delta = (dt.date() - today).days
        if delta == 0:
            return f"today, {clock}"
        if delta == 1:
            return f"tomorrow, {clock}"
    return dt.strftime("%a %d %b, ") + clock


def render_reminder(
    offset: int | str, target_iso: str, cfg: Any, now: datetime | None = None
) -> str:
    """One reminder message. Each offset says something different: 24 h is for
    preparing, 2 h is a warning, 15 min means be at the keyboard."""
    where = f"{esc(cfg.cinema_name)}, {esc(cfg.cinema_city)}"
    film = esc(cfg.film_title)
    who = f"{film} · {where}"
    url = esc(cfg.film_page_url)

    if offset == "open":
        return (
            "🟢 <b>SALE IS OPEN — GO</b>\n"
            f"{who}\n"
            f"👉 {url}"
        )

    try:
        minutes = int(offset)
    except (TypeError, ValueError):
        minutes = None

    if minutes == 15:
        return (
            f"⏰ <b>Sale opens in 15 minutes — {esc(_clock(target_iso))}</b>\n"
            f"{who}\n"
            "Have pathe.fr open and be signed in.\n"
            f"👉 {url}"
        )
    if minutes == 120:
        return (
            f"⏰ <b>Sale opens in 2 hours — {esc(_clock(target_iso))}</b>\n"
            f"{who}\n"
            "Sign in on pathe.fr and save a card now.\n"
            f"👉 {url}"
        )
    if minutes == 1440:
        return (
            f"⏰ <b>Sale opens {esc(_when_phrase(target_iso, now))}</b>\n"
            f"{who}\n"
            "Prep now: sign in on pathe.fr and save a card.\n"
            f"👉 {url}"
        )

    label = OFFSET_LABELS.get(minutes, f"{offset} minutes")
    return (
        f"⏰ <b>Sale opens in ~{esc(label)} — {esc(_when_phrase(target_iso, now))}</b>\n"
        f"{who}\n"
        f"👉 {url}"
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
