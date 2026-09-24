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

Permission to send is arbitrated here too (OTW-28).  ``reserve`` extends the
fetched tip of the shared ref with a reservation and pushes it as a plain
fast-forward, which the remote accepts only while that tip is still current:
a compare-and-swap that a host racing from an older tip cannot win.  The
policy — what may be reserved, and when — lives in ``watcher/delivery.py``.

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
import stat
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from . import state as state_mod
from .detect import TZ_PARIS
from .state_merge import (
    StateMergeError,
    ack_satisfied,
    merge_states,
    reservation_settled,
    resolve_reservation,
)

log = logging.getLogger(__name__)

DEFAULT_MARKER_PATH = ".cache/state-rebase-failure.json"
DEFAULT_STORE_PATH = ".cache/state-sync"
DEFAULT_STATE_REF = "refs/heads/runtime-state"
FETCHED_STATE_REF = "refs/otw/runtime-state"
STATE_REF_FILE = "state.json"
TRANSPORT_FAILURE_FILE = "transport-failure.json"
TRANSPORT_FAILURE_THRESHOLD = 3
DEFAULT_SEED_PATH = "state/state.json"
# This store's reservation-holder id (OTW-28). It lives beside the live file it
# describes: only a process of the same store can prove from that file that an
# earlier reservation of its own never reached Telegram. A fresh cloud runner
# gets a fresh store and so a fresh id, and never takes over a predecessor.
HOLDER_FILE = "holder"
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
# `locked` hands every level below it a pipe, whose fd number this variable names.
# A level that can write its survivor record nowhere on disk announces the group
# there as well: an exit status cannot reach the lock holder once the watchdog
# or a stop signal has ended the shell in between, and a pipe needs no disk.
SURVIVOR_CHANNEL_ENV = "OTW_SURVIVOR_FD"
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
    # A reservation may have been used for a send nobody has a receipt for yet.
    "reservations",
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
    group id and that group is exactly the tree this invocation owns.  A group
    whose remaining members are all unreaped zombies has nothing left running:
    macOS reports it as EPERM, which counts as gone alongside ESRCH, while Linux
    still answers 0 and is asked ``/proc`` instead.  One live member keeps the
    group alive on both, so nothing still running is ever read as stopped.
    """
    try:
        os.killpg(pid, signum)
    except (ProcessLookupError, PermissionError):
        return False
    return not _only_zombies_left(pid)


PROC_ROOT = Path("/proc")


def _group_member_states(pgid: int) -> list[str] | None:
    """The state letter of every process in ``pgid``, or None without ``/proc``.

    Reads each ``/proc/<pid>/stat``; the fields after the command name, which may
    itself contain spaces and parentheses, are state, ppid, pgrp.  A process that
    exits mid-scan is skipped.
    """
    try:
        entries = os.listdir(PROC_ROOT)
    except OSError:
        return None
    states: list[str] = []
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            text = (PROC_ROOT / entry / "stat").read_text(encoding="utf-8", errors="replace")
            fields = text[text.rindex(")") + 1 :].split()
            if int(fields[2]) == pgid:
                states.append(fields[0])
        except (OSError, ValueError, IndexError):
            continue
    return states


def _only_zombies_left(pgid: int) -> bool:
    """True only when ``/proc`` shows members in ``pgid`` and all are zombies.

    Anything short of that proof — no ``/proc`` (macOS), no member found, or any
    member in another state — answers False, so the caller keeps treating the
    group as alive.  The scan runs twice: a member that forked just before it
    became a zombie cannot hide from both passes.
    """
    for _ in range(2):
        states = _group_member_states(pgid)
        if not states or any(state not in ("Z", "X") for state in states):
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
def _handling_stop_signals(handler) -> Iterator[None]:
    """Route SIGINT, SIGTERM and SIGHUP to ``handler`` for the enclosed block.

    launchd stops a job with SIGTERM and a manual run ends with Ctrl-C.  Off the
    main thread handlers cannot be installed, and the caller keeps whatever
    handling it already has.
    """
    installed: dict[int, object] = {}
    try:
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            installed[signum] = signal.signal(signum, handler)
    except ValueError:
        pass
    try:
        yield
    finally:
        for signum, previous in installed.items():
            signal.signal(signum, previous)


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

    A stop signal aimed at a firing never reaches a child that leads its own
    session, so without this a state sync would die and leave its Git child and
    remote helper running while the lock looks free.  The handler stops the tree
    and only notes the signal; the caller re-raises it once the tree is confirmed
    gone, so the supervisor can never exit first.

    When the tree does *not* go, waiting on is wrong: a surviving Git child holding
    a captured pipe keeps ``communicate`` blocked until the level above SIGKILLs
    this process, with nothing recorded.  So the handler records the survivor
    there and then and raises, which ends the wait and lets the caller report it.

    ``supervised`` is filled in by the caller, so the handler is already in place
    while the child starts: a signal in that instant is acted on once it exists.
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

    with _handling_stop_signals(handler):
        yield


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
    be inside, and a failed write is reported rather than raised, because the
    caller has its own, stronger answer for that case.  The marker comes first;
    failing that, the lock file already there (rewriting it needs no new inode and
    no directory change).  When neither takes it, the group is announced to the
    lock holder, which can still record it after this process is gone.
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
    if lock is not None:
        try:
            record_survivor_in_lock(lock, pgid, detail)
            return True
        except OSError as failure:
            log.error("could not record process group %s in %s: %s", pgid, lock, failure)
    _announce_survivor(pgid)
    return False


def _announce_survivor(pgid: int) -> None:
    """Tell the lock holder about a group no durable record could hold.

    One non-blocking write, so it is safe from a signal handler, and silent on
    failure: the exit status still carries the same news wherever it can arrive.
    The fd is used only while it is still a pipe, never a file that reused its
    number after being closed somewhere in between.
    """
    raw = os.environ.get(SURVIVOR_CHANNEL_ENV)
    if not raw or pgid <= 0:
        return
    try:
        fd = int(raw)
        if stat.S_ISFIFO(os.fstat(fd).st_mode):
            os.write(fd, f"{pgid}\n".encode("ascii"))
    except (ValueError, OSError):
        pass


@contextmanager
def _survivor_channel() -> Iterator[tuple[int, int]]:
    """The pipe `locked` hands down for ``_announce_survivor``: (reader, writer).

    Both ends are non-blocking, so a level below can never stall on a full pipe
    and reading it back never waits for an EOF that a leftover holder would delay.
    """
    reader, writer = os.pipe()
    try:
        os.set_blocking(reader, False)
        os.set_blocking(writer, False)
        yield reader, writer
    finally:
        os.close(reader)
        os.close(writer)


def _announced_survivors(reader: int) -> list[int]:
    """Every distinct process group announced on the channel so far."""
    data = b""
    while True:
        try:
            chunk = os.read(reader, 4096)
        except BlockingIOError:
            break
        if not chunk:
            break
        data += chunk
    found: list[int] = []
    for token in data.split():
        try:
            pgid = int(token)
        except ValueError:
            continue
        if pgid > 1 and pgid not in found:
            found.append(pgid)
    return found


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
    pass_fds: Sequence[int] = (),
    on_signal: Callable[[], object] | None = None,
) -> tuple[subprocess.CompletedProcess[str], bool]:
    """Run a child under a finite wait, owning the whole tree it starts.

    Returns the completed process and whether the deadline — rather than the
    child — ended it.  Every way this call can end stops that tree first: the
    deadline, the child's own exit, and a signal aimed at this process, which is
    re-raised only once the tree is gone.  A supervisor holding the overlap lock
    therefore never releases it while a writer of its own is still alive, and a
    tree that will not stop is recorded before this returns or raises, whichever
    path it takes.  ``on_signal`` runs once the tree is gone and before the
    signal is re-raised, while the caller still holds whatever it holds.
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
            pass_fds=tuple(pass_fds),
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
        if on_signal is not None:
            on_signal()
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
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    if env_extra:
        env.update(env_extra)
    limit = GIT_TIMEOUT_SECONDS
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
    merged["reservations"] = _reconcile_reservations(snapshots, merged)
    return state_mod.migrate_state(merged)


