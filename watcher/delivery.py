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

from . import coalesce, detect, notify
from . import state as state_mod
from .coalesce import Alert
from .detect import TZ_PARIS, Finding, as_aware, parse_iso

log = logging.getLogger("watcher.delivery")

_CONDITION_PREFIX = "condition:"


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


def _member_expired(member: dict, now: datetime) -> bool:
    expiry = parse_iso(member.get("expires_at"))
    return expiry is not None and now >= as_aware(expiry)


def _valid_open_ping(record: dict, now: datetime) -> bool:
    ack = record.get("ack", {})
    return (
        ack.get("type") == "reminder"
        and ack.get("offset") == "open"
        and not _expired(record, now)
    )


def _sale_target_slugs(state: dict, target: str) -> set[str]:
    slugs = {
        slug for slug, opening in state.get("sales", {}).items() if opening == target
    }
    suffix = f":{target}"
    for key in state.get("alerts", {}):
        if key.startswith("sale:") and key.endswith(suffix):
            slugs.add(key[len("sale:"):-len(suffix)])
    for record in state.get("outbox", {}).values():
        for topic in record.get("topics", []):
            parsed = _condition(topic)
            if (
                parsed is not None
                and parsed[0].startswith("pathe-sale:")
                and parsed[1] == target
            ):
                slugs.add(parsed[0].removeprefix("pathe-sale:"))
    return slugs


def _bookability_confirmed(state: dict, cfg: Any, domain: str) -> bool:
    slug = domain.removeprefix("pathe-bookability:")
    if any(key.startswith(f"tickets:{slug}:") for key in state.get("alerts", {})):
        return True
    formats = state.get("formats_seen", {}).get(slug, [])
    wanted = getattr(cfg, "pathe_target_format", "")
    return wanted in formats if wanted else bool(formats)


def _open_ping_bookable(state: dict, cfg: Any, record: dict, now: datetime) -> bool:
    return _valid_open_ping(record, now) and any(
        parsed is not None
        and parsed[0].startswith("pathe-bookability:")
        and _bookability_confirmed(state, cfg, parsed[0])
        for topic in record.get("topics", [])
        for parsed in [_condition(topic)]
    )


def _reminder_topics(ctx: Any, target: str) -> list[str]:
    return [
        _condition_topic("pathe-sale-target", target),
        *(
            _condition_topic(f"pathe-bookability:{slug}", "not-bookable")
            for slug in sorted(_sale_target_slugs(ctx.state, target))
        ),
    ]


def _condition_topic(domain: str, value: str) -> str:
    return f"{_CONDITION_PREFIX}{domain}={value}"


def _condition(topic: str) -> tuple[str, str] | None:
    if not topic.startswith(_CONDITION_PREFIX):
        return None
    domain, separator, value = topic[len(_CONDITION_PREFIX):].partition("=")
    return (domain, value) if domain and separator and value else None


def _alert_identity(kinds: list[str], topics: list[str]) -> str | None:
    """Return a stable identity when one historical key can name two events.

    Pathé deliberately reuses one daily WATCHER_ERROR key when a degraded
    watch escalates to blind. The condition distinguishes those owed messages;
    rendered outage duration does not, because it changes every run.
    """
    if "WATCHER_ERROR" not in kinds:
        return None
    conditions = sorted({topic for topic in topics if _condition(topic) is not None})
    return json.dumps(["WATCHER_ERROR", conditions], separators=(",", ":"))


def _member_delivery_id(member: dict) -> str:
    return _delivery_id(
        [member["key"]],
        _alert_identity([member["kind"]], member.get("topics", [])),
    )


def _pending_condition_domains(ctx: Any, prefix: str) -> set[str]:
    domains: set[str] = set()
    for record in ctx.state.setdefault("outbox", {}).values():
        for topic in record.get("topics", []):
            parsed = _condition(topic)
            if parsed is not None and parsed[0].startswith(prefix):
                domains.add(parsed[0])
    return domains


