"""OTW-09: the local half supervises the scheduled cloud half."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx

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
    last_success = NOW - timedelta(hours=19)
    delivered = _capture_delivery(monkeypatch, ctx)
    monkeypatch.setattr(
        cloud, "latest_successful_scheduled_run", lambda *args: last_success
    )

    jobs.run_cloud_supervision_job(ctx, NOW)

    assert len(delivered) == 1
    finding = delivered[0]
    assert finding.kind == "WATCHER_ERROR"
    assert finding.kind not in ctx.cfg.silent_kinds
    assert finding.key == f"cloud_stale:{last_success.isoformat()}"
    assert finding.lines[0] == "Dune : Troisième partie · Pathé Odysseum"
    assert "cloud failover and supervision are dark" in finding.lines[-1]


def test_quiet_but_successful_cloud_run_is_healthy_and_changes_no_state(monkeypatch):
    ctx = _context()
    before = deepcopy(ctx.state)
    delivered = _capture_delivery(monkeypatch, ctx)
    monkeypatch.setattr(
        cloud,
        "latest_successful_scheduled_run",
        lambda *args: NOW - timedelta(minutes=30),
    )

    health = jobs.run_cloud_supervision_job(ctx, NOW)

    assert health == "healthy"
    assert delivered == []
    assert ctx.state == before


def test_github_api_blip_fails_quietly(monkeypatch):
    ctx = _context()
    before = deepcopy(ctx.state)
    delivered = _capture_delivery(monkeypatch, ctx)

    def unavailable(*args):
        raise cloud.CloudStatusError("temporary API failure")

    monkeypatch.setattr(cloud, "latest_successful_scheduled_run", unavailable)

    health = jobs.run_cloud_supervision_job(ctx, NOW)

    assert health == "unknown"
    assert delivered == []
    assert ctx.state == before


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
        "latest_successful_scheduled_run",
        lambda *args: (_ for _ in ()).throw(cloud.CloudStatusError("API blip")),
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
    old_success = NOW - timedelta(days=2)
    latest = [old_success]
    monkeypatch.setattr(
        cloud, "latest_successful_scheduled_run", lambda *args: latest[0]
    )

    jobs.run_cloud_supervision_job(ctx, NOW)
    jobs.run_cloud_supervision_job(ctx, NOW)
    assert [finding.key for finding in delivered] == [
        f"cloud_stale:{old_success.isoformat()}"
    ]

    # A fresh success is healthy and re-arms a later, distinct outage without
    # deleting the durable receipt for the first one.
    later_success = NOW - timedelta(hours=1)
    latest[0] = later_success
    jobs.run_cloud_supervision_job(ctx, NOW)
    assert len(delivered) == 1

    later_now = NOW + timedelta(days=1)
    jobs.run_cloud_supervision_job(ctx, later_now)
    assert [finding.key for finding in delivered] == [
        f"cloud_stale:{old_success.isoformat()}",
        f"cloud_stale:{later_success.isoformat()}",
    ]


def test_fresh_cloud_retires_failed_stale_alert_before_recovery(tmp_path, monkeypatch):
    ctx = _durable_context(tmp_path)
    attempts = []
    latest = [NOW - timedelta(days=2)]
    monkeypatch.setattr(
        cloud, "latest_successful_scheduled_run", lambda *args: latest[0]
    )
    monkeypatch.setattr(
        notify,
        "send_telegram",
        lambda cfg, text, **kwargs: attempts.append(text)
        or notify.SendResult("failed"),
    )

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
    latest[0] = NOW + timedelta(minutes=1)
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
        "latest_successful_scheduled_run",
        lambda *args: NOW - timedelta(days=2),
    )
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
        "latest_successful_scheduled_run",
        lambda *args: NOW - timedelta(days=2),
    )
    monkeypatch.setattr(
        notify,
        "send_telegram",
        lambda cfg, text, **kwargs: attempts.append(text)
        or notify.SendResult("failed"),
    )
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


def test_actions_api_requests_only_the_latest_scheduled_success(monkeypatch):
    calls = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"workflow_runs": [{"updated_at": "2026-09-20T08:15:00Z"}]}

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return Response()

    monkeypatch.setattr(cloud.httpx, "get", fake_get)

    result = cloud.latest_successful_scheduled_run(
        "serioznuh/odysseum-ticket-watch", "watch.yml"
    )

    assert result == datetime(2026, 9, 20, 8, 15, tzinfo=timezone.utc)
    assert calls[0][0].endswith(
        "/repos/serioznuh/odysseum-ticket-watch/actions/workflows/watch.yml/runs"
    )
    assert calls[0][1]["params"] == {
        "event": "schedule",
        "status": "success",
        "per_page": 1,
    }


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
        cloud.latest_successful_scheduled_run(
            "serioznuh/odysseum-ticket-watch", "watch.yml"
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
    assert "if: ${{ always() }}" in workflow[watcher_pass:probe]
