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

Every child process started here waits for a bounded time (OTW-29).  The local
wrapper holds one overlap lock across deployment, both state syncs and the
watcher, so a Git child that never answers used to silence the reminder ladder
for every later firing too.  ``bounded`` and ``locked`` supply that boundary to
the shell wrapper, which is why no platform ``timeout`` binary is required.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import signal
import subprocess
import sys
import time
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

# OTW-29 bounds every child this module starts.  One Git invocation gets this
# long, network round trips included; a wedged fetch or push is the case that
# matters and it is reported as a transport failure, never as a new alert path.
GIT_TIMEOUT_SECONDS = 45.0
# The wrapper's deployment pull, which can legitimately transfer more than a
# state blob.  Deployment stays first: exceeding this is a failed deployment, so
# the firing continues on the installed code exactly as before.
DEPLOY_TIMEOUT_SECONDS = 90.0
# Total allowance for stopping one supervised tree: every SIGTERM → SIGKILL
# round, retry and drain of a single invocation draws from this one window, so a
# level's whole cleanup takes at most this long. Its first half is spent waiting
# after SIGTERM, which is the window a level below gets to finish in.
CLEANUP_BUDGET_SECONDS = 10.0
# The nested budget for one Git tree, and the headroom for recording a survivor
# afterwards. Both must fit in half of the outer budget above — the time the
# supervisor spends after SIGTERM before it escalates to SIGKILL — or a sync
# stopped from above would be killed mid-cleanup and its sessioned Git group,
# which no other level can see, would go unrecorded. A test pins the arithmetic.
GIT_CLEANUP_BUDGET_SECONDS = 3.0
SURVIVOR_RECORD_ALLOWANCE_SECONDS = 1.0
# Overall deadline for one local firing: deployment, both syncs and the watcher
# together.  Two launchd intervals — far more than a healthy pass needs (its own
# polling budget is 0.8 of one interval) yet finite, so a hung tree costs the
# reminder ladder two firings instead of every firing until someone notices.
LOCAL_RUN_DEADLINE_SECONDS = 2 * state_mod.LOCAL_FIRING_INTERVAL_MINUTES * 60
# `locked`/`bounded` exit with this when a deadline, not the child, ended the
# run.  Distinct from BOOTSTRAP_REQUIRED_EXIT so the wrappers' blocking
# condition stays unambiguous, and non-zero so the launchd log is actionable.
LOCAL_RUN_TIMEOUT_EXIT = 4
# Rounds of SIGTERM/SIGKILL before a tree is declared unstoppable.
CLEANUP_ATTEMPTS = 3
# Exit status when a supervised tree outlived every one of those rounds. An
# advisory lock cannot outlive the process that holds it, so the refusal has to
# be durable instead: the surviving group is recorded next to the lock and the
# next firing stops on it rather than starting a second writer beside it. It
# clears itself as soon as that group is really gone.
UNCONFIRMED_TREE_EXIT = 5
UNCONFIRMED_TREE_SUFFIX = ".unconfirmed"
DEFAULT_LOCK_PATH = ".cache/local-check.lock"
# Worse than the above, and deliberately told apart from it: the group is still
# running and *no* durable record of it could be written, so the next firing is
# not protected by anything. Returning the ordinary blocked status here would
# claim a guard that does not exist; this one asks for a human.
UNGUARDED_TREE_EXIT = 6
# Every status a startup wrapper must stop on instead of carrying on: each keeps a
# firing from delivering or from putting a second writer beside something live.
# scripts/local-check.sh and .github/workflows/watch.yml both block on all of
# them, and a test pins that.
WRAPPER_BLOCKING_EXITS = (
    BOOTSTRAP_REQUIRED_EXIT,
    UNCONFIRMED_TREE_EXIT,
    UNGUARDED_TREE_EXIT,
)

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


class StateSyncCleanupError(StateSyncError):
    """A supervised process tree outlived every attempt to stop it.

    The work itself may well have succeeded; what failed is the guarantee that
    this invocation leaves nothing of its own running.  It is reported rather
    than swallowed because the caller is about to release the overlap lock.
    """

    def __init__(self, message: str, *, pgid: int) -> None:
        super().__init__(message)
        self.pgid = pgid


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


def unconfirmed_tree_path(lock: str | Path) -> Path:
    """Where a firing records a tree it could not confirm stopped."""
    path = Path(lock)
    return path.with_name(path.name + UNCONFIRMED_TREE_SUFFIX)


_SURVIVOR_MARKER: Path | None = None


