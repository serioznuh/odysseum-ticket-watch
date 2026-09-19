"""OTW-09: the local half supervises the scheduled cloud half."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx

from watcher import cloud, jobs
from watcher import state as state_mod
from watcher.detect import TZ_PARIS

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

    jobs.run_cloud_supervision_job(ctx, NOW)

    assert delivered == []
    assert ctx.state == before


def test_github_api_blip_fails_quietly(monkeypatch):
    ctx = _context()
    before = deepcopy(ctx.state)
    delivered = _capture_delivery(monkeypatch, ctx)

    def unavailable(*args):
        raise cloud.CloudStatusError("temporary API failure")

    monkeypatch.setattr(cloud, "latest_successful_scheduled_run", unavailable)

    jobs.run_cloud_supervision_job(ctx, NOW)

    assert delivered == []
    assert ctx.state == before


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


def test_scheduled_workflow_probes_telegram_before_the_watcher_pass():
    root = Path(__file__).resolve().parent.parent
    workflow = (root / ".github" / "workflows" / "watch.yml").read_text(
        encoding="utf-8"
    )

    probe = workflow.index("--check-telegram")
    watcher_pass = workflow.index("Run watcher (", probe)
    assert probe < watcher_pass
