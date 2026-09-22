"""Runtime-state synchronization, isolated from the deployed code checkout.

The live JSON is stored under ``.cache`` and transported on a dedicated Git
branch.  Git plumbing creates commits on that branch without checking it out,
so a state conflict, rejected push or corrupt remote state cannot dirty,
rebase or roll back the ``main`` worktree.  Reconciliation deliberately reuses
``state_merge.merge_states``: delivery receipts keep OTW-14's union semantics,
while owner/health fields retain its strict three-way behavior.

The durable failure marker predates this boundary.  Its path and finding key
remain stable so an in-flight failure episode is not re-alerted on upgrade.

Creating the shared ref is never ordinary work (OTW-30).  A confirmed absence
cannot be told apart from deletion of an established ref, so ``synchronize``
refuses it and the operator runs ``init`` (a genuinely new installation) or
``recover`` (an existing one, reconciling every surviving store first).
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
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from . import state as state_mod
from .detect import TZ_PARIS
from .state_merge import StateMergeError, ack_satisfied, merge_states

log = logging.getLogger(__name__)

DEFAULT_MARKER_PATH = ".cache/state-rebase-failure.json"
DEFAULT_STORE_PATH = ".cache/state-sync"
DEFAULT_STATE_REF = "refs/heads/runtime-state"
FETCHED_STATE_REF = "refs/otw/runtime-state"
STATE_REF_FILE = "state.json"
TRANSPORT_FAILURE_FILE = "transport-failure.json"
TRANSPORT_FAILURE_THRESHOLD = 3
DEFAULT_SEED_PATH = "state/state.json"
# `sync` exits with this on every confirmed absence of the shared ref: no local
# snapshot can show a receipt that lived only in the ref, so the startup wrappers
# must stop rather than let the watcher deliver from an unverified history.
# Mirrored by scripts/local-check.sh and .github/workflows/watch.yml; a test pins them.
BOOTSTRAP_REQUIRED_EXIT = 3

# Any of these proves this installation already notified the user: receipts and
# the reminder ladder directly, and the baselines that only advance once their
# alert was delivered.  An empty snapshot proves nothing either way.
_DELIVERY_EVIDENCE_FIELDS = (
    "alerts",
    "delivery_receipts",
    "outbox",
    "reminders_sent",
    "sales",
    "formats_seen",
    "shows_seen",
    "last_heartbeat",
)


class StateSyncError(RuntimeError):
    """The shared state could not be synchronized without risking history."""


class StateSyncTransportError(StateSyncError):
    """The remote Git transport is temporarily unavailable or rejected."""


class StateSyncRefAbsentError(StateSyncError):
    """The shared state ref is confirmed absent, which only an operator may fix.

    Ordinary delivery stops either way: whatever this clone holds locally, it
    cannot see a receipt that lived only in the ref — a reminder the cloud sent
    while the Mac slept, for instance — so sending again is exactly the risk.
    ``established`` only decides whether a durable marker is warranted, i.e.
    whether there is an installation history for the owner to be alerted about.
    """

    def __init__(self, message: str, *, established: bool) -> None:
        super().__init__(message)
        self.established = established


def has_delivery_evidence(state: dict) -> bool:
    """True when a snapshot proves an alert, reminder or heartbeat was sent."""
    return any(state.get(field) for field in _DELIVERY_EVIDENCE_FIELDS)


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


class _Store:
    """The resolved on-disk locations of one clone's runtime-state store."""

    def __init__(self, repo: str | Path, store: str | Path) -> None:
        self.repo = Path(repo).resolve()
        self.dir = _resolve_under(self.repo, store)
        self.live = self.dir / STATE_REF_FILE
        self.base = self.dir / "base.json"
        self.lock = self.dir / "sync.lock"
        self.dir.mkdir(parents=True, exist_ok=True)


def _store_evidence(paths: _Store) -> bool:
    """True when any local store shows this installation already notified.

    Unreadable local state counts: a file this boundary cannot parse is not
    proof that nothing was sent, so it is treated as an existing installation.
    """
    for path in (paths.live, paths.base):
        if not path.exists():
            continue
        try:
            state = _load_file(path, f"local state {path.name}")
        except StateSyncError:
            return True
        if has_delivery_evidence(state):
            return True
    return False


