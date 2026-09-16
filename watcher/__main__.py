"""CLI entry point.

Modes:
  check  — full pass: Pathé API + news feeds, alerts, reminders, heartbeat.
  remind — state-only pass (no Pathé/news requests): send due sale reminders.

Usage:
  python -m watcher --mode check [--dry-run] [--verbose]
  python -m watcher --mode remind
  python -m watcher --test-telegram
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from datetime import datetime, timedelta

from . import __version__, cinesa, coalesce, detect, news, notify, pathe
from . import state as state_mod
from .config import load_config
from .detect import TZ_PARIS, Finding

log = logging.getLogger("watcher")
PARTIAL_PATHE_FAILURE = "Per-listing Pathé failure:"


def summarize_pathe_error(error: str) -> tuple[str, int | None]:
    """Turn httpx's multiline status message into a concise alert line."""
    status = re.search(
        r"(?:Client|Server) error '(\d{3}) ([^']+)' for url '([^']+)'", error
    )
    if status:
        return f"HTTP {status.group(1)} {status.group(2)} from {status.group(3)}", int(
            status.group(1)
        )
    # Idempotent on its own output: the summary is what gets stored in state for
    # the cloud pass to read back, so re-summarising it must not lose the code.
    already = re.match(r"HTTP (\d{3})\b", error)
    if already:
        return " ".join(error.split())[:300], int(already.group(1))
    return " ".join(error.split())[:300], None


def watch_label(cfg) -> str:
    """Which watch a message is about. Every alert carries this: with more than
    one film or cinema in play, an unlabelled alert is ambiguous."""
    return f"{cfg.film_title} · {cfg.cinema_name}"


def cinesa_label(cfg) -> str:
    return f"{cfg.cinesa_film_title} · {cfg.cinesa_site_name}"


