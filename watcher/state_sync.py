"""Runtime-state synchronization, isolated from the deployed code checkout.

The live JSON is stored under ``.cache`` and transported on a dedicated Git
branch.  Git plumbing creates commits on that branch without checking it out,
so a state conflict, rejected push or corrupt remote state cannot dirty,
rebase or roll back the ``main`` worktree.  Reconciliation deliberately reuses
``state_merge.merge_states``: delivery receipts keep OTW-14's union semantics,
while owner/health fields retain its strict three-way behavior.

The durable failure marker predates this boundary.  Its path and finding key
remain stable so an in-flight failure episode is not re-alerted on upgrade.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import subprocess
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from . import state as state_mod
from .detect import TZ_PARIS
from .state_merge import StateMergeError, merge_states

log = logging.getLogger(__name__)

DEFAULT_MARKER_PATH = ".cache/state-rebase-failure.json"
DEFAULT_STORE_PATH = ".cache/state-sync"
DEFAULT_STATE_REF = "refs/heads/runtime-state"
FETCHED_STATE_REF = "refs/otw/runtime-state"
STATE_REF_FILE = "state.json"
TRANSPORT_FAILURE_FILE = "transport-failure.json"
TRANSPORT_FAILURE_THRESHOLD = 3


class StateSyncError(RuntimeError):
    """The shared state could not be synchronized without risking history."""


class StateSyncTransportError(StateSyncError):
    """The remote Git transport is temporarily unavailable or rejected."""


def _resolve_under(repo: Path, path: str | Path) -> Path:
    candidate = Path(path)
    return candidate if candidate.is_absolute() else repo / candidate


def _git(
    repo: Path,
    *args: str,
    input_text: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        env=env,
        input=input_text,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip() or "unknown git error"
        raise StateSyncError(f"git {' '.join(args[:2])} failed: {detail}")
    return result


def _decode_state(text: str, label: str) -> dict:
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-standard JSON constant {value}")

    try:
        loaded = json.loads(text, parse_constant=reject_constant)
        return state_mod.migrate_state(loaded)
    except (json.JSONDecodeError, ValueError, state_mod.StateError) as exc:
        raise StateSyncError(f"{label} is incompatible or invalid: {exc}") from exc


def _load_file(path: Path, label: str) -> dict:
    try:
        return _decode_state(path.read_text(encoding="utf-8"), label)
    except OSError as exc:
        raise StateSyncError(f"could not read {label} at {path}: {exc}") from exc


def _remote_state(
    repo: Path, remote: str, state_ref: str
) -> tuple[str | None, dict | None]:
    advertised = _git(repo, "ls-remote", "--exit-code", remote, state_ref, check=False)
    if advertised.returncode == 2:
        _git(repo, "update-ref", "-d", FETCHED_STATE_REF, check=False)
        return None, None
    if advertised.returncode != 0:
        detail = (advertised.stderr or advertised.stdout).strip()
        raise StateSyncTransportError(f"could not query shared state ref: {detail}")

    fetched = _git(
        repo,
        "fetch",
        "--quiet",
        remote,
        f"+{state_ref}:{FETCHED_STATE_REF}",
        check=False,
    )
    if fetched.returncode != 0:
        detail = (fetched.stderr or fetched.stdout).strip()
        raise StateSyncTransportError(f"could not fetch shared state ref: {detail}")
    commit = _git(repo, "rev-parse", FETCHED_STATE_REF).stdout.strip()
    shown = _git(repo, "show", f"{FETCHED_STATE_REF}:{STATE_REF_FILE}")
    return commit, _decode_state(shown.stdout, "shared state")


def _state_commit(repo: Path, state: dict, parent: str | None) -> str:
    rendered = json.dumps(state_mod.migrate_state(state), indent=2, sort_keys=True) + "\n"
    blob = _git(repo, "hash-object", "-w", "--stdin", input_text=rendered).stdout.strip()
    tree = _git(
        repo,
        "mktree",
        input_text=f"100644 blob {blob}\t{STATE_REF_FILE}\n",
    ).stdout.strip()
    args = ["commit-tree", tree]
    if parent is not None:
        args.extend(["-p", parent])
    now = datetime.now(timezone.utc)
    args.extend(["-m", f"state: sync {now:%Y-%m-%dT%H:%M:%SZ} [skip ci]"])
    env = os.environ.copy()
    env.update(
        {
            "GIT_AUTHOR_NAME": "ticket-watch-state-sync",
            "GIT_AUTHOR_EMAIL": "state-sync@localhost",
            "GIT_COMMITTER_NAME": "ticket-watch-state-sync",
            "GIT_COMMITTER_EMAIL": "state-sync@localhost",
        }
    )
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise StateSyncError(f"could not create shared state commit: {detail}")
    return result.stdout.strip()


@contextmanager
def file_lock(path: str | Path, *, blocking: bool) -> Iterator[bool]:
    """Hold an advisory process lock, yielding False for a busy nonblocking lock."""
    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        os.chmod(lock_path, 0o600)
        operation = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(lock_file.fileno(), operation)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def synchronize(
    repo: str | Path = ".",
    *,
    store: str | Path = DEFAULT_STORE_PATH,
    seed: str | Path = "state/state.json",
    remote: str = "origin",
    state_ref: str = DEFAULT_STATE_REF,
    push_attempts: int = 3,
) -> Path:
    """Reconcile local live state with the dedicated remote state ref.

    ``base.json`` is the last remote snapshot incorporated locally.  It is
    updated before a push, so a rejected push or crash leaves enough evidence
    for the next run to retry without treating its receipts as remote-owned.
    The live file is never replaced until every input passes schema validation
    and OTW-14's merge succeeds.
    """
    repo_path = Path(repo).resolve()
    store_path = _resolve_under(repo_path, store)
    live_path = store_path / STATE_REF_FILE
    base_path = store_path / "base.json"
    lock_path = store_path / "sync.lock"
    seed_path = _resolve_under(repo_path, seed)
    store_path.mkdir(parents=True, exist_ok=True)

    with file_lock(lock_path, blocking=True):
        local = _load_file(live_path, "local live state") if live_path.exists() else None
        last_error = "shared state push did not succeed"
        for _ in range(max(1, push_attempts)):
            remote_commit, upstream = _remote_state(repo_path, remote, state_ref)
            if local is None:
                if upstream is not None:
                    local = upstream
                elif seed_path.exists():
                    local = _load_file(seed_path, "bootstrap state")
                else:
                    raise StateSyncError(
                        "shared state does not exist and no valid bootstrap state is available"
                    )

            if base_path.exists():
                base = _load_file(base_path, "state sync base")
            elif upstream is not None:
                base = upstream
            else:
                base = local

            try:
                merged = (
                    merge_states(base, upstream, local)
                    if upstream is not None
                    else state_mod.migrate_state(local)
                )
            except (StateMergeError, state_mod.StateError) as exc:
                raise StateSyncError(f"shared state reconciliation failed: {exc}") from exc

            # Persist the reconciled local copy before attempting the network
            # write. A rejected or interrupted push therefore cannot lose a
            # receipt produced by the just-finished watcher pass.
            state_mod.save_state(live_path, merged)
            state_mod.save_state(base_path, upstream if upstream is not None else base)
            local = merged

            if upstream is not None and merged == upstream:
                state_mod.save_state(base_path, merged)
                return live_path

            commit = _state_commit(repo_path, merged, remote_commit)
            pushed = _git(
                repo_path,
                "push",
                "--quiet",
                remote,
                f"{commit}:{state_ref}",
                check=False,
            )
            if pushed.returncode == 0:
                state_mod.save_state(base_path, merged)
                return live_path
            last_error = (pushed.stderr or pushed.stdout).strip() or last_error

        raise StateSyncTransportError(
            f"could not push shared state after {max(1, push_attempts)} attempt(s): "
            f"{last_error}"
        )


def failure_key(marker: dict[str, str]) -> str:
    """The normal alert-dedup key for one unresolved failure episode."""
    return f"state_sync_error:{marker['episode']}"


def _clean_detail(detail: str) -> str:
    return " ".join(detail.split())[:200]


def load_transport_failure(path: str | Path) -> dict[str, object] | None:
    streak_path = Path(path)
    if not streak_path.exists():
        return None
    try:
        streak = json.loads(streak_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid state-sync transport streak {streak_path}: {exc}") from exc
    if not isinstance(streak, dict):
        raise TypeError(f"invalid state-sync transport streak {streak_path}: expected object")
    count = streak.get("count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise RuntimeError(
            f"invalid state-sync transport streak {streak_path}: count is invalid"
        )
    for field in ("first_seen", "detail"):
        if not isinstance(streak.get(field), str) or not streak[field]:
            raise RuntimeError(
                f"invalid state-sync transport streak {streak_path}: {field} is missing"
            )
    return {
        "count": count,
        "first_seen": streak["first_seen"],
        "detail": streak["detail"],
    }


def record_transport_failure(
    detail: str,
    path: str | Path,
    *,
    threshold: int = TRANSPORT_FAILURE_THRESHOLD,
    now: datetime | None = None,
) -> dict[str, object]:
    """Advance a capped local streak for consecutive Git transport failures."""
    streak_path = Path(path)
    try:
        previous = load_transport_failure(streak_path)
    except (RuntimeError, TypeError):
        log.exception("replacing invalid state-sync transport streak")
        previous = None
    limit = max(1, threshold)
    count = min(int(previous["count"]) + 1, limit) if previous else 1
    first_seen = (
        str(previous["first_seen"])
        if previous
        else (now or datetime.now(TZ_PARIS)).astimezone(TZ_PARIS).isoformat()
    )
    streak = {
        "count": count,
        "first_seen": first_seen,
        "detail": _clean_detail(detail) or "Git transport failed",
    }
    if previous == streak:
        return streak
    streak_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = streak_path.with_suffix(f"{streak_path.suffix}.tmp")
    temporary.write_text(json.dumps(streak, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, streak_path)
    return streak


def clear_transport_failure(path: str | Path) -> None:
    try:
        Path(path).unlink()
    except FileNotFoundError:
        pass


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
        "detail": _clean_detail(detail) or "runtime state synchronization failed",
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
    sync = subparsers.add_parser("sync")
    sync.add_argument("--repo", default=".")
    sync.add_argument("--store", default=DEFAULT_STORE_PATH)
    sync.add_argument("--seed", default="state/state.json")
    sync.add_argument("--remote", default="origin")
    sync.add_argument("--ref", default=DEFAULT_STATE_REF)
    sync.add_argument("--push-attempts", type=int, default=3)
    sync.add_argument(
        "--transport-failure-threshold",
        type=int,
        default=TRANSPORT_FAILURE_THRESHOLD,
    )
    sync.add_argument("--marker", default=DEFAULT_MARKER_PATH)
    locked = subparsers.add_parser("locked")
    locked.add_argument("--lock", default=".cache/local-check.lock")
    locked.add_argument("child", nargs=argparse.REMAINDER)
    record = subparsers.add_parser("record")
    record.add_argument("--detail", required=True)
    record.add_argument("--path", default=DEFAULT_MARKER_PATH)
    resolve = subparsers.add_parser("resolve")
    resolve.add_argument("--state", default=f"{DEFAULT_STORE_PATH}/{STATE_REF_FILE}")
    resolve.add_argument("--path", default=DEFAULT_MARKER_PATH)
    args = parser.parse_args(argv)
    try:
        if args.command == "sync":
            repo = Path(args.repo).resolve()
            marker = _resolve_under(repo, args.marker)
            transport_streak = _resolve_under(repo, args.store) / TRANSPORT_FAILURE_FILE
            try:
                live_path = synchronize(
                    repo,
                    store=args.store,
                    seed=args.seed,
                    remote=args.remote,
                    state_ref=args.ref,
                    push_attempts=args.push_attempts,
                )
            except StateSyncTransportError as exc:
                streak = record_transport_failure(
                    str(exc),
                    transport_streak,
                    threshold=args.transport_failure_threshold,
                )
                count = int(streak["count"])
                threshold = max(1, args.transport_failure_threshold)
                if count >= threshold:
                    record_failure(
                        f"runtime state transport failed {count} consecutive times: {exc}",
                        marker,
                    )
                print(
                    f"state synchronization transport failed ({count}/{threshold}): {exc}",
                    file=sys.stderr,
                )
                return 1
            except (OSError, StateSyncError, state_mod.StateError) as exc:
                clear_transport_failure(transport_streak)
                record_failure(f"runtime state synchronization failed: {exc}", marker)
                print(f"state synchronization failed: {exc}", file=sys.stderr)
                return 1
            clear_transport_failure(transport_streak)
            resolve_failure(live_path, marker)
        elif args.command == "locked":
            child: Sequence[str] = args.child
            if child and child[0] == "--":
                child = child[1:]
            if not child:
                parser.error("locked requires a child command after --")
            with file_lock(args.lock, blocking=False) as acquired:
                if not acquired:
                    print("local check already running; skipping overlapping firing", file=sys.stderr)
                    return 0
                env = os.environ.copy()
                env["OTW_LOCAL_CHECK_LOCKED"] = "1"
                return subprocess.run(list(child), env=env, check=False).returncode
        elif args.command == "record":
            record_failure(args.detail, args.path)
        else:
            resolve_failure(args.state, args.path)
    except (OSError, RuntimeError, TypeError, state_mod.StateError) as exc:
        print(f"state-sync marker update failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