def _ref_absent_error(state_ref: str, established: bool) -> StateSyncRefAbsentError:
    """Report a confirmed absence, without recreating the ref from a seed."""
    if established:
        return StateSyncRefAbsentError(
            f"shared state ref {state_ref} is missing while this clone holds an "
            "installation history; refusing to recreate it silently and refusing to "
            "deliver, because a receipt that lived only in the ref cannot be seen "
            "here. Reconcile every surviving store and run "
            "`python -m watcher.state_sync recover` "
            "(README 'State bootstrap and recovery')",
            established=True,
        )
    return StateSyncRefAbsentError(
        f"shared state ref {state_ref} is missing and this clone holds no verified "
        "delivery history; refusing to deliver from an unverified seed. Run "
        "`python -m watcher.state_sync init` for a genuinely new installation, or "
        "`recover` for an existing one (README 'State bootstrap and recovery')",
        established=False,
    )


def reconcile_stores(stores: Sequence[dict]) -> dict:
    """Union what every surviving store proves, and quarantine what it doesn't.

    An older backup is never proof that an alert was *not* sent, so receipts,
    reminder rungs and the baselines they gate take OTW-14's union.  A delivery
    any store left in flight stays ``uncertain``, so recovery cannot turn an
    unknown Telegram outcome into a replay.  Stores are applied in order, so the
    freshest one (passed last) owns the plain health fields.
    """
    if not stores:
        raise StateSyncError("no runtime-state store to reconcile")
    try:
        snapshots = [state_mod.migrate_state(store) for store in stores]
        merged = snapshots[0]
        for snapshot in snapshots[1:]:
            # base == upstream keeps every union rule while letting the later
            # snapshot win the fields that have no domain semantics.
            merged = merge_states(merged, merged, snapshot)
    except (StateMergeError, state_mod.StateError) as exc:
        raise StateSyncError(
            f"surviving runtime-state stores could not be reconciled: {exc}"
        ) from exc
    merged["outbox"] = _reconcile_outbox(snapshots, merged)
    return state_mod.migrate_state(merged)


def _reconcile_outbox(snapshots: Sequence[dict], merged: dict) -> dict:
    """Rebuild the outbox from every store rather than fold it.

    The fold reads a record a later store never knew about as retired, which is
    right against a real base and wrong here: these stores share none.  Only a
    receipt or a satisfied acknowledgement retires work, so the union is the
    honest answer, and any store that left an attempt in flight makes the result
    ``uncertain``.  Record bodies come from the last store that carries them,
    matching the plain-field rule above.
    """
    rebuilt: dict[str, dict] = {}
    in_flight: dict[str, dict] = {}
    for snapshot in snapshots:
        for delivery_id, record in snapshot["outbox"].items():
            rebuilt[delivery_id] = deepcopy(record)
            if record["status"] in {"sending", "uncertain"}:
                in_flight.setdefault(delivery_id, record)
    for delivery_id, record in rebuilt.items():
        source = in_flight.get(delivery_id)
        if source is None:
            continue
        record["status"] = "uncertain"
        if "claim" not in record and "claim" in source:
            record["claim"] = deepcopy(source["claim"])
    return {
        delivery_id: record
        for delivery_id, record in rebuilt.items()
        if not _delivery_settled(merged, delivery_id, record)
    }


def _delivery_settled(merged: dict, delivery_id: str, record: dict) -> bool:
    """True when the reconciled state itself proves this attempt was delivered."""
    if any(
        receipt["delivery_id"] == delivery_id
        for receipt in merged["delivery_receipts"].values()
    ):
        return True
    return not record["force"] and ack_satisfied(merged, record["ack"])


def initialize(
    repo: str | Path = ".",
    *,
    store: str | Path = DEFAULT_STORE_PATH,
    seed: str | Path = DEFAULT_SEED_PATH,
    remote: str = "origin",
    state_ref: str = DEFAULT_STATE_REF,
) -> tuple[Path, bool]:
    """Create the shared state ref for a genuinely new installation.

    Refuses as soon as anything local proves the user was already notified: a
    missing ref is then a recovery, not a first run.  The live file is written
    only once a ref backs it, so a failed initialization can never leave an
    unverified seed behind for ordinary delivery.  Returns the live path and
    whether an independently created remote ref was adopted instead.
    """
    paths = _Store(repo, store)
    seed_path = _resolve_under(paths.repo, seed)

    with file_lock(paths.lock, blocking=True):
        # Live *and* base: a clone that lost only its live file still proves the
        # installation's history through the last incorporated base, and seeding
        # over that would make every alert it recorded eligible again.
        if _store_evidence(paths):
            raise StateSyncError(
                "a local store already holds delivery evidence, so this is not a new "
                "installation; reconcile every surviving store with `recover` instead"
            )
        existing = (
            _load_file(paths.live, "local live state") if paths.live.exists() else None
        )
        _, upstream = _remote_state(paths.repo, remote, state_ref)
        if upstream is not None:
            return _adopt_remote(paths, upstream), True

        initial = existing if existing is not None else _load_file(seed_path, "seed state")
        commit = _state_commit(paths.repo, initial, None)
        pushed = _git(
            paths.repo, "push", "--quiet", remote, f"{commit}:{state_ref}", check=False
        )
        if pushed.returncode != 0:
            _, upstream = _remote_state(paths.repo, remote, state_ref)
            if upstream is not None:
                # Another host created the ref between the check and the push.
                # Its history is authoritative and stays exactly as it is.
                return _adopt_remote(paths, upstream), True
            detail = (pushed.stderr or pushed.stdout).strip() or "push was rejected"
            raise StateSyncTransportError(
                f"could not create shared state ref {state_ref}: {detail}"
            )
        state_mod.save_state(paths.live, initial)
        state_mod.save_state(paths.base, initial)
        return paths.live, False