def _has_condition_domain(record: dict, domains: set[str]) -> bool:
    return any(
        parsed is not None and parsed[0] in domains
        for topic in record.get("topics", [])
        for parsed in [_condition(topic)]
    )


def _listing_metadata_authoritative(
    snapshot: Any, slug: str, show: dict | None
) -> bool:
    return snapshot.listing_metadata_authoritative(slug, show)


def _pathe_dates_authoritative(snapshot: Any) -> bool:
    """Whether this snapshot can disprove target-format date availability."""
    shows = [show for show in snapshot.matched_shows if show.get("slug")]
    if not shows or len(shows) != len(snapshot.matched_shows):
        return False

    # Any failed per-listing fetch can hide a listing or session carrying the
    # target format, including sessions on a regular (non-selected) listing.
    if any(
        not result.healthy
        for endpoints in snapshot.listing_results.values()
        for result in endpoints.values()
    ):
        return False

    return all(
        _listing_metadata_authoritative(snapshot, show["slug"], show)
        and snapshot.endpoint_healthy(show["slug"], "showtimes")
        for show in shows
    )


def _standalone_record(record: dict, member: dict) -> dict:
    status = record["status"]
    if status == "sending":
        status = "uncertain"
    result = {
        "keys": [member["key"]],
        "kinds": [member["kind"]],
        "text": member["text"],
        "silent": member["silent"],
        "created_at": record["created_at"],
        "topics": list(member["topics"]),
        "status": status,
        "ack": {"type": "alerts", "keys": [member["key"]]},
        "force": record["force"],
        "members": [deepcopy(member)],
    }
    if "expires_at" in member:
        result["expires_at"] = member["expires_at"]
    if status == "uncertain" and "claim" in record:
        result["claim"] = deepcopy(record["claim"])
    return result


def _retain_members(
    ctx: Any,
    delivery_id: str,
    record: dict,
    survivors: list[dict],
    reason: str,
) -> bool:
    """Remove selected merged members without discarding unrelated work."""
    members = record.get("members")
    if members is None or len(survivors) == len(members):
        return False

    ctx.state["outbox"].pop(delivery_id, None)
    replacement_ids = []
    for member in survivors:
        replacement_id = _member_delivery_id(member)
        if _has_receipt(ctx.state, replacement_id) or (
            not record["force"]
            and state_mod.already_sent(ctx.state, member["key"])
        ):
            continue
        replacement_ids.append(replacement_id)
        replacement = _standalone_record(record, member)
        existing = ctx.state["outbox"].get(replacement_id)
        if existing is None:
            ctx.state["outbox"][replacement_id] = replacement
        elif replacement["status"] == "uncertain":
            existing["status"] = "uncertain"
            if "claim" in replacement:
                existing["claim"] = replacement["claim"]

    if delivery_id in ctx.delivery_attempts:
        ctx.delivery_attempts.update(replacement_ids)
    log.info(
        "retired %s member(s) from outbox work %s; retained %d",
        reason,
        delivery_id,
        len(survivors),
    )
    return True


def _member_superseded(member: dict, incoming: list[dict]) -> bool:
    incoming_keys = {candidate["key"] for candidate in incoming}
    incoming_topics = {
        topic for candidate in incoming for topic in candidate.get("topics", [])
    }
    incoming_conditions = {
        parsed[0]: parsed[1]
        for topic in incoming_topics
        for parsed in [_condition(topic)]
        if parsed is not None
    }
    if member["key"] in incoming_keys:
        return True
    for topic in member.get("topics", []):
        if topic in incoming_topics:
            return True
        parsed = _condition(topic)
        if (
            parsed is not None
            and parsed[0] in incoming_conditions
            and incoming_conditions[parsed[0]] != parsed[1]
        ):
            return True
    return False