def fmt_duration(delta: timedelta) -> str:
    """Human span: '45 min', '6 h 20 m', '3 days'."""
    total = max(0, int(delta.total_seconds()))
    if total < 3600:
        return f"{total // 60} min"
    if total < 48 * 3600:
        hours, minutes = divmod(total // 60, 60)
        return f"{hours} h {minutes} m" if minutes else f"{hours} h"
    return f"{total // 86400} days"


def short_dt(dt: datetime | None) -> str:
    """'Wed 2 Sep, 07:11' — Paris time, no year, no timezone suffix."""
    if dt is None:
        return "unknown"
    dt = detect.as_aware(dt).astimezone(detect.TZ_PARIS)
    return f"{dt:%a} {dt.day} {dt:%b}, {dt:%H:%M}"


def blind_since(st: dict, now: datetime) -> tuple[str, str | None]:
    """(when the watcher last saw Pathé, how long it has been blind)."""
    last = detect.parse_iso(st.get("last_check_ok"))
    if last is None:
        return "the watcher started", None
    last = detect.as_aware(last)
    return short_dt(last), fmt_duration(now - last)


def running_in_ci() -> bool:
    return bool(os.environ.get("GITHUB_ACTIONS"))


def pathe_cause(error: str, *, ci: bool = False) -> tuple[str, str]:
    """(cause line, what-happens-next line) for a Pathé failure.

    `ci` describes the machine that hit the error, which is only knowable for
    an error the current process just caught. A cause read back out of state
    was recorded by the *other* half, so callers doing that must leave it
    False — the cloud pass always runs in Actions, and would otherwise report
    every one of the Mac's 403s as the expected datacenter block.
    """
    if error.startswith(PARTIAL_PATHE_FAILURE):
        affected = error.removeprefix(PARTIAL_PATHE_FAILURE).strip()
        return (
            f"Cause: Pathé listing data is unavailable ({affected}).",
            "Catalogue signals still work; retrying the missing data every 5 min.",
        )
    summary, status = summarize_pathe_error(error)
    if status == 403 and "refused by origin" in error:
        # Pathé's own nginx declining a listing, which no amount of waiting or
        # re-running fixes and which has nothing to do with the IP. Kept ahead
        # of the block branches so CI cannot relabel it a datacenter block.
        return (
            "Cause: Pathé is refusing a listing (403), not your IP.",
            "Expected on event listings; check the logs for which call failed.",
        )
    if status == 403:
        if ci:
            # Pathé blocks GitHub datacenter IPs outright, so this one will not
            # clear on its own and no local retry is scheduled (OTW-03).
            return (
                "Cause: Pathé blocks GitHub datacenter IPs (403).",
                "Expected in CI — run the check locally instead.",
            )
        return (
            "Cause: Pathé is blocking your IP (403).",
            "Retrying every 5 min — usually clears by itself.",
        )
    if status is not None:
        return (
            f"Cause: Pathé returned HTTP {status}.",
            "Retrying every 5 min; check the logs if it persists.",
        )
    return (
        f"Cause: {summary[:160]}",
        "Retrying every 5 min; check the logs if it persists.",
    )


def build_error_finding(cfg, st: dict, error: str, now: datetime) -> Finding:
    """Fired once the local half is confidently blind or persistently degraded."""
    cause, tail = pathe_cause(error, ci=running_in_ci())
    when, blind_for = blind_since(st, now)
    degraded = error.startswith(PARTIAL_PATHE_FAILURE)
    since = (
        f"Full listing coverage unavailable since {when}"
        if degraded
        else f"No sale detection since {when}"
    )
    since += f" ({blind_for})." if blind_for else "."
    return Finding(
        kind="WATCHER_ERROR",
        key=f"error:{now:%Y-%m-%d}",
        confidence="high",
        title="Pathé watch is DEGRADED" if degraded else "Pathé watch is BLIND",
        lines=[watch_label(cfg), since, cause, tail],
        url=cfg.film_page_url,
    )


def build_recovered_finding(cfg, st: dict, now: datetime) -> Finding:
    """Sent on the first successful check after an outage. Reads `st` before the
    caller refreshes `last_check_ok`, so the blind span is still recoverable."""
    _, blind_for = blind_since(st, now)
    label = (
        "Degraded"
        if str(st.get("last_error", "")).startswith(PARTIAL_PATHE_FAILURE)
        else "Blind"
    )
    if blind_for:
        line = f"{label} for {blind_for}. "
    elif label == "Degraded":
        line = "Degraded state cleared. "
    else:
        line = ""
    return Finding(
        kind="RECOVERED",
        key=f"recovered:{now:%Y-%m-%dT%H%M}",
        confidence="high",
        title="Pathé watch is back",
        lines=[watch_label(cfg), f"{line}Checks are running normally."],
        url=cfg.film_page_url,
    )


def record_pathe_failure(cfg, st: dict, error: str, now: datetime, *, dry_run: bool) -> bool:
    """Advance the shared Pathé supervision streak and alert at its threshold."""
    st["failure_streak"] = min(
        st.get("failure_streak", 0) + 1, cfg.failure_streak_threshold
    )
    summary, status = summarize_pathe_error(error)
    # Store no endpoint URL for ordinary outages, and only stable endpoint/slug
    # names for partial failures, so an unchanged outage settles in state.
    if error.startswith(PARTIAL_PATHE_FAILURE):
        recorded = error[:300]
    elif status and "refused by origin" in error:
        recorded = f"HTTP {status} refused by origin"
    elif status:
        recorded = f"HTTP {status}"
    else:
        recorded = summary[:120]
    if st.get("last_error") != recorded:
        st["last_error"] = recorded

    # With adaptive cadence, retries come every 5 min — require both a failure
    # streak AND 6h without a fully healthy snapshot before crying wolf.
    if (
        st["failure_streak"] >= cfg.failure_streak_threshold
        and not state_mod.is_check_fresh(st, 6.0, now)
        and not st.get("error_alerted")
    ):
        finding = build_error_finding(cfg, st, error, now)
        if notify.send_telegram(
            cfg,
            notify.render_finding(finding),
            dry_run=dry_run,
            silent=notify.is_silent(cfg, finding.kind),
        ):
            st["error_alerted"] = True
            return True
    return False


def stale_period(blind: timedelta, stale_hours: int) -> int:
    """Which 24 h slot of an outage we are in: 0 at the alert threshold, then
    one per day. Measured from the threshold rather than from the last good
    check, so repeats land 24h apart — the same wall-clock time, except across
    a Europe/Paris DST change, where the hour shifts by one.
    """
    return (blind - timedelta(hours=stale_hours)).days


def build_stale_finding(cfg, st: dict, blind: timedelta, key: str, day: int) -> Finding:
    """Cloud-side supervision. `day` 1 is the first alert at the threshold;
    every later one is a silent 24 h repeat, so a long outage cannot go quiet.

    The cloud pass never calls Pathé, so it reads the local half's stable cause.
    A partial-failure marker distinguishes incomplete listing data from a dark
    local process; `error_alerted` distinguishes other reported failures from a
    Mac that never got as far as reporting.
    """
    repeat = day > 1
    when = short_dt(detect.parse_iso(st.get("last_check_ok")))
    partial = str(st.get("last_error", "")).startswith(PARTIAL_PATHE_FAILURE)
    if partial or (st.get("error_alerted") and st.get("last_error")):
        cause, _ = pathe_cause(str(st["last_error"]))  # ci=False: recorded by the Mac
        if repeat and not partial:
            cause = cause.replace("Cause: Pathé is", "Cause: Pathé is still")
    else:
        cause = "Cause: the Mac hasn't completed a check — off, asleep, or can't push."
    # The key is `last_check_ok`, which goes stale when the *local half* stops —
    # and that half runs the news feeds and Cinesa too, so naming only Pathé
    # understates the outage (OTW-07).
    dark = "Pathé and news checks"
    if getattr(cfg, "cinesa_enabled", False):
        dark = "Pathé, news and Cinesa checks"
    title = (
        f"Pathé still degraded — day {day}"
        if partial and repeat
        else (
            f"Pathé listing checks degraded — {fmt_duration(blind)}"
            if partial
            else (f"Still blind — day {day}" if repeat else f"Local checks have stopped — {fmt_duration(blind)}")
        )
    )
    return Finding(
        kind="WATCHER_STILL_BLIND" if repeat else "WATCHER_ERROR",
        key=key,
        confidence="high",
        title=title,
        lines=[
            watch_label(cfg),
            f"Last fully healthy check: {when}." if partial else f"Last successful check: {when}.",
            cause,
            (
                "Catalogue checks still work; affected listing details are incomplete."
                if partial
                else f"{dark} are dark — cloud reminders still run."
            ),
        ],
        url=cfg.film_page_url,
    )


def _cinesa_error_status(error: str | Exception) -> int | None:
    status = getattr(error, "status_code", None)
    if status is None:
        response = getattr(error, "response", None)
        status = getattr(response, "status_code", None)
    if status is not None:
        try:
            return int(status)
        except (TypeError, ValueError):
            pass

    text = str(error)
    match = re.search(r'''(?:\bHTTP\s+|[\'"])(\d{3})\b''', text)
    return int(match.group(1)) if match else None


def build_cinesa_error_finding(
    cfg, error: str | Exception, key: str, *, day: int = 1, since: str | None = None
) -> Finding:
    """Cinesa half is blind. Kept separate from the Pathé error: the two halves
    fail for unrelated reasons and one must never mask the other.

    Cinesa state still carries no per-run success timestamp (at 5-min cadence
    it would rewrite and push state.json ~288x a day); `since` comes from
    `blind_since`, stamped once when an outage is first confirmed.
    """
    status = _cinesa_error_status(error)
    text = str(error)
    if status == 403:
        cause = "Cause: Cinesa is blocking your IP (403)."
        tail = "Retrying every 5 min — the cached token is kept."
    elif status == 401:
        cause = "Cause: Cinesa rejected the token."
        tail = "A fresh token is minted on the next retry."
    elif "Chrome" in text or "CDP" in text or "challenge" in text.lower():
        cause = "Cause: the token step couldn't drive Chrome."
        tail = "Needs you: check Chrome is installed and the Mac is logged in and awake."
    else:
        cause = f"Cause: {' '.join(text.split())[:160]}"
        tail = "Retrying every 5 min; check the logs if it persists."
    repeat = day > 1
    return Finding(
        kind="WATCHER_STILL_BLIND" if repeat else "WATCHER_ERROR",
        key=key,
        confidence="high",
        title=f"Cinesa watch still blind — day {day}" if repeat else "Cinesa watch is BLIND",
        lines=[
            cinesa_label(cfg),
            (
                f"Not watched since {since} — the Pathé half is unaffected."
                if since
                else "Not being watched right now — the Pathé half is unaffected."
            ),
            cause,
            tail,
        ],
        url=cfg.cinesa_page_url,
    )


def build_cinesa_recovered_finding(cfg, now: datetime) -> Finding:
    return Finding(
        kind="RECOVERED",
        key=f"cinesa_recovered:{now:%Y-%m-%dT%H%M}",
        confidence="high",
        title="Cinesa watch is back",
        lines=[cinesa_label(cfg), "Checks are running normally."],
        url=cfg.cinesa_page_url,
    )


def build_heartbeat(cfg, snap: detect.Snapshot, st: dict, now: datetime) -> Finding:
    now = detect.as_aware(now).astimezone(TZ_PARIS)
    primary = next(
        (s for s in snap.matched_shows if s.get("slug") == cfg.primary_slug), None
    )
    wanted = cfg.pathe_target_format
    # `sales` in state is a delivery/dedup baseline, not current evidence. It
    # deliberately retains withdrawn/old dates. Only a future timestamp on a
    # live, selected listing belongs in the status, and it is national rather
    # than a promise that the user's dates will open then.
    sales = sorted({
        detect.as_aware(dt)
        for show in snap.matched_shows if detect.selected_listing(show, cfg)
        for dt in [detect.parse_iso(show.get("salesOpeningDatetime"))]
        if dt is not None and detect.as_aware(dt) > now
    })
    if sales:
        label = "opening" if len(sales) == 1 else "openings"
        sale_line = f"Upcoming national sale {label}: " + "; ".join(
            detect.fmt_dt_short(dt) for dt in sales
        ) + "."
    else:
        scope = " for this format" if wanted else ""
        sale_line = f"No upcoming national sale opening published{scope}."
    lines = [
        watch_label(cfg),
        f"Release {detect.fmt_release(primary) if primary else cfg.release_date}.",
    ]
    if wanted:
        lines.append(f"Watching: {detect.FORMAT_LABELS[wanted]}.")
    if cfg.pathe_target_dates:
        # Reuse the alert detector's exact day + format evidence. Sent keys and
        # the historical formats_seen baseline must not imply current booking.
        confirmed = {f.key for f in detect.target_date_findings(snap, cfg, now)}
        for day in cfg.pathe_target_dates:
            if day < now.date().isoformat():
                status = "date has passed"
            elif detect.pathe_date_key(cfg, day) in confirmed:
                status = "booking confirmed"
            else:
                status = "booking not confirmed"
            lines.append(f"{detect.fmt_day(day)}: {status}.")
    else:
        bookable = False
        for show in snap.matched_shows:
            slug = show.get("slug", "")
            entry = snap.cinema_entries.get(slug) or {}
            if detect.selected_listing(show, cfg) and (
                entry.get("isBookable") is True or entry.get("bookable") is True
            ) and (not wanted or snap.endpoint_healthy(slug, "showtimes")):
                bookable = True
            for day, sessions in (snap.showtimes.get(slug) or {}).items():
                if day < now.date().isoformat():
                    continue
                for session in sessions:
                    fmt = detect.classify_format(
                        show.get("title"), slug, " ".join(session.get("tags") or []),
                        session.get("auditoriumName"), session.get("specialShowtimeDetails"),
                    )
                    if session.get("status") == "available" and (not wanted or fmt == wanted):
                        bookable = True
        status = "booking confirmed" if bookable else "booking not confirmed"
        listed = "Listed at the cinema" if snap.cinema_entries else "Not yet listed"
        lines.append(f"{listed} · {status}.")
    lines += [sale_line, f"{detect.plural(len(snap.matched_shows), 'listing')} watched."]
    if cfg.cinesa_enabled:
        cin = st.get("cinesa", {})
        imax = {True: "IMAX scheduled", False: "no IMAX scheduled"}.get(
            cin.get("imax_present"), "IMAX unknown"
        )
        lines += [
            "",
            cinesa_label(cfg),
            (
                f"Bookable to {cin.get('horizon') or 'unknown'}"
                f" ({cin.get('day_count') or 0} days) · {imax}."
            ),
            "Watching: " + (", ".join(cfg.cinesa_target_dates) or "no target dates"),
        ]
    degraded = snap.degraded_results
    if degraded:
        lines += [
            "Pathé check degraded — catalogue signals are available, but listing data is incomplete:",
            "; ".join(f"{endpoint}: {slug}" for slug, endpoint, _result in degraded) + ".",
        ]
        title = "All quiet — Pathé partly degraded"
    else:
        lines.append("All checks healthy.")
        title = "All quiet — nothing new"
    return Finding(
        kind="HEARTBEAT",
        key=f"heartbeat:{now:%Y-%m-%d}",
        confidence="high",
        title=title,
        lines=lines,
        url=(cfg.pathe_page_url or cfg.film_page_url) if wanted else cfg.film_page_url,
    )


def heartbeat_due(st: dict, now: datetime, days: int) -> bool:
    if days <= 0:
        return False
    last = detect.parse_iso(st.get("last_heartbeat"))
    return last is None or (now - detect.as_aware(last)) >= timedelta(days=days)


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="watcher", description=__doc__)
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("--state", default=None, help="override state file path")
    parser.add_argument("--mode", choices=["check", "remind"], default="check")
    parser.add_argument("--dry-run", action="store_true", help="print alerts instead of sending; do not save state")
    parser.add_argument(
        "--bootstrap-state",
        action="store_true",
        help="create empty state for a genuinely new installation and exit; never overwrites",
    )
    parser.add_argument(
        "--skip-if-checked-within",
        type=float,
        default=0,
        metavar="HOURS",
        help="check mode: skip the Pathé/news half when its last successful check is"
        " newer than this (fixed threshold); the Cinesa half still runs",
    )
    parser.add_argument(
        "--adaptive-cadence",
        action="store_true",
        help="check mode: compute the Pathé freshness threshold from the sale-target"
        " proximity (war-room mode near the opening). Does not gate the reminder"
        " ladder or the Cinesa half, which run every time",
    )
    parser.add_argument(
        "--reminder-grace-minutes",
        type=float,
        default=0.0,
        metavar="MINUTES",
        help="failover mode: only send a reminder whose window opened at least this"
        " long ago. The local half omits it (grace 0 — it fires every 5 min and"
        " owns the ladder); the cloud pass passes more than that firing interval,"
        " so it only sends what the local half missed",
    )
    parser.add_argument("--test-telegram", action="store_true", help="send a test message and exit")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)
    if args.bootstrap_state and args.dry_run:
        parser.error("--bootstrap-state cannot be combined with --dry-run")

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    # httpx logs full request URLs at INFO; the Telegram URL embeds the bot
    # token, which must never reach logs (GitHub Actions logs can be public).
    logging.getLogger("httpx").setLevel(logging.WARNING)

    cfg = load_config(args.config)
    state_path = args.state or cfg.state_file
    try:
        if args.bootstrap_state:
            state_mod.bootstrap_state(state_path)
            log.info("new state bootstrapped at %s", state_path)
            return 0
        st = state_mod.load_state(state_path)
    except state_mod.StateError as exc:
        log.error("%s", exc)
        return 2
    now = datetime.now(TZ_PARIS)

    if args.test_telegram:
        ok = notify.send_telegram(
            cfg,
            f"✅ <b>odysseum-ticket-watch</b> v{__version__} is talking to you.\n"
            f"Watching: {cfg.film_title} @ {cfg.cinema_name}",
            dry_run=args.dry_run,
        )
        return 0 if ok else 1

    if not args.dry_run and not (cfg.telegram_token and cfg.telegram_chat_id):
        log.error(
            "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set (or use --dry-run)."
        )
        return 1

    # The cadence guard governs the Pathé half only. Cinesa is a single small
    # call against a host that is neither bot-gated nor rate-limited, and the
    # point is catching a schedule change within minutes — so it runs on every
    # firing. When Pathé is not due and Cinesa is off, the whole check block is
    # skipped and the run stays the original zero-network no-op — but it must
    # still fall through to the reminder ladder below, which the local half now
    # owns (OTW-15). Cinesa is off by default, so an early `return 0` here would
    # swallow the 15-min warning on any firing the cadence guard calls fresh.
    pathe_due = True
    if args.mode == "check":
        threshold = args.skip_if_checked_within
        if args.adaptive_cadence:
            threshold = state_mod.adaptive_staleness_hours(st, cfg, now)
        if threshold > 0 and state_mod.is_check_fresh(st, threshold, now):
            log.info(
                "last successful Pathé check (%s) is newer than %.2fh%s — skipping it",
                st.get("last_check_ok"),
                threshold,
                " [adaptive tier]" if args.adaptive_cadence else "",
            )
            pathe_due = False
        elif args.adaptive_cadence:
            log.info("adaptive cadence tier: check when older than %.2fh — running", threshold)

    sent_any = False

    if args.mode == "check" and (pathe_due or cfg.cinesa_enabled):
        findings: list[Finding] = []
        snap: detect.Snapshot | None = None
        client = pathe.make_client()

        try:
            if pathe_due:
                snap = pathe.fetch_snapshot(client, cfg)
        except Exception as e:
            log.exception("Pathé check failed")
            sent_any = record_pathe_failure(
                cfg, st, str(e), now, dry_run=args.dry_run
            ) or sent_any

        if snap is not None:
            degradation = snap.degradation_summary()
            if degradation:
                log.warning("%s", degradation)
                sent_any = record_pathe_failure(
                    cfg, st, degradation, now, dry_run=args.dry_run
                ) or sent_any
            else:
                if st.get("error_alerted"):
                    findings.append(build_recovered_finding(cfg, st, now))
                st["failure_streak"] = 0
                st["error_alerted"] = False
                # Stale cause + spent stale keys must not survive into the next
                # outage: they would make the cloud pass report the wrong reason.
                st.pop("last_error", None)
                for spent in [k for k in st.get("alerts", {}) if k.startswith("stale:")]:
                    st["alerts"].pop(spent, None)
                st["last_check_ok"] = now.isoformat()
            findings.extend(detect.analyze_pathe(snap, st, cfg, now))

        if pathe_due and cfg.news_enabled:
            try:
                items = news.fetch_news_items(client, cfg)
                findings.extend(detect.analyze_news(items, cfg, st, now))
            except Exception:
                log.exception("news check failed (non-fatal)")

        csnap: detect.CinesaSnapshot | None = None
        cinesa_error_key: str | None = None
        if cfg.cinesa_enabled:
            cin = st.setdefault("cinesa", {})
            try:
                csnap = cinesa.fetch_snapshot(cfg)
            except Exception as e:
                log.exception("Cinesa check failed")
                # Capped at the alert threshold: nothing reads a larger value,
                # and a counter that kept growing would rewrite state.json on
                # every firing of a long outage, commit and push included.
                cin["failure_streak"] = min(
                    cin.get("failure_streak", 0) + 1, cfg.failure_streak_threshold
                )
                if cin["failure_streak"] >= cfg.failure_streak_threshold:
                    # Stamped once, when the outage is first confirmed — not per
                    # run, which at 5-min cadence would push state ~288x a day.
                    cin.setdefault("blind_since", now.isoformat())
                    started = detect.parse_iso(cin.get("blind_since")) or now
                    # The key is already day-stamped, so it yields exactly one
                    # alert per day; day 1 buzzes, later days repeat silently.
                    day = (now.date() - detect.as_aware(started).date()).days + 1
                    cinesa_error_key = f"cinesa_error:{now:%Y-%m-%d}"
                    findings.append(
                        build_cinesa_error_finding(
                            cfg,
                            e,
                            cinesa_error_key,
                            day=day,
                            since=short_dt(started) if day > 1 else None,
                        )
                    )
            else:
                if cin.get("error_alerted"):
                    findings.append(build_cinesa_recovered_finding(cfg, now))
                cin.pop("blind_since", None)
                findings.extend(detect.analyze_cinesa(csnap, st, cfg, now))

        pending: list[Finding] = []
        for f in findings:
            if state_mod.already_sent(st, f.key):
                log.debug("suppressed duplicate alert %s", f.key)
                continue
            if any(p.key == f.key for p in pending):
                # Same key twice in one pass — a listing the catalogue returned
                # twice. Harmless before merging (the first send marked it), but
                # it would repeat itself inside a merged message.
                log.debug("dropped repeated finding %s", f.key)
                continue
            pending.append(f)

        for alert in coalesce.merge(pending, cfg):
            f = alert.finding
            if alert.merged:
                log.info(
                    "merged %d findings into one alert (keys=%s)",
                    len(alert.keys),
                    " ".join(alert.keys),
                )
            log.info("alert [%s] %s (key=%s)", f.kind, f.title, f.key)
            # One loud member is enough to buzz: merging must never silence an
            # alert that would have arrived with sound on its own.
            if notify.send_telegram(
                cfg,
                notify.render_finding(f),
                dry_run=args.dry_run,
                silent=all(notify.is_silent(cfg, k) for k in alert.kinds),
            ):
                for key in alert.keys:
                    state_mod.mark_sent(st, key, now)
                sent_any = True

        # The error flag flips only once the alert really went out, so a failed
        # send retries on the next run instead of being silently swallowed.
        if cinesa_error_key and state_mod.already_sent(st, cinesa_error_key):
            st.setdefault("cinesa", {})["error_alerted"] = True

        if csnap is not None:
            # Same rule for the IMAX baseline: advancing it after a failed send
            # would make analyze_cinesa agree with the new reality and never
            # re-raise the transition, losing the alert for good.
            imax_delivered = all(
                state_mod.already_sent(st, f.key)
                for f in findings
                if f.kind in ("CINESA_IMAX_GONE", "CINESA_IMAX_BACK")
            )
            state_mod.update_from_cinesa(st, csnap, cfg, now, advance_imax=imax_delivered)

        if snap is not None:
            # Advancing these baselines after a failed send would make the next
            # analysis agree with the new reality and lose one-shot alerts.
            one_shot_delivered = all(
                state_mod.already_sent(st, f.key)
                for f in findings
                if f.kind in ("NEW_LISTING", "TICKETS_AVAILABLE")
            )
            # `sales` is SALE_DATE's baseline and fails on its own: recording an
            # opening that was never announced retired the alert for good.
            sale_delivered = all(
                state_mod.already_sent(st, f.key)
                for f in findings
                if f.kind in ("SALE_DATE", "SALE_DATE_CHANGED")
            )
            state_mod.update_from_snapshot(
                st,
                snap,
                cfg,
                now,
                advance_one_shot=one_shot_delivered,
                advance_sales=sale_delivered,
            )
            if not sent_any and heartbeat_due(st, now, cfg.heartbeat_days):
                hb = build_heartbeat(cfg, snap, st, now)
                if notify.send_telegram(
                    cfg,
                    notify.render_finding(hb),
                    dry_run=args.dry_run,
                    silent=notify.is_silent(cfg, hb.kind),
                ):
                    st["last_heartbeat"] = now.isoformat()

    # The reminder ladder is owned by the LOCAL half: launchd fires it every
    # 5 min — three chances inside a 15-min warning — and it runs with grace 0.
    # The cloud pass is the failover for a sleeping Mac and passes
    # --reminder-grace-minutes, so it only sends what the local half did not.
    # Two things keep two writers off `reminders_sent`, and both are needed:
    # that grace, and `scripts/local-check.sh` pulling *before* it runs — a
    # clone that has not pulled cannot see what the failover already sent.
    # Reminders used to be cloud-only for exactly that reason, but the cron is
    # far too unreliable to own them: measured over the 9.6 days to 2026-09-03
    # the */15 workflow ran 100 times of the 920 it implies (10.9%), median gap
    # 58 min, mean 139 min, max 693 min — enough to skip the 15-min warning
    # outright, or to sleep through the opening.
    #
    # Read the clock AGAIN here. `now` was taken before the Pathé/news/Cinesa
    # block, which can burn minutes on a bad connection — Pathé alone retries
    # every request three times against a 20 s timeout, over several requests —
    # and the ladder is the one thing in this function whose correctness is
    # measured in minutes. A run that starts at T-16 and reaches this line at
    # T+5 must send the "SALE IS OPEN" ping, not the 15-min warning worded from
    # a clock that has already expired. Every other `now` here stays the
    # run-start reading on purpose: `last_check_ok`, the dedup keys and the
    # staleness arithmetic all record *when this batch ran*, and a single run's
    # bookkeeping has to agree with itself.
    ladder_now = datetime.now(TZ_PARIS)
    due = state_mod.due_reminders(
        st, cfg.reminder_offsets_minutes, ladder_now, args.reminder_grace_minutes, cfg=cfg
    )
    for r in due:
        text = notify.render_reminder(r["offset"], r["target"], cfg, ladder_now)
        log.info("reminder due: %s before %s", r["offset"], r["target"])
        if notify.send_telegram(cfg, text, dry_run=args.dry_run):
            state_mod.mark_reminder(st, r["target"], r["offset"], cfg.reminder_offsets_minutes)

    # Supervision: alert when the Pathé check (running on another machine
    # than this cloud pass) stopped reporting.
    if not args.adaptive_cadence and state_mod.is_check_stale(st, cfg.stale_check_hours, now):
        last_ok = detect.as_aware(detect.parse_iso(st.get("last_check_ok")))
        blind = now - last_ok
        # Alert once at the threshold, then every 24h for as long as it lasts —
        # a blind spell that goes quiet after one message is the failure mode
        # this exists to prevent. Periods are measured from the first alert, so
        # every repeat lands at the same clock time.
        period = stale_period(blind, cfg.stale_check_hours)
        key = f"stale:{st.get('last_check_ok')}:{period}"
        if not state_mod.already_sent(st, key):
            stale = build_stale_finding(cfg, st, blind, key, period + 1)
            if notify.send_telegram(
                cfg,
                notify.render_finding(stale),
                dry_run=args.dry_run,
                silent=notify.is_silent(cfg, stale.kind),
            ):
                state_mod.mark_sent(st, key, now)

    if args.dry_run:
        log.info("dry-run: state NOT saved (%s)", state_path)
    else:
        state_mod.save_state(state_path, st)
        log.info("state saved to %s", state_path)
    return 0


if __name__ == "__main__":
    sys.exit(run())
