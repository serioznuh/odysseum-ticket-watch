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

from . import __version__, cinesa, detect, news, notify, pathe
from . import state as state_mod
from .config import load_config
from .detect import TZ_PARIS, Finding

log = logging.getLogger("watcher")


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
            "Retrying every 15 min — usually clears by itself.",
        )
    if status is not None:
        return (
            f"Cause: Pathé returned HTTP {status}.",
            "Retrying every 15 min; check the logs if it persists.",
        )
    return (
        f"Cause: {summary[:160]}",
        "Retrying every 15 min; check the logs if it persists.",
    )


def build_error_finding(cfg, st: dict, error: str, now: datetime) -> Finding:
    """Fired once per outage, as soon as the local half is confidently blind."""
    cause, tail = pathe_cause(error, ci=running_in_ci())
    when, blind_for = blind_since(st, now)
    since = f"No sale detection since {when}"
    since += f" ({blind_for})." if blind_for else "."
    return Finding(
        kind="WATCHER_ERROR",
        key=f"error:{now:%Y-%m-%d}",
        confidence="high",
        title="Pathé watch is BLIND",
        lines=[watch_label(cfg), since, cause, tail],
        url=cfg.film_page_url,
    )


def build_recovered_finding(cfg, st: dict, now: datetime) -> Finding:
    """Sent on the first successful check after an outage. Reads `st` before the
    caller refreshes `last_check_ok`, so the blind span is still recoverable."""
    _, blind_for = blind_since(st, now)
    line = f"Blind for {blind_for}. " if blind_for else ""
    return Finding(
        kind="RECOVERED",
        key=f"recovered:{now:%Y-%m-%dT%H%M}",
        confidence="high",
        title="Pathé watch is back",
        lines=[watch_label(cfg), f"{line}Checks are running normally."],
        url=cfg.film_page_url,
    )


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

    The cloud pass never calls Pathé, so it cannot see *why* it is blind. It
    infers that from `error_alerted`: set means the local half ran, failed and
    alerted; clear means the Mac never got as far as reporting.
    """
    repeat = day > 1
    when = short_dt(detect.parse_iso(st.get("last_check_ok")))
    if st.get("error_alerted") and st.get("last_error"):
        cause, _ = pathe_cause(str(st["last_error"]))  # ci=False: recorded by the Mac
        if repeat:
            cause = cause.replace("Cause: Pathé is", "Cause: Pathé is still")
    else:
        cause = "Cause: the Mac hasn't completed a check — off, asleep, or can't push."
    # The key is `last_check_ok`, which goes stale when the *local half* stops —
    # and that half runs the news feeds and Cinesa too, so naming only Pathé
    # understates the outage (OTW-07).
    dark = "Pathé and news checks"
    if getattr(cfg, "cinesa_enabled", False):
        dark = "Pathé, news and Cinesa checks"
    return Finding(
        kind="WATCHER_STILL_BLIND" if repeat else "WATCHER_ERROR",
        key=key,
        confidence="high",
        title=(
            f"Still blind — day {day}"
            if repeat
            else f"Local checks have stopped — {fmt_duration(blind)}"
        ),
        lines=[
            watch_label(cfg),
            f"Last successful check: {when}.",
            cause,
            f"{dark} are dark — cloud reminders still run.",
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

    Cinesa state still carries no per-run success timestamp (at 15-min cadence
    it would rewrite and push state.json ~96x a day); `since` comes from
    `blind_since`, stamped once when an outage is first confirmed.
    """
    status = _cinesa_error_status(error)
    text = str(error)
    if status == 403:
        cause = "Cause: Cinesa is blocking your IP (403)."
        tail = "Retrying every 15 min — the cached token is kept."
    elif status == 401:
        cause = "Cause: Cinesa rejected the token."
        tail = "A fresh token is minted on the next retry."
    elif "Chrome" in text or "CDP" in text or "challenge" in text.lower():
        cause = "Cause: the token step couldn't drive Chrome."
        tail = "Needs you: check Chrome is installed and the Mac is logged in and awake."
    else:
        cause = f"Cause: {' '.join(text.split())[:160]}"
        tail = "Retrying every 15 min; check the logs if it persists."
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
    primary = next(
        (s for s in snap.matched_shows if s.get("slug") == cfg.primary_slug), None
    )
    sales = st.get("sales", {})
    sale_line = "no sale date yet"
    if len(sales) == 1:
        # One watched listing is the normal case; naming its slug adds nothing.
        sale_line = "sale opens " + detect.fmt_dt_short(
            detect.parse_iso(next(iter(sales.values())))
        )
    elif sales:
        parts = [
            f"{slug}: {detect.fmt_dt_short(detect.parse_iso(iso))}"
            for slug, iso in sales.items()
        ]
        sale_line = "sales open — " + "; ".join(parts)
    listed = "Listed at the cinema" if snap.cinema_entries else "Not yet listed"
    bookable = "sessions bookable" if snap.showtimes else "nothing bookable"
    lines = [
        watch_label(cfg),
        f"Release {detect.fmt_release(primary) if primary else cfg.release_date} · {sale_line}.",
        f"{listed} · {bookable} · {detect.plural(len(snap.matched_shows), 'listing')} watched.",
    ]
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
    lines.append("All checks healthy.")
    return Finding(
        kind="HEARTBEAT",
        key=f"heartbeat:{now:%Y-%m-%d}",
        confidence="high",
        title="All quiet — nothing new",
        lines=lines,
        url=cfg.film_page_url,
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
        " proximity (war-room mode near the opening); reminders are left to the"
        " cloud pass. Does not gate the Cinesa half, which runs every time",
    )
    parser.add_argument("--test-telegram", action="store_true", help="send a test message and exit")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)

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
    st = state_mod.load_state(state_path)
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
    # firing. When Pathé is not due and Cinesa is off, the run is still the
    # original zero-network no-op.
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
            if not cfg.cinesa_enabled:
                return 0
        elif args.adaptive_cadence:
            log.info("adaptive cadence tier: check when older than %.2fh — running", threshold)

    sent_any = False

    if args.mode == "check":
        findings: list[Finding] = []
        snap: detect.Snapshot | None = None
        client = pathe.make_client()

        try:
            if pathe_due:
                snap = pathe.fetch_snapshot(client, cfg)
        except Exception as e:
            log.exception("Pathé check failed")
            # Capped at the alert threshold: nothing reads a larger value,
            # and a counter that kept growing would rewrite state.json on
            # every firing of a long outage, commit and push included.
            st["failure_streak"] = min(
                st.get("failure_streak", 0) + 1, cfg.failure_streak_threshold
            )
            # The cloud pass never calls Pathé, so it cannot tell "IP blocked"
            # from "the Mac never checked in" — the two need opposite responses.
            # Recording the cause here lets it say which. Written only when the
            # text changes: a steady outage writes state once, not every 15 min.
            summary, status = summarize_pathe_error(str(e))
            # Store the status without the failing URL: fetch_snapshot hits
            # several endpoints, and an outage that flapped between them would
            # otherwise rewrite state — and commit and push — every 15 min.
            # The marker survives into state (still URL-free, still one stable
            # string per outage) so the cloud pass reports the right cause too.
            if status and "refused by origin" in str(e):
                recorded = f"HTTP {status} refused by origin"
            elif status:
                recorded = f"HTTP {status}"
            else:
                recorded = summary[:120]
            if st.get("last_error") != recorded:
                st["last_error"] = recorded
            # With adaptive cadence, retries come every 15 min — require both
            # a failure streak AND 6h without success before crying wolf.
            if (
                st["failure_streak"] >= cfg.failure_streak_threshold
                and not state_mod.is_check_fresh(st, 6.0, now)
                and not st.get("error_alerted")
            ):
                err = build_error_finding(cfg, st, str(e), now)
                if notify.send_telegram(
                    cfg,
                    notify.render_finding(err),
                    dry_run=args.dry_run,
                    silent=notify.is_silent(cfg, err.kind),
                ):
                    st["error_alerted"] = True
                    sent_any = True

        if snap is not None:
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
                    # run, which at 15-min cadence would push state ~96x a day.
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

        for f in findings:
            if state_mod.already_sent(st, f.key):
                log.debug("suppressed duplicate alert %s", f.key)
                continue
            log.info("alert [%s] %s (key=%s)", f.kind, f.title, f.key)
            if notify.send_telegram(
                cfg,
                notify.render_finding(f),
                dry_run=args.dry_run,
                silent=notify.is_silent(cfg, f.kind),
            ):
                state_mod.mark_sent(st, f.key, now)
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
            state_mod.update_from_snapshot(
                st, snap, cfg, now, advance_one_shot=one_shot_delivered
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

    # Reminders and supervision are owned by the cloud pass; adaptive local
    # runs skip them so two writers never race on the same state keys.
    due = [] if args.adaptive_cadence else state_mod.due_reminders(
        st, cfg.reminder_offsets_minutes, now
    )
    for r in due:
        text = notify.render_reminder(r["offset"], r["target"], cfg, now)
        log.info("reminder due: %s before %s", r["offset"], r["target"])
        if notify.send_telegram(cfg, text, dry_run=args.dry_run):
            state_mod.mark_reminder(st, r["target"], r["offset"], cfg.reminder_offsets_minutes)

    # Supervision: alert when the Pathé check (running on another machine
    # than the cloud reminder pass) stopped reporting.
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
