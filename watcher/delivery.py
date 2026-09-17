"""Durable notification outbox and sanitised delivery receipts.

The outbox closes the ordinary crash window around Telegram delivery: work is
saved before the request, and a confirmed receipt plus its acknowledgement are
saved immediately afterwards.  It cannot make Telegram exactly-once.  A crash
or timeout after an attempt begins is quarantined as ``uncertain`` and is not
automatically replayed, because the message may already be on the phone.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from copy import deepcopy
from datetime import date, datetime, time, timedelta
from typing import Any

from . import notify
from . import state as state_mod
from .coalesce import Alert
from .detect import TZ_PARIS, Finding, as_aware, parse_iso

log = logging.getLogger("watcher.delivery")


class DeliveryPersistenceError(state_mod.StateError):
    """The durable transition around a notification could not be saved."""


def _persist(ctx: Any) -> None:
    if ctx.dry_run or not ctx.state_path:
        return
    writer = ctx.state_writer or state_mod.save_state
    writer(ctx.state_path, ctx.state)


def _delivery_id(keys: list[str], identity: str | None = None) -> str:
    encoded = json.dumps(
        [sorted(keys), identity], ensure_ascii=True, separators=(",", ":")
    )
    return "telegram:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]


def _owner() -> str:
    return "cloud" if os.environ.get("GITHUB_ACTIONS") else "local"


def _result(value: Any) -> notify.SendResult:
    # Test doubles and older integrations returned bool.  Keep accepting them
    # at this boundary while production uses the explicit three-way outcome.
    if isinstance(value, notify.SendResult):
        return value
    if value is True:
        return notify.SendResult("confirmed")
    if value is False:
        return notify.SendResult("failed")
    raise TypeError("notification sender returned an invalid delivery outcome")


def _apply_ack(state: dict, ack: dict, now: datetime) -> None:
    if ack["type"] == "alerts":
        # One in-memory mutation followed by one atomic state-file replace:
        # every member key of a merged message is acknowledged, or none is.
        for key in ack["keys"]:
            state_mod.mark_sent(state, key, now)
    elif ack["type"] == "reminder":
        state_mod.mark_reminder(
            state,
            ack["target"],
            ack["offset"],
            [int(offset) for offset in ack["offsets"]],
        )
    elif ack["type"] == "heartbeat":
        state["last_heartbeat"] = ack["at"]
    else:  # state validation should make this unreachable
        raise ValueError(f"unsupported acknowledgement {ack['type']!r}")


def _ack_satisfied(state: dict, ack: dict) -> bool:
    if ack["type"] == "alerts":
        return all(state_mod.already_sent(state, key) for key in ack["keys"])
    if ack["type"] == "reminder":
        return ack["offset"] in state.get("reminders_sent", {}).get(ack["target"], [])
    if ack["type"] == "heartbeat":
        return state.get("last_heartbeat") == ack["at"]
    return False


def _has_receipt(state: dict, delivery_id: str) -> bool:
    return any(
        receipt.get("delivery_id") == delivery_id
        for receipt in state.get("delivery_receipts", {}).values()
    )


def _expired(record: dict, now: datetime) -> bool:
    expiry = parse_iso(record.get("expires_at"))
    return expiry is not None and now >= as_aware(expiry)


def _retire_obsolete(ctx: Any, now: datetime, topics: list[str], keys: list[str]) -> None:
    changed = False
    wanted_topics = set(topics)
    for delivery_id, record in list(ctx.state.setdefault("outbox", {}).items()):
        same_work = set(record["keys"]) == set(keys)
        superseded = bool(wanted_topics & set(record.get("topics", []))) and not same_work
        if _expired(record, now) or superseded or _has_receipt(ctx.state, delivery_id):
            reason = "expired" if _expired(record, now) else "superseded"
            log.info("retired %s outbox work %s", reason, delivery_id)
            ctx.state["outbox"].pop(delivery_id, None)
            changed = True
    if changed:
        _persist(ctx)


def _attempt(ctx: Any, delivery_id: str, now: datetime) -> bool:
    record = ctx.state["outbox"][delivery_id]
    if record["status"] != "pending":
        return False

    claim_token = uuid.uuid4().hex
    record["status"] = "sending"
    record["claim"] = {"owner": _owner(), "token": claim_token, "at": now.isoformat()}
    _persist(ctx)

    try:
        outcome = _result(
            notify.send_telegram(
                ctx.cfg,
                record["text"],
                dry_run=ctx.dry_run,
                silent=record["silent"],
            )
        )
    except Exception:
        # The sender raised after the attempt marker was durable.  We cannot
        # prove whether Telegram accepted the request, so quarantine it.
        log.exception("notification attempt ended without a known outcome")
        record["status"] = "uncertain"
        try:
            _persist(ctx)
        except Exception:
            log.exception("could not persist uncertain delivery recovery")
        raise

    if outcome.status == "confirmed":
        before_confirmation = deepcopy(ctx.state)
        receipt = {
            "delivery_id": delivery_id,
            "keys": list(record["keys"]),
            "delivered_at": now.isoformat(),
        }
        if outcome.message_id is not None:
            receipt["telegram_message_id"] = outcome.message_id
        ctx.state.setdefault("delivery_receipts", {})[claim_token] = receipt
        _apply_ack(ctx.state, record["ack"], now)
        ctx.state["outbox"].pop(delivery_id, None)
        try:
            _persist(ctx)
        except Exception as exc:
            # Never let the coordinator's final save turn an unpersisted
            # confirmation into a trusted one.  The durable pre-send state is
            # ``sending``; both it and this in-memory state recover as uncertain.
            ctx.state.clear()
            ctx.state.update(before_confirmation)
            ctx.state["outbox"][delivery_id]["status"] = "uncertain"
            try:
                _persist(ctx)
            except Exception:
                log.exception("could not persist uncertain delivery recovery")
            raise DeliveryPersistenceError(
                "Telegram confirmed delivery but its receipt could not be persisted; "
                "the outbox item is uncertain and will not be replayed automatically"
            ) from exc
        return True

    if outcome.status == "failed":
        record["status"] = "pending"
        record.pop("claim", None)
        _persist(ctx)
        return False

    record["status"] = "uncertain"
    _persist(ctx)
    return False


def enqueue(
    ctx: Any,
    *,
    keys: list[str],
    kinds: list[str],
    text: str,
    silent: bool,
    ack: dict,
    now: datetime,
    topics: list[str] | None = None,
    expires_at: datetime | None = None,
    identity: str | None = None,
    force: bool = False,
) -> bool:
    """Persist one logical message, attempt it, then persist its receipt."""
    topics = topics or []
    _retire_obsolete(ctx, now, topics, keys)
    if expires_at is not None and now >= expires_at:
        log.info("discarded already-obsolete delivery work (keys=%s)", " ".join(keys))
        return False
    delivery_id = _delivery_id(keys, identity)

    if _has_receipt(ctx.state, delivery_id) or (not force and _ack_satisfied(ctx.state, ack)):
        ctx.state.setdefault("outbox", {}).pop(delivery_id, None)
        return False

    outbox = ctx.state.setdefault("outbox", {})
    existing = outbox.get(delivery_id)
    if existing is None:
        record = {
            "keys": list(keys),
            "kinds": list(kinds),
            "text": text,
            "silent": silent,
            "created_at": now.isoformat(),
            "topics": list(topics),
            "status": "pending",
            "ack": deepcopy(ack),
            "force": force,
        }
        if expires_at is not None:
            record["expires_at"] = expires_at.isoformat()
        outbox[delivery_id] = record
        try:
            _persist(ctx)
        except Exception:
            outbox.pop(delivery_id, None)
            raise
    elif existing["status"] == "sending":
        # A pre-existing sending marker can only be from an interrupted run or
        # another host's synchronized claim.  Its outcome is unknowable here.
        existing["status"] = "uncertain"
        _persist(ctx)
        return False
    elif existing["status"] == "uncertain":
        return False
    else:
        # Reminder countdown copy benefits from a fresh clock.  Other pending
        # messages retain their original bytes and expiry, so a long outage
        # settles instead of rewriting shared state every firing.
        if "REMINDER" in kinds and existing["text"] != text:
            existing["text"] = text
            _persist(ctx)

    return _attempt(ctx, delivery_id, now)


def _day_expiry(day: str) -> datetime | None:
    try:
        parsed = date.fromisoformat(day)
    except ValueError:
        return None
    return datetime.combine(parsed + timedelta(days=1), time.min, tzinfo=TZ_PARIS)


def _finding_policy(finding: Finding, now: datetime) -> tuple[list[str], datetime | None]:
    if finding.kind in {"SALE_DATE", "SALE_DATE_CHANGED"} and finding.sale_datetime:
        expiry = parse_iso(finding.sale_datetime)
        suffix = f":{finding.sale_datetime}"
        slug = finding.key[len("sale:"):-len(suffix)]
        return [f"sale:{slug}"], as_aware(expiry) if expiry else None
    if finding.kind in {"CINESA_TARGET_DATE", "CINESA_TARGET_NO_IMAX"}:
        parts = finding.key.split(":")
        if len(parts) >= 4:
            return [f"cinesa-availability:{':'.join(parts[-3:])}"], _day_expiry(parts[-1])
    if finding.kind == "PATHE_TARGET_DATE":
        day = finding.key.rsplit(":", 1)[-1]
        return [f"pathe-availability:{day}"], _day_expiry(day)
    if finding.kind in {"CINESA_IMAX_GONE", "CINESA_IMAX_BACK"}:
        return ["cinesa-imax-presence"], None
    return [], None


def deliver_alert(ctx: Any, alert: Alert, now: datetime, *, force: bool = False) -> bool:
    topics: list[str] = []
    expiries: list[datetime] = []
    members = alert.members or [alert.finding]
    for member in members:
        member_topics, expiry = _finding_policy(member, now)
        topics.extend(member_topics)
        if expiry is not None:
            expiries.append(expiry)
    return enqueue(
        ctx,
        keys=alert.keys,
        kinds=alert.kinds,
        text=notify.render_finding(alert.finding),
        silent=all(notify.is_silent(ctx.cfg, kind) for kind in alert.kinds),
        ack={"type": "alerts", "keys": list(alert.keys)},
        now=now,
        topics=list(dict.fromkeys(topics)),
        expires_at=min(expiries) if expiries else None,
        # Pathé's degraded -> blind escalation deliberately reuses its
        # historical Finding.key on the same day.  Content distinguishes those
        # two owed messages without changing that key format.
        identity=(
            notify.render_finding(alert.finding)
            if alert.finding.kind == "WATCHER_ERROR"
            else None
        ),
        force=force,
    )


def deliver_reminder(ctx: Any, reminder: dict, now: datetime) -> bool:
    target = reminder["target"]
    offset = str(reminder["offset"])
    target_dt = as_aware(parse_iso(target))
    if offset == "open":
        expiry = target_dt + timedelta(hours=6)
    else:
        offsets = sorted(ctx.cfg.reminder_offsets_minutes, reverse=True)
        index = offsets.index(int(offset))
        next_offset = offsets[index + 1] if index + 1 < len(offsets) else 0
        expiry = target_dt - timedelta(minutes=next_offset)
    return enqueue(
        ctx,
        keys=[f"reminder:{target}:{offset}"],
        kinds=["REMINDER"],
        text=notify.render_reminder(reminder["offset"], target, ctx.cfg, now),
        silent=False,
        ack={
            "type": "reminder",
            "target": target,
            "offset": offset,
            "offsets": [str(value) for value in ctx.cfg.reminder_offsets_minutes],
        },
        now=now,
        topics=[f"reminder:{target}"],
        expires_at=expiry,
    )


def deliver_heartbeat(ctx: Any, finding: Finding, now: datetime) -> bool:
    alert = Alert(finding, [finding.key], [finding.kind], [finding])
    return enqueue(
        ctx,
        keys=alert.keys,
        kinds=alert.kinds,
        text=notify.render_finding(finding),
        silent=notify.is_silent(ctx.cfg, finding.kind),
        ack={"type": "heartbeat", "at": now.isoformat()},
        now=now,
        topics=["heartbeat"],
        expires_at=now + timedelta(days=7),
    )


def recover(ctx: Any, now: datetime) -> bool:
    """Retire stale work, quarantine interrupted attempts, retry safe pending work."""
    sent = False
    changed = False
    for delivery_id, record in list(ctx.state.setdefault("outbox", {}).items()):
        if (
            _expired(record, now)
            or _has_receipt(ctx.state, delivery_id)
            or (not record["force"] and _ack_satisfied(ctx.state, record["ack"]))
        ):
            ctx.state["outbox"].pop(delivery_id, None)
            changed = True
        elif record["status"] == "sending":
            record["status"] = "uncertain"
            changed = True
    if changed:
        _persist(ctx)
    for delivery_id, record in list(ctx.state["outbox"].items()):
        if record["status"] == "pending":
            sent = _attempt(ctx, delivery_id, now) or sent
    return sent