def _reconcile_reservations(snapshots: Sequence[dict], merged: dict) -> dict:
    """Union every store's reservations, like the outbox above.

    The fold would read an entry a later store lacks as deleted, and dropping a
    live reservation during recovery would let a second host claim work whose
    first holder may already have sent it.  Only a proof of delivery retires one.
    """
    rebuilt: dict[str, dict] = {}
    for snapshot in snapshots:
        for logical_id, entry in snapshot["reservations"].items():
            rebuilt[logical_id] = deepcopy(
                resolve_reservation(rebuilt.get(logical_id), entry)
            )
    return {
        logical_id: entry
        for logical_id, entry in rebuilt.items()
        if not reservation_settled(merged, logical_id, entry)
    }


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


def reserve(
    repo: str | Path = ".",
    *,
    claim: Callable[[dict], dict | None],
    store: str | Path = DEFAULT_STORE_PATH,
    remote: str = "origin",
    state_ref: str = DEFAULT_STATE_REF,
    push_attempts: int = 3,
) -> bool:
    """Publish a delivery reservation on the shared ref, or report why not.

    ``claim`` is shown a copy of the shared tip and returns the reservation
    entries to add, or None to decline.  The entries are committed on top of
    exactly that tip and pushed as a fast-forward, so the remote accepts them
    only if nobody moved the ref in between: whoever pushes first wins, and a
    loser is re-shown the new tip — holding the winner's reservation — on its
    next attempt.  True means this call's entries are on the ref.

    Nothing local is written: the pass's own changes still travel with the
    post-run sync, whose merge honours what was reserved here.  Every Git wait
    is bounded (OTW-29); a timeout raises the ordinary transport error, which
    is *not* an answer — the push may have landed — so the caller must neither
    send nor treat the work as refused.  A confirmed absence of the ref raises
    as it does for ``synchronize`` (OTW-30): no send from an unverified history.
    """
    paths = _Store(repo, store)
    with file_lock(paths.lock, blocking=True):
        last_error = "reservation push was rejected"
        for _ in range(max(1, push_attempts)):
            remote_commit, upstream = _remote_state(paths.repo, remote, state_ref)
            if upstream is None:
                raise _ref_absent_error(state_ref, _store_evidence(paths))
            entries = claim(deepcopy(upstream))
            if entries is None:
                return False
            candidate = deepcopy(upstream)
            candidate["reservations"].update(deepcopy(entries))
            commit = _state_commit(paths.repo, candidate, remote_commit)
            pushed = _git(
                paths.repo,
                "push",
                "--quiet",
                remote,
                f"{commit}:{state_ref}",
                check=False,
            )
            if pushed.returncode == 0:
                return True
            # Rejected: the ref moved since it was fetched (or the remote
            # refused outright). Nothing landed; decide again from the new tip.
            last_error = (pushed.stderr or pushed.stdout).strip() or last_error
        raise StateSyncTransportError(
            f"delivery reservation not confirmed after {max(1, push_attempts)} "
            f"attempt(s): {last_error}"
        )