def _retire_obsolete(
    ctx: Any,
    now: datetime,
    topics: list[str],
    keys: list[str],
    members: list[dict] | None = None,
) -> None:
    changed = False
    wanted_topics = set(topics)
    wanted_conditions = {
        parsed[0]: parsed[1]
        for topic in topics
        for parsed in [_condition(topic)]
        if parsed is not None
    }
    for delivery_id, record in list(ctx.state.setdefault("outbox", {}).items()):
        if _has_receipt(ctx.state, delivery_id):
            ctx.state["outbox"].pop(delivery_id, None)
            changed = True
            continue

        record_members = record.get("members")
        if record_members is not None and members is not None:
            same_work = (
                set(record["keys"]) == set(keys)
                and set(record.get("topics", [])) == set(topics)
            )
            survivors = [
                member
                for member in record_members
                if not _member_expired(member, now)
                and not (
                    not record["force"]
                    and state_mod.already_sent(ctx.state, member["key"])
                )
                and not (
                    not same_work and _member_superseded(member, members)
                )
            ]
            reason = "expired, acknowledged or superseded"
            changed = (
                _retain_members(ctx, delivery_id, record, survivors, reason)
                or changed
            )
            continue

        same_work = set(record["keys"]) == set(keys)
        record_topics = set(record.get("topics", []))
        conflicting_condition = any(
            parsed[0] in wanted_conditions
            and wanted_conditions[parsed[0]] != parsed[1]
            for topic in record_topics
            for parsed in [_condition(topic)]
            if parsed is not None
        )
        # A passed opening's valid open ping and the next future opening's
        # ladder are separate obligations. A different sale-target topic must
        # not retire the ping; expiry or authoritative reconciliation will.
        superseded = (
            bool(wanted_topics & record_topics) and not same_work
        ) or (conflicting_condition and not _valid_open_ping(record, now))
        if _expired(record, now) or superseded:
            reason = "expired" if _expired(record, now) else "superseded"
            log.info("retired %s outbox work %s", reason, delivery_id)
            ctx.state["outbox"].pop(delivery_id, None)
            changed = True
    if changed:
        _persist(ctx)


def _attempt(ctx: Any, delivery_id: str, now: datetime) -> bool:
    record = ctx.state["outbox"][delivery_id]
    if record["status"] != "pending" or delivery_id in ctx.delivery_attempts:
        return False
    ctx.delivery_attempts.add(delivery_id)

    claim_token = uuid.uuid4().hex
    before_claim = deepcopy(record)
    record["status"] = "sending"
    record["claim"] = {"owner": _owner(), "token": claim_token, "at": now.isoformat()}
    try:
        _persist(ctx)
    except Exception:
        # Telegram has not been called. Restore the known-unsent state so
        # same-run recovery cannot reinterpret a failed claim save as an
        # interrupted send and quarantine it permanently.
        record.clear()
        record.update(before_claim)
        raise

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
    members: list[dict] | None = None,
    identity: str | None = None,
    force: bool = False,
    retry_existing: bool = True,
) -> bool:
    """Persist one logical message, attempt it, then persist its receipt."""
    topics = topics or []
    _retire_obsolete(ctx, now, topics, keys, members)
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
        if members is not None:
            record["members"] = deepcopy(members)
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

    if existing is not None and not retry_existing:
        return False

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
        return [
            f"sale:{slug}",
            _condition_topic(f"pathe-sale:{slug}", finding.sale_datetime),
        ], as_aware(expiry) if expiry else None
    if finding.kind == "TICKETS_AVAILABLE":
        _prefix, slug, formats = finding.key.split(":", 2)
        return [
            _condition_topic(f"pathe-bookability:{slug}", "bookable"),
            *(
                _condition_topic(f"pathe-tickets:{slug}", fmt)
                for fmt in formats.split(",")
            ),
        ], None
    if finding.kind == "NEW_LISTING":
        slug = finding.key.removeprefix("new_show:")
        return [
            _condition_topic(f"pathe-listing:{slug}", "present")
        ], now + timedelta(days=7)
    if finding.kind == "CINEMA_LISTED":
        slug = finding.key.removeprefix("cinema_listed:")
        return [
            _condition_topic(f"pathe-bookability:{slug}", "not-bookable")
        ], now + timedelta(days=7)
    if finding.kind in {"CINESA_TARGET_DATE", "CINESA_TARGET_NO_IMAX"}:
        parts = finding.key.split(":")
        if len(parts) >= 4:
            domain = f"cinesa-availability:{':'.join(parts[-3:])}"
            value = "imax" if finding.kind == "CINESA_TARGET_DATE" else "no-imax"
            return [_condition_topic(domain, value)], _day_expiry(parts[-1])
    if finding.kind == "PATHE_TARGET_DATE":
        day = finding.key.rsplit(":", 1)[-1]
        return [
            _condition_topic(f"pathe-availability:{day}", "open")
        ], _day_expiry(day)
    if finding.kind in {"CINESA_IMAX_GONE", "CINESA_IMAX_BACK"}:
        value = "absent" if finding.kind == "CINESA_IMAX_GONE" else "present"
        return [_condition_topic("cinesa-imax-presence", value)], None
    if finding.key.startswith("error:"):
        level = "degraded" if "DEGRADED" in finding.title else "blind"
        return [_condition_topic("pathe-health", level)], None
    if finding.key.startswith("stale:"):
        return [_condition_topic("pathe-health", "blind")], now + timedelta(days=1)
    if finding.key.startswith("cloud_stale:"):
        return [_condition_topic("cloud-health", "stale")], now + timedelta(days=1)
    if finding.key.startswith("recovered:"):
        return [_condition_topic("pathe-health", "healthy")], None
    if finding.key.startswith("cinesa_error:"):
        return [_condition_topic("cinesa-health", "blind")], None
    if finding.key.startswith("cinesa_recovered:"):
        return [_condition_topic("cinesa-health", "healthy")], None
    if finding.key.startswith("cinesa_leak:"):
        return [
            _condition_topic("cinesa-token", "stuck")
        ], now + timedelta(days=1)
    if finding.key.startswith("state_sync_error:"):
        return [], now + timedelta(days=1)
    if finding.kind == "NEWS_LEAD":
        return [], now + timedelta(days=7)
    if finding.kind == "HEARTBEAT":
        return [
            _condition_topic("cloud-health", "healthy")
        ], now + timedelta(days=7)
    return [], None


