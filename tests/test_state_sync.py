"""User-facing supervision for failed local state-rebase recovery."""

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
        "automatic state/state.json rebase recovery failed",
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
    assert "Local state rebase recovery failed" in text
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
