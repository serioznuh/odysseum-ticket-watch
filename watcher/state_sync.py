"""Durable local marker for a state-rebase recovery failure episode."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

from . import state as state_mod
from .detect import TZ_PARIS

log = logging.getLogger(__name__)

DEFAULT_MARKER_PATH = ".cache/state-rebase-failure.json"


def failure_key(marker: dict[str, str]) -> str:
    """The normal alert-dedup key for one unresolved failure episode."""
    return f"state_sync_error:{marker['episode']}"


def _clean_detail(detail: str) -> str:
    return " ".join(detail.split())[:200]


def load_failure(path: str | Path = DEFAULT_MARKER_PATH) -> dict[str, str] | None:
    marker_path = Path(path)
    if not marker_path.exists():
        return None
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid state-sync failure marker {marker_path}: {exc}") from exc
    if not isinstance(marker, dict):
        raise TypeError(f"invalid state-sync failure marker {marker_path}: expected object")
    for field in ("episode", "first_seen", "detail"):
        if not isinstance(marker.get(field), str) or not marker[field]:
            raise RuntimeError(
                f"invalid state-sync failure marker {marker_path}: {field} is missing"
            )
    return {field: marker[field] for field in ("episode", "first_seen", "detail")}


def record_failure(
    detail: str,
    path: str | Path = DEFAULT_MARKER_PATH,
    *,
    now: datetime | None = None,
) -> dict[str, str]:
    marker_path = Path(path)
    try:
        existing = load_failure(marker_path)
    except (RuntimeError, TypeError):
        log.exception("replacing invalid state-sync failure marker")
        existing = None
    if existing is not None:
        return existing

    first_seen = (now or datetime.now(TZ_PARIS)).astimezone(TZ_PARIS).isoformat()
    marker = {
        "episode": first_seen,
        "first_seen": first_seen,
        "detail": _clean_detail(detail) or "state rebase recovery failed",
    }
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = marker_path.with_suffix(f"{marker_path.suffix}.tmp")
    temporary.write_text(json.dumps(marker, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, marker_path)
    return marker


def clear_failure(path: str | Path = DEFAULT_MARKER_PATH) -> None:
    try:
        Path(path).unlink()
    except FileNotFoundError:
        pass


def resolve_failure(
    state_path: str | Path,
    marker_path: str | Path = DEFAULT_MARKER_PATH,
) -> bool:
    """Clear a recovered episode only after its user alert was delivered.

    A pull can recover before Telegram does.  Keeping the marker until its
    normal Finding receipt reaches state.json makes the diagnostic durable
    across that ordering without inventing a second delivery channel.
    """
    marker = load_failure(marker_path)
    if marker is None:
        return False
    state = state_mod.load_state(state_path)
    if not state_mod.already_sent(state, failure_key(marker)):
        return False
    clear_failure(marker_path)
    return True


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    record = subparsers.add_parser("record")
    record.add_argument("--detail", required=True)
    record.add_argument("--path", default=DEFAULT_MARKER_PATH)
    resolve = subparsers.add_parser("resolve")
    resolve.add_argument("--state", default="state/state.json")
    resolve.add_argument("--path", default=DEFAULT_MARKER_PATH)
    args = parser.parse_args(argv)
    try:
        if args.command == "record":
            record_failure(args.detail, args.path)
        else:
            resolve_failure(args.state, args.path)
    except (OSError, RuntimeError, TypeError, state_mod.StateError) as exc:
        print(f"state-sync marker update failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
