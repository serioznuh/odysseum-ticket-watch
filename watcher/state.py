"""JSON state: alert dedup, known facts baseline, reminder ladder."""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from . import detect

log = logging.getLogger(__name__)

# Consecutive IMAX-free checks needed before `imax_present` flips to False.
# Doubles as the ceiling for the streak counter: past the confirmation point a
# bigger number would mean nothing, but it would keep the state file changing
# on every firing (see update_from_cinesa).
IMAX_ABSENT_CONFIRM = 2

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
    # Cinesa (Diagonal Mar) half — namespaced so it never collides with Pathé.
    # No per-run timestamp lives here on purpose: the Cinesa check runs on
    # every 15-min firing, and a field that changed each time would make
    # local-check.sh commit and push ~96 times a day. Only real changes land.
    "cinesa": {
        "imax_present": None,       # None until the first successful check
        "imax_absent_streak": 0,    # consecutive non-empty checks without IMAX
        "horizon": None,            # last bookable business date (YYYY-MM-DD)
        "day_count": 0,
        "last_change": None,        # when horizon/IMAX last actually moved
        "failure_streak": 0,
        "error_alerted": False,
    },
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
    nested_defaults = {k: v for k, v in merged.items() if isinstance(v, dict) and k == "cinesa"}
    merged.update(loaded)
    # `update` is shallow: re-apply defaults for sub-keys a state file written
    # by an older version does not have yet, so new fields arrive initialised.
    for key, defaults in nested_defaults.items():
        section = dict(defaults)
        section.update(merged.get(key) or {})
        merged[key] = section
    migrate_stale_keys(merged)
    return merged


LEGACY_STALE_KEY = re.compile(
    r"stale:\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?"
)


def migrate_stale_keys(state: dict) -> None:
    """Adopt the periodic stale key without re-alerting.

    The supervision alert used to be keyed `stale:{last_check_ok}` and fired
    once per outage. It now repeats every 24h, so the key carries the period:
    `stale:{last_check_ok}:{period}`. Without this migration the already-sent
    old key would no longer match, and a machine that is currently blind would
    get one duplicate alert on upgrade.
    """
    alerts = state.get("alerts")
    if not isinstance(alerts, dict):
        return
    for key in list(alerts):
        # Match the legacy shape exactly. "Does the remainder parse as a
        # timestamp?" is NOT a valid test: fromisoformat accepts sub-minute UTC
        # offsets, so "...+02:00:37" parses happily and every two-digit period
        # (outage days 11-100) would be mistaken for an un-migrated key and
        # renamed after each send — re-alerting on every cloud pass.
        if not LEGACY_STALE_KEY.fullmatch(key):
            continue
        alerts.setdefault(f"{key}:0", alerts[key])
        alerts.pop(key, None)


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


def update_from_snapshot(
    state: dict,
    snap: detect.Snapshot,
    cfg: Any,
    now: datetime,
    advance_one_shot: bool = True,
) -> None:
    """Record the Pathé snapshot as the new baseline (call after alerting).

    `advance_one_shot=False` freezes the one-shot alert baselines (`shows_seen`
    and `formats_seen`) while still recording sales and current ticket
    availability: the caller passes it when a NEW_LISTING or TICKETS_AVAILABLE
    finding was generated but not delivered, so the next run raises it again.
    """
    if not advance_one_shot:
        log.info(
            "Pathé: one-shot alert not delivered — keeping the previous listing/format baselines"
        )
    for show in snap.matched_shows:
        slug = show.get("slug", "")
        if not slug:
            continue
        if advance_one_shot and slug not in state["shows_seen"]:
            state["shows_seen"].append(slug)
        if show.get("salesOpeningDatetime"):
            state["sales"][slug] = show["salesOpeningDatetime"]

        days = snap.showtimes.get(slug) or {}
        entry = snap.cinema_entries.get(slug) or {}
        if days:
            summary = detect.summarize_sessions(show, days)
            if advance_one_shot:
                fmts = set(state["formats_seen"].get(slug, [])) | set(summary["counts"])
                state["formats_seen"][slug] = sorted(fmts)
            state["tickets_available"] = True
        elif entry.get("isBookable") or entry.get("bookable"):
            if advance_one_shot:
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