def recover(
    repo: str | Path = ".",
    *,
    store: str | Path = DEFAULT_STORE_PATH,
    remote: str = "origin",
    state_ref: str = DEFAULT_STATE_REF,
    extra: Sequence[str | Path] = (),
) -> tuple[Path, bool]:
    """Rebuild the shared ref for an existing installation, owner-approved.

    Every surviving store is reconciled — the live file, the last incorporated
    base, and any backup given in ``extra`` — so confirmed receipts survive and
    in-flight attempts stay quarantined.  The tracked seed is never a store: it
    would look like proof that nothing was ever sent.

    A ref that exists when the write happens is folded in as another store, at
    whatever content it holds *then*: a host that recreated the ref meanwhile
    may be the only place an uncertain attempt is recorded, and its history is
    left exactly as it is.  Local stores still own the plain health fields, so a
    racing seed cannot drop a live reminder ladder.
    """
    paths = _Store(repo, store)
    with file_lock(paths.lock, blocking=True):
        older: list[dict] = []
        for item in extra:
            path = _resolve_under(paths.repo, item)
            older.append(_load_file(path, f"recovery store {path}"))
        if paths.base.exists():
            older.append(_load_file(paths.base, "state sync base"))
        live = (
            _load_file(paths.live, "local live state") if paths.live.exists() else None
        )
        if not older and live is None:
            raise StateSyncError(
                "no surviving runtime-state store to recover from; the tracked seed is "
                "not proof that an alert was never sent. Supply a backup with --from, "
                "or use `init` for a genuinely new installation"
            )

        def reconcile(upstream: dict | None) -> dict:
            shared = [] if upstream is None else [upstream]
            local = [] if live is None else [live]
            return reconcile_stores([*older, *shared, *local])

        _, upstream = _remote_state(paths.repo, remote, state_ref)
        if upstream is None:
            recovered = reconcile(None)
            state_mod.save_state(paths.live, recovered)
            commit = _state_commit(paths.repo, recovered, None)
            pushed = _git(
                paths.repo, "push", "--quiet", remote, f"{commit}:{state_ref}", check=False
            )
            if pushed.returncode == 0:
                state_mod.save_state(paths.base, recovered)
                return paths.live, False
            # The push lost a race or failed outright. Re-read the ref: only what
            # is there now may be treated as the shared history.
            _, upstream = _remote_state(paths.repo, remote, state_ref)
            if upstream is None:
                detail = (pushed.stderr or pushed.stdout).strip() or "push was rejected"
                raise StateSyncTransportError(
                    f"could not restore shared state ref {state_ref}: {detail}"
                )

        # The ref exists — created concurrently by another host, or never lost.
        # Reconcile against it and record it as the incorporated base, so the
        # next ordinary sync reads nothing it holds as locally deleted and its
        # quarantined work cannot be dropped. Its history is not rewritten.
        recovered = reconcile(upstream)
        state_mod.save_state(paths.live, recovered)
        state_mod.save_state(paths.base, upstream)
        return paths.live, True


def _adopt_remote(paths: _Store, upstream: dict) -> Path:
    state_mod.save_state(paths.live, upstream)
    state_mod.save_state(paths.base, upstream)
    return paths.live


