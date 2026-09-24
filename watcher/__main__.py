"""CLI entry point.

Modes:
  check  — full pass: Pathé API + news feeds, alerts, reminders, heartbeat.
  remind — state-only pass (no Pathé/news/Cinesa requests): send due sale
           reminders and run supervision.
  remind --with-news — add cloud-safe news feeds; still no Pathé or Cinesa.

Usage:
  python -m watcher --mode check [--dry-run] [--verbose]
  python -m watcher --mode remind
  python -m watcher --mode remind --with-news
  python -m watcher --test-telegram
"""

# This module is deliberately thin (OTW-19): it parses arguments, loads config
# and state, decides what "now" means, and hands a RunContext to the
# coordinator in `watcher/runner.py`, which runs the pass as an ordered
# sequence of bounded jobs. The alert builders live in `watcher/alerts.py` and
# are re-exported below, which is how they have always been addressed.

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

from . import __version__, notify, runner, state_sync
from . import state as state_mod
from .alerts import (  # noqa: F401  (re-exported: the alert builders' public home)
    blind_since,
    build_cinesa_error_finding,
    build_cinesa_leak_finding,
    build_cinesa_recovered_finding,
    build_cloud_stale_finding,
    build_error_finding,
    build_heartbeat,
    build_recovered_finding,
    build_stale_finding,
    cinesa_label,
    fmt_duration,
    heartbeat_due,
    pathe_cause,
    record_pathe_failure,
    running_in_ci,
    short_dt,
    stale_period,
    summarize_pathe_error,
    watch_label,
)
from .config import load_config
from .detect import TZ_PARIS
from .jobs import RunContext

log = logging.getLogger("watcher")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="watcher", description=__doc__)
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("--state", default=None, help="override state file path")
    parser.add_argument("--mode", choices=["check", "remind"], default="check")
    parser.add_argument(
        "--with-news",
        action="store_true",
        help="remind mode: also scan cloud-safe news sources; never Pathé or Cinesa",
    )
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
    parser.add_argument(
        "--check-telegram",
        action="store_true",
        help="validate the Telegram bot and chat without sending a message",
    )
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--version", action="version", version=__version__)
    return parser


def delivery_coordinator(state_path: str, *, dry_run: bool):
    """The shared-reservation handle, when this pass delivers from shared state.

    Both wrappers run from the repository root against the synchronized store's
    live file, and every send there must first win a reservation on the shared
    ref (OTW-28). A dry run sends nothing; any other state file is not shared.
    """
    if dry_run:
        return None
    repo = Path.cwd()
    live = repo / state_sync.DEFAULT_STORE_PATH / state_sync.STATE_REF_FILE
    if Path(state_path).resolve() != live.resolve():
        log.info("state %s is not the synchronized store: delivery is not coordinated", state_path)
        return None
    return state_sync.DeliveryCoordinator(repo)


def run(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.bootstrap_state and args.dry_run:
        parser.error("--bootstrap-state cannot be combined with --dry-run")
    if args.with_news and args.mode != "remind":
        parser.error("--with-news requires --mode remind")

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

    if args.test_telegram:
        ok = notify.send_telegram(
            cfg,
            f"✅ <b>odysseum-ticket-watch</b> v{__version__} is talking to you.\n"
            f"Watching: {cfg.film_title} @ {cfg.cinema_name}",
            dry_run=args.dry_run,
        )
        return 0 if ok else 1

    if args.check_telegram:
        return 0 if notify.check_telegram_credentials(cfg) else 1

    if not args.dry_run and not (cfg.telegram_token and cfg.telegram_chat_id):
        log.error(
            "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set (or use --dry-run)."
        )
        return 1

    ctx = RunContext(
        cfg=cfg,
        state=st,
        # Resolved here, in this module's namespace, so the CLI stays the one
        # place that decides what "now" means for a pass.
        clock=lambda: datetime.now(TZ_PARIS),
        mode=args.mode,
        with_news=args.with_news,
        dry_run=args.dry_run,
        adaptive_cadence=args.adaptive_cadence,
        skip_if_checked_within=args.skip_if_checked_within,
        reminder_grace_minutes=args.reminder_grace_minutes,
        coordinator=delivery_coordinator(state_path, dry_run=args.dry_run),
    )
    return runner.execute(ctx, state_path)


if __name__ == "__main__":
    sys.exit(run())