def deliver_alert(ctx: Any, alert: Alert, now: datetime, *, force: bool = False) -> bool:
    members = alert.members or [alert.finding]
    policies = [(*_finding_policy(member, now), member) for member in members]
    live_members = [
        member
        for member_topics, expiry, member in policies
        if expiry is None or now < expiry
    ]
    if not live_members:
        return False
    if len(live_members) != len(members):
        alert = coalesce.merge(live_members, ctx.cfg)[0]
        members = live_members

    member_records: list[dict] = []
    topics: list[str] = []
    for member in members:
        member_topics, expiry = _finding_policy(member, now)
        topics.extend(member_topics)
        member_record = {
            "key": member.key,
            "kind": member.kind,
            "text": notify.render_finding(member),
            "silent": notify.is_silent(ctx.cfg, member.kind),
            "topics": list(member_topics),
        }
        if expiry is not None:
            member_record["expires_at"] = expiry.isoformat()
        member_records.append(member_record)
    return enqueue(
        ctx,
        keys=alert.keys,
        kinds=alert.kinds,
        text=notify.render_finding(alert.finding),
        silent=all(notify.is_silent(ctx.cfg, kind) for kind in alert.kinds),
        ack={"type": "alerts", "keys": list(alert.keys)},
        now=now,
        topics=list(dict.fromkeys(topics)),
        members=member_records,
        # Pathé's degraded -> blind escalation deliberately reuses its
        # historical Finding.key on the same day. The condition distinguishes
        # those events while remaining stable as rendered durations change.
        identity=_alert_identity(alert.kinds, topics),
        force=force,
    )


