"""User-facing supervision for failed runtime-state synchronization."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import ClassVar

import pytest

from watcher import jobs, notify, state_sync
from watcher.detect import TZ_PARIS
from watcher.state import DEFAULT_STATE, save_state

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


def ref_absent(*, local_evidence: bool):
    def fail(*args, **kwargs):
        raise state_sync.StateSyncRefAbsentError(
            "shared state ref refs/heads/runtime-state is missing",
            local_evidence=local_evidence,
        )

    return fail


def test_missing_ref_without_local_evidence_blocks_the_firing(tmp_path, monkeypatch):
    """Exit 3 is what the startup wrappers stop on: with no verified history
    there is nothing to deduplicate against, so nothing may be delivered."""
    monkeypatch.setattr(state_sync, "synchronize", ref_absent(local_evidence=False))
    argv = ["sync", "--repo", str(tmp_path)]

    assert state_sync.run(argv) == state_sync.BOOTSTRAP_REQUIRED_EXIT
    # No alert can reach the user from a runner in this condition, so no marker
    # is left behind to fire later out of context.
    assert state_sync.load_failure(tmp_path / state_sync.DEFAULT_MARKER_PATH) is None
    assert not (
        tmp_path / state_sync.DEFAULT_STORE_PATH / state_sync.TRANSPORT_FAILURE_FILE
    ).exists()


def test_missing_ref_with_local_receipts_marks_immediately(tmp_path, monkeypatch):
    monkeypatch.setattr(state_sync, "synchronize", ref_absent(local_evidence=True))

    assert state_sync.run(["sync", "--repo", str(tmp_path)]) == 1
    marker = state_sync.load_failure(tmp_path / state_sync.DEFAULT_MARKER_PATH)
    assert marker is not None
    # The alert quotes this detail verbatim and truncates at 200 characters, so
    # the operator action has to survive that cut.
    assert "is missing; run `watcher.state_sync recover`" in marker["detail"]
    assert len(marker["detail"]) < 200


def test_missing_ref_is_not_counted_as_a_transport_outage(tmp_path, monkeypatch):
    streak_path = (
        tmp_path / state_sync.DEFAULT_STORE_PATH / state_sync.TRANSPORT_FAILURE_FILE
    )
    state_sync.record_transport_failure("offline", streak_path)
    monkeypatch.setattr(state_sync, "synchronize", ref_absent(local_evidence=False))

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