def use_survivor_marker(path: str | Path) -> None:
    """Point every bounded child in this process at one survivor record.

    Set once from the CLI, so a Git child started deep inside a sync records a
    surviving group in the very place the wrapper's next firing looks.
    """
    global _SURVIVOR_MARKER
    _SURVIVOR_MARKER = Path(path)


def _lock_for_marker(marker: Path) -> Path | None:
    """The lock file a survivor marker belongs to, so both sinks stay reachable."""
    if marker.name.endswith(UNCONFIRMED_TREE_SUFFIX):
        return marker.with_name(marker.name[: -len(UNCONFIRMED_TREE_SUFFIX)])
    return None


def survivor_marker_path(explicit: str | Path | None = None) -> Path:
    if explicit is not None:
        return Path(explicit)
    if _SURVIVOR_MARKER is not None:
        return _SURVIVOR_MARKER
    return _resolve_under(Path.cwd(), unconfirmed_tree_path(DEFAULT_LOCK_PATH))


def _signal_group(pid: int, signum: int) -> bool:
    """Signal one process group, reporting whether anything signalable is left.

    The supervised child leads its own session, so ``pid`` is also its process
    group id and that group is exactly the tree this invocation owns.  EPERM
    counts as gone alongside ESRCH: a group whose only remaining member is the
    unreaped child reports EPERM, while one live member still answers 0
    (measured on macOS), so nothing still running is ever read as stopped.
    """
    try:
        os.killpg(pid, signum)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def _drain_group(pid: int, *, grace: float) -> bool:
    """True once nothing in the owned process group is running any more."""
    deadline = time.monotonic() + max(0.0, grace)
    while True:
        if not _signal_group(pid, 0):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


class _CleanupWindow:
    """One bounded allowance for stopping this invocation's tree.

    Every path that may need cleanup — the deadline, the signal handler, the final
    confirmation, each retry round — draws from this same window, so their worst
    cases cannot add up past the allowance the level above grants before its own
    SIGKILL.  That matters most for a state sync: it has to finish stopping its
    sessioned Git group *and* record the survivor before the supervisor above it
    escalates, because that supervisor can neither see nor record that group.

    The window opens on first use, so a healthy invocation never starts a clock,
    and ``first`` is the SIGTERM half: the part spent waiting before SIGKILL.
    """

    def __init__(self, budget: float) -> None:
        self.budget = max(0.1, budget)
        self._closes_at: float | None = None

    def remaining(self) -> float:
        if self._closes_at is None:
            self._closes_at = time.monotonic() + self.budget
        return max(0.0, self._closes_at - time.monotonic())

    def first(self) -> float:
        return min(self.remaining(), self.budget / 2)


def _stop_group(pid: int, *, window: _CleanupWindow) -> bool:
    """SIGTERM then SIGKILL the owned process group, reporting whether it went.

    This is the one escalation every path uses — the deadline, the final
    confirmation, and the signal handler.  It only signals and sleeps, never
    touching ``Popen`` state the main thread may be inside, so running it from a
    handler cannot deadlock against the wait it interrupts.
    """
    _signal_group(pid, signal.SIGTERM)
    if _drain_group(pid, grace=window.first()):
        return True
    _signal_group(pid, signal.SIGKILL)
    return _drain_group(pid, grace=window.remaining())


def _stop_tree(
    process: subprocess.Popen, *, window: _CleanupWindow
) -> tuple[str | None, str | None]:
    """Stop the owned tree and reap the child, for the main thread only.

    Same escalation as ``_stop_group``, plus the reap that only the owner of the
    ``Popen`` may do: the grandchildren a hung Git leaves behind (``ssh``,
    ``git-remote-https``) go with it, and nothing outside the tree can.
    """
    captured: tuple[str | None, str | None] = (None, None)
    for signum, wait in (
        (signal.SIGTERM, window.first),
        (signal.SIGKILL, window.remaining),
    ):
        _signal_group(process.pid, signum)
        try:
            captured = process.communicate(timeout=max(0.1, wait()))
            break
        except subprocess.TimeoutExpired:
            continue
    if process.poll() is None:
        # Something outside the group is holding a pipe open. The child is dead;
        # reap it without waiting on its output.
        try:
            process.wait(timeout=max(0.1, window.remaining()))
        except subprocess.TimeoutExpired:
            log.error("supervised child %s did not exit after SIGKILL", process.pid)
    return captured