def deliver_reminder(
    ctx: Any,
    reminder: dict,
    now: datetime,
    *,
    retry_existing: bool = True,
) -> bool:
    target = reminder["target"]
    offset = str(reminder["offset"])
    target_dt = as_aware(parse_iso(target))
    topics = _reminder_topics(ctx, target)
    bookability_domains = {
        parsed[0]
        for topic in topics
        for parsed in [_condition(topic)]
        if parsed is not None and parsed[0].startswith("pathe-bookability:")
    }
    if offset == "open" and any(
        _bookability_confirmed(ctx.state, ctx.cfg, domain)
        for domain in bookability_domains
    ):
        reconcile_observations(
            ctx,
            observed_domains=bookability_domains,
            active_conditions={
                _condition_topic(domain, "bookable")
                for domain in bookability_domains
            },
        )
        return False
    if offset == "open":
        expiry = target_dt + state_mod.OPEN_PING_VALIDITY
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
        topics=topics,
        expires_at=expiry,
        retry_existing=retry_existing,
    )


def deliver_heartbeat(ctx: Any, finding: Finding, now: datetime) -> bool:
    alert = Alert(finding, [finding.key], [finding.kind], [finding])
    topics, expires_at = _finding_policy(finding, now)
    return enqueue(
        ctx,
        keys=alert.keys,
        kinds=alert.kinds,
        text=notify.render_finding(finding),
        silent=notify.is_silent(ctx.cfg, finding.kind),
        ack={"type": "heartbeat", "at": now.isoformat()},
        now=now,
        topics=topics or ["heartbeat"],
        expires_at=expires_at,
    )


def reconcile_observations(
    ctx: Any,
    *,
    observed_domains: set[str],
    active_conditions: set[str],
) -> None:
    """Retire queued messages contradicted by authoritative observations.

    Each merged member is evaluated independently. Domains absent from
    ``observed_domains`` are unknown, never false.
    """
    changed = False
    for delivery_id, record in list(ctx.state.setdefault("outbox", {}).items()):
        members = record.get("members")
        if members is not None:
            survivors = [
                member
                for member in members
                if not any(
                    parsed[0] in observed_domains
                    and topic not in active_conditions
                    for topic in member.get("topics", [])
                    for parsed in [_condition(topic)]
                    if parsed is not None
                )
            ]
            changed = (
                _retain_members(
                    ctx, delivery_id, record, survivors, "contradicted"
                )
                or changed
            )
            continue
        contradicted = any(
            parsed[0] in observed_domains and topic not in active_conditions
            for topic in record.get("topics", [])
            for parsed in [_condition(topic)]
            if parsed is not None
        )
        if contradicted:
            log.info("retired contradicted outbox work %s", delivery_id)
            ctx.state["outbox"].pop(delivery_id, None)
            changed = True
    if changed:
        _persist(ctx)


def reconcile_cloud_health(ctx: Any, health: str) -> None:
    """Publish an authoritative Actions result before outbox recovery.

    Older pending heartbeats predate their cloud-health topic. Bind it here so
    upgrading during an outage cannot replay an unqualified "healthy" message.
    """
    if health not in {"healthy", "stale"}:
        raise ValueError(f"unsupported cloud health {health!r}")
    healthy_topic = _condition_topic("cloud-health", "healthy")
    stale_topic = _condition_topic("cloud-health", "stale")
    changed = False
    for record in ctx.state.setdefault("outbox", {}).values():
        if (
            record.get("ack", {}).get("type") == "heartbeat"
            and healthy_topic not in record.get("topics", [])
        ):
            record.setdefault("topics", []).append(healthy_topic)
            changed = True
        if any(key.startswith("cloud_stale:") for key in record.get("keys", [])):
            if stale_topic not in record.get("topics", []):
                record.setdefault("topics", []).append(stale_topic)
                changed = True
            for member in record.get("members", []):
                if (
                    member.get("key", "").startswith("cloud_stale:")
                    and stale_topic not in member.get("topics", [])
                ):
                    member.setdefault("topics", []).append(stale_topic)
                    changed = True
    if changed:
        _persist(ctx)
    reconcile_observations(
        ctx,
        observed_domains={"cloud-health"},
        active_conditions={_condition_topic("cloud-health", health)},
    )


