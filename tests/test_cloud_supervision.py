"""OTW-09: the local half supervises the scheduled cloud half."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from watcher import cloud, delivery, jobs, notify, runner
from watcher import state as state_mod
from watcher.detect import TZ_PARIS, Finding, Snapshot

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=TZ_PARIS)


def _cfg():
    return SimpleNamespace(
        cloud_repository="serioznuh/odysseum-ticket-watch",
        cloud_workflow="watch.yml",
        cloud_stale_hours=18,
        film_title="Dune : Troisième partie",
        cinema_name="Pathé Odysseum",
        film_page_url="https://www.pathe.fr/films/dune-troisieme-partie",
        silent_kinds=[],
    )


def _context():
    return jobs.RunContext(
        cfg=_cfg(),
        state=deepcopy(state_mod.DEFAULT_STATE),
        clock=lambda: NOW,
        mode="check",
    )


def _durable_context(tmp_path):
    ctx = _context()
    state_path = tmp_path / "state.json"
    state_mod.save_state(state_path, ctx.state)
    ctx.state_path = str(state_path)
    return ctx


def _seen_stale_before(when):
    """Pretend an earlier firing already saw the stale verdict a window ago."""
    cloud.record_stale_sighting(when - cloud.STALE_CONFIRMATION)


def _capture_delivery(monkeypatch, ctx):
    delivered = []

    def fake_deliver(_ctx, findings, now, force_keys=None):
        delivered.extend(findings)
        for finding in findings:
            state_mod.mark_sent(ctx.state, finding.key, now)
        return bool(findings)

    monkeypatch.setattr(jobs, "deliver", fake_deliver)
    return delivered


def test_dead_cloud_raises_one_loud_well_labelled_alert(monkeypatch):
    ctx = _context()
    delivered = _capture_delivery(monkeypatch, ctx)
    monkeypatch.setattr(
        cloud, "has_successful_scheduled_run", lambda *args, **kwargs: False
    )
    _seen_stale_before(NOW)

    jobs.run_cloud_supervision_job(ctx, NOW)

    assert len(delivered) == 1
    finding = delivered[0]
    assert finding.kind == "WATCHER_ERROR"
    assert finding.kind not in ctx.cfg.silent_kinds
    assert finding.key == "cloud_stale:episode:1"
    assert finding.lines[0] == "Dune : Troisième partie · Pathé Odysseum"
    assert "No successful scheduled cloud run was found in the last 18 h." in (
        finding.lines
    )
    assert all("Last successful" not in line for line in finding.lines)
    assert "cloud failover and supervision are dark" in finding.lines[-1]


def test_quiet_but_successful_cloud_run_is_healthy_and_changes_no_state(monkeypatch):
    ctx = _context()
    before = deepcopy(ctx.state)
    delivered = _capture_delivery(monkeypatch, ctx)
    monkeypatch.setattr(
        cloud,
        "has_successful_scheduled_run",
        lambda *args, **kwargs: True,
    )

    health = jobs.run_cloud_supervision_job(ctx, NOW)

    assert health == "healthy"
    assert delivered == []
    assert ctx.state == before


def test_github_api_blip_fails_quietly(monkeypatch):
    ctx = _context()
    before = deepcopy(ctx.state)
    delivered = _capture_delivery(monkeypatch, ctx)

    def unavailable(*args, **kwargs):
        raise cloud.CloudStatusError("temporary API failure")

    monkeypatch.setattr(cloud, "has_successful_scheduled_run", unavailable)

    health = jobs.run_cloud_supervision_job(ctx, NOW)

    assert health == "unknown"
    assert delivered == []
    assert ctx.state == before


def test_one_empty_page_is_unconfirmed_and_stays_quiet(monkeypatch):
    """2026-10-02: one firing got an empty page for a window holding four
    successes, and the next firing saw them again. One page is not an outage.
    """
    ctx = _context()
    before = deepcopy(ctx.state)
    delivered = _capture_delivery(monkeypatch, ctx)
    healthy = [False]
    monkeypatch.setattr(
        cloud, "has_successful_scheduled_run", lambda *args, **kwargs: healthy[0]
    )

    assert jobs.run_cloud_supervision_job(ctx, NOW) == "unknown"
    assert delivered == []
    assert ctx.state == before
    assert cloud.STALE_SUSPICION_PATH.exists()

    healthy[0] = True
    assert jobs.run_cloud_supervision_job(ctx, NOW + timedelta(minutes=5)) == "healthy"
    assert not cloud.STALE_SUSPICION_PATH.exists()

    # The cleared sighting cannot confirm a later stale verdict.
    healthy[0] = False
    later = NOW + timedelta(minutes=40)
    assert jobs.run_cloud_supervision_job(ctx, later) == "unknown"
    assert delivered == []


def test_stale_verdict_alerts_only_once_confirmed_a_window_later(monkeypatch):
    ctx = _context()
    delivered = _capture_delivery(monkeypatch, ctx)
    monkeypatch.setattr(
        cloud, "has_successful_scheduled_run", lambda *args, **kwargs: False
    )

    window = cloud.STALE_CONFIRMATION
    for minutes in range(0, int(window.total_seconds() // 60), 5):
        now = NOW + timedelta(minutes=minutes)
        assert jobs.run_cloud_supervision_job(ctx, now) == "unknown"
    assert delivered == []

    assert jobs.run_cloud_supervision_job(ctx, NOW + window) == "stale"
    assert [finding.key for finding in delivered] == ["cloud_stale:episode:1"]


def test_api_blip_between_sightings_neither_confirms_nor_resets(monkeypatch):
    ctx = _context()
    delivered = _capture_delivery(monkeypatch, ctx)
    verdicts = iter([False, cloud.CloudStatusError("blip"), False])

    def probe(*args, **kwargs):
        verdict = next(verdicts)
        if isinstance(verdict, Exception):
            raise verdict
        return verdict

    monkeypatch.setattr(cloud, "has_successful_scheduled_run", probe)

    assert jobs.run_cloud_supervision_job(ctx, NOW) == "unknown"
    assert jobs.run_cloud_supervision_job(
        ctx, NOW + timedelta(minutes=15)
    ) == "unknown"
    assert jobs.run_cloud_supervision_job(
        ctx, NOW + cloud.STALE_CONFIRMATION
    ) == "stale"
    assert [finding.key for finding in delivered] == ["cloud_stale:episode:1"]


def test_sighting_after_a_long_gap_restarts_confirmation(monkeypatch):
    ctx = _context()
    delivered = _capture_delivery(monkeypatch, ctx)
    monkeypatch.setattr(
        cloud, "has_successful_scheduled_run", lambda *args, **kwargs: False
    )

    assert jobs.run_cloud_supervision_job(ctx, NOW) == "unknown"
    # The Mac slept: the next sighting is not continuous with the first.
    woke = NOW + cloud.STALE_SIGHTING_MAX_GAP + timedelta(minutes=5)
    assert jobs.run_cloud_supervision_job(ctx, woke) == "unknown"
    assert delivered == []
    assert jobs.run_cloud_supervision_job(
        ctx, woke + cloud.STALE_CONFIRMATION
    ) == "stale"
    assert len(delivered) == 1


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        "[]",
        '{"first_seen": "2026-09-20T11:00:00+02:00"}',
        (
            '{"first_seen": "2026-09-20T11:50:00+02:00",'
            ' "last_seen": "2026-09-20T11:00:00+02:00"}'
        ),
        (
            '{"first_seen": "2026-09-20T13:00:00+02:00",'
            ' "last_seen": "2026-09-20T13:00:00+02:00"}'
        ),
    ],
    ids=["malformed", "not-object", "missing-field", "inverted", "future"],
)
def test_untrustworthy_sighting_record_restarts_confirmation(content, monkeypatch):
    ctx = _context()
    delivered = _capture_delivery(monkeypatch, ctx)
    monkeypatch.setattr(
        cloud, "has_successful_scheduled_run", lambda *args, **kwargs: False
    )
    cloud.STALE_SUSPICION_PATH.write_text(content, encoding="utf-8")

    assert jobs.run_cloud_supervision_job(ctx, NOW) == "unknown"
    assert delivered == []


def _dry_context():
    ctx = _context()
    ctx.dry_run = True
    return ctx


def test_stale_dry_run_leaves_no_evidence_for_a_later_real_alert(monkeypatch):
    dry = _dry_context()
    _capture_delivery(monkeypatch, dry)
    monkeypatch.setattr(
        cloud, "has_successful_scheduled_run", lambda *args, **kwargs: False
    )

    assert jobs.run_cloud_supervision_job(dry, NOW) == "unknown"
    assert not cloud.STALE_SUSPICION_PATH.exists()

    # The rehearsal cannot serve as the first sighting of a real firing.
    real = _context()
    delivered = _capture_delivery(monkeypatch, real)
    later = NOW + cloud.STALE_CONFIRMATION
    assert jobs.run_cloud_supervision_job(real, later) == "unknown"
    assert delivered == []


def test_healthy_dry_run_keeps_an_existing_sighting(monkeypatch):
    cloud.record_stale_sighting(NOW)
    before = cloud.STALE_SUSPICION_PATH.read_text(encoding="utf-8")
    dry = _dry_context()
    _capture_delivery(monkeypatch, dry)
    monkeypatch.setattr(
        cloud, "has_successful_scheduled_run", lambda *args, **kwargs: True
    )

    assert jobs.run_cloud_supervision_job(dry, NOW + timedelta(minutes=5)) == "healthy"
    assert cloud.STALE_SUSPICION_PATH.read_text(encoding="utf-8") == before


def test_dry_run_reports_the_verdict_a_real_firing_would_without_writing(
    monkeypatch,
):
    _seen_stale_before(NOW)
    before = cloud.STALE_SUSPICION_PATH.read_text(encoding="utf-8")
    dry = _dry_context()
    delivered = _capture_delivery(monkeypatch, dry)
    monkeypatch.setattr(
        cloud, "has_successful_scheduled_run", lambda *args, **kwargs: False
    )

    assert jobs.run_cloud_supervision_job(dry, NOW) == "stale"
    assert [finding.key for finding in delivered] == ["cloud_stale:episode:1"]
    assert cloud.STALE_SUSPICION_PATH.read_text(encoding="utf-8") == before


def test_api_blip_binds_and_defers_a_legacy_pending_heartbeat(tmp_path, monkeypatch):
    ctx = _durable_context(tmp_path)
    attempts = []
    heartbeat = Finding(
        kind="HEARTBEAT",
        key="heartbeat:2026-09-20",
        confidence="high",
        title="All quiet — nothing new",
        lines=["Dune : Troisième partie · Pathé Odysseum", "All checks healthy."],
        url=None,
    )
    monkeypatch.setattr(
        notify,
        "send_telegram",
        lambda cfg, text, **kwargs: attempts.append(text)
        or notify.SendResult("failed"),
    )
    assert delivery.deliver_heartbeat(ctx, heartbeat, NOW) is False
    pending = next(iter(ctx.state["outbox"].values()))
    pending["topics"] = []  # pre-round-2 record
    ctx.delivery_attempts.clear()

    monkeypatch.setattr(
        cloud,
        "has_successful_scheduled_run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            cloud.CloudStatusError("API blip")
        ),
    )
    assert jobs.run_cloud_supervision_job(ctx, NOW + timedelta(minutes=5)) == "unknown"
    assert pending["topics"] == ["condition:cloud-health=healthy"]
    assert delivery.recover(
        ctx,
        NOW + timedelta(minutes=5),
        blocked_condition_domains={"cloud-health"},
    ) is False
    assert len(attempts) == 1


def test_cloud_alert_dedups_per_outage_and_rearms_after_a_later_success(monkeypatch):
    ctx = _context()
    delivered = _capture_delivery(monkeypatch, ctx)
    healthy = [False]
    monkeypatch.setattr(
        cloud, "has_successful_scheduled_run", lambda *args, **kwargs: healthy[0]
    )
    _seen_stale_before(NOW)

    jobs.run_cloud_supervision_job(ctx, NOW)
    jobs.run_cloud_supervision_job(ctx, NOW)
    assert [finding.key for finding in delivered] == ["cloud_stale:episode:1"]

    # A fresh success is healthy and re-arms a later, distinct outage without
    # deleting the durable receipt for the first one.
    healthy[0] = True
    jobs.run_cloud_supervision_job(ctx, NOW)
    assert len(delivered) == 1

    healthy[0] = False
    later_now = NOW + timedelta(days=1)
    _seen_stale_before(later_now)
    jobs.run_cloud_supervision_job(ctx, later_now)
    assert [finding.key for finding in delivered] == [
        "cloud_stale:episode:1",
        "cloud_stale:episode:2",
    ]


def test_legacy_timestamp_keys_wait_for_positive_recovery_before_rearming(monkeypatch):
    ctx = _context()
    delivered = _capture_delivery(monkeypatch, ctx)
    ctx.state["alerts"].update(
        {
            "cloud_stale:2026-09-13T08:15:00+02:00": NOW.isoformat(),
            "cloud_stale:2026-09-06T08:15:00+02:00": NOW.isoformat(),
        }
    )
    healthy = [False]
    monkeypatch.setattr(
        cloud, "has_successful_scheduled_run", lambda *args, **kwargs: healthy[0]
    )
    _seen_stale_before(NOW)

    assert jobs.run_cloud_supervision_job(ctx, NOW) == "stale"
    assert delivered == []

    healthy[0] = True
    assert jobs.run_cloud_supervision_job(ctx, NOW) == "healthy"
    assert {
        key for key in ctx.state["alerts"] if key.startswith("cloud_recovered:")
    } == {
        "cloud_recovered:2026-09-13T08:15:00+02:00",
        "cloud_recovered:2026-09-06T08:15:00+02:00",
    }

    healthy[0] = False
    _seen_stale_before(NOW + timedelta(days=1))
    assert jobs.run_cloud_supervision_job(ctx, NOW + timedelta(days=1)) == "stale"
    assert [finding.key for finding in delivered] == ["cloud_stale:episode:1"]


def test_fresh_cloud_retires_failed_stale_alert_before_recovery(tmp_path, monkeypatch):
    ctx = _durable_context(tmp_path)
    attempts = []
    healthy = [False]
    monkeypatch.setattr(
        cloud, "has_successful_scheduled_run", lambda *args, **kwargs: healthy[0]
    )
    monkeypatch.setattr(
        notify,
        "send_telegram",
        lambda cfg, text, **kwargs: attempts.append(text)
        or notify.SendResult("failed"),
    )
    _seen_stale_before(NOW)

    assert jobs.run_cloud_supervision_job(ctx, NOW) == "stale"
    assert len(attempts) == 1
    assert next(iter(ctx.state["outbox"].values()))["topics"] == [
        "condition:cloud-health=stale"
    ]

    # Compatibility with a failed alert queued by the first OTW-09 revision,
    # before cloud-health conditions existed on outbox records.
    pending = next(iter(ctx.state["outbox"].values()))
    pending["topics"] = []
    for member in pending["members"]:
        member["topics"] = []

    # The next run proves recovery before outbox replay. The pending loud alert
    # is contradicted and removed, never sent after the outage has ended.
    ctx.delivery_attempts.clear()
    healthy[0] = True
    assert jobs.run_cloud_supervision_job(ctx, NOW + timedelta(minutes=5)) == "healthy"
    assert ctx.state["outbox"] == {}
    assert delivery.recover(ctx, NOW + timedelta(minutes=5)) is False
    assert len(attempts) == 1


def test_stale_cloud_suppresses_weekly_healthy_heartbeat(monkeypatch):
    ctx = _context()
    ctx.cfg.heartbeat_days = 7
    monkeypatch.setattr(
        delivery,
        "deliver_heartbeat",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("stale cloud must suppress a healthy heartbeat")
        ),
    )

    jobs.run_heartbeat_job(ctx, Snapshot(), NOW, False, "stale")


def test_pending_healthy_heartbeat_is_retired_when_cloud_turns_stale(
    tmp_path, monkeypatch
):
    ctx = _durable_context(tmp_path)
    attempts = []
    heartbeat = Finding(
        kind="HEARTBEAT",
        key="heartbeat:2026-09-20",
        confidence="high",
        title="All quiet — nothing new",
        lines=["Dune : Troisième partie · Pathé Odysseum", "All checks healthy."],
        url=None,
    )
    monkeypatch.setattr(
        notify,
        "send_telegram",
        lambda cfg, text, **kwargs: attempts.append(text)
        or notify.SendResult("failed"),
    )
    assert delivery.deliver_heartbeat(ctx, heartbeat, NOW) is False
    assert "All checks healthy" in attempts[0]

    ctx.delivery_attempts.clear()
    monkeypatch.setattr(
        cloud,
        "has_successful_scheduled_run",
        lambda *args, **kwargs: False,
    )
    _seen_stale_before(NOW + timedelta(minutes=5))
    assert jobs.run_cloud_supervision_job(ctx, NOW + timedelta(minutes=5)) == "stale"
    assert all(
        record["kinds"] != ["HEARTBEAT"]
        for record in ctx.state["outbox"].values()
    )
    delivery.recover(ctx, NOW + timedelta(minutes=5))
    assert sum("All checks healthy" in text for text in attempts) == 1


def test_cloud_recovery_binds_but_never_replays_pending_cloud_health_work(
    tmp_path, monkeypatch
):
    ctx = _durable_context(tmp_path)
    attempts = []
    monkeypatch.setattr(
        cloud,
        "has_successful_scheduled_run",
        lambda *args, **kwargs: False,
    )
    monkeypatch.setattr(
        notify,
        "send_telegram",
        lambda cfg, text, **kwargs: attempts.append(text)
        or notify.SendResult("failed"),
    )
    _seen_stale_before(NOW)
    assert jobs.run_cloud_supervision_job(ctx, NOW) == "stale"
    pending = next(iter(ctx.state["outbox"].values()))
    pending["topics"] = []
    for member in pending["members"]:
        member["topics"] = []
    ctx.delivery_attempts.clear()

    assert delivery.recover_cloud(ctx, NOW + timedelta(minutes=5)) is False
    assert pending["topics"] == ["condition:cloud-health=stale"]
    assert pending["members"][0]["topics"] == [
        "condition:cloud-health=stale"
    ]
    assert len(attempts) == 1


def test_runner_probes_cloud_before_recovery_and_heartbeat(monkeypatch, tmp_path):
    ctx = _context()
    ctx.dry_run = True
    trace = []
    monkeypatch.setattr(
        runner, "_run_source_jobs", lambda *args: (False, Snapshot())
    )
    monkeypatch.setattr(jobs, "run_reminder_job", lambda *args, **kwargs: False)
    monkeypatch.setattr(jobs, "run_state_sync_failure_job", lambda *args: False)
    monkeypatch.setattr(jobs, "run_supervision_job", lambda *args: None)
    monkeypatch.setattr(
        jobs,
        "run_cloud_supervision_job",
        lambda *args: trace.append("cloud") or "stale",
    )
    monkeypatch.setattr(
        delivery,
        "recover",
        lambda *args, **kwargs: trace.append("recover") or False,
    )

    def heartbeat(*args):
        trace.append(("heartbeat", args[-1]))

    monkeypatch.setattr(jobs, "run_heartbeat_job", heartbeat)

    assert runner.execute(ctx, str(tmp_path / "state.json")) == 0
    assert trace == ["cloud", "recover", ("heartbeat", "stale")]


def test_both_cloud_runner_branches_use_condition_aware_recovery(
    monkeypatch, tmp_path
):
    trace = []
    monkeypatch.setattr(jobs, "run_reminder_job", lambda *args, **kwargs: False)
    monkeypatch.setattr(jobs, "run_state_sync_failure_job", lambda *args: False)
    monkeypatch.setattr(jobs, "run_supervision_job", lambda *args: None)
    monkeypatch.setattr(runner, "_run_cloud_news_job", lambda *args: False)
    monkeypatch.setattr(
        delivery,
        "recover_cloud",
        lambda *args: trace.append("cloud-recovery") or False,
    )
    monkeypatch.setattr(
        delivery,
        "recover",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("cloud mode used unguarded recovery")
        ),
    )

    for with_news in (False, True):
        ctx = _context()
        ctx.mode = "remind"
        ctx.with_news = with_news
        ctx.dry_run = True
        assert runner.execute(ctx, str(tmp_path / f"state-{with_news}.json")) == 0

    assert trace == ["cloud-recovery", "cloud-recovery"]


def _api_run(run_id, created_at, updated_at, **overrides):
    run = {
        "id": run_id,
        "event": "schedule",
        "status": "completed",
        "conclusion": "success",
        "created_at": created_at,
        "updated_at": updated_at,
    }
    run.update(overrides)
    return run


def _response(body):
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            if isinstance(body, Exception):
                raise body
            return body

    return Response()


def test_actions_api_queries_and_validates_the_complete_health_window(monkeypatch):
    calls = []
    body = {
        "total_count": 2,
        "workflow_runs": [
            _api_run(1, "2026-09-19T15:50:00Z", "2026-09-19T15:55:00Z"),
            _api_run(2, "2026-09-20T08:10:00Z", "2026-09-20T08:15:00Z"),
        ],
    }

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return _response(body)

    monkeypatch.setattr(cloud.httpx, "get", fake_get)

    result = cloud.has_successful_scheduled_run(
        "serioznuh/odysseum-ticket-watch",
        "watch.yml",
        since=NOW - timedelta(hours=18),
        until=NOW,
    )

    assert result is True
    assert calls[0][0].endswith(
        "/repos/serioznuh/odysseum-ticket-watch/actions/workflows/watch.yml/runs"
    )
    assert calls[0][1]["params"] == {
        "event": "schedule",
        "status": "success",
        "created": "2026-09-19T15:45:00Z..2026-09-20T10:00:00Z",
        "per_page": 74,
    }


@pytest.mark.parametrize("old_day", ["2026-09-13", "2026-09-06"])
def test_old_anonymous_row_fixture_cannot_raise_a_false_alert(
    old_day, monkeypatch
):
    """The two production responses omitted a known recent run and violated
    the new created filter. Such contradictory evidence is unknown, not stale.
    """
    ctx = _context()
    delivered = _capture_delivery(monkeypatch, ctx)
    body = {
        "total_count": 1,
        "workflow_runs": [
            _api_run(
                1,
                f"{old_day}T08:10:00Z",
                f"{old_day}T08:15:00Z",
            )
        ],
    }
    monkeypatch.setattr(cloud.httpx, "get", lambda *args, **kwargs: _response(body))

    assert jobs.run_cloud_supervision_job(ctx, NOW) == "unknown"
    assert delivered == []


def test_alternating_old_rows_during_real_outage_stay_one_episode(monkeypatch):
    ctx = _context()
    delivered = _capture_delivery(monkeypatch, ctx)
    bodies = iter(
        [
            {
                "total_count": 1,
                "workflow_runs": [
                    _api_run(
                        13,
                        "2026-09-19T15:50:00Z",
                        "2026-09-19T15:55:00Z",
                    )
                ],
            },
            {
                "total_count": 1,
                "workflow_runs": [
                    _api_run(
                        6,
                        "2026-09-19T15:55:00Z",
                        "2026-09-19T16:00:00Z",
                    )
                ],
            },
        ]
    )
    monkeypatch.setattr(
        cloud.httpx,
        "get",
        lambda *args, **kwargs: _response(next(bodies)),
    )
    _seen_stale_before(NOW)

    assert jobs.run_cloud_supervision_job(ctx, NOW) == "stale"
    assert jobs.run_cloud_supervision_job(ctx, NOW + timedelta(minutes=5)) == "stale"
    assert [finding.key for finding in delivered] == ["cloud_stale:episode:1"]


def test_complete_empty_actions_page_is_authoritative_absence(monkeypatch):
    monkeypatch.setattr(
        cloud.httpx,
        "get",
        lambda *args, **kwargs: _response({"total_count": 0, "workflow_runs": []}),
    )

    assert cloud.has_successful_scheduled_run(
        "serioznuh/odysseum-ticket-watch",
        "watch.yml",
        since=NOW - timedelta(hours=18),
        until=NOW,
    ) is False


def test_partial_page_with_recent_success_is_healthy_and_rearms(monkeypatch):
    ctx = _context()
    delivered = _capture_delivery(monkeypatch, ctx)
    state_mod.mark_sent(ctx.state, "cloud_stale:episode:1", NOW - timedelta(days=1))
    body = {
        "total_count": 2,
        "workflow_runs": [
            _api_run(2, "2026-09-20T08:10:00Z", "2026-09-20T08:15:00Z")
        ],
    }
    monkeypatch.setattr(cloud.httpx, "get", lambda *args, **kwargs: _response(body))

    assert jobs.run_cloud_supervision_job(ctx, NOW) == "healthy"
    assert state_mod.already_sent(ctx.state, "cloud_recovered:episode:1")
    assert delivered == []

    monkeypatch.setattr(
        cloud, "has_successful_scheduled_run", lambda *args, **kwargs: False
    )
    _seen_stale_before(NOW + timedelta(days=1))
    assert jobs.run_cloud_supervision_job(ctx, NOW + timedelta(days=1)) == "stale"
    assert [finding.key for finding in delivered] == ["cloud_stale:episode:2"]


@pytest.mark.parametrize(
    "body",
    [
        ValueError("not JSON"),
        {"workflow_runs": []},
        {"total_count": 2, "workflow_runs": [_api_run(
            1, "2026-09-19T15:50:00Z", "2026-09-19T15:55:00Z"
        )]},
        {"total_count": 1, "workflow_runs": [{"id": 1}]},
        {"total_count": 1, "workflow_runs": [_api_run(
            1,
            "2026-09-20T08:10:00Z",
            "2026-09-20T08:15:00Z",
            event="workflow_dispatch",
        )]},
    ],
    ids=["malformed-json", "missing-count", "partial", "malformed-row", "contradictory"],
)
def test_uncertain_actions_pages_fail_quiet(body, monkeypatch):
    ctx = _context()
    before = deepcopy(ctx.state)
    delivered = _capture_delivery(monkeypatch, ctx)
    monkeypatch.setattr(cloud.httpx, "get", lambda *args, **kwargs: _response(body))

    assert jobs.run_cloud_supervision_job(ctx, NOW) == "unknown"
    assert delivered == []
    assert ctx.state == before


def test_actions_api_transport_error_is_not_liveness_evidence(monkeypatch):
    request = httpx.Request("GET", "https://api.github.com/")
    monkeypatch.setattr(
        cloud.httpx,
        "get",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            httpx.ConnectError("offline", request=request)
        ),
    )

    try:
        cloud.has_successful_scheduled_run(
            "serioznuh/odysseum-ticket-watch",
            "watch.yml",
            since=NOW - timedelta(hours=18),
            until=NOW,
        )
    except cloud.CloudStatusError as exc:
        assert "unavailable" in str(exc)
    else:
        raise AssertionError("API failure must not look like a stale success")


def test_scheduled_workflow_probes_telegram_after_failover_without_gating_it():
    root = Path(__file__).resolve().parent.parent
    workflow = (root / ".github" / "workflows" / "watch.yml").read_text(
        encoding="utf-8"
    )

    watcher_pass = workflow.index("Run watcher (")
    probe = workflow.index("--check-telegram", watcher_pass)
    assert watcher_pass < probe
    # `always()`: a failed failover pass must never cost the credential probe.
    # It may carry further conditions (OTW-30 skips it when state was never
    # initialized, pinned in test_state_sync.py), but not the watcher's outcome.
    condition = workflow[watcher_pass:probe]
    assert "if: ${{ always()" in condition
    assert "steps.mode" not in condition[condition.index("if: ${{ always()") :]