def _confirm_stopped(
    process: subprocess.Popen,
    *,
    window: _CleanupWindow,
    attempts: int = CLEANUP_ATTEMPTS,
) -> None:
    """Leave nothing of the owned tree running, whatever ended the wait.

    The child is reaped by now, but it may have left part of its own tree behind,
    and a signal may have arrived mid-cleanup.  The escalation therefore runs
    again, up to ``attempts`` times — a process still in uninterruptible I/O can
    outlive one SIGKILL round without being unkillable — but always inside the one
    window, never ``attempts`` times its length.

    Raising is the point of the last line: the caller holds the overlap lock, and
    returning normally would let it release that lock and report a finished run
    while a writer of this firing is still alive.  Nothing here may be *presumed*
    stopped — only observed stopped.
    """
    if _drain_group(process.pid, grace=window.first()):
        return
    for _ in range(max(1, attempts)):
        if _stop_group(process.pid, window=window):
            return
        if window.remaining() <= 0:
            break
    raise StateSyncCleanupError(
        f"process group {process.pid} was still running after SIGTERM/SIGKILL "
        f"within {window.budget:g}s",
        pgid=process.pid,
    )


@contextmanager
def _stop_tree_on_signal(
    supervised: list[subprocess.Popen],
    *,
    window: _CleanupWindow,
    received: list[int],
    marker: Path,
    what: str,
) -> Iterator[None]:
    """Take the owned tree down with this supervisor when it is asked to stop.

    launchd stops a job with SIGTERM, a manual run ends with Ctrl-C, and the
    overall deadline signals a whole firing's group — none of which reach a child
    that leads its own session, so without this a state sync would die and leave
    its Git child and remote helper running while the lock looks free.  The
    handler stops the tree and only records the signal; the caller re-raises it
    once the tree is confirmed gone, so the supervisor can never exit first.

    When the tree does *not* go, waiting is the wrong thing to do next: a
    surviving Git child holding a captured pipe keeps ``communicate`` blocked
    until the level above escalates to SIGKILL, and this process would then die
    without recording anything.  So the handler records the survivor there and
    then — the same durable record every other path writes — and raises, which
    ends the wait and lets the caller report it.

    ``supervised`` is filled in by the caller rather than passed by value, so the
    handler is already in place while the child is being started: a signal in that
    instant finds nothing to stop and is acted on as soon as there is a child.
    """

    def handler(signum: int, frame: object) -> None:
        received.append(signum)
        for process in supervised:
            if _stop_group(process.pid, window=window):
                continue
            failure = StateSyncCleanupError(
                f"process group {process.pid} was still running after the stop "
                f"signal {signum} asked for",
                pgid=process.pid,
            )
            _record_survivor(
                failure.pgid,
                _survivor_detail(failure, what),
                marker,
                lock=_lock_for_marker(marker),
            )
            raise failure

    installed: dict[int, object] = {}
    try:
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            installed[signum] = signal.signal(signum, handler)
    except ValueError:
        # Not the main thread: the caller keeps whatever handling it has.
        pass
    try:
        yield
    finally:
        for signum, previous in installed.items():
            signal.signal(signum, previous)


def _survivor_detail(exc: StateSyncCleanupError, what: str) -> str:
    return f"{what} could not stop its own process tree: {exc}"


def _record_survivor(
    pgid: int,
    detail: str,
    marker: Path,
    *,
    lock: Path | None = None,
) -> bool:
    """Write the durable record of a surviving tree, reporting whether it landed.

    Safe from a signal handler: it touches no ``Popen`` state the main thread may
    be inside, and a write that fails is reported rather than raised, because the
    caller has its own, stronger answer for that case.  When the marker cannot be
    written and a lock file is already there, the record goes into that file
    instead — rewriting it needs no new inode and no directory change.
    """
    try:
        record_unconfirmed_tree(marker, pgid, detail)
        return True
    except OSError as failure:
        # Retried on a short loop, so the reason without a traceback each round:
        # the operator message on stderr carries what to do about it.
        log.error(
            "could not record surviving process group %s at %s: %s",
            pgid,
            marker,
            failure,
        )
    if lock is None:
        return False
    try:
        record_survivor_in_lock(lock, pgid, detail)
    except OSError as failure:
        log.error("could not record process group %s in %s: %s", pgid, lock, failure)
        return False
    return True


def _die_by_signal(signum: int) -> None:
    """Exit the way the signal asked, now that the owned tree has stopped.

    Deferring the death this far is the point: the process that holds the overlap
    lock outlives every child it started, so the next firing cannot see the lock
    released while a writer of this one is still alive.
    """
    log.error("stopped by signal %s after stopping this invocation's children", signum)
    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)