def store_holder(repo: str | Path = ".", store: str | Path = DEFAULT_STORE_PATH) -> str:
    """This store's reservation-holder id, created on first use.

    If it cannot be persisted the id is scoped to this process, which only ever
    makes the store more cautious: it then cannot prove an earlier reservation
    was its own, and waits for that one's receipt, release or expiry instead.
    """
    kind = "cloud" if os.environ.get("GITHUB_ACTIONS") else "local"
    path = _Store(repo, store).dir / HOLDER_FILE
    try:
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    except FileNotFoundError:
        pass
    except OSError:
        log.exception("could not read the reservation holder id at %s", path)
    holder = f"{kind}:{uuid.uuid4().hex[:16]}"
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(holder + "\n")
    except FileExistsError:
        return path.read_text(encoding="utf-8").strip() or holder
    except OSError:
        log.exception("could not persist the reservation holder id at %s", path)
    return holder


class DeliveryCoordinator:
    """The watcher's handle on shared delivery reservations (OTW-28).

    A Git child of a reservation that outlives every attempt to stop it is
    recorded exactly where the wrapper's next firing looks (OTW-29), and this
    coordinator then refuses every later reservation of the pass rather than
    start a second Git writer beside it; ``blocked_exit`` is the status the
    watcher exits with, which both startup wrappers treat as a hard stop.
    """

    def __init__(
        self,
        repo: str | Path = ".",
        *,
        store: str | Path = DEFAULT_STORE_PATH,
        remote: str = "origin",
        state_ref: str = DEFAULT_STATE_REF,
        lock: str | Path = DEFAULT_LOCK_PATH,
        holder: str | None = None,
    ) -> None:
        self.repo = Path(repo).resolve()
        self.store = store
        self.remote = remote
        self.state_ref = state_ref
        self.lock = lock
        self.holder = holder or store_holder(self.repo, store)
        self.blocked_exit: int | None = None

    def reserve(self, claim: Callable[[dict], dict | None]) -> bool:
        if self.blocked_exit is not None:
            raise StateSyncError(
                "an earlier reservation left a Git process group running; no further "
                "reservation starts beside it"
            )
        try:
            return reserve(
                self.repo,
                claim=claim,
                store=self.store,
                remote=self.remote,
                state_ref=self.state_ref,
            )
        except StateSyncCleanupError as exc:
            lock_path, marker = _survivor_paths(self.repo, self.lock)
            self.blocked_exit = _report_unconfirmed_tree(
                exc,
                marker,
                "delivery reservation",
                lock=lock_path,
                hold=SURVIVOR_RECORD_ALLOWANCE_SECONDS,
            )
            raise


