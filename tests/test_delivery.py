"""Fault-injection coverage for the durable notification boundary (OTW-20)."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from typing import ClassVar

import pytest

from watcher import coalesce, delivery, jobs, notify
from watcher.detect import TZ_PARIS, CinesaSnapshot, FetchResult, Finding, Snapshot
from watcher.state import DEFAULT_STATE, load_state, save_state
from watcher.state_merge import merge_states

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=TZ_PARIS)


class Cfg:
    silent_kinds: ClassVar = list(notify.DEFAULT_SILENT_KINDS)
    telegram_token = "secret-token"
    telegram_chat_id = "private-chat"
    reminder_offsets_minutes: ClassVar = [1440, 120, 15]
    cinema_name = "Pathé Odysseum"
    cinema_city = "Montpellier"
    film_title = "Dune : Troisième partie"
    pathe_target_format = "imax70"
    pathe_target_dates: ClassVar = []
    primary_slug = "dune"
    cinema_slug = "odysseum"
    pathe_page_url = "https://example.invalid/event"
    film_page_url = "https://example.invalid/film"
    cinesa_target_dates: ClassVar = []
    cinesa_site_id = "032"
    cinesa_film_id = "HO00003228"
    cinesa_imax_attribute_id = "imax"


def finding(
    key: str = "new_show:dune-imax",
    *,
    kind: str = "NEW_LISTING",
    title: str = "New listing",
    sale_datetime: str | None = None,
) -> Finding:
    return Finding(
        kind=kind,
        key=key,
        confidence="high",
        title=title,
        lines=["Dune : Troisième partie · Pathé Odysseum"],
        url="https://example.invalid",
        sale_datetime=sale_datetime,
        merge_item="imax70",
    )


def context(path, state=None, writer=None):
    state = deepcopy(DEFAULT_STATE) if state is None else state
    save_state(path, state)
    return jobs.RunContext(
        cfg=Cfg,
        state=state,
        clock=lambda: NOW,
        state_path=str(path),
        state_writer=writer,
    )


def alert(*members: Finding) -> coalesce.Alert:
    lead = members[0]
    return coalesce.Alert(
        finding=lead,
        keys=[member.key for member in members],
        kinds=[member.kind for member in members],
        members=list(members),
    )


def test_restart_after_enqueue_but_before_send_retries_pending_work(
    tmp_path, monkeypatch
):
    state_path = tmp_path / "state.json"
    writes = 0

    def crash_before_claim(path, state):
        nonlocal writes
        writes += 1
        if writes == 2:
            raise RuntimeError("process stopped before Telegram")
        save_state(path, state)

    ctx = context(state_path, writer=crash_before_claim)
    sent = []
    monkeypatch.setattr(
        notify,
        "send_telegram",
        lambda *args, **kwargs: sent.append("sent") or notify.SendResult("confirmed", 7),
    )

    with pytest.raises(RuntimeError, match="before Telegram"):
        delivery.deliver_alert(ctx, alert(finding()), NOW)

    assert sent == []
    durable = load_state(state_path)
    assert next(iter(durable["outbox"].values()))["status"] == "pending"

    restarted = context(state_path, state=durable)
    assert delivery.recover(restarted, NOW + timedelta(minutes=1)) is True
    recovered = load_state(state_path)
    assert recovered["outbox"] == {}
    assert "new_show:dune-imax" in recovered["alerts"]
    assert next(iter(recovered["delivery_receipts"].values()))[
        "telegram_message_id"
    ] == 7


def test_failed_claim_persistence_stays_pending_and_never_becomes_uncertain(
    tmp_path, monkeypatch
):
    """A failed pre-send save proves Telegram was never called. Same-run
    recovery must preserve that fact rather than quarantine the work."""
    state_path = tmp_path / "state.json"
    writes = 0

    def fail_claim_once(path, state):
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("claim save failed before Telegram")
        save_state(path, state)

    ctx = context(state_path, writer=fail_claim_once)
    calls = []
    monkeypatch.setattr(
        notify,
        "send_telegram",
        lambda *args, **kwargs: calls.append("send") or notify.SendResult("confirmed"),
    )

    with pytest.raises(OSError, match="before Telegram"):
        delivery.deliver_alert(ctx, alert(finding()), NOW)

    assert calls == []
    assert next(iter(ctx.state["outbox"].values()))["status"] == "pending"
    assert delivery.recover(ctx, NOW) is False
    durable = load_state(state_path)
    assert next(iter(durable["outbox"].values()))["status"] == "pending"

    restarted = context(state_path, state=durable)
    assert delivery.recover(restarted, NOW + timedelta(minutes=1)) is True
    assert calls == ["send"]
    assert load_state(state_path)["outbox"] == {}


def test_confirmed_receipt_survives_a_later_process_crash(tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    ctx = context(state_path)
    monkeypatch.setattr(
        notify,
        "send_telegram",
        lambda *args, **kwargs: notify.SendResult("confirmed", 42),
    )

    assert delivery.deliver_alert(ctx, alert(finding()), NOW) is True
    # No coordinator/final save follows: loading the file models a later crash.
    durable = load_state(state_path)

    assert durable["outbox"] == {}
    assert durable["alerts"]["new_show:dune-imax"] == NOW.isoformat()
    receipt = next(iter(durable["delivery_receipts"].values()))
    assert receipt == {
        "delivery_id": receipt["delivery_id"],
        "keys": ["new_show:dune-imax"],
        "delivered_at": NOW.isoformat(),
        "telegram_message_id": 42,
    }
    rendered = state_path.read_text(encoding="utf-8")
    assert Cfg.telegram_token not in rendered
    assert Cfg.telegram_chat_id not in rendered


def test_uncertain_outcome_is_quarantined_and_not_replayed(tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    ctx = context(state_path)
    calls = []
    monkeypatch.setattr(
        notify,
        "send_telegram",
        lambda *args, **kwargs: calls.append("attempt") or notify.SendResult("uncertain"),
    )

    assert delivery.deliver_alert(ctx, alert(finding()), NOW) is False
    uncertain = load_state(state_path)
    record = next(iter(uncertain["outbox"].values()))
    assert record["status"] == "uncertain"
    assert uncertain["alerts"] == {}
    assert uncertain["delivery_receipts"] == {}

    restarted = context(state_path, state=uncertain)
    assert delivery.recover(restarted, NOW + timedelta(minutes=5)) is False
    assert calls == ["attempt"]


def test_sender_crash_is_durable_uncertain_and_still_fails_the_job(
    tmp_path, monkeypatch
):
    state_path = tmp_path / "state.json"
    ctx = context(state_path)

    def crash(*args, **kwargs):
        raise RuntimeError("connection vanished during send")

    monkeypatch.setattr(notify, "send_telegram", crash)

    with pytest.raises(RuntimeError, match="vanished"):
        delivery.deliver_alert(ctx, alert(finding()), NOW)

    durable = load_state(state_path)
    assert next(iter(durable["outbox"].values()))["status"] == "uncertain"
    assert durable["alerts"] == {}


def test_failed_receipt_persistence_recovers_as_uncertain(tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    writes = 0

    def fail_confirmation_once(path, state):
        nonlocal writes
        writes += 1
        if writes == 3:
            raise OSError("disk full after Telegram confirmation")
        save_state(path, state)

    ctx = context(state_path, writer=fail_confirmation_once)
    monkeypatch.setattr(
        notify,
        "send_telegram",
        lambda *args, **kwargs: notify.SendResult("confirmed", 99),
    )

    with pytest.raises(delivery.DeliveryPersistenceError, match="receipt"):
        delivery.deliver_alert(ctx, alert(finding()), NOW)

    durable = load_state(state_path)
    assert next(iter(durable["outbox"].values()))["status"] == "uncertain"
    assert durable["alerts"] == {}
    assert durable["delivery_receipts"] == {}


def test_merged_group_failure_then_success_acknowledges_every_key_atomically(
    tmp_path, monkeypatch
):
    state_path = tmp_path / "state.json"
    ctx = context(state_path)
    members = (finding("new_show:a"), finding("new_show:b"))
    outcomes = iter((notify.SendResult("failed"), notify.SendResult("confirmed", 12)))
    monkeypatch.setattr(notify, "send_telegram", lambda *args, **kwargs: next(outcomes))

    assert delivery.deliver_alert(ctx, alert(*members), NOW) is False
    failed = load_state(state_path)
    assert failed["alerts"] == {}
    assert next(iter(failed["outbox"].values()))["keys"] == ["new_show:a", "new_show:b"]

    restarted = context(state_path, state=failed)
    assert delivery.recover(restarted, NOW + timedelta(minutes=1)) is True
    confirmed = load_state(state_path)
    assert set(confirmed["alerts"]) == {"new_show:a", "new_show:b"}
    receipt = next(iter(confirmed["delivery_receipts"].values()))
    assert receipt["keys"] == ["new_show:a", "new_show:b"]


def test_overlapping_hosts_preserve_both_attempt_receipts_without_exactly_once_claim(
    tmp_path, monkeypatch
):
    base = deepcopy(DEFAULT_STATE)
    local_path = tmp_path / "local.json"
    cloud_path = tmp_path / "cloud.json"
    local = context(local_path, state=deepcopy(base))
    cloud = context(cloud_path, state=deepcopy(base))
    message_ids = iter((101, 102))
    monkeypatch.setattr(
        notify,
        "send_telegram",
        lambda *args, **kwargs: notify.SendResult("confirmed", next(message_ids)),
    )

    assert delivery.deliver_alert(local, alert(finding()), NOW) is True
    assert delivery.deliver_alert(cloud, alert(finding()), NOW) is True
    merged = merge_states(base, local.state, cloud.state, now=NOW)

    assert merged["outbox"] == {}
    assert {receipt["telegram_message_id"] for receipt in merged["delivery_receipts"].values()} == {
        101,
        102,
    }
    assert list(merged["alerts"]) == ["new_show:dune-imax"]


def test_moved_opening_supersedes_stale_pending_work_without_rewriting_keys(
    tmp_path, monkeypatch
):
    state_path = tmp_path / "state.json"
    ctx = context(state_path)
    monkeypatch.setattr(
        notify, "send_telegram", lambda *args, **kwargs: notify.SendResult("failed")
    )
    old_time = "2026-10-01T09:00:00+02:00"
    new_time = "2026-10-02T09:00:00+02:00"
    old = finding(
        f"sale:dune-imax:{old_time}", kind="SALE_DATE", sale_datetime=old_time
    )
    moved = finding(
        f"sale:dune-imax:{new_time}",
        kind="SALE_DATE_CHANGED",
        title="Sale moved",
        sale_datetime=new_time,
    )

    delivery.deliver_alert(ctx, alert(old), NOW)
    delivery.deliver_alert(ctx, alert(moved), NOW + timedelta(minutes=5))
    durable = load_state(state_path)

    assert durable["alerts"] == {}
    assert len(durable["outbox"]) == 1
    assert next(iter(durable["outbox"].values()))["keys"] == [moved.key]
    assert old.key == f"sale:dune-imax:{old_time}"


def test_obsolete_reminder_is_retired_instead_of_replayed(tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    ctx = context(state_path)
    target = (NOW + timedelta(hours=2)).isoformat()
    monkeypatch.setattr(
        notify, "send_telegram", lambda *args, **kwargs: notify.SendResult("failed")
    )

    delivery.deliver_reminder(ctx, {"target": target, "offset": 120}, NOW)
    pending = load_state(state_path)
    assert pending["outbox"]

    calls = []
    monkeypatch.setattr(
        notify,
        "send_telegram",
        lambda *args, **kwargs: calls.append("stale replay") or notify.SendResult("confirmed"),
    )
    restarted = context(state_path, state=pending)
    delivery.recover(restarted, datetime.fromisoformat(target) - timedelta(minutes=10))

    assert calls == []
    assert load_state(state_path)["outbox"] == {}


def test_changed_availability_retires_the_stale_pending_advice(tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    ctx = context(state_path)
    monkeypatch.setattr(
        notify, "send_telegram", lambda *args, **kwargs: notify.SendResult("failed")
    )
    day = "2026-09-20"
    no_imax = finding(
        f"cinesa_target_noimax:032:HO00003228:{day}",
        kind="CINESA_TARGET_NO_IMAX",
        title="Open without IMAX",
    )
    with_imax = finding(
        f"cinesa_target:032:HO00003228:{day}",
        kind="CINESA_TARGET_DATE",
        title="Open with IMAX",
    )

    delivery.deliver_alert(ctx, alert(no_imax), NOW)
    delivery.deliver_alert(ctx, alert(with_imax), NOW + timedelta(minutes=5))
    durable = load_state(state_path)

    assert len(durable["outbox"]) == 1
    assert next(iter(durable["outbox"].values()))["keys"] == [with_imax.key]
    assert durable["alerts"] == {}


def test_latest_snapshots_retire_all_disproved_availability_alerts(
    tmp_path, monkeypatch
):
    """No replacement finding is required: authoritative absence contradicts
    Pathé date, generic-ticket and Cinesa date advice in the same way."""
    state_path = tmp_path / "state.json"
    ctx = context(state_path)
    day = "2026-09-20"
    monkeypatch.setattr(Cfg, "pathe_target_dates", [day])
    monkeypatch.setattr(Cfg, "cinesa_target_dates", [day])
    calls = []
    monkeypatch.setattr(
        notify,
        "send_telegram",
        lambda *args, **kwargs: calls.append("attempt") or notify.SendResult("failed"),
    )

    pathe_date = finding(
        f"pathe_target:{Cfg.cinema_slug}:{Cfg.primary_slug}:imax70:{day}",
        kind="PATHE_TARGET_DATE",
    )
    tickets = finding("tickets:dune-imax:imax70", kind="TICKETS_AVAILABLE")
    cinesa_date = finding(
        f"cinesa_target:{Cfg.cinesa_site_id}:{Cfg.cinesa_film_id}:{day}",
        kind="CINESA_TARGET_DATE",
    )
    for item in (pathe_date, tickets, cinesa_date):
        assert delivery.deliver_alert(ctx, alert(item), NOW) is False
    assert len(ctx.state["outbox"]) == 3

    delivery.reconcile_source_observations(
        ctx,
        now=NOW,
        pathe_snapshot=Snapshot(
            matched_shows=[
                {
                    "slug": "dune-imax",
                    "title": "Dune IMAX 70mm",
                    "isMovie": False,
                }
            ]
        ),
        pathe_health="healthy",
        cinesa_snapshot=CinesaSnapshot(
            days=[{"date": "2026-09-19", "attributes": []}]
        ),
        cinesa_health="healthy",
    )

    assert ctx.state["outbox"] == {}
    assert delivery.recover(ctx, NOW + timedelta(minutes=1)) is False
    assert calls == ["attempt", "attempt", "attempt"]


def test_failed_detail_placeholder_does_not_retire_pending_sale(
    tmp_path, monkeypatch
):
    state_path = tmp_path / "state.json"
    ctx = context(state_path)
    sale = (NOW + timedelta(days=10)).isoformat()
    item = finding(
        f"sale:dune-imax:{sale}", kind="SALE_DATE", sale_datetime=sale
    )
    monkeypatch.setattr(
        notify, "send_telegram", lambda *args, **kwargs: notify.SendResult("failed")
    )

    assert delivery.deliver_alert(ctx, alert(item), NOW) is False
    delivery.reconcile_source_observations(
        ctx,
        now=NOW,
        pathe_snapshot=Snapshot(
            matched_shows=[{"slug": "dune-imax", "title": "dune-imax"}],
            listing_results={
                "dune-imax": {
                    "detail": FetchResult.failed("detail request failed")
                }
            },
        ),
        pathe_health="unhealthy",
    )

    assert next(iter(ctx.state["outbox"].values()))["keys"] == [item.key]


def test_failed_primary_showtimes_does_not_retire_pending_target_date(
    tmp_path, monkeypatch
):
    state_path = tmp_path / "state.json"
    ctx = context(state_path)
    day = "2026-09-20"
    monkeypatch.setattr(Cfg, "pathe_target_dates", [day])
    monkeypatch.setattr(
        notify, "send_telegram", lambda *args, **kwargs: notify.SendResult("failed")
    )
    item = finding(
        f"pathe_target:{Cfg.cinema_slug}:{Cfg.primary_slug}:imax70:{day}",
        kind="PATHE_TARGET_DATE",
    )

    assert delivery.deliver_alert(ctx, alert(item), NOW) is False
    delivery.reconcile_source_observations(
        ctx,
        now=NOW,
        pathe_snapshot=Snapshot(
            matched_shows=[{"slug": Cfg.primary_slug, "title": "Dune"}],
            listing_results={
                Cfg.primary_slug: {
                    "showtimes": FetchResult.failed("showtimes request failed")
                }
            },
        ),
        pathe_health="unhealthy",
    )

    assert next(iter(ctx.state["outbox"].values()))["keys"] == [item.key]


def test_empty_pathe_selection_does_not_retire_pending_target_date(
    tmp_path, monkeypatch
):
    state_path = tmp_path / "state.json"
    ctx = context(state_path)
    day = "2026-09-20"
    monkeypatch.setattr(Cfg, "pathe_target_dates", [day])
    monkeypatch.setattr(
        notify, "send_telegram", lambda *args, **kwargs: notify.SendResult("failed")
    )
    item = finding(
        f"pathe_target:{Cfg.cinema_slug}:{Cfg.primary_slug}:imax70:{day}",
        kind="PATHE_TARGET_DATE",
    )

    assert delivery.deliver_alert(ctx, alert(item), NOW) is False
    delivery.reconcile_source_observations(
        ctx,
        now=NOW,
        pathe_snapshot=Snapshot(),
        pathe_health="healthy",
    )

    assert next(iter(ctx.state["outbox"].values()))["keys"] == [item.key]


def test_empty_session_day_preserves_detected_listing_format(
    tmp_path, monkeypatch
):
    state_path = tmp_path / "state.json"
    ctx = context(state_path)
    monkeypatch.setattr(
        notify, "send_telegram", lambda *args, **kwargs: notify.SendResult("failed")
    )
    item = finding("tickets:dune-imax:imax70", kind="TICKETS_AVAILABLE")

    assert delivery.deliver_alert(ctx, alert(item), NOW) is False
    delivery.reconcile_source_observations(
        ctx,
        now=NOW,
        pathe_snapshot=Snapshot(
            matched_shows=[
                {"slug": "dune-imax", "title": "Dune IMAX 70mm"}
            ],
            showtimes={"dune-imax": {"2026-09-20": []}},
        ),
        pathe_health="healthy",
    )

    assert next(iter(ctx.state["outbox"].values()))["keys"] == [item.key]


def test_complete_contradicting_sale_evidence_retires_pending_sale(
    tmp_path, monkeypatch
):
    state_path = tmp_path / "state.json"
    ctx = context(state_path)
    old_sale = (NOW + timedelta(days=10)).isoformat()
    new_sale = (NOW + timedelta(days=11)).isoformat()
    item = finding(
        f"sale:dune-imax:{old_sale}",
        kind="SALE_DATE",
        sale_datetime=old_sale,
    )
    monkeypatch.setattr(
        notify, "send_telegram", lambda *args, **kwargs: notify.SendResult("failed")
    )

    assert delivery.deliver_alert(ctx, alert(item), NOW) is False
    delivery.reconcile_source_observations(
        ctx,
        now=NOW,
        pathe_snapshot=Snapshot(
            matched_shows=[
                {
                    "slug": "dune-imax",
                    "title": "Dune IMAX 70mm",
                    "salesOpeningDatetime": new_sale,
                }
            ]
        ),
        pathe_health="healthy",
    )

    assert ctx.state["outbox"] == {}


def test_partial_regeneration_supersedes_only_its_merged_member(
    tmp_path, monkeypatch
):
    state_path = tmp_path / "state.json"
    ctx = context(state_path)
    sale = (NOW + timedelta(days=10)).isoformat()
    member_a = finding(
        f"sale:a-imax:{sale}", kind="SALE_DATE", sale_datetime=sale
    )
    member_b = finding(
        f"sale:b-imax:{sale}", kind="SALE_DATE", sale_datetime=sale
    )
    monkeypatch.setattr(
        notify, "send_telegram", lambda *args, **kwargs: notify.SendResult("failed")
    )

    assert delivery.deliver_alert(ctx, alert(member_a, member_b), NOW) is False
    assert delivery.deliver_alert(
        ctx, alert(member_a), NOW + timedelta(minutes=5)
    ) is False

    assert {tuple(record["keys"]) for record in ctx.state["outbox"].values()} == {
        (member_a.key,),
        (member_b.key,),
    }


def test_confirmed_blind_alert_supersedes_failed_degraded_alert(
    tmp_path, monkeypatch
):
    state_path = tmp_path / "state.json"
    ctx = context(state_path)
    key = "error:2026-09-17"
    degraded = finding(key, kind="WATCHER_ERROR", title="Pathé watch is DEGRADED")
    blind = finding(key, kind="WATCHER_ERROR", title="Pathé watch is BLIND")
    calls = []
    outcomes = iter(
        (notify.SendResult("failed"), notify.SendResult("confirmed"))
    )
    monkeypatch.setattr(
        notify,
        "send_telegram",
        lambda _cfg, text, **kwargs: calls.append(text) or next(outcomes),
    )

    assert delivery.deliver_alert(ctx, alert(degraded), NOW, force=True) is False
    assert delivery.deliver_alert(
        ctx, alert(blind), NOW + timedelta(minutes=5), force=True
    ) is True
    assert delivery.recover(ctx, NOW + timedelta(minutes=6)) is False

    assert len(calls) == 2
    assert "DEGRADED" in calls[0]
    assert "BLIND" in calls[1]
    assert ctx.state["outbox"] == {}


def test_book_now_supersedes_failed_cinema_listed_alert(
    tmp_path, monkeypatch
):
    state_path = tmp_path / "state.json"
    ctx = context(state_path)
    listed = finding("cinema_listed:dune-imax", kind="CINEMA_LISTED")
    tickets = finding("tickets:dune-imax:imax70", kind="TICKETS_AVAILABLE")
    calls = []
    outcomes = iter(
        (notify.SendResult("failed"), notify.SendResult("confirmed"))
    )
    monkeypatch.setattr(
        notify,
        "send_telegram",
        lambda _cfg, text, **kwargs: calls.append(text) or next(outcomes),
    )

    assert delivery.deliver_alert(ctx, alert(listed), NOW) is False
    assert delivery.deliver_alert(
        ctx, alert(tickets), NOW + timedelta(minutes=5)
    ) is True
    assert delivery.recover(ctx, NOW + timedelta(minutes=6)) is False

    assert len(calls) == 2
    assert ctx.state["outbox"] == {}


def test_merged_expiry_retires_only_the_elapsed_member(tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    ctx = context(state_path)
    first_day = "2026-09-20"
    later_day = "2026-09-22"
    first = finding(
        f"pathe_target:{Cfg.cinema_slug}:{Cfg.primary_slug}:imax70:{first_day}",
        kind="PATHE_TARGET_DATE",
    )
    later = finding(
        f"pathe_target:{Cfg.cinema_slug}:{Cfg.primary_slug}:imax70:{later_day}",
        kind="PATHE_TARGET_DATE",
    )
    monkeypatch.setattr(
        notify, "send_telegram", lambda *args, **kwargs: notify.SendResult("failed")
    )

    assert delivery.deliver_alert(ctx, alert(first, later), NOW) is False
    delivery.recover(
        ctx, datetime(2026, 9, 21, 12, 0, tzinfo=TZ_PARIS)
    )

    assert [record["keys"] for record in ctx.state["outbox"].values()] == [
        [later.key]
    ]


def test_merged_observation_contradiction_retires_only_its_member(
    tmp_path, monkeypatch
):
    state_path = tmp_path / "state.json"
    ctx = context(state_path)
    first_day = "2026-09-20"
    later_day = "2026-09-22"
    first = finding(
        f"pathe_target:{Cfg.cinema_slug}:{Cfg.primary_slug}:imax70:{first_day}",
        kind="PATHE_TARGET_DATE",
    )
    later = finding(
        f"pathe_target:{Cfg.cinema_slug}:{Cfg.primary_slug}:imax70:{later_day}",
        kind="PATHE_TARGET_DATE",
    )
    monkeypatch.setattr(
        notify, "send_telegram", lambda *args, **kwargs: notify.SendResult("failed")
    )

    assert delivery.deliver_alert(ctx, alert(first, later), NOW) is False
    delivery.reconcile_observations(
        ctx,
        observed_domains={f"pathe-availability:{first_day}"},
        active_conditions=set(),
    )

    assert [record["keys"] for record in ctx.state["outbox"].values()] == [
        [later.key]
    ]


POLICY_CASES = [
    (
        "SALE_DATE",
        "sale:dune:2026-10-01T09:00:00+02:00",
        "Sale",
        "2026-10-01T09:00:00+02:00",
    ),
    (
        "SALE_DATE_CHANGED",
        "sale:dune:2026-10-02T09:00:00+02:00",
        "Sale moved",
        "2026-10-02T09:00:00+02:00",
    ),
    ("TICKETS_AVAILABLE", "tickets:dune:imax70", "Book now", None),
    (
        "PATHE_TARGET_DATE",
        "pathe_target:odysseum:dune:imax70:2026-09-20",
        "Open",
        None,
    ),
    ("NEW_LISTING", "new_show:dune-imax", "New listing", None),
    ("CINEMA_LISTED", "cinema_listed:dune", "Listed", None),
    ("NEWS_LEAD", "news:abc", "News", None),
    ("RECOVERED", "recovered:2026-09-17T1200", "Pathé watch is back", None),
    ("HEARTBEAT", "heartbeat:2026-09-17", "All quiet", None),
    (
        "CINESA_TARGET_DATE",
        "cinesa_target:032:HO00003228:2026-09-20",
        "Open",
        None,
    ),
    (
        "CINESA_TARGET_NO_IMAX",
        "cinesa_target_noimax:032:HO00003228:2026-09-20",
        "No IMAX",
        None,
    ),
    ("CINESA_IMAX_GONE", "cinesa_imax_gone:2026-09-17", "Gone", None),
    ("CINESA_IMAX_BACK", "cinesa_imax_back:2026-09-17", "Back", None),
]


@pytest.mark.parametrize(
    ("kind", "key", "title", "sale_datetime"), POLICY_CASES
)
def test_every_non_error_alert_kind_has_supersession_or_expiry(
    kind, key, title, sale_datetime
):
    item = finding(
        key, kind=kind, title=title, sale_datetime=sale_datetime
    )

    topics, expiry = delivery._finding_policy(item, NOW)

    assert topics or expiry is not None, kind


def test_policy_matrix_covers_every_non_error_alert_kind():
    assert {case[0] for case in POLICY_CASES} == set(notify.ICONS) - {
        "WATCHER_ERROR",
        "WATCHER_STILL_BLIND",
    }


def test_missing_sale_target_does_not_disprove_pending_open_ping(
    tmp_path, monkeypatch
):
    """`None` can mean sales just went live. Only a different observed target
    contradicts the old ladder; the high-value open ping remains retryable."""
    state_path = tmp_path / "state.json"
    target = (NOW - timedelta(minutes=1)).isoformat()
    ctx = context(state_path)
    calls = []
    outcomes = iter((notify.SendResult("failed"), notify.SendResult("confirmed")))
    monkeypatch.setattr(
        notify,
        "send_telegram",
        lambda *args, **kwargs: calls.append("attempt") or next(outcomes),
    )

    assert delivery.deliver_reminder(
        ctx, {"target": target, "offset": "open"}, NOW
    ) is False
    ctx.state["sale_target"] = None
    delivery.reconcile_source_observations(
        ctx,
        now=NOW,
        pathe_snapshot=Snapshot(),
        pathe_health="healthy",
    )

    assert len(ctx.state["outbox"]) == 1
    restarted = context(state_path, state=load_state(state_path))
    assert delivery.recover(restarted, NOW + timedelta(minutes=1)) is True
    assert calls == ["attempt", "attempt"]