def reconcile_source_observations(
    ctx: Any,
    *,
    now: datetime,
    pathe_snapshot: Any = None,
    pathe_health: str | None = None,
    cinesa_snapshot: Any = None,
    cinesa_health: str | None = None,
    cinesa_token_stuck: bool | None = None,
) -> None:
    """Publish the latest authoritative source facts to the outbox."""
    observed: set[str] = set()
    active: set[str] = set()
    topics_bound = False

    for domain, health in (
        ("pathe-health", pathe_health),
        ("cinesa-health", cinesa_health),
    ):
        if health is not None:
            observed.add(domain)
            active.add(_condition_topic(domain, health))

    if pathe_snapshot is not None:
        shows = {
            show.get("slug", ""): show
            for show in pathe_snapshot.matched_shows
            if show.get("slug")
        }
        for record in ctx.state.setdefault("outbox", {}).values():
            ack = record.get("ack", {})
            if ack.get("type") != "reminder" or ack.get("offset") != "open":
                continue
            target = ack.get("target")
            for slug, show in shows.items():
                if (
                    show.get("salesOpeningDatetime") == target
                    and detect.selected_listing(show, ctx.cfg)
                    and _listing_metadata_authoritative(
                        pathe_snapshot, slug, show
                    )
                ):
                    topic = _condition_topic(
                        f"pathe-bookability:{slug}", "not-bookable"
                    )
                    if topic not in record["topics"]:
                        record["topics"].append(topic)
                        topics_bound = True
        for domain in _pending_condition_domains(ctx, "pathe-sale:"):
            slug = domain.removeprefix("pathe-sale:")
            show = shows.get(slug)
            if not _listing_metadata_authoritative(pathe_snapshot, slug, show):
                continue
            observed.add(domain)
            sale = show.get("salesOpeningDatetime") if show else None
            if show and sale and detect.selected_listing(show, ctx.cfg):
                active.add(_condition_topic(domain, sale))

        for domain in _pending_condition_domains(ctx, "pathe-listing:"):
            slug = domain.removeprefix("pathe-listing:")
            show = shows.get(slug)
            if not _listing_metadata_authoritative(pathe_snapshot, slug, show):
                continue
            observed.add(domain)
            if show is not None and detect.selected_listing(show, ctx.cfg):
                active.add(_condition_topic(domain, "present"))

        for domain in _pending_condition_domains(ctx, "pathe-bookability:"):
            slug = domain.removeprefix("pathe-bookability:")
            show = shows.get(slug)
            acknowledged = _bookability_confirmed(ctx.state, ctx.cfg, domain)
            if not acknowledged and not _listing_metadata_authoritative(
                pathe_snapshot, slug, show
            ):
                continue
            if not acknowledged and not pathe_snapshot.endpoint_healthy(
                slug, "showtimes"
            ):
                continue
            if not pathe_snapshot.healthy and not acknowledged:
                continue
            observed.add(domain)
            if acknowledged:
                active.add(_condition_topic(domain, "bookable"))
                continue
            if show is None:
                continue
            days = pathe_snapshot.showtimes.get(slug) or {}
            entry = pathe_snapshot.cinema_entries.get(slug) or {}
            entry_bookable = bool(
                entry.get("isBookable") or entry.get("bookable")
            )
            if days or entry_bookable:
                active.add(_condition_topic(domain, "bookable"))
            elif detect.selected_listing(show, ctx.cfg):
                active.add(_condition_topic(domain, "not-bookable"))

        for domain in _pending_condition_domains(ctx, "pathe-tickets:"):
            slug = domain.removeprefix("pathe-tickets:")
            show = shows.get(slug)
            if not _listing_metadata_authoritative(pathe_snapshot, slug, show):
                continue
            if not pathe_snapshot.endpoint_healthy(slug, "showtimes"):
                continue
            observed.add(domain)
            if show is None:
                continue
            days = pathe_snapshot.showtimes.get(slug) or {}
            entry = pathe_snapshot.cinema_entries.get(slug) or {}
            if days:
                counts = detect.summarize_sessions(show, days)["counts"]
                formats = set(counts) or {
                    detect.classify_format(show.get("title"), slug)
                }
            elif entry.get("isBookable") or entry.get("bookable"):
                formats = {detect.classify_format(show.get("title"), slug)}
            else:
                formats = set()
            active.update(_condition_topic(domain, fmt) for fmt in formats)

        if _pathe_dates_authoritative(pathe_snapshot):
            open_days = {
                finding.key.rsplit(":", 1)[-1]
                for finding in detect.target_date_findings(
                    pathe_snapshot, ctx.cfg, now
                )
            }
            for day in getattr(ctx.cfg, "pathe_target_dates", []):
                domain = f"pathe-availability:{day}"
                observed.add(domain)
                if day in open_days:
                    active.add(_condition_topic(domain, "open"))

        reported_targets = {
            show["salesOpeningDatetime"]
            for show in pathe_snapshot.matched_shows
            if show.get("slug")
            and show.get("salesOpeningDatetime")
            and detect.selected_listing(show, ctx.cfg)
            and _listing_metadata_authoritative(
                pathe_snapshot, show["slug"], show
            )
        }
        if pathe_snapshot.sale_observations_complete():
            observed.add("pathe-sale-target")
            active.update(
                _condition_topic("pathe-sale-target", target)
                for target in reported_targets
            )

    if cinesa_snapshot is not None and cinesa_snapshot.days:
        known = {day["date"] for day in cinesa_snapshot.days}
        imax = set(
            detect.imax_days(
                cinesa_snapshot.days, ctx.cfg.cinesa_imax_attribute_id
            )
        )
        for day in getattr(ctx.cfg, "cinesa_target_dates", []):
            domain = (
                f"cinesa-availability:{ctx.cfg.cinesa_site_id}:"
                f"{ctx.cfg.cinesa_film_id}:{day}"
            )
            observed.add(domain)
            if day in known:
                value = "imax" if day in imax else "no-imax"
                active.add(_condition_topic(domain, value))
        observed.add("cinesa-imax-presence")
        active.add(
            _condition_topic(
                "cinesa-imax-presence", "present" if imax else "absent"
            )
        )

    if cinesa_token_stuck is not None:
        observed.add("cinesa-token")
        if cinesa_token_stuck:
            active.add(_condition_topic("cinesa-token", "stuck"))

    reconcile_observations(
        ctx, observed_domains=observed, active_conditions=active
    )
    if topics_bound:
        _persist(ctx)