def _run_bounded(
    command: Sequence[str],
    *,
    timeout: float,
    cleanup_budget: float | None = None,
    cwd: str | Path | None = None,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
    capture: bool = True,
    survivor_marker: str | Path | None = None,
) -> tuple[subprocess.CompletedProcess[str], bool]:
    """Run a child under a finite wait, owning the whole tree it starts.

    Returns the completed process and whether the deadline — rather than the
    child — ended it.  Every way this call can end stops that tree first: the
    deadline, the child's own exit, and a signal aimed at this process, which is
    re-raised only once the tree is gone.  A supervisor holding the overlap lock
    therefore never releases it while a writer of its own is still alive, and a
    tree that will not stop is recorded before this returns or raises, whichever
    path it takes.
    """
    window = _CleanupWindow(
        CLEANUP_BUDGET_SECONDS if cleanup_budget is None else cleanup_budget
    )
    marker = survivor_marker_path(survivor_marker)
    what = _clean_detail(" ".join(command))
    timed_out = False
    interrupted: list[int] = []
    supervised: list[subprocess.Popen] = []
    # Installed for the child's whole lifetime — while it starts, while this waits
    # and while it is cleaned up — so no signal can end this process in a window
    # where part of the tree is still running.
    with _stop_tree_on_signal(
        supervised, window=window, received=interrupted, marker=marker, what=what
    ):
        process = subprocess.Popen(
            list(command),
            cwd=cwd,
            env=env,
            text=True,
            stdin=subprocess.PIPE if input_text is not None else None,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
            start_new_session=True,
        )
        supervised.append(process)
        if interrupted:
            # Stopped while the child was starting: stop it rather than begin a
            # wait this process has already been told to abandon.
            stdout, stderr = _stop_tree(process, window=window)
        else:
            try:
                stdout, stderr = process.communicate(input=input_text, timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                stdout, stderr = _stop_tree(process, window=window)
        _confirm_stopped(process, window=window)
    if interrupted:
        _die_by_signal(interrupted[0])
    # A tree that survived even SIGKILL leaves no status; report it as killed
    # rather than let None reach a caller that expects an exit code.
    returncode = -signal.SIGKILL if process.returncode is None else process.returncode
    completed = subprocess.CompletedProcess(
        list(command), returncode, stdout or "", stderr or ""
    )
    return completed, timed_out


def _exit_code(returncode: int) -> int:
    """A shell-style status, so a child killed by a signal stays non-zero."""
    return returncode if returncode >= 0 else 128 - returncode


def _git(
    repo: Path,
    *args: str,
    input_text: str | None = None,
    check: bool = True,
    env_extra: dict[str, str] | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    if env_extra:
        env.update(env_extra)
    limit = GIT_TIMEOUT_SECONDS if timeout is None else timeout
    result, timed_out = _run_bounded(
        ["git", *args],
        cwd=repo,
        env=env,
        input_text=input_text,
        timeout=limit,
        cleanup_budget=GIT_CLEANUP_BUDGET_SECONDS,
    )
    if timed_out:
        # A Git child that never answers is a transport condition, not a state
        # integrity one, so the existing capped streak covers it and a flaky
        # network invents no new loud alert. `check=False` callers are not
        # exempt: a timeout is no answer about what the shared ref holds.
        raise StateSyncTransportError(
            f"git {' '.join(args[:2])} did not answer within {limit:g}s; "
            "its process tree was stopped"
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
        try:
            _git(repo, "update-ref", "-d", FETCHED_STATE_REF, check=False)
        except StateSyncCleanupError:
            # A Git child that outlived cleanup must always reach the caller that
            # records it. Folding it into the absence path would report a missing
            # ref while leaving that group running and unrecorded, and a later
            # firing would then run beside it.
            raise
        except StateSyncError:
            # The absence is already confirmed and the wrappers stop on that
            # condition alone, so dropping a stale fetched ref must never be
            # able to downgrade it into a mere transport failure.
            log.exception("could not drop the stale fetched state ref")
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
    result = _git(
        repo,
        *args,
        check=False,
        env_extra={
            "GIT_AUTHOR_NAME": "ticket-watch-state-sync",
            "GIT_AUTHOR_EMAIL": "state-sync@localhost",
            "GIT_COMMITTER_NAME": "ticket-watch-state-sync",
            "GIT_COMMITTER_EMAIL": "state-sync@localhost",
        },
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


def record_unconfirmed_tree(
    path: str | Path, pgid: int, detail: str, *, now: datetime | None = None
) -> dict[str, object]:
    """Remember a surviving tree, so the next firing stays out of its way."""
    record = {
        "pgid": pgid,
        "recorded_at": (now or datetime.now(TZ_PARIS)).astimezone(TZ_PARIS).isoformat(),
        "detail": _clean_detail(detail) or "a supervised process tree could not be stopped",
    }
    marker = Path(path)
    marker.parent.mkdir(parents=True, exist_ok=True)
    temporary = marker.with_suffix(f"{marker.suffix}.tmp")
    temporary.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, marker)
    return record


def record_survivor_in_lock(
    lock: str | Path, pgid: int, detail: str, *, now: datetime | None = None
) -> dict[str, object]:
    """Write the survivor record into the lock file this firing already holds.

    The last resort when the marker beside it cannot be written: rewriting a file
    that already exists needs no new inode and no directory change, so it can
    still land where creating that marker cannot.  The lock file is only ever
    truncated and rewritten here, never created and never removed — a lock nobody
    deletes is the whole point.
    """
    record = {
        "pgid": pgid,
        "recorded_at": (now or datetime.now(TZ_PARIS)).astimezone(TZ_PARIS).isoformat(),
        "detail": _clean_detail(detail) or "a supervised process tree could not be stopped",
    }
    handle = os.open(str(lock), os.O_WRONLY | os.O_TRUNC)  # never O_CREAT
    try:
        os.write(handle, (json.dumps(record, sort_keys=True) + "\n").encode("utf-8"))
    finally:
        os.close(handle)
    return record


def _clear_lock_record(path: Path) -> None:
    """Drop a stale record from the lock file without ever removing the file."""
    with path.open("r+", encoding="utf-8") as handle:
        handle.truncate(0)


def _survivor_from(path: Path, clear) -> dict[str, object] | None:
    """One source's record, while the group it names may still be running."""
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            return None
        record = json.loads(text)
        pgid = record["pgid"]
        # 0 is deliberate: the outermost supervisor writes it when a nested level
        # reported a survivor it could not name. Nothing can prove such a group is
        # gone, so that record never self-clears — see below.
        if isinstance(pgid, bool) or not isinstance(pgid, int) or pgid < 0 or pgid == 1:
            raise ValueError(f"invalid process group {pgid!r}")
    except (OSError, json.JSONDecodeError, TypeError, KeyError, ValueError) as exc:
        return {
            "pgid": 0,
            "recorded_at": "",
            "source": str(path),
            "detail": f"unreadable survivor record {path} ({exc}); it cannot prove "
            "the tree stopped",
        }
    if pgid == 0 or _signal_group(int(pgid), 0):
        # An unnamed survivor keeps blocking: only a human can decide it is gone,
        # and `_signal_group(0, ...)` would mean *this* process's own group.
        record["source"] = str(path)
        return record
    clear(path)
    return None


def surviving_tree(
    path: str | Path, lock: str | Path | None = None
) -> dict[str, object] | None:
    """The recorded tree while it may still be running, else None.

    A recorded group that no longer answers is gone for good, so the record is
    cleared and the next firing proceeds normally — this refusal heals itself.
    One that still answers keeps the firing out: starting a second writer beside
    it is exactly the race the overlap lock exists to prevent.  A record this
    boundary cannot read is not proof that anything stopped, so it fails closed.

    Both places a firing can leave that record are read: the marker beside the
    lock, and the lock file itself when the marker could not be written.
    """
    found = _survivor_from(Path(path), clear_unconfirmed_tree)
    if found is not None or lock is None:
        return found
    return _survivor_from(Path(lock), _clear_lock_record)


def clear_unconfirmed_tree(path: str | Path) -> None:
    try:
        Path(path).unlink()
    except FileNotFoundError:
        pass


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


@contextmanager
def _hold_through_signals(what: str) -> Iterator[list[int]]:
    """Keep a termination signal from ending this process while it *is* the guard.

    Until a survivor is recorded, this process is the only thing keeping the next
    firing out: it holds the overlap lock, or keeps the wrapper that holds it
    waiting on its child.  Dying here would drop that guard with nothing durable
    in its place, so a signal is noted and deferred instead.  The loop it wraps is
    bounded, so the deferral cannot be indefinite; only SIGKILL can cut it short,
    and nothing in this process can change that.
    """
    deferred: list[int] = []

    def handler(signum: int, frame: object) -> None:
        deferred.append(signum)
        log.error("deferring signal %s: %s has no durable guard yet", signum, what)

    installed: dict[int, object] = {}
    try:
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            installed[signum] = signal.signal(signum, handler)
    except ValueError:
        pass
    try:
        yield deferred
    finally:
        for signum, previous in installed.items():
            signal.signal(signum, previous)


def _report_unconfirmed_tree(
    exc: StateSyncCleanupError,
    marker: str | Path,
    what: str,
    *,
    lock: str | Path | None = None,
    hold: float = SURVIVOR_RECORD_ALLOWANCE_SECONDS,
    poll: float = 0.2,
) -> int:
    """Record the survivor and refuse, instead of reporting a finished run.

    A marker that was never written cannot stop the next firing, so a failed write
    is not something to log and move on from.  While it cannot be written, this
    process stays alive instead: for ``locked`` that keeps the overlap lock held,
    so the next firing skips as overlapping, and for a sync it keeps the wrapper
    above waiting on its child, which holds the same lock.  Staying is only useful
    while the survivor lives, and it is bounded either way, so an undying group
    plus an unwritable disk still ends in a loud non-zero exit rather than a
    process that never returns.  Signals stay handled for the whole of that wait —
    the same lifetime rule the supervised paths follow — because a default SIGTERM
    here would end the one guard in place.

    ``hold`` follows the same nesting discipline as the cleanup budget: it is the
    allowance the *caller* actually has left, never a fixed span.  The default is
    the small reserved recording allowance, which is what a nested level (a sync,
    a bounded pull) has before the supervisor above it escalates to SIGKILL —
    holding longer there would only get this process killed mid-hold, so its
    status would never reach the level that has to act on it.  Only the lock
    holder itself passes a longer one, and only as much as is left of the firing's
    documented lifetime.
    """
    detail = _survivor_detail(exc, what)
    marker_path = Path(marker)
    lock_path = None if lock is None else Path(lock)
    deadline = time.monotonic() + max(0.0, hold)
    recorded = False
    with _hold_through_signals(what) as deferred:
        while True:
            if _record_survivor(exc.pgid, detail, marker_path, lock=lock_path):
                recorded = True
                break
            if not _signal_group(exc.pgid, 0):
                # The group finally went while the write kept failing: there is
                # nothing left for the next firing to run into.
                break
            if time.monotonic() >= deadline:
                break
            # Holding on *is* the guard while the marker is missing.
            print(
                f"{detail}; the survivor record at {marker_path} cannot be written, "
                "so this process keeps the overlap lock while process group "
                f"{exc.pgid} is alive",
                file=sys.stderr,
            )
            time.sleep(max(0.05, poll))
    if deferred:
        # Reported rather than obeyed: this exit carries more than the signal does.
        print(
            f"deferred signal(s) {sorted(set(deferred))} while holding the lock for "
            f"process group {exc.pgid}",
            file=sys.stderr,
        )
    if recorded:
        print(
            f"{detail}. The next firing stops until process group {exc.pgid} is gone "
            f"(marker: {marker_path})",
            file=sys.stderr,
        )
        return UNCONFIRMED_TREE_EXIT
    if not _signal_group(exc.pgid, 0):
        # Nothing is left to run into, so this is an ordinary block after all:
        # the group went away while the writes kept failing.
        print(
            f"{detail}. Process group {exc.pgid} ended before it could be recorded, "
            "so nothing is left for the next firing to run into",
            file=sys.stderr,
        )
        return UNCONFIRMED_TREE_EXIT
    # The honest answer, and a different one: this exit may not be read as "the
    # next firing is blocked", because nothing durable says so.
    print(
        f"{detail}. Process group {exc.pgid} is still running and could be recorded "
        f"neither at {marker_path} nor in the lock file, so THE NEXT FIRING IS NOT "
        "PROTECTED: stop that group by hand (`ps -g`) and fix why those paths are "
        "unwritable before the watcher runs again",
        file=sys.stderr,
    )
    return UNGUARDED_TREE_EXIT


def _guard_unnamed_survivor(
    marker: Path,
    lock: Path,
    detail: str,
    *,
    hold: float,
    poll: float = 0.2,
) -> int:
    """Leave a block behind for a survivor a nested level could not record.

    Reached only on a nested ``UNGUARDED_TREE_EXIT``: something of that firing is
    still running and neither sink took it there.  This level is the outermost and
    the one holding the lock, so it is the last that can leave anything behind —
    releasing the lock as if nothing needed guarding is the one thing it may not
    do.  The group cannot be named from here, so the record says so: it blocks
    every later firing until a human clears it, because nothing can prove an
    unnamed group has gone.  While even that cannot be written, holding the lock is
    the guard, for as long as this firing's lifetime allows.
    """
    deadline = time.monotonic() + max(0.0, hold)
    with _hold_through_signals("local check") as deferred:
        while True:
            if _record_survivor(0, detail, marker, lock=lock):
                print(
                    f"{detail}. Recorded as an unnamed survivor: every later firing "
                    f"stops until you check what is still running and clear that "
                    f"record (delete the marker file, or empty the lock file; never "
                    f"delete the lock itself)",
                    file=sys.stderr,
                )
                return UNGUARDED_TREE_EXIT
            if time.monotonic() >= deadline:
                break
            print(
                f"{detail}; it can be recorded neither at {marker} nor in {lock}, so "
                "this process keeps the overlap lock while it retries",
                file=sys.stderr,
            )
            time.sleep(max(0.05, poll))
    if deferred:
        print(f"deferred signal(s) {sorted(set(deferred))} while holding the lock", file=sys.stderr)
    print(
        f"{detail}. It could be recorded nowhere, so THE NEXT FIRING IS NOT "
        "PROTECTED: find what is still running (`ps -ax`), stop it, and fix why "
        f"{marker} and {lock} are unwritable before the watcher runs again",
        file=sys.stderr,
    )
    return UNGUARDED_TREE_EXIT


def _child_command(
    child: Sequence[str], parser: argparse.ArgumentParser, command: str
) -> list[str]:
    if child and child[0] == "--":
        child = child[1:]
    if not child:
        parser.error(f"{command} requires a child command after --")
    return list(child)


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
    sync.add_argument(
        "--lock",
        default=DEFAULT_LOCK_PATH,
        help="the local overlap lock whose surviving-tree marker this shares, so a"
        " Git child this sync cannot stop keeps the next firing out",
    )
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
    locked.add_argument("--lock", default=DEFAULT_LOCK_PATH)
    locked.add_argument(
        "--deadline",
        type=float,
        default=LOCAL_RUN_DEADLINE_SECONDS,
        help="overall seconds for the whole locked run (deploy, syncs, watcher)",
    )
    locked.add_argument(
        "--cleanup-budget", type=float, default=CLEANUP_BUDGET_SECONDS
    )
    locked.add_argument("child", nargs=argparse.REMAINDER)
    bounded = subparsers.add_parser(
        "bounded",
        help="run one child under a finite wait, stopping its whole tree on timeout",
    )
    bounded.add_argument("--timeout", type=float, default=DEPLOY_TIMEOUT_SECONDS)
    bounded.add_argument("--lock", default=DEFAULT_LOCK_PATH)
    # Nested under `locked` (the wrapper's deployment pull), so it draws from the
    # nested budget, not the supervisor's: its cleanup plus the reserved recording
    # allowance has to fit in the window that supervisor waits before SIGKILL.
    bounded.add_argument(
        "--cleanup-budget", type=float, default=GIT_CLEANUP_BUDGET_SECONDS
    )
    bounded.add_argument("child", nargs=argparse.REMAINDER)
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
            # One place for every bounded child here to record a surviving group,
            # including a Git child started deep inside `synchronize`.
            lock_path = _resolve_under(repo, args.lock)
            unconfirmed = unconfirmed_tree_path(lock_path)
            use_survivor_marker(unconfirmed)
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
            except StateSyncCleanupError as exc:
                # A Git child this sync could not stop. It leads a session of its
                # own, so the wrapper above can neither see nor stop it: record it
                # where the next firing looks before it starts a second writer,
                # and leave one durable marker so the owner hears about a machine
                # that no longer honors SIGKILL.
                try:
                    record_failure(
                        "runtime state synchronization could not stop its Git child; "
                        f"process group {exc.pgid} is still running",
                        marker,
                    )
                except OSError:
                    log.exception("could not record the surviving Git child")
                # Nested under the wrapper's supervisor: this has only the
                # reserved recording allowance before that level SIGKILLs it, and
                # a status it never returns cannot stop the next firing.
                return _report_unconfirmed_tree(
                    exc,
                    unconfirmed,
                    "state synchronization",
                    lock=lock_path,
                    hold=SURVIVOR_RECORD_ALLOWANCE_SECONDS,
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
        elif args.command in {"init", "recover"}:
            repo = Path(args.repo).resolve()
            lock_path = _resolve_under(repo, DEFAULT_LOCK_PATH)
            unconfirmed = unconfirmed_tree_path(lock_path)
            use_survivor_marker(unconfirmed)
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
            except StateSyncCleanupError as exc:
                # These run in the production clone too, so a Git child they could
                # not stop has to be recorded here as well: the scheduled firing
                # that comes next must not start beside it.
                return _report_unconfirmed_tree(
                    exc, unconfirmed, f"state {args.command}", lock=lock_path
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
            child = _child_command(args.child, parser, "locked")
            lock_path = _resolve_under(Path.cwd(), args.lock)
            unconfirmed = unconfirmed_tree_path(lock_path)
            use_survivor_marker(unconfirmed)
            with file_lock(args.lock, blocking=False) as acquired:
                if not acquired:
                    print("local check already running; skipping overlapping firing", file=sys.stderr)
                    return 0
                # Holding the lock is not enough on its own: an earlier firing
                # may have ended without being able to stop its own tree, and an
                # advisory lock dies with the process that held it. That firing
                # recorded what survived, and this one stays out while it lives.
                survivor = surviving_tree(unconfirmed, lock_path)
                if survivor is not None:
                    # pgid 0 is the unnamed case: there is nothing to inspect by
                    # group, and nothing can prove it gone, so only a human clears it.
                    named = int(survivor["pgid"]) > 0
                    subject = (
                        f"process group {survivor['pgid']}"
                        if named
                        else "a process group it could not name"
                    )
                    inspect_with = (
                        f"`ps -g {survivor['pgid']}`" if named else "`ps -ax`"
                    )
                    print(
                        f"local check blocked: an earlier firing left {subject} running "
                        f"({survivor['detail']}). Starting a second writer beside it is "
                        "exactly what the overlap lock prevents. Inspect "
                        f"{inspect_with}, stop what is left, then clear that record in "
                        f"{survivor.get('source', unconfirmed)} — delete the marker "
                        "file, or empty the lock file; never delete the lock itself",
                        file=sys.stderr,
                    )
                    return UNCONFIRMED_TREE_EXIT
                env = os.environ.copy()
                env["OTW_LOCAL_CHECK_LOCKED"] = "1"
                # The lock is released only after _run_bounded has stopped and
                # drained this run's own tree, so the next firing can never
                # acquire it while an earlier writer is still alive.
                started = time.monotonic()
                try:
                    result, timed_out = _run_bounded(
                        child,
                        env=env,
                        capture=False,
                        timeout=args.deadline,
                        cleanup_budget=args.cleanup_budget,
                    )
                except StateSyncCleanupError as exc:
                    # This level *is* the lock holder, so holding on is a real
                    # guard and it may use whatever is left of the firing's
                    # documented lifetime — the deadline plus one cleanup
                    # allowance — and not a second more, so a hung tree still
                    # costs the reminder ladder two firings and no more.
                    spent = time.monotonic() - started
                    return _report_unconfirmed_tree(
                        exc,
                        unconfirmed,
                        "local check",
                        lock=lock_path,
                        hold=max(0.0, args.deadline + args.cleanup_budget - spent),
                    )
                if timed_out:
                    print(
                        f"local check exceeded its {args.deadline:g}s deadline; the run's "
                        "process tree was stopped. Saved state and receipts are intact; "
                        "check the Git remote and the launchd log",
                        file=sys.stderr,
                    )
                    return LOCAL_RUN_TIMEOUT_EXIT
                inner = _exit_code(result.returncode)
                if inner == UNGUARDED_TREE_EXIT and surviving_tree(
                    unconfirmed, lock_path
                ) is None:
                    # A nested level reported a survivor it could record nowhere.
                    # This level holds the lock and has the rest of the firing's
                    # lifetime, so it is the last chance to leave a block behind;
                    # passing the status through and releasing the lock would let
                    # the next firing start beside whatever is still running. An
                    # existing record is left alone: one that names its group is
                    # strictly better, because it can clear itself.
                    spent = time.monotonic() - started
                    return _guard_unnamed_survivor(
                        unconfirmed,
                        lock_path,
                        "a nested level of this local check left a process group "
                        "running that it could neither stop nor record",
                        hold=max(0.0, args.deadline + args.cleanup_budget - spent),
                    )
                return inner
        elif args.command == "bounded":
            child = _child_command(args.child, parser, "bounded")
            lock_path = _resolve_under(Path.cwd(), args.lock)
            unconfirmed = unconfirmed_tree_path(lock_path)
            use_survivor_marker(unconfirmed)
            try:
                result, timed_out = _run_bounded(
                    child,
                    capture=False,
                    timeout=args.timeout,
                    cleanup_budget=args.cleanup_budget,
                )
            except StateSyncCleanupError as exc:
                # Nested too (the wrapper's deployment pull runs under `locked`),
                # so the same reserved allowance applies.
                return _report_unconfirmed_tree(
                    exc,
                    unconfirmed,
                    " ".join(child),
                    lock=lock_path,
                    hold=SURVIVOR_RECORD_ALLOWANCE_SECONDS,
                )
            if timed_out:
                print(
                    f"`{' '.join(child)}` did not finish within {args.timeout:g}s; "
                    "its process tree was stopped",
                    file=sys.stderr,
                )
                return LOCAL_RUN_TIMEOUT_EXIT
            return _exit_code(result.returncode)
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