def synchronize(
    repo: str | Path = ".",
    *,
    store: str | Path = DEFAULT_STORE_PATH,
    remote: str = "origin",
    state_ref: str = DEFAULT_STATE_REF,
    push_attempts: int = 3,
) -> Path:
    """Reconcile local live state with the dedicated remote state ref.

    ``base.json`` is the last remote snapshot incorporated locally.  It is
    updated before a push, so a rejected push or crash leaves enough evidence
    for the next run to retry without treating its receipts as remote-owned.
    The live file is never replaced until every input passes schema validation
    and OTW-14's merge succeeds.  A confirmed absence of the shared ref is an
    operator condition, never a bootstrap: it is reported before anything local
    is touched, so live and base evidence survive it unchanged.
    """
    paths = _Store(repo, store)
    repo_path = paths.repo
    live_path = paths.live
    base_path = paths.base

    with file_lock(paths.lock, blocking=True):
        local = _load_file(live_path, "local live state") if live_path.exists() else None
        last_error = "shared state push did not succeed"
        for _ in range(max(1, push_attempts)):
            remote_commit, upstream = _remote_state(repo_path, remote, state_ref)
            if upstream is None:
                raise _ref_absent_error(state_ref, _store_evidence(paths))

            if local is None:
                local = upstream
            base = (
                _load_file(base_path, "state sync base")
                if base_path.exists()
                else upstream
            )

            try:
                merged = merge_states(base, upstream, local)
            except (StateMergeError, state_mod.StateError) as exc:
                raise StateSyncError(f"shared state reconciliation failed: {exc}") from exc

            # Persist the reconciled local copy before attempting the network
            # write. A rejected or interrupted push therefore cannot lose a
            # receipt produced by the just-finished watcher pass.
            state_mod.save_state(live_path, merged)
            state_mod.save_state(base_path, upstream)
            local = merged

            if merged == upstream:
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
    sync.add_argument("--remote", default="origin")
    sync.add_argument("--ref", default=DEFAULT_STATE_REF)
    sync.add_argument("--push-attempts", type=int, default=3)
    sync.add_argument(
        "--transport-failure-threshold",
        type=int,
        default=TRANSPORT_FAILURE_THRESHOLD,
    )
    sync.add_argument("--marker", default=DEFAULT_MARKER_PATH)
    init = subparsers.add_parser(
        "init", help="create the shared state ref for a genuinely new installation"
    )
    init.add_argument("--repo", default=".")
    init.add_argument("--store", default=DEFAULT_STORE_PATH)
    init.add_argument("--seed", default=DEFAULT_SEED_PATH)
    init.add_argument("--remote", default="origin")
    init.add_argument("--ref", default=DEFAULT_STATE_REF)
    restore = subparsers.add_parser(
        "recover", help="rebuild the shared state ref from every surviving store"
    )
    restore.add_argument("--repo", default=".")
    restore.add_argument("--store", default=DEFAULT_STORE_PATH)
    restore.add_argument("--remote", default="origin")
    restore.add_argument("--ref", default=DEFAULT_STATE_REF)
    restore.add_argument(
        "--from",
        dest="extra",
        action="append",
        default=[],
        metavar="PATH",
        help="an additional surviving state file (a backup, or another clone's live"
        " copy); repeatable. The tracked seed is never one",
    )
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
                    remote=args.remote,
                    state_ref=args.ref,
                    push_attempts=args.push_attempts,
                )
            except StateSyncRefAbsentError as exc:
                # Report and return the blocking code whatever else happens: both
                # wrappers key their stop on this exit status alone, so a failed
                # bookkeeping write must never downgrade it into "continue".
                print(f"state synchronization blocked: {exc}", file=sys.stderr)
                try:
                    # The transport answered, so this is not an outage streak.
                    clear_transport_failure(transport_streak)
                    if exc.established:
                        # An existing installation is blocked: keep a durable
                        # marker so the owner gets one loud alert as soon as
                        # recovery lets a pass run again. The marker detail is
                        # capped at 200 characters, so it carries the action
                        # rather than the full diagnostic printed above.
                        record_failure(
                            f"shared runtime-state ref {args.ref} is missing; run "
                            "`watcher.state_sync recover` after reconciling every "
                            "surviving store",
                            marker,
                        )
                except OSError:
                    log.exception("could not record the blocked state-sync condition")
                return BOOTSTRAP_REQUIRED_EXIT
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
        elif args.command in {"init", "recover"}:
            repo = Path(args.repo).resolve()
            try:
                if args.command == "init":
                    live_path, adopted = initialize(
                        repo,
                        store=args.store,
                        seed=args.seed,
                        remote=args.remote,
                        state_ref=args.ref,
                    )
                else:
                    live_path, adopted = recover(
                        repo,
                        store=args.store,
                        remote=args.remote,
                        state_ref=args.ref,
                        extra=args.extra,
                    )
            except (OSError, StateSyncError, state_mod.StateError) as exc:
                print(f"state {args.command} failed: {exc}", file=sys.stderr)
                return 1
            if adopted:
                print(
                    f"shared state ref {args.ref} already exists and was left intact; "
                    f"reconciled local state is at {live_path}. Run "
                    "`python -m watcher.state_sync sync` to union it upstream."
                )
            else:
                print(f"created shared state ref {args.ref} from {live_path}")
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
