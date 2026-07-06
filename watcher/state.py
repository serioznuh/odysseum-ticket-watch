"""JSON state: alert dedup, known facts baseline, reminder ladder."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from . import detect

log = logging.getLogger(__name__)

DEFAULT_STATE: dict = {
    "version": 1,
    "alerts": {},          # dedup key -> ISO timestamp of when the alert was sent
    "sales": {},           # show slug -> salesOpeningDatetime ISO (as last seen)
    "formats_seen": {},    # show slug -> [format classes with sessions already alerted]
    "shows_seen": [],      # matched show slugs already known
    "reminders_sent": {},  # sale target ISO -> ["1440", "120", "15", "open"]
    "sale_target": None,   # earliest upcoming salesOpeningDatetime (ISO)
    "tickets_available": False,
    "failure_streak": 0,
    "error_alerted": False,
    "last_check_ok": None,
    "last_heartbeat": None,
}


def load_state(path: str | Path) -> dict:
    p = Path(path)
    if not p.exists():
        return json.loads(json.dumps(DEFAULT_STATE))
    try:
        loaded = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.error("state file unreadable (%s) — starting fresh, old file kept as .bak", e)
        try:
            p.replace(p.with_suffix(".json.bak"))
        except OSError:
            pass
        return json.loads(json.dumps(DEFAULT_STATE))
    merged = json.loads(json.dumps(DEFAULT_STATE))
    merged.update(loaded)
    return merged


def save_state(path: str | Path, state: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, p)


def already_sent(state: dict, key: str) -> bool:
    return key in state.get("alerts", {})


def mark_sent(state: dict, key: str, now: datetime) -> None:
    state.setdefault("alerts", {})[key] = now.isoformat()


def update_from_snapshot(state: dict, snap: detect.Snapshot, cfg: Any, now: datetime) -> None:
    """Record the snapshot as the new baseline (call after alerts were handled)."""
    for show in snap.matched_shows:
        slug = show.get("slug", "")
        if not slug:
            continue
        if slug not in state["shows_seen"]:
            state["shows_seen"].append(slug)
        if show.get("salesOpeningDatetime"):
            state["sales"][slug] = show["salesOpeningDatetime"]

        days = snap.showtimes.get(slug) or {}
        entry = snap.cinema_entries.get(slug) or {}
        if days:
            summary = detect.summarize_sessions(show, days)
            fmts = set(state["formats_seen"].get(slug, [])) | set(summary["counts"])
            state["formats_seen"][slug] = sorted(fmts)
            state["tickets_available"] = True
        elif entry.get("isBookable") or entry.get("bookable"):
            fmt = detect.classify_format(show.get("title"), slug)
            fmts = set(state["formats_seen"].get(slug, [])) | {fmt}
            state["formats_seen"][slug] = sorted(fmts)
            state["tickets_available"] = True

    future = []
    for iso in state["sales"].values():
        dt = detect.parse_iso(iso)
        if dt and detect.as_aware(dt) > now:
            future.append((detect.as_aware(dt), iso))
    if future:
        state["sale_target"] = min(future)[1]


def due_reminders(state: dict, offsets_minutes: list[int], now: datetime) -> list[dict]:
    """Return at most one due reminder: the most imminent unsent offset, or the
    'open' ping once the sale time has passed (within a 6h grace window).

    Reminders stop entirely once tickets are known to be available.
    """
    if state.get("tickets_available"):
        return []
    iso = state.get("sale_target")
    dt = detect.parse_iso(iso) if iso else None
    if dt is None:
        return []
    dt = detect.as_aware(dt)
    sent = set(state.get("reminders_sent", {}).get(iso, []))

    if now >= dt:
        if "open" not in sent and (now - dt) <= timedelta(hours=6):
            return [{"offset": "open", "target": iso}]
        return []

    if "open" in sent:
        return []
    active = [
        o
        for o in sorted(offsets_minutes)
        if now >= dt - timedelta(minutes=o) and str(o) not in sent
    ]
    if active:
        return [{"offset": min(active), "target": iso}]
    return []


def mark_reminder(state: dict, target_iso: str, offset: int | str, offsets_minutes: list[int]) -> None:
    """Mark `offset` sent; also skip any larger (earlier) offsets already in the past."""
    sent = set(state.setdefault("reminders_sent", {}).get(target_iso, []))
    if offset == "open":
        sent.add("open")
    else:
        sent.update(str(o) for o in offsets_minutes if o >= int(offset))
    state["reminders_sent"][target_iso] = sorted(sent)