def recover(
    ctx: Any,
    now: datetime,
    *,
    blocked_condition_domains: set[str] | None = None,
) -> bool:
    """Retire stale work, quarantine interrupted attempts, retry safe pending work."""
    blocked_condition_domains = blocked_condition_domains or set()
    sent = False
    changed = False
    for delivery_id, record in list(ctx.state.setdefault("outbox", {}).items()):
        if _has_receipt(ctx.state, delivery_id) or _open_ping_bookable(
            ctx.state, ctx.cfg, record, now
        ):
            ctx.state["outbox"].pop(delivery_id, None)
            changed = True
        elif record.get("members") is not None:
            survivors = [
                member
                for member in record["members"]
                if not _member_expired(member, now)
                and not (
                    not record["force"]
                    and state_mod.already_sent(ctx.state, member["key"])
                )
            ]
            changed = (
                _retain_members(
                    ctx,
                    delivery_id,
                    record,
                    survivors,
                    "expired or acknowledged",
                )
                or changed
            )
            current = ctx.state["outbox"].get(delivery_id)
            if current is not None and current["status"] == "sending":
                current["status"] = "uncertain"
                changed = True
        elif _expired(record, now) or (
            not record["force"] and _ack_satisfied(ctx.state, record["ack"])
        ):
            ctx.state["outbox"].pop(delivery_id, None)
            changed = True
        elif record["status"] == "sending":
            record["status"] = "uncertain"
            changed = True
    if changed:
        _persist(ctx)
    for delivery_id, record in list(ctx.state["outbox"].items()):
        if record["status"] == "pending" and not _has_condition_domain(
            record, blocked_condition_domains
        ):
            sent = _attempt(ctx, delivery_id, now) or sent
    return sent
