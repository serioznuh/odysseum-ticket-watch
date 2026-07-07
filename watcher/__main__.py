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
import sys
from datetime import datetime, timedelta

from . import __version__, detect, news, notify, pathe, state as state_mod
from .config import load_config
from .detect import TZ_PARIS, Finding

log = logging.getLogger("watcher")


def build_error_finding(cfg, streak: int, error: str, now: datetime) -> Finding:
    return Finding(
        kind="WATCHER_ERROR",
        key=f"error:{now:%Y-%m-%d}",
        confidence="high",
        title="Watcher cannot reach the Pathé API",
        lines=[
            f"{streak} consecutive daily checks have failed.",
            f"Last error: {error[:300]}",
            "Possible causes: Akamai bot protection extended to /api, API redesign, network issue.",
            "The watcher is currently BLIND — check the GitHub Actions logs, or run it locally.",
        ],
        url=cfg.film_page_url,
    )


def build_recovered_finding(cfg, now: datetime) -> Finding:
    return Finding(
        kind="RECOVERED",
        key=f"recovered:{now:%Y-%m-%dT%H%M}",
        confidence="high",
        title="Watcher recovered — Pathé API reachable again",
        lines=["Checks are running normally again."],
        url=cfg.film_page_url,
    )


def build_heartbeat(cfg, snap: detect.Snapshot, st: dict, now: datetime) -> Finding:
    primary = next(
        (s for s in snap.matched_shows if s.get("slug") == cfg.primary_slug), None
    )
    sales = st.get("sales", {})
    sale_line = "not yet announced"
    if sales:
        parts = [f"{slug}: {detect.fmt_dt(detect.parse_iso(iso))}" for slug, iso in sales.items()]
        sale_line = "; ".join(parts)
    return Finding(
        kind="HEARTBEAT",
        key=f"heartbeat:{now:%Y-%m-%d}",
        confidence="high",
        title=f"Watcher alive — nothing new ({cfg.film_title})",
        lines=[
            f"Release date (Pathé): {detect.fmt_release(primary) if primary else cfg.release_date}",
            f"Sale opening: {sale_line}",
            f"Listed at {cfg.cinema_name}: {'yes' if snap.cinema_entries else 'no'}",
            f"Bookable sessions at {cfg.cinema_name}: {'YES' if snap.showtimes else 'none'}",
            f"Pathé listings watched: {len(snap.matched_shows)}",
            "Daily checks are running normally.",
        ],
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
        help="check mode: exit at once when the last successful check is newer than this (for retry slots)",
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

    if (
        args.mode == "check"
        and args.skip_if_checked_within > 0
        and state_mod.is_check_fresh(st, args.skip_if_checked_within, now)
    ):
        log.info(
            "last successful check (%s) is newer than %.1fh — retry slot not needed, exiting",
            st.get("last_check_ok"),
            args.skip_if_checked_within,
        )
        return 0

    sent_any = False

    if args.mode == "check":
        findings: list[Finding] = []
        snap: detect.Snapshot | None = None
        client = pathe.make_client()

        try:
            snap = pathe.fetch_snapshot(client, cfg)
        except Exception as e:  # noqa: BLE001 — any fetch failure is handled the same way
            log.exception("Pathé check failed")
            st["failure_streak"] = st.get("failure_streak", 0) + 1
            if (
                st["failure_streak"] >= cfg.failure_streak_threshold
                and not st.get("error_alerted")
            ):
                err = build_error_finding(cfg, st["failure_streak"], str(e), now)
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
                findings.append(build_recovered_finding(cfg, now))
            st["failure_streak"] = 0
            st["error_alerted"] = False
            st["last_check_ok"] = now.isoformat()
            findings.extend(detect.analyze_pathe(snap, st, cfg, now))

        if cfg.news_enabled:
            try:
                items = news.fetch_news_items(client, cfg)
                findings.extend(detect.analyze_news(items, cfg, st, now))
            except Exception:  # noqa: BLE001 — news layer must never kill the run
                log.exception("news check failed (non-fatal)")

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

        if snap is not None:
            state_mod.update_from_snapshot(st, snap, cfg, now)
            if not sent_any and heartbeat_due(st, now, cfg.heartbeat_days):
                hb = build_heartbeat(cfg, snap, st, now)
                if notify.send_telegram(
                    cfg,
                    notify.render_finding(hb),
                    dry_run=args.dry_run,
                    silent=notify.is_silent(cfg, hb.kind),
                ):
                    st["last_heartbeat"] = now.isoformat()

    # Reminders run in both modes.
    for r in state_mod.due_reminders(st, cfg.reminder_offsets_minutes, now):
        text = notify.render_reminder(r["offset"], r["target"], cfg)
        log.info("reminder due: %s before %s", r["offset"], r["target"])
        if notify.send_telegram(cfg, text, dry_run=args.dry_run):
            state_mod.mark_reminder(st, r["target"], r["offset"], cfg.reminder_offsets_minutes)

    # Supervision (both modes): the daily Pathé check runs on a different
    # machine than the cloud reminder pass — alert if it stopped reporting.
    if state_mod.is_check_stale(st, cfg.stale_check_hours, now):
        key = f"stale:{st.get('last_check_ok')}"
        if not state_mod.already_sent(st, key):
            stale = Finding(
                kind="WATCHER_ERROR",
                key=key,
                confidence="high",
                title="No successful Pathé check recently",
                lines=[
                    f"Last successful check: {detect.fmt_dt(detect.parse_iso(st.get('last_check_ok')))}.",
                    f"Threshold: {cfg.stale_check_hours}h — the daily check job seems to have stopped"
                    " (machine off/asleep for days, launchd job unloaded, or git push failing).",
                    "Cloud reminders still run, but new Pathé signals are NOT being watched.",
                ],
                url=cfg.film_page_url,
            )
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