def _survivor_record(pgid: int, detail: str, now: datetime | None) -> dict[str, object]:
    return {
        "pgid": pgid,
        "recorded_at": (now or datetime.now(TZ_PARIS)).astimezone(TZ_PARIS).isoformat(),
        "detail": _clean_detail(detail) or "a supervised process tree could not be stopped",
    }


def record_unconfirmed_tree(
    path: str | Path, pgid: int, detail: str, *, now: datetime | None = None
) -> dict[str, object]:
    """Remember a surviving tree, so the next firing stays out of its way."""
    record = _survivor_record(pgid, detail, now)
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
    record = _survivor_record(pgid, detail, now)
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
        # 0 is deliberate: the lock holder writes it for survivors one named
        # record cannot cover. Nothing can prove such a group is gone, so that
        # record never self-clears — see below.
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


def _hold_until_recorded(
    pgid: int,
    detail: str,
    marker: Path,
    lock: Path | None,
    *,
    hold: float,
    poll: float,
    what: str,
) -> bool:
    """Retry the survivor record while this process is the only guard left.

    Until the record lands, this process is what keeps the next firing out: it
    holds the overlap lock, or keeps the wrapper that holds it waiting on its
    child.  So it stays, retrying, and a termination signal is deferred rather
    than obeyed — dying here would drop the guard with nothing in its place.  The
    wait is bounded by ``hold``; only SIGKILL cuts it shorter.

    Returns True once the record landed.  A named group that goes away ends the
    wait early, since nothing is left to guard; an unnamed one (``pgid`` 0) cannot
    be observed, so only ``hold`` ends that wait.
    """
    deadline = time.monotonic() + max(0.0, hold)
    deferred: list[int] = []

    def defer(signum: int, frame: object) -> None:
        deferred.append(signum)
        log.error("deferring signal %s: %s has no durable guard yet", signum, what)

    recorded = False
    with _handling_stop_signals(defer):
        while True:
            if _record_survivor(pgid, detail, marker, lock=lock):
                recorded = True
                break
            gone = pgid > 0 and not _signal_group(pgid, 0)
            if gone or time.monotonic() >= deadline:
                break
            print(
                f"{detail}; it can be recorded neither at {marker} nor in the lock "
                "file, so this process keeps the overlap lock while it retries",
                file=sys.stderr,
            )
            time.sleep(max(0.05, poll))
    if deferred:
        # Reported rather than obeyed: this exit carries more than the signal does.
        print(
            f"deferred signal(s) {sorted(set(deferred))} while holding the lock",
            file=sys.stderr,
        )
    return recorded


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

    A record that was never written cannot stop the next firing, so a failed write
    is not something to log and move on from: see ``_hold_until_recorded``.  Exit 5
    means the next firing is guarded (or the group has gone); 6 means the group is
    still running and nothing durable says so.

    ``hold`` follows the same nesting discipline as the cleanup budget: it is the
    allowance the *caller* actually has left, never a fixed span.  The default is
    the reserved recording allowance, which is all a nested level (a sync, a
    bounded pull) has before the supervisor above it escalates to SIGKILL — a
    status this process never gets to return cannot stop anything.  Only the lock
    holder passes more: what is left of the firing's documented lifetime.
    """
    detail = _survivor_detail(exc, what)
    marker_path = Path(marker)
    lock_path = None if lock is None else Path(lock)
    if _hold_until_recorded(
        exc.pgid, detail, marker_path, lock_path, hold=hold, poll=poll, what=what
    ):
        print(
            f"{detail}. The next firing stops until process group {exc.pgid} is gone "
            f"(marker: {marker_path})",
            file=sys.stderr,
        )
        return UNCONFIRMED_TREE_EXIT
    if not _signal_group(exc.pgid, 0):
        print(
            f"{detail}. Process group {exc.pgid} ended before it could be recorded, "
            "so nothing is left for the next firing to run into",
            file=sys.stderr,
        )
        return UNCONFIRMED_TREE_EXIT
    # A different answer on purpose: this exit may not be read as "the next firing
    # is blocked", because nothing durable says so.
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
    """Leave a block behind for survivors this level cannot name in one record.

    Only the lock holder calls this, as the last level that can leave anything
    behind.  The record names no group (pgid 0), so it blocks every later firing
    until a human clears it: nothing can prove an unnamed group gone.  While even
    that cannot be written, holding the lock is the guard, for as long as this
    firing's lifetime allows.
    """
    if _hold_until_recorded(
        0, detail, marker, lock, hold=hold, poll=poll, what="local check"
    ):
        print(
            f"{detail}. Recorded as an unnamed survivor: every later firing stops "
            "until you check what is still running and clear that record (delete "
            "the marker file, or empty the lock file; never delete the lock itself)",
            file=sys.stderr,
        )
    else:
        print(
            f"{detail}. It could be recorded nowhere, so THE NEXT FIRING IS NOT "
            "PROTECTED: find what is still running (`ps -ax`), stop it, and fix why "
            f"{marker} and {lock} are unwritable before the watcher runs again",
            file=sys.stderr,
        )
    return UNGUARDED_TREE_EXIT


def _guard_survivors(
    survivors: Sequence[StateSyncCleanupError],
    *,
    unknown: bool,
    marker: Path,
    lock: Path,
    hold: float,
) -> int | None:
    """Leave one durable record covering every group this firing left running.

    ``survivors`` are the groups known to outlive the firing; ``unknown`` means a
    nested level reported one it could name to nobody.  A single known group is
    recorded by name, so the block clears itself once that group is gone — unless
    a record for a *different* group is already there, which one record cannot
    cover as well.  Anything more is recorded unnamed.  None: nothing to guard.
    """
    if not survivors and not unknown:
        return None
    if len(survivors) == 1 and not unknown:
        (only,) = survivors
        recorded = surviving_tree(marker, lock)
        if recorded is None or recorded["pgid"] == only.pgid:
            return _report_unconfirmed_tree(
                only, marker, "local check", lock=lock, hold=hold
            )
    groups = ", ".join(str(survivor.pgid) for survivor in survivors)
    left = f"process group(s) {groups}" if groups else "a process group"
    return _guard_unnamed_survivor(
        marker,
        lock,
        f"this local check left {left} running that it could neither stop nor "
        "record by name",
        hold=hold,
    )


def _blocked_message(survivor: dict[str, object], marker: Path) -> str:
    pgid = int(str(survivor["pgid"]))
    if pgid > 0:
        subject, inspect_with = f"process group {pgid}", f"`ps -g {pgid}`"
    else:
        # The unnamed case: nothing to inspect by group, and only a human clears it.
        subject, inspect_with = "a process group it could not name", "`ps -ax`"
    return (
        f"local check blocked: an earlier firing left {subject} running "
        f"({survivor['detail']}). Starting a second writer beside it is exactly what "
        f"the overlap lock prevents. Inspect {inspect_with}, stop what is left, then "
        f"clear that record in {survivor.get('source', marker)} — delete the marker "
        "file, or empty the lock file; never delete the lock itself"
    )


def _survivor_paths(base: Path, lock: str | Path) -> tuple[Path, Path]:
    """The overlap lock and its survivor marker, which every bounded child here uses.

    Set once per CLI call, so a Git child started deep inside a sync records a
    surviving group in the very place the wrapper's next firing looks.
    """
    lock_path = _resolve_under(base, lock)
    marker = unconfirmed_tree_path(lock_path)
    use_survivor_marker(marker)
    return lock_path, marker


def _run_locked(
    child: Sequence[str], *, lock: str, deadline: float, cleanup_budget: float
) -> int:
    """Run one firing under the overlap lock and its overall deadline.

    The lock is released only after ``_run_bounded`` has stopped and drained this
    run's own tree, so the next firing can never acquire it while an earlier
    writer is still alive.  Whatever outlived the firing — this level's own group,
    or a sessioned Git group a nested level announced on the survivor channel —
    is recorded before the lock goes, however the firing ended: a nested exit
    status cannot reach this level once the watchdog, launchd or Ctrl-C has
    stopped the shell in between.
    """
    lock_path, marker = _survivor_paths(Path.cwd(), lock)
    with file_lock(lock_path, blocking=False) as acquired:
        if not acquired:
            print("local check already running; skipping overlapping firing", file=sys.stderr)
            return 0
        # Holding the lock is not enough on its own: an earlier firing may have
        # ended without being able to stop its own tree, and an advisory lock dies
        # with the process that held it. That firing recorded what survived, and
        # this one stays out while it lives.
        survivor = surviving_tree(marker, lock_path)
        if survivor is not None:
            print(_blocked_message(survivor, marker), file=sys.stderr)
            return UNCONFIRMED_TREE_EXIT

        # This level holds the lock, so holding on is a real guard, for what is
        # left of the firing's documented lifetime (the deadline plus one cleanup
        # allowance) and not a second more.
        lifetime_ends = time.monotonic() + deadline + cleanup_budget
        with _survivor_channel() as (reader, writer):

            def guard(status: int, survivors: Sequence[StateSyncCleanupError] = ()) -> int:
                return _guard_leftovers(
                    reader,
                    status=status,
                    survivors=survivors,
                    marker=marker,
                    lock=lock_path,
                    hold=max(0.0, lifetime_ends - time.monotonic()),
                )

            env = os.environ.copy()
            env["OTW_LOCAL_CHECK_LOCKED"] = "1"
            env[SURVIVOR_CHANNEL_ENV] = str(writer)
            try:
                result, timed_out = _run_bounded(
                    child,
                    env=env,
                    capture=False,
                    timeout=deadline,
                    cleanup_budget=cleanup_budget,
                    pass_fds=(writer,),
                    # Stopped by launchd or Ctrl-C: guard before dying by that signal.
                    on_signal=lambda: guard(0),
                )
            except StateSyncCleanupError as exc:
                return guard(UNCONFIRMED_TREE_EXIT, [exc])
            if timed_out:
                print(
                    f"local check exceeded its {deadline:g}s deadline; the run's "
                    "process tree was stopped. Saved state and receipts are intact; "
                    "check the Git remote and the launchd log",
                    file=sys.stderr,
                )
                return guard(LOCAL_RUN_TIMEOUT_EXIT)
            return guard(_exit_code(result.returncode))


def _guard_leftovers(
    reader: int,
    *,
    status: int,
    survivors: Sequence[StateSyncCleanupError],
    marker: Path,
    lock: Path,
    hold: float,
) -> int:
    """The lock holder's last step: record what the firing left running, if any.

    ``survivors`` are this level's own; the rest are the groups nested levels
    announced on the survivor channel and that are still alive.  A nested exit 6
    with nothing announced and nothing recorded still leaves something running
    that no level can name.  Returns ``status`` when there is nothing to guard.
    """
    known = {survivor.pgid for survivor in survivors}
    announced = _announced_survivors(reader)
    leftovers = list(survivors) + [
        StateSyncCleanupError(
            f"process group {pgid} was announced by a nested level that could "
            "neither stop nor record it",
            pgid=pgid,
        )
        for pgid in announced
        if pgid not in known and _signal_group(pgid, 0)
    ]
    unknown = (
        status == UNGUARDED_TREE_EXIT
        and not announced
        and surviving_tree(marker, lock) is None
    )
    guarded = _guard_survivors(
        leftovers, unknown=unknown, marker=marker, lock=lock, hold=hold
    )
    return status if guarded is None else guarded


def _run_bounded_command(
    child: Sequence[str], *, lock: str, timeout: float, cleanup_budget: float
) -> int:
    """One child under a finite wait: the wrapper's deployment pull.

    It runs nested under ``locked``, so it has only the reserved recording
    allowance to record a survivor before that level escalates to SIGKILL.
    """
    lock_path, marker = _survivor_paths(Path.cwd(), lock)
    try:
        result, timed_out = _run_bounded(
            child, capture=False, timeout=timeout, cleanup_budget=cleanup_budget
        )
    except StateSyncCleanupError as exc:
        return _report_unconfirmed_tree(
            exc,
            marker,
            " ".join(child),
            lock=lock_path,
            hold=SURVIVOR_RECORD_ALLOWANCE_SECONDS,
        )
    if timed_out:
        print(
            f"`{' '.join(child)}` did not finish within {timeout:g}s; "
            "its process tree was stopped",
            file=sys.stderr,
        )
        return LOCAL_RUN_TIMEOUT_EXIT
    return _exit_code(result.returncode)


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
            lock_path, unconfirmed = _survivor_paths(repo, args.lock)
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
            lock_path, unconfirmed = _survivor_paths(repo, DEFAULT_LOCK_PATH)
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
            return _run_locked(
                _child_command(args.child, parser, "locked"),
                lock=args.lock,
                deadline=args.deadline,
                cleanup_budget=args.cleanup_budget,
            )
        elif args.command == "bounded":
            return _run_bounded_command(
                _child_command(args.child, parser, "bounded"),
                lock=args.lock,
                timeout=args.timeout,
                cleanup_budget=args.cleanup_budget,
            )
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
