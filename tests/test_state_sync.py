"""User-facing supervision for failed runtime-state synchronization."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime
from typing import ClassVar

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