def update_from_cinesa(
    state: dict,
    snap: detect.CinesaSnapshot,
    cfg: Any,
    now: datetime,
    advance_imax: bool = True,
) -> None:
    """Record the Cinesa snapshot as the new baseline (call after alerting).

    An empty snapshot never flips `imax_present`: a transient API hiccup would
    otherwise manufacture an "IMAX disappeared" alert on the next check.
    `imax_present` only goes False once absence is confirmed twice, which is
    the same threshold analyze_cinesa uses before it alerts.

    `advance_imax=False` freezes the IMAX baseline (`imax_present` and its
    streak) while still recording the horizon: the caller passes it when an
    IMAX gone/back alert was generated but not delivered, so the next run sees
    the same transition again instead of losing the alert forever.
    """
    cin = state.setdefault("cinesa", {})
    before = dict(cin)
    cin["failure_streak"] = 0
    cin["error_alerted"] = False
    if not snap.days:
        log.warning("cinesa: snapshot has no bookable days — not updating IMAX baseline")
        return

    cin["horizon"] = snap.days[-1]["date"]
    cin["day_count"] = len(snap.days)
    if not advance_imax:
        log.info("cinesa: IMAX alert not delivered — keeping the previous IMAX baseline")
    elif detect.imax_days(snap.days, cfg.cinesa_imax_attribute_id):
        cin["imax_present"] = True
        cin["imax_absent_streak"] = 0
    else:
        # Capped: only the confirmation threshold is ever read, and an
        # ever-growing counter would diff the state file on every firing.
        cin["imax_absent_streak"] = min(
            cin.get("imax_absent_streak", 0) + 1, IMAX_ABSENT_CONFIRM
        )
        if cin["imax_absent_streak"] >= IMAX_ABSENT_CONFIRM:
            cin["imax_present"] = False

    # Timestamp only a genuine change, so an unchanged schedule leaves the
    # state file byte-identical and the 15-min job has nothing to commit.
    if {k: v for k, v in cin.items() if k != "last_change"} != {
        k: v for k, v in before.items() if k != "last_change"
    }:
        cin["last_change"] = now.isoformat()


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


def adaptive_staleness_hours(state: dict, cfg: Any, now: datetime) -> float:
    """Allowed staleness of the last successful check before checking again.

    War-room curve around an announced sale opening: tightens as the target
    approaches, stays tight from 4 h before until 6 h after (sessions appear
    right at opening), then relaxes once tickets are known to be bookable.
    The launchd firing interval (15 min) is the effective floor.
    """
    target = detect.parse_iso(state.get("sale_target"))
    if target is not None:
        hours_to_target = (detect.as_aware(target) - now).total_seconds() / 3600
        if -6 <= hours_to_target <= 4:
            return cfg.cadence_opening_window_minutes / 60
        if 0 < hours_to_target <= 48:
            return cfg.cadence_final_48h_hours
        if 0 < hours_to_target <= 7 * 24:
            return cfg.cadence_within_week_hours
    if state.get("tickets_available"):
        return cfg.cadence_after_tickets_hours
    return cfg.cadence_baseline_hours


def is_check_fresh(state: dict, hours: float, now: datetime) -> bool:
    """True when the last successful Pathé check is newer than `hours`.

    Used by retry slots to exit instantly when the primary run already
    succeeded. False when there has never been a successful check.
    """
    if hours <= 0:
        return False
    last = detect.parse_iso(state.get("last_check_ok"))
    return last is not None and (now - detect.as_aware(last)) < timedelta(hours=hours)


def is_check_stale(state: dict, hours: int, now: datetime) -> bool:
    """True when the last successful Pathé check is older than `hours`.

    Never stale before the first successful check (setup phase).
    """
    if hours <= 0:
        return False
    last = detect.parse_iso(state.get("last_check_ok"))
    return last is not None and (now - detect.as_aware(last)) > timedelta(hours=hours)


def mark_reminder(state: dict, target_iso: str, offset: int | str, offsets_minutes: list[int]) -> None:
    """Mark `offset` sent; also skip any larger (earlier) offsets already in the past."""
    sent = set(state.setdefault("reminders_sent", {}).get(target_iso, []))
    if offset == "open":
        sent.add("open")
    else:
        sent.update(str(o) for o in offsets_minutes if o >= int(offset))
    state["reminders_sent"][target_iso] = sorted(sent)
