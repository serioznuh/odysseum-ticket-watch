"""User-facing supervision for failed runtime-state synchronization."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import ClassVar

import pytest

from watcher import jobs, notify, state_sync
from watcher.detect import TZ_PARIS
from watcher.state import DEFAULT_STATE, LOCAL_FIRING_INTERVAL_MINUTES, save_state

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=TZ_PARIS)


class Cfg:
    film_title = "Dune : Troisième partie"
    cinema_name = "Pathé Odysseum"
    film_page_url = "https://www.pathe.fr/films/dune-troisieme-partie-50828"
    silent_kinds: ClassVar = list(notify.DEFAULT_SILENT_KINDS)


def test_failure_marker_reaches_one_loud_deduplicated_alert(tmp_path, monkeypatch):
    marker_path = tmp_path / "state-rebase-failure.json"
    marker = state_sync.record_failure(
        "runtime state synchronization failed",
        marker_path,
        now=NOW,
    )
    state = deepcopy(DEFAULT_STATE)
    sent = []

    def fake_send(cfg, text, **kwargs):
        sent.append((text, kwargs))
        return True

    monkeypatch.setattr(notify, "send_telegram", fake_send)
    ctx = jobs.RunContext(
        cfg=Cfg,
        state=state,
        clock=lambda: NOW,
        state_sync_marker=str(marker_path),
    )

    assert jobs.run_state_sync_failure_job(ctx, NOW) is True
    assert jobs.run_state_sync_failure_job(ctx, NOW) is False

    assert len(sent) == 1
    text, kwargs = sent[0]
    assert "Dune : Troisième partie · Pathé Odysseum" in text
    assert "Runtime state synchronization failed" in text
    assert kwargs["silent"] is False
    assert state_sync.failure_key(marker) in state["alerts"]


def test_resolved_marker_waits_for_alert_receipt_before_clearing(tmp_path):
    marker_path = tmp_path / "state-rebase-failure.json"
    marker = state_sync.record_failure("merge failed", marker_path, now=NOW)
    state_path = tmp_path / "state.json"
    state = deepcopy(DEFAULT_STATE)
    save_state(state_path, state)

    assert state_sync.resolve_failure(state_path, marker_path) is False
    assert marker_path.exists()

    state["alerts"][state_sync.failure_key(marker)] = NOW.isoformat()
    save_state(state_path, state)

    assert state_sync.resolve_failure(state_path, marker_path) is True
    assert not marker_path.exists()


def test_record_failure_preserves_one_episode_until_resolved(tmp_path):
    marker_path = tmp_path / "state-rebase-failure.json"
    first = state_sync.record_failure("first detail", marker_path, now=NOW)
    later = state_sync.record_failure(
        "second detail",
        marker_path,
        now=datetime(2026, 9, 17, 12, 5, tzinfo=TZ_PARIS),
    )

    assert later == first
    assert json.loads(marker_path.read_text(encoding="utf-8"))["detail"] == (
        "first detail"
    )


def test_transient_transport_failures_require_a_consecutive_streak(
    tmp_path, monkeypatch
):
    marker_path = tmp_path / state_sync.DEFAULT_MARKER_PATH
    streak_path = (
        tmp_path
        / state_sync.DEFAULT_STORE_PATH
        / state_sync.TRANSPORT_FAILURE_FILE
    )

    def fail_transport(*args, **kwargs):
        raise state_sync.StateSyncTransportError("GitHub temporarily unavailable")

    monkeypatch.setattr(state_sync, "synchronize", fail_transport)
    argv = ["sync", "--repo", str(tmp_path)]
    for expected_count in range(1, state_sync.TRANSPORT_FAILURE_THRESHOLD):
        assert state_sync.run(argv) == 1
        assert state_sync.load_failure(marker_path) is None
        assert state_sync.load_transport_failure(streak_path)["count"] == expected_count
    ctx = jobs.RunContext(
        cfg=Cfg,
        state=deepcopy(DEFAULT_STATE),
        clock=lambda: NOW,
        state_sync_marker=str(marker_path),
    )
    assert jobs.run_state_sync_failure_job(ctx, NOW) is False

    assert state_sync.run(argv) == 1
    marker = state_sync.load_failure(marker_path)
    assert marker is not None
    assert (
        f"{state_sync.TRANSPORT_FAILURE_THRESHOLD} consecutive times"
        in marker["detail"]
    )


def test_success_resets_transient_transport_streak(tmp_path, monkeypatch):
    streak_path = (
        tmp_path
        / state_sync.DEFAULT_STORE_PATH
        / state_sync.TRANSPORT_FAILURE_FILE
    )
    calls = iter(("fail", "fail", "success", "fail"))

    def synchronize_once(*args, **kwargs):
        if next(calls) == "fail":
            raise state_sync.StateSyncTransportError("offline")
        return tmp_path / "state.json"

    monkeypatch.setattr(state_sync, "synchronize", synchronize_once)
    argv = ["sync", "--repo", str(tmp_path)]
    assert state_sync.run(argv) == 1
    assert state_sync.run(argv) == 1
    assert state_sync.load_transport_failure(streak_path)["count"] == 2
    assert state_sync.run(argv) == 0
    assert not streak_path.exists()
    assert state_sync.run(argv) == 1
    assert state_sync.load_transport_failure(streak_path)["count"] == 1
    assert state_sync.load_failure(tmp_path / state_sync.DEFAULT_MARKER_PATH) is None


def test_actionable_state_failure_marks_immediately(tmp_path, monkeypatch):
    def fail_integrity(*args, **kwargs):
        raise state_sync.StateSyncError("shared state schema is incompatible")

    monkeypatch.setattr(state_sync, "synchronize", fail_integrity)

    assert state_sync.run(["sync", "--repo", str(tmp_path)]) == 1
    marker = state_sync.load_failure(tmp_path / state_sync.DEFAULT_MARKER_PATH)
    assert marker is not None
    assert "schema is incompatible" in marker["detail"]


# ---------------------------------------------------------------------------
# OTW-30: a missing shared ref is an operator condition, not a bootstrap
# ---------------------------------------------------------------------------

IN_FLIGHT_ID = "telegram:news-leak-42"


def ref_absent(*, established: bool):
    def fail(*args, **kwargs):
        raise state_sync.StateSyncRefAbsentError(
            "shared state ref refs/heads/runtime-state is missing",
            established=established,
        )

    return fail


def test_missing_ref_always_blocks_the_firing(tmp_path, monkeypatch):
    """Exit 3 is what the startup wrappers stop on, and a confirmed absence
    earns it whatever this clone holds: a receipt that lived only in the ref —
    a reminder the cloud sent while the Mac slept — is invisible here, so any
    local snapshot can be missing it and send it again."""
    argv = ["sync", "--repo", str(tmp_path)]
    for established in (False, True):
        state_sync.clear_failure(tmp_path / state_sync.DEFAULT_MARKER_PATH)
        monkeypatch.setattr(
            state_sync, "synchronize", ref_absent(established=established)
        )
        assert state_sync.run(argv) == state_sync.BOOTSTRAP_REQUIRED_EXIT


def test_missing_ref_marks_an_established_installation_for_one_alert(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(state_sync, "synchronize", ref_absent(established=True))

    assert state_sync.run(["sync", "--repo", str(tmp_path)]) == (
        state_sync.BOOTSTRAP_REQUIRED_EXIT
    )
    marker = state_sync.load_failure(tmp_path / state_sync.DEFAULT_MARKER_PATH)
    assert marker is not None
    # The alert quotes this detail verbatim and truncates at 200 characters, so
    # the operator action has to survive that cut. It is delivered once recovery
    # lets a pass run again; the blocked firings report to stderr meanwhile.
    assert "is missing; run `watcher.state_sync recover`" in marker["detail"]
    assert len(marker["detail"]) < 200


def test_missing_ref_on_a_new_install_leaves_no_alert_behind(tmp_path, monkeypatch):
    monkeypatch.setattr(state_sync, "synchronize", ref_absent(established=False))

    assert state_sync.run(["sync", "--repo", str(tmp_path)]) == (
        state_sync.BOOTSTRAP_REQUIRED_EXIT
    )
    # Nothing was ever sent from here, so there is no history to alert about and
    # the operator running `init` must not be greeted by a red herring.
    assert state_sync.load_failure(tmp_path / state_sync.DEFAULT_MARKER_PATH) is None


def test_missing_ref_is_not_counted_as_a_transport_outage(tmp_path, monkeypatch):
    streak_path = (
        tmp_path / state_sync.DEFAULT_STORE_PATH / state_sync.TRANSPORT_FAILURE_FILE
    )
    state_sync.record_transport_failure("offline", streak_path)
    monkeypatch.setattr(state_sync, "synchronize", ref_absent(established=False))

    assert state_sync.run(["sync", "--repo", str(tmp_path)]) == (
        state_sync.BOOTSTRAP_REQUIRED_EXIT
    )
    # The transport answered "no such ref": that is an answer, not an outage.
    assert not streak_path.exists()


def test_delivery_evidence_separates_a_new_install_from_a_recovery():
    assert state_sync.has_delivery_evidence(deepcopy(DEFAULT_STATE)) is False
    for field, value in (
        ("alerts", {"sale:x": NOW.isoformat()}),
        ("reminders_sent", {"2026-12-01T09:00:00+01:00": ["15"]}),
        ("shows_seen", ["dune-troisieme-partie"]),
        ("last_heartbeat", NOW.isoformat()),
    ):
        state = deepcopy(DEFAULT_STATE)
        state[field] = value
        assert state_sync.has_delivery_evidence(state) is True, field


def in_flight_record(status: str) -> dict:
    return {
        "keys": ["news:leak-42"],
        "kinds": ["NEWS_LEAD"],
        "text": "Dune : Troisième partie · Pathé Odysseum — press lead",
        "silent": True,
        "force": False,
        "created_at": NOW.isoformat(),
        "topics": ["news"],
        "status": status,
        "ack": {"type": "alerts", "keys": ["news:leak-42"]},
        "claim": {"owner": "local", "token": "abc123", "at": NOW.isoformat()},
    }


def test_reconciliation_unions_receipts_and_quarantines_every_open_attempt():
    """Recovery sees stores with no common base. An older backup is not proof
    that an alert was never sent, and an attempt one store left in flight stays
    unknown even when a newer store no longer mentions it."""
    older = deepcopy(DEFAULT_STATE)
    older["alerts"]["news:old"] = NOW.isoformat()
    older["reminders_sent"]["2026-12-01T09:00:00+01:00"] = ["1440"]
    older["outbox"][IN_FLIGHT_ID] = in_flight_record("sending")
    newer = deepcopy(DEFAULT_STATE)
    newer["alerts"]["news:new"] = NOW.isoformat()
    newer["reminders_sent"]["2026-12-01T09:00:00+01:00"] = ["120"]
    newer["last_check_ok"] = NOW.isoformat()

    merged = state_sync.reconcile_stores([older, newer])

    assert set(merged["alerts"]) == {"news:old", "news:new"}
    assert merged["reminders_sent"]["2026-12-01T09:00:00+01:00"] == ["120", "1440"]
    assert merged["last_check_ok"] == NOW.isoformat()
    assert merged["outbox"][IN_FLIGHT_ID]["status"] == "uncertain"
    assert merged["outbox"][IN_FLIGHT_ID]["claim"]["token"] == "abc123"


def test_reconciliation_keeps_a_confirmed_delivery_out_of_the_outbox():
    delivered = deepcopy(DEFAULT_STATE)
    delivered["alerts"]["news:leak-42"] = NOW.isoformat()
    delivered["delivery_receipts"]["attempt-1"] = {
        "delivery_id": IN_FLIGHT_ID,
        "keys": ["news:leak-42"],
        "delivered_at": NOW.isoformat(),
        "telegram_message_id": 7,
    }
    stale_attempt = deepcopy(DEFAULT_STATE)
    stale_attempt["outbox"][IN_FLIGHT_ID] = in_flight_record("uncertain")

    merged = state_sync.reconcile_stores([stale_attempt, delivered])

    # A receipt is the one thing that settles an attempt; it must not come back
    # as work, and its dedup key stays recorded.
    assert merged["outbox"] == {}
    assert "news:leak-42" in merged["alerts"]


def test_reconciliation_needs_at_least_one_surviving_store():
    with pytest.raises(state_sync.StateSyncError, match="no runtime-state store"):
        state_sync.reconcile_stores([])


def _script_exit_code(root: Path) -> int:
    script = (root / "scripts" / "local-check.sh").read_text(encoding="utf-8")
    match = re.search(r"STATE_BOOTSTRAP_REQUIRED_EXIT=(\d+)", script)
    assert match, "local-check.sh no longer honors the missing-ref exit code"
    return int(match.group(1))


def test_startup_wrappers_stop_on_the_missing_ref_exit_code():
    """Both wrappers must honor the block: continuing here would deliver from
    an unverified seed. The numbers live in three files, so pin them."""
    root = Path(__file__).resolve().parent.parent
    assert _script_exit_code(root) == state_sync.BOOTSTRAP_REQUIRED_EXIT

    script = (root / "scripts" / "local-check.sh").read_text(encoding="utf-8")
    guard = script.index('"$pre_sync_status" -eq "$STATE_BOOTSTRAP_REQUIRED_EXIT"')
    assert guard < script.index("-m watcher \\"), (
        "the missing-ref guard must run before the watcher can send"
    )

    workflow = (root / ".github" / "workflows" / "watch.yml").read_text(encoding="utf-8")
    assert f"steps.presync.outputs.code == '{state_sync.BOOTSTRAP_REQUIRED_EXIT}'" in (
        workflow
    )
    for gated in ("Validate Telegram credentials", "Synchronize runtime state (after)"):
        section = workflow[workflow.index(gated) :]
        assert (
            f"steps.presync.outputs.code != '{state_sync.BOOTSTRAP_REQUIRED_EXIT}'"
            in section[: section.index("run:")]
        ), f"{gated} must be skipped when initialization is required"


def test_blocked_exit_survives_a_failing_transport_streak_cleanup(
    tmp_path, monkeypatch
):
    """Both wrappers stop on this exit status alone. Incidental bookkeeping
    failure must not turn the block into "continue and deliver"."""
    monkeypatch.setattr(state_sync, "synchronize", ref_absent(established=True))

    def fail_cleanup(path):
        raise OSError("read-only file system")

    monkeypatch.setattr(state_sync, "clear_transport_failure", fail_cleanup)

    assert state_sync.run(["sync", "--repo", str(tmp_path)]) == (
        state_sync.BOOTSTRAP_REQUIRED_EXIT
    )


def test_blocked_exit_survives_a_failing_marker_write(tmp_path, monkeypatch):
    monkeypatch.setattr(state_sync, "synchronize", ref_absent(established=True))

    def fail_marker(detail, path=state_sync.DEFAULT_MARKER_PATH, **kwargs):
        raise OSError("no space left on device")

    monkeypatch.setattr(state_sync, "record_failure", fail_marker)

    assert state_sync.run(["sync", "--repo", str(tmp_path)]) == (
        state_sync.BOOTSTRAP_REQUIRED_EXIT
    )


def test_reconciliation_keeps_work_a_later_store_never_knew_about():
    """These stores share no base, so a store that lacks a record is not saying
    it was retired. A `pending` item only one store holds must survive too: it
    is real queued work, and only a receipt or satisfied ack retires it."""
    holder = deepcopy(DEFAULT_STATE)
    holder["outbox"]["telegram:queued"] = in_flight_record("pending")
    holder["outbox"]["telegram:queued"]["keys"] = ["news:queued"]
    holder["outbox"]["telegram:queued"]["ack"] = {
        "type": "alerts",
        "keys": ["news:queued"],
    }
    holder["outbox"][IN_FLIGHT_ID] = in_flight_record("uncertain")
    unaware = deepcopy(DEFAULT_STATE)
    unaware["last_check_ok"] = NOW.isoformat()

    merged = state_sync.reconcile_stores([holder, unaware])

    assert set(merged["outbox"]) == {"telegram:queued", IN_FLIGHT_ID}
    assert merged["outbox"]["telegram:queued"]["status"] == "pending"
    assert merged["outbox"][IN_FLIGHT_ID]["status"] == "uncertain"


# ---------------------------------------------------------------------------
# OTW-29: nothing in a local firing waits forever
# ---------------------------------------------------------------------------


def test_local_run_deadline_is_finite_and_leaves_room_for_every_bounded_step():
    """The overall watchdog is a backstop, never a competitor: a healthy pass —
    its whole polling budget plus a bounded deployment pull and both syncs' first
    network waits — has to finish well inside it, and it still has to be finite
    so a wedged tree cannot hold the lock across every later firing."""
    assert state_sync.LOCAL_RUN_DEADLINE_SECONDS == (
        2 * LOCAL_FIRING_INTERVAL_MINUTES * 60
    )
    assert state_sync.LOCAL_RUN_DEADLINE_SECONDS >= (
        jobs.POLLING_BUDGET_SECONDS
        + state_sync.DEPLOY_TIMEOUT_SECONDS
        + 2 * state_sync.GIT_TIMEOUT_SECONDS
    )
    # A timeout must stay tellable apart from the missing-ref block and success.
    assert state_sync.LOCAL_RUN_TIMEOUT_EXIT not in {
        0,
        state_sync.BOOTSTRAP_REQUIRED_EXIT,
    }


def test_git_timeout_is_reported_as_a_transport_failure(tmp_path, monkeypatch):
    """A Git child that never answers gets the existing capped streak, not a new
    loud alert path — and never the confirmed-absence block, because a timeout is
    no answer about what the shared ref holds."""
    def timed_out(command, **kwargs):
        return subprocess.CompletedProcess(list(command), 0, "", ""), True

    monkeypatch.setattr(state_sync, "_run_bounded", timed_out)
    with pytest.raises(state_sync.StateSyncTransportError, match="did not answer"):
        state_sync._git(tmp_path, "ls-remote", "--exit-code", "origin", "refs/x")
    # check=False callers are not exempt: a timeout is not a returncode.
    with pytest.raises(state_sync.StateSyncTransportError):
        state_sync._git(tmp_path, "push", "origin", "x", check=False)


def test_local_check_bounds_the_deployment_pull_and_the_whole_firing():
    """The wrapper holds one overlap lock across deployment, both syncs and the
    watcher, so an unbounded Git child there costs every later firing too."""
    root = Path(__file__).resolve().parent.parent
    lines = (root / "scripts" / "local-check.sh").read_text(encoding="utf-8").splitlines()

    def line_of(needle: str) -> int:
        return next(index for index, line in enumerate(lines) if needle in line)

    locked = line_of("state_sync locked")
    pull = line_of("git pull --ff-only")
    pre_sync = line_of("sync_state || pre_sync_status")
    # The pull runs through the stdlib boundary, and deployment still comes first.
    assert "state_sync bounded" in "".join(lines[pull - 1 : pull + 1])
    assert locked < pull < pre_sync

    # A surviving process group is a hard stop wherever it is reported — the same
    # class of handling as the missing ref, never folded into "failed, carry on".
    script_text = "\n".join(lines)
    for name, code in (
        ("UNCONFIRMED", state_sync.UNCONFIRMED_TREE_EXIT),
        ("UNGUARDED", state_sync.UNGUARDED_TREE_EXIT),
    ):
        assert re.search(rf"^STATE_{name}_TREE_EXIT={code}$", script_text, re.MULTILINE)
        assert f'"$STATE_{name}_TREE_EXIT"' in script_text  # the check uses it
    stops = [
        index for index, line in enumerate(lines) if line.startswith(("if surviving_group", "  if surviving_group"))
    ]
    assert len(stops) == 3, "deployment, pre-run sync and post-run sync each stop on it"
    post_sync = line_of("sync_state || sync_status")
    assert stops[0] < pre_sync and stops[1] < post_sync
    # …and the post-run check comes before the folding that could mask it.
    assert stops[2] < line_of('"$status" -eq 0')


def test_termination_stays_forwarded_while_the_tree_is_cleaned_up():
    """A signal arriving during timeout cleanup must still be forwarded to the
    owned tree. If the handlers were uninstalled before cleanup, that signal
    would end this supervisor while part of its tree could still be running."""
    original = signal.getsignal(signal.SIGTERM)
    observed = {}
    real_stop_tree = state_sync._stop_tree

    def watch_cleanup(process, *, window):
        observed["during"] = signal.getsignal(signal.SIGTERM)
        return real_stop_tree(process, window=window)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(state_sync, "_stop_tree", watch_cleanup)
        _, timed_out = state_sync._run_bounded(
            ["/bin/sh", "-c", "sleep 30 & wait"],
            timeout=0.2,
            cleanup_budget=1.0,
            capture=False,
        )

    assert timed_out is True
    assert callable(observed["during"]) and observed["during"] is not original
    # …and the supervisor hands the signals back once no child of its own is left.
    assert signal.getsignal(signal.SIGTERM) is original


def test_a_git_tree_is_cleaned_up_inside_its_supervisors_allowance():
    """A sync stopped by the overall deadline has to finish stopping its own Git
    child before the supervisor above it escalates to SIGKILL, which that Git
    child would otherwise outlive."""
    assert (
        state_sync.GIT_CLEANUP_BUDGET_SECONDS
        + state_sync.SURVIVOR_RECORD_ALLOWANCE_SECONDS
        <= state_sync.CLEANUP_BUDGET_SECONDS / 2
    )


# ---------------------------------------------------------------------------
# OTW-29: a tree that outlives cleanup is reported, never presumed gone
# ---------------------------------------------------------------------------


def unstoppable(monkeypatch) -> None:
    """Make every escalation report that the group is still running."""
    monkeypatch.setattr(state_sync, "_drain_group", lambda pid, *, grace: False)


def test_cleanup_that_cannot_confirm_a_stop_raises_instead_of_returning(
    monkeypatch,
):
    unstoppable(monkeypatch)
    with pytest.raises(state_sync.StateSyncCleanupError, match="still running"):
        state_sync._run_bounded(
            ["/bin/echo", "done"], timeout=5, cleanup_budget=0.05, capture=False
        )


def test_a_tree_that_outlives_cleanup_blocks_the_next_firing(tmp_path, monkeypatch):
    """The lock cannot be held past this process's own life, so the refusal has
    to outlive it: the firing records what survived instead of reporting a clean
    finish, and the next one stops on that rather than joining a live writer."""
    lock = tmp_path / "local-check.lock"
    marker = state_sync.unconfirmed_tree_path(lock)
    unstoppable(monkeypatch)

    assert state_sync.run(
        ["locked", "--lock", str(lock), "--cleanup-budget", "0.05", "--", "/bin/echo", "ran"]
    ) == state_sync.UNCONFIRMED_TREE_EXIT
    recorded = json.loads(marker.read_text(encoding="utf-8"))
    assert recorded["pgid"] > 1
    assert "could not stop" in recorded["detail"]


def test_a_recorded_survivor_keeps_the_next_firing_out_until_it_is_gone(tmp_path):
    lock = tmp_path / "local-check.lock"
    marker = state_sync.unconfirmed_tree_path(lock)
    sentinel = tmp_path / "second-writer-ran"
    child = ["/bin/sh", "-c", f"touch {sentinel}"]
    survivor = subprocess.Popen(["/bin/sh", "-c", "sleep 30"], start_new_session=True)
    try:
        state_sync.record_unconfirmed_tree(marker, survivor.pid, "simulated survivor")

        # The lock itself is free — the guard is the evidence, not the lock.
        assert state_sync.run(["locked", "--lock", str(lock), "--", *child]) == (
            state_sync.UNCONFIRMED_TREE_EXIT
        )
        assert not sentinel.exists()
        assert marker.exists()
    finally:
        survivor.kill()
        survivor.wait()

    # Once that group is really gone the refusal clears itself: no manual step is
    # needed for the common case, and the watcher resumes on the next firing.
    assert state_sync.run(["locked", "--lock", str(lock), "--", *child]) == 0
    assert sentinel.exists()
    assert not marker.exists()


def test_an_unreadable_survivor_marker_fails_closed(tmp_path):
    """A marker this boundary cannot read is not proof that anything stopped."""
    lock = tmp_path / "local-check.lock"
    marker = state_sync.unconfirmed_tree_path(lock)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("{ truncated", encoding="utf-8")
    sentinel = tmp_path / "second-writer-ran"

    assert state_sync.run(
        ["locked", "--lock", str(lock), "--", "/bin/sh", "-c", f"touch {sentinel}"]
    ) == state_sync.UNCONFIRMED_TREE_EXIT
    assert not sentinel.exists()


def test_exit_codes_stay_distinguishable():
    assert len(
        {
            0,
            state_sync.BOOTSTRAP_REQUIRED_EXIT,
            state_sync.LOCAL_RUN_TIMEOUT_EXIT,
            state_sync.UNCONFIRMED_TREE_EXIT,
        }
    ) == 4


def test_a_sync_that_cannot_stop_its_git_child_blocks_the_next_firing(
    tmp_path, monkeypatch
):
    """That Git child leads a session of its own, so the wrapper above can
    neither see nor stop it. The sync records it where the next firing looks,
    and leaves one durable marker so the owner hears about the machine."""
    def cleanup_failed(*args, **kwargs):
        raise state_sync.StateSyncCleanupError(
            "process group 4242 was still running", pgid=4242
        )

    monkeypatch.setattr(state_sync, "synchronize", cleanup_failed)

    assert state_sync.run(["sync", "--repo", str(tmp_path)]) == (
        state_sync.UNCONFIRMED_TREE_EXIT
    )
    survivor = json.loads(
        (tmp_path / f"{state_sync.DEFAULT_LOCK_PATH}{state_sync.UNCONFIRMED_TREE_SUFFIX}")
        .read_text(encoding="utf-8")
    )
    assert survivor["pgid"] == 4242
    failure = state_sync.load_failure(tmp_path / state_sync.DEFAULT_MARKER_PATH)
    assert failure is not None and "could not stop its Git child" in failure["detail"]


def never_drains(monkeypatch) -> None:
    """A group that never goes, consuming each wait exactly like the real drain."""

    def drain(pid, *, grace):
        time.sleep(max(0.0, grace))
        return False

    monkeypatch.setattr(state_sync, "_drain_group", drain)


def test_cleanup_retries_all_fit_inside_one_allowance(monkeypatch):
    """Round-2 finding: the retries must not multiply the allowance. A sync gets
    one window to stop its Git group and record it, because the level above
    escalates to SIGKILL after half of its own; per-attempt waits would blow
    straight through that and the survivor would never be recorded."""
    never_drains(monkeypatch)
    budget = 0.4

    started = time.monotonic()
    with pytest.raises(state_sync.StateSyncCleanupError):
        state_sync._run_bounded(
            ["/bin/echo", "done"], timeout=5, cleanup_budget=budget, capture=False
        )
    spent = time.monotonic() - started

    # One budget, with slack for scheduling — not the 7 waits the rounds contain.
    assert spent < budget * 2, f"cleanup spent {spent:.2f}s of a {budget:g}s budget"


def test_a_survivor_that_cannot_be_recorded_keeps_the_lock_instead(
    tmp_path, monkeypatch
):
    """A marker that was never written cannot stop the next firing, so failing to
    write it may not be logged and shrugged off. The process stays alive — which
    is what keeps the overlap lock held — while the surviving group is still
    there, and it reports the *unguarded* status rather than claiming a block it
    cannot back up (later round-2 finding)."""
    def unwritable(*args, **kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr(state_sync, "record_unconfirmed_tree", unwritable)
    survivor = subprocess.Popen(["/bin/sh", "-c", "sleep 30"], start_new_session=True)
    exc = state_sync.StateSyncCleanupError("still running", pgid=survivor.pid)
    try:
        started = time.monotonic()
        code = state_sync._report_unconfirmed_tree(
            exc, tmp_path / "survivor.json", "local check", hold=0.4, poll=0.05
        )
        held = time.monotonic() - started
    finally:
        survivor.kill()
        survivor.wait()

    assert code == state_sync.UNGUARDED_TREE_EXIT
    assert code != state_sync.UNCONFIRMED_TREE_EXIT  # never read as "guarded"
    assert held >= 0.4, "the lock was released with no durable guard in place"


def test_the_hold_ends_once_the_survivor_is_gone(tmp_path, monkeypatch):
    """…and it is not a blind wait: nothing is left to run into, so it returns."""
    def unwritable(*args, **kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr(state_sync, "record_unconfirmed_tree", unwritable)
    gone = subprocess.Popen(["/bin/sh", "-c", "exit 0"], start_new_session=True)
    gone.wait()
    exc = state_sync.StateSyncCleanupError("was still running", pgid=gone.pid)

    started = time.monotonic()
    assert state_sync._report_unconfirmed_tree(
        exc, tmp_path / "survivor.json", "local check", hold=30, poll=0.05
    ) == state_sync.UNCONFIRMED_TREE_EXIT
    assert time.monotonic() - started < 5


def test_a_surviving_git_child_is_never_reclassified_as_ref_absence(
    tmp_path, monkeypatch
):
    """Round-2 finding: the stale-ref cleanup runs inside the confirmed-absence
    branch. A Git child that outlived cleanup there has to reach the caller that
    records it, or the firing reports a missing ref and leaves that group running
    and unrecorded for the next one to join."""
    def git_with(cleanup_failure: bool):
        def fake_git(repo, *args, **kwargs):
            if args[0] == "ls-remote":
                return subprocess.CompletedProcess(["git", *args], 2, "", "")
            if cleanup_failure:
                raise state_sync.StateSyncCleanupError("still running", pgid=4242)
            raise state_sync.StateSyncError("update-ref refused")

        return fake_git

    monkeypatch.setattr(state_sync, "_git", git_with(True))
    with pytest.raises(state_sync.StateSyncCleanupError):
        state_sync._remote_state(tmp_path, "origin", state_sync.DEFAULT_STATE_REF)

    # Any other failure of that housekeeping still may not mask the absence.
    monkeypatch.setattr(state_sync, "_git", git_with(False))
    assert state_sync._remote_state(
        tmp_path, "origin", state_sync.DEFAULT_STATE_REF
    ) == (None, None)


def test_an_operator_command_also_records_a_surviving_git_child(tmp_path, monkeypatch):
    """`init`/`recover` run in the production clone, so the scheduled firing that
    comes next must not start beside a Git group they could not stop either."""
    def cleanup_failed(*args, **kwargs):
        raise state_sync.StateSyncCleanupError("still running", pgid=4243)

    monkeypatch.setattr(state_sync, "initialize", cleanup_failed)

    assert state_sync.run(["init", "--repo", str(tmp_path)]) == (
        state_sync.UNCONFIRMED_TREE_EXIT
    )
    survivor = json.loads(
        (tmp_path / f"{state_sync.DEFAULT_LOCK_PATH}{state_sync.UNCONFIRMED_TREE_SUFFIX}")
        .read_text(encoding="utf-8")
    )
    assert survivor["pgid"] == 4243


def kill_this_process_after(delay: float) -> subprocess.Popen:
    """A helper that delivers a real SIGTERM to this process, once."""
    return subprocess.Popen(
        ["/bin/sh", "-c", f"sleep {delay}; kill -TERM {os.getpid()}"]
    )


def test_a_signal_that_cannot_stop_the_tree_records_it_before_giving_up(
    tmp_path, monkeypatch
):
    """Round-1 finding: when the stop a signal asks for does not take, waiting on
    is exactly wrong — a surviving Git child holding a captured pipe keeps the
    wait blocked until the level above SIGKILLs this process, and nothing would be
    recorded. The handler records the survivor itself and ends the wait."""
    marker = tmp_path / "survivor.json"
    never_drains(monkeypatch)
    killer = kill_this_process_after(0.4)
    try:
        with pytest.raises(state_sync.StateSyncCleanupError) as failure:
            state_sync._run_bounded(
                ["/bin/sh", "-c", "trap '' TERM; sleep 20"],
                timeout=20,
                cleanup_budget=0.2,
                survivor_marker=marker,
            )
    finally:
        killer.wait()

    recorded = json.loads(marker.read_text(encoding="utf-8"))
    assert recorded["pgid"] == failure.value.pgid
    assert "could not stop" in recorded["detail"]
    try:  # the tree was SIGKILLed on the way out; do not leave it unreaped
        os.waitpid(failure.value.pgid, 0)
    except (ChildProcessError, OSError):
        pass


def test_the_survivor_retry_keeps_signals_handled(tmp_path, capsys):
    """Round-1 finding: that retry loop used to run with default signal handling,
    so a SIGTERM there ended the only guard in place. It is deferred instead —
    this test process would not survive the signal otherwise."""
    original = signal.getsignal(signal.SIGTERM)
    survivor = subprocess.Popen(["/bin/sh", "-c", "sleep 30"], start_new_session=True)
    exc = state_sync.StateSyncCleanupError("still running", pgid=survivor.pid)

    with pytest.MonkeyPatch.context() as patch:
        def unwritable(*args, **kwargs):
            raise OSError("read-only file system")

        patch.setattr(state_sync, "record_unconfirmed_tree", unwritable)
        killer = kill_this_process_after(0.2)
        try:
            started = time.monotonic()
            code = state_sync._report_unconfirmed_tree(
                exc, tmp_path / "survivor.json", "local check", hold=0.6, poll=0.05
            )
            held = time.monotonic() - started
        finally:
            killer.wait()
            survivor.kill()
            survivor.wait()

    assert code == state_sync.UNGUARDED_TREE_EXIT
    assert held >= 0.6, "the signal cut the hold short"
    assert "deferred signal" in capsys.readouterr().err
    assert signal.getsignal(signal.SIGTERM) is original


def test_both_wrappers_block_on_every_blocking_exit_code():
    """Round-1 finding: the cloud wrapper blocked only on the missing ref, so a
    pre-sync that left a live Git group fell through to sending and to a second
    Git writer. Both wrappers must stop on the whole set."""
    root = Path(__file__).resolve().parent.parent
    script = (root / "scripts" / "local-check.sh").read_text(encoding="utf-8")
    workflow = (root / ".github" / "workflows" / "watch.yml").read_text(encoding="utf-8")
    assert state_sync.WRAPPER_BLOCKING_EXITS == (
        state_sync.BOOTSTRAP_REQUIRED_EXIT,
        state_sync.UNCONFIRMED_TREE_EXIT,
        state_sync.UNGUARDED_TREE_EXIT,
    )

    for code in state_sync.WRAPPER_BLOCKING_EXITS:
        assert re.search(rf"^STATE_[A-Z_]+={code}$", script, re.MULTILINE), (
            f"local-check.sh no longer mirrors blocking exit {code}"
        )
        assert f'steps.presync.outputs.code == \'{code}\'' in workflow, (
            f"the cloud wrapper has no step that stops the job on exit {code}"
        )
        for gated in ("Validate Telegram credentials", "Synchronize runtime state (after)"):
            section = workflow[workflow.index(gated) :]
            assert f"steps.presync.outputs.code != '{code}'" in section[
                : section.index("run:")
            ], f"{gated} must be skipped on exit {code}"


def test_the_lock_file_carries_the_record_when_the_marker_cannot_be_written(
    tmp_path, monkeypatch
):
    """Round-2 finding, the other half: rather than give up on guarding the next
    firing, the record goes into the lock file this firing already holds —
    rewriting an existing file needs no new inode and no directory change, so it
    can land where creating the marker beside it cannot."""
    lock = tmp_path / "local-check.lock"
    lock.write_text("", encoding="utf-8")
    marker = state_sync.unconfirmed_tree_path(lock)
    sentinel = tmp_path / "second-writer-ran"
    child = ["/bin/sh", "-c", f"touch {sentinel}"]
    survivor = subprocess.Popen(["/bin/sh", "-c", "sleep 30"], start_new_session=True)
    exc = state_sync.StateSyncCleanupError("still running", pgid=survivor.pid)

    def unwritable(*args, **kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr(state_sync, "record_unconfirmed_tree", unwritable)
    try:
        assert state_sync._report_unconfirmed_tree(
            exc, marker, "local check", lock=lock, hold=1, poll=0.05
        ) == state_sync.UNCONFIRMED_TREE_EXIT  # guarded after all, so not 6
        monkeypatch.undo()

        assert not marker.exists()  # the marker never landed
        assert state_sync.surviving_tree(marker, lock)["pgid"] == survivor.pid
        # …and that is enough to keep the next firing out.
        assert state_sync.run(["locked", "--lock", str(lock), "--", *child]) == (
            state_sync.UNCONFIRMED_TREE_EXIT
        )
        assert not sentinel.exists()
    finally:
        survivor.kill()
        survivor.wait()

    # It clears itself the same way, by truncation — the lock file is never removed.
    assert state_sync.run(["locked", "--lock", str(lock), "--", *child]) == 0
    assert sentinel.exists()
    assert lock.exists() and lock.read_text(encoding="utf-8").strip() == ""


def test_the_block_message_names_the_record_it_wants_cleared(tmp_path, capsys):
    """Two places can hold the record, and the lock file is not one an operator
    may delete — so the message has to name the right one and say how."""
    lock = tmp_path / "local-check.lock"
    lock.write_text("", encoding="utf-8")
    survivor = subprocess.Popen(["/bin/sh", "-c", "sleep 30"], start_new_session=True)
    try:
        state_sync.record_survivor_in_lock(lock, survivor.pid, "simulated survivor")
        assert state_sync.run(
            ["locked", "--lock", str(lock), "--", "/bin/echo", "ran"]
        ) == state_sync.UNCONFIRMED_TREE_EXIT
    finally:
        survivor.kill()
        survivor.wait()

    blocked = capsys.readouterr().err
    assert str(lock) in blocked
    assert "never delete the lock itself" in blocked
