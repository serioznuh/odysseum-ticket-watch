"""Fault-injection coverage for the durable notification boundary (OTW-20)."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from typing import ClassVar

import pytest

from watcher import coalesce, delivery, jobs, notify, runner
from watcher import state as state_mod
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


def test_failed_final_bookkeeping_save_costs_no_receipt_and_replays_nothing(
    tmp_path, monkeypatch, caplog
):
    """OTW-23: the coordinator's last save is not what makes a send durable.

    One confirmed alert and one attempt with an unknown outcome are already on
    disk when that save fails. It must report the problem, leave the validated
    file exactly as it is, and leave the next pass with the same two facts.
    """
    state_path = tmp_path / "state.json"
    ctx = context(state_path)
    monkeypatch.setattr(
        notify, "send_telegram", lambda *a, **kw: notify.SendResult("confirmed", 11)
    )
    assert delivery.deliver_alert(ctx, alert(finding()), NOW) is True

    def crash(*args, **kwargs):
        raise RuntimeError("connection vanished during send")

    monkeypatch.setattr(notify, "send_telegram", crash)
    with pytest.raises(RuntimeError, match="vanished"):
        delivery.deliver_alert(
            ctx, alert(finding(key="new_show:dune-imax-second")), NOW
        )
    durable = state_path.read_bytes()

    # An invalid in-memory field is the failure observed on 2026-09-21: it used
    # to surface only here, as an uncaught traceback out of the CLI.
    ctx.state["sales"] = {"dune-imax": "2026-11-05T08:00:00"}
    caplog.clear()  # the sender crash above logged its own (expected) traceback
    with caplog.at_level("ERROR"):
        assert runner._save_final_state(ctx, str(state_path)) is False

    assert state_path.read_bytes() == durable
    assert "must include a UTC offset" in caplog.text
    assert "Traceback" not in caplog.text

    calls = []
    monkeypatch.setattr(
        notify,
        "send_telegram",
        lambda *a, **kw: calls.append("send") or notify.SendResult("confirmed", 12),
    )
    restarted = context(state_path, state=load_state(state_path))
    assert delivery.recover(restarted, NOW + timedelta(minutes=5)) is False
    after = load_state(state_path)

    assert calls == []
    assert after["alerts"] == {"new_show:dune-imax": NOW.isoformat()}
    assert [r["keys"] for r in after["delivery_receipts"].values()] == [
        ["new_show:dune-imax"]
    ]
    assert [r["status"] for r in after["outbox"].values()] == ["uncertain"]


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


def test_uncoordinated_overlapping_hosts_preserve_both_attempt_receipts(
    tmp_path, monkeypatch
):
    """Without a coordinator (a dry run, or state outside the synchronized
    store) claims stay local, exactly as OTW-20 documented: merging preserves
    both receipts but cannot undo the duplicate. Production passes are
    coordinated; see the OTW-28 tests below."""
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


def test_unreadable_opening_withholds_only_the_sale_condition(
    tmp_path, monkeypatch
):
    """OTW-23, per field: the listing's opening is unknown, so a pending sale
    message must survive — while its healthy session evidence still retires the
    stale ticket alert for the same listing."""
    state_path = tmp_path / "state.json"
    ctx = context(state_path)
    sale = (NOW + timedelta(days=10)).isoformat()
    sale_item = finding(f"sale:dune-imax:{sale}", kind="SALE_DATE", sale_datetime=sale)
    ticket_item = finding("tickets:dune-imax:imax70", kind="TICKETS_AVAILABLE")
    monkeypatch.setattr(
        notify, "send_telegram", lambda *args, **kwargs: notify.SendResult("failed")
    )

    assert delivery.deliver_alert(ctx, alert(sale_item), NOW) is False
    assert delivery.deliver_alert(ctx, alert(ticket_item), NOW) is False

    delivery.reconcile_source_observations(
        ctx,
        now=NOW,
        # The opening was published but unreadable, so the boundary dropped it.
        # Sessions and the programme entry are healthy and say "nothing
        # bookable", which is exactly the evidence that retires a ticket alert.
        pathe_snapshot=Snapshot(
            matched_shows=[{"slug": "dune-imax", "title": "Dune IMAX 70mm"}],
            unreadable_metadata={"dune-imax": ["salesOpeningDatetime"]},
        ),
        pathe_health="healthy",
    )

    remaining = [record["keys"] for record in ctx.state["outbox"].values()]
    assert remaining == [[sale_item.key]]


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


def test_watcher_error_identity_survives_changing_duration_and_respects_receipt(
    tmp_path, monkeypatch
):
    """A definite failure must not fork one outage into many queued IDs, and
    a later forced pass must honor the receipt for that same condition."""
    state_path = tmp_path / "state.json"
    calls = []
    outcomes = iter(
        (notify.SendResult("failed"), notify.SendResult("confirmed", 77))
    )
    monkeypatch.setattr(
        notify,
        "send_telegram",
        lambda _cfg, text, **kwargs: calls.append(text) or next(outcomes),
    )
    first = finding(
        "error:2026-09-17",
        kind="WATCHER_ERROR",
        title="Pathé watch is BLIND (6 h)",
    )
    later = finding(
        first.key,
        kind="WATCHER_ERROR",
        title="Pathé watch is BLIND (7 h)",
    )

    first_ctx = context(state_path)
    assert delivery.deliver_alert(first_ctx, alert(first), NOW, force=True) is False
    failed = load_state(state_path)
    assert len(failed["outbox"]) == 1
    delivery_id = next(iter(failed["outbox"]))

    retry_ctx = context(state_path, state=failed)
    assert delivery.deliver_alert(
        retry_ctx, alert(later), NOW + timedelta(hours=1), force=True
    ) is True
    confirmed = load_state(state_path)
    assert confirmed["outbox"] == {}
    assert next(iter(confirmed["delivery_receipts"].values()))["delivery_id"] == delivery_id

    acknowledged_ctx = context(state_path, state=confirmed)
    assert delivery.deliver_alert(
        acknowledged_ctx,
        alert(
            finding(
                first.key,
                kind="WATCHER_ERROR",
                title="Pathé watch is BLIND (8 h)",
            )
        ),
        NOW + timedelta(hours=2),
        force=True,
    ) is False
    assert len(calls) == 2


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


def test_merged_split_does_not_recreate_an_acknowledged_member(
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
    state_mod.mark_sent(ctx.state, later.key, NOW)
    delivery.reconcile_observations(
        ctx,
        observed_domains={f"pathe-availability:{first_day}"},
        active_conditions=set(),
    )

    assert ctx.state["outbox"] == {}


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


def test_every_condition_policy_has_an_observation_path_or_expiry():
    """Keep condition emitters paired with reconciliation as policies grow."""
    items = [
        finding(key, kind=kind, title=title, sale_datetime=sale_datetime)
        for kind, key, title, sale_datetime in POLICY_CASES
    ]
    items.extend(
        [
            finding("error:2026-09-17", kind="WATCHER_ERROR", title="DEGRADED"),
            finding("stale:last-ok:0", kind="WATCHER_ERROR", title="Blind"),
            finding(
                "cloud_stale:2026-09-17T10:00:00+02:00",
                kind="WATCHER_ERROR",
                title="Cloud checks stopped",
            ),
            finding("cinesa_error:2026-09-17", kind="WATCHER_ERROR"),
            finding("cinesa_recovered:2026-09-17T1200", kind="RECOVERED"),
            finding(
                "cinesa_leak:2026-09-17T10:00:00+02:00:0",
                kind="WATCHER_ERROR",
            ),
        ]
    )
    exact = {
        "pathe-health",
        "cloud-health",
        "cinesa-health",
        "cinesa-token",
        "cinesa-imax-presence",
        "pathe-sale-target",
    }
    prefixes = (
        "pathe-sale:",
        "pathe-listing:",
        "pathe-bookability:",
        "pathe-tickets:",
        "pathe-availability:",
        "cinesa-availability:",
    )

    policies = [delivery._finding_policy(item, NOW) for item in items]
    policies.append(([delivery._condition_topic("pathe-sale-target", "target")], None))
    for topics, expiry in policies:
        for topic in topics:
            condition = delivery._condition(topic)
            if condition is None:
                continue
            domain, _value = condition
            assert expiry is not None or domain in exact or domain.startswith(prefixes)


def test_cleared_cinesa_lock_retires_failed_owner_advice(tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    ctx = context(state_path)
    calls = []
    monkeypatch.setattr(
        notify,
        "send_telegram",
        lambda *args, **kwargs: calls.append("attempt") or notify.SendResult("failed"),
    )
    leak = finding(
        "cinesa_leak:2026-09-17T10:00:00+02:00:0",
        kind="WATCHER_ERROR",
        title="Cinesa token step needs you",
    )

    assert delivery.deliver_alert(ctx, alert(leak), NOW) is False
    delivery.reconcile_source_observations(
        ctx,
        now=NOW + timedelta(minutes=5),
        cinesa_token_stuck=False,
    )

    assert ctx.state["outbox"] == {}
    assert delivery.recover(ctx, NOW + timedelta(minutes=6)) is False
    assert calls == ["attempt"]


def test_authoritative_sale_withdrawal_retires_failed_open_ping(
    tmp_path, monkeypatch
):
    """A complete snapshot can prove that an old future opening was removed."""
    state_path = tmp_path / "state.json"
    target = (NOW + timedelta(hours=2)).isoformat()
    ctx = context(state_path)
    ctx.state["sale_target"] = target
    calls = []
    monkeypatch.setattr(
        notify,
        "send_telegram",
        lambda *args, **kwargs: calls.append("attempt") or notify.SendResult("failed"),
    )

    assert delivery.deliver_reminder(
        ctx, {"target": target, "offset": 120}, NOW
    ) is False
    snapshot = Snapshot()
    state_mod.update_from_snapshot(ctx.state, snapshot, Cfg, NOW)
    assert ctx.state["sale_target"] is None
    delivery.reconcile_source_observations(
        ctx,
        now=NOW,
        pathe_snapshot=snapshot,
        pathe_health="healthy",
    )

    assert ctx.state["outbox"] == {}
    assert delivery.recover(ctx, NOW + timedelta(minutes=1)) is False
    assert calls == ["attempt"]


def test_showtimes_degradation_cannot_retire_failed_open_ping(
    tmp_path, monkeypatch
):
    state_path = tmp_path / "state.json"
    target = (NOW - timedelta(minutes=30)).isoformat()
    ctx = context(state_path)
    ctx.state["sale_target"] = target
    calls = []
    outcomes = iter((notify.SendResult("failed"), notify.SendResult("confirmed")))
    monkeypatch.setattr(
        notify,
        "send_telegram",
        lambda *args, **kwargs: calls.append("attempt") or next(outcomes),
    )
    degraded = Snapshot(
        matched_shows=[{"slug": "dune", "title": "Dune"}],
        listing_results={
            "dune": {"showtimes": FetchResult.failed("showtimes unavailable")}
        },
    )

    assert delivery.deliver_reminder(
        ctx, {"target": target, "offset": "open"}, NOW
    ) is False
    state_mod.update_from_snapshot(ctx.state, degraded, Cfg, NOW)
    assert ctx.state["sale_target"] == target
    delivery.reconcile_source_observations(
        ctx,
        now=NOW,
        pathe_snapshot=degraded,
        pathe_health="degraded",
    )

    assert len(ctx.state["outbox"]) == 1
    restarted = context(state_path, state=load_state(state_path))
    assert delivery.recover(restarted, NOW + timedelta(minutes=1)) is True
    assert calls == ["attempt", "attempt"]


def test_new_state_target_cannot_retire_still_reported_open_ping(
    tmp_path, monkeypatch
):
    state_path = tmp_path / "state.json"
    old = (NOW - timedelta(minutes=30)).isoformat()
    new = (NOW + timedelta(days=1)).isoformat()
    ctx = context(state_path)
    calls = []
    outcomes = iter((notify.SendResult("failed"), notify.SendResult("confirmed")))
    monkeypatch.setattr(
        notify,
        "send_telegram",
        lambda *args, **kwargs: calls.append("attempt") or next(outcomes),
    )

    assert delivery.deliver_reminder(
        ctx, {"target": old, "offset": "open"}, NOW
    ) is False
    ctx.state["sale_target"] = new
    snapshot = Snapshot(
        matched_shows=[
            {
                "slug": "dune",
                "title": "Dune IMAX 70mm",
                "salesOpeningDatetime": old,
            },
            {
                "slug": "dune-next",
                "title": "Dune IMAX 70mm",
                "salesOpeningDatetime": new,
            },
        ]
    )
    delivery.reconcile_source_observations(
        ctx,
        now=NOW,
        pathe_snapshot=snapshot,
        pathe_health="healthy",
    )

    assert len(ctx.state["outbox"]) == 1
    restarted = context(state_path, state=load_state(state_path))
    assert delivery.recover(restarted, NOW + timedelta(minutes=1)) is True
    assert calls == ["attempt", "attempt"]


def test_unknown_sale_withdrawal_preserves_failed_open_ping(tmp_path, monkeypatch):
    """A missing target on a degraded detail response remains unknown."""
    state_path = tmp_path / "state.json"
    target = (NOW + timedelta(hours=2)).isoformat()
    ctx = context(state_path)
    calls = []
    outcomes = iter((notify.SendResult("failed"), notify.SendResult("confirmed")))
    monkeypatch.setattr(
        notify,
        "send_telegram",
        lambda *args, **kwargs: calls.append("attempt") or next(outcomes),
    )

    assert delivery.deliver_reminder(
        ctx, {"target": target, "offset": 120}, NOW
    ) is False
    ctx.state["sale_target"] = None
    delivery.reconcile_source_observations(
        ctx,
        now=NOW,
        pathe_snapshot=Snapshot(
            matched_shows=[{"slug": "dune", "title": "Dune"}],
            listing_results={
                "dune": {"detail": FetchResult.failed("detail unavailable")}
            },
        ),
        pathe_health="degraded",
    )

    assert len(ctx.state["outbox"]) == 1
    restarted = context(state_path, state=load_state(state_path))
    assert delivery.recover(restarted, NOW + timedelta(minutes=1)) is True
    assert calls == ["attempt", "attempt"]


# ---------------------------------------------------------------------------
# OTW-28: shared reservations decide which host may call Telegram
# ---------------------------------------------------------------------------


class SharedRef:
    """The runtime-state ref, reduced to what arbitration needs: one shared
    snapshot that only a compare-and-swap reservation or a sync changes."""

    def __init__(self, state: dict) -> None:
        self.state = deepcopy(state)

    def sync(self, ctx: jobs.RunContext, base: dict) -> dict:
        """The post-run sync: three-way merge, publish, adopt. Returns new base."""
        merged = merge_states(base, self.state, ctx.state, now=NOW)
        self.state = deepcopy(merged)
        ctx.state.clear()
        ctx.state.update(deepcopy(merged))
        save_state(ctx.state_path, ctx.state)
        return deepcopy(merged)


class RefCoordinator:
    """A `state_sync.DeliveryCoordinator` over a `SharedRef`.

    ``fail`` is raised instead of an answer; with ``landed`` the entries reach
    the ref first — a push whose acknowledgement was lost.
    """

    blocked_exit = None

    def __init__(
        self, ref, holder, *, fail=None, landed=False, before_claim=None,
        after_confirm=None,
    ):
        self.ref = ref
        self.holder = holder
        self.fail = fail
        self.landed = landed
        self.before_claim = before_claim
        self.after_confirm = after_confirm
        self.claims = 0

    def reserve(self, claim):
        if self.before_claim is not None and self.claims == 0:
            self.before_claim()
        self.claims += 1
        entries = claim(deepcopy(self.ref.state))
        if entries is None:
            return False
        if self.fail is not None:
            if self.landed:
                self.ref.state["reservations"].update(deepcopy(entries))
            raise self.fail
        self.ref.state["reservations"].update(deepcopy(entries))
        if self.after_confirm is not None:
            self.after_confirm()
        return True


class Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def host(path, ref, holder, *, clock=None, writer=None, **coordinator):
    """One host's pass, starting from the shared snapshot (its pre-run sync)."""
    ctx = context(path, state=deepcopy(ref.state), writer=writer)
    ctx.coordinator = RefCoordinator(ref, holder, **coordinator)
    if clock is not None:
        ctx.clock = clock
    return ctx


def recording_sender(monkeypatch, calls, status="confirmed"):
    message_ids = iter(range(500, 600))

    def send(cfg, text, **kwargs):
        calls.append(text)
        return notify.SendResult(status, next(message_ids))

    monkeypatch.setattr(notify, "send_telegram", send)


def receipts_per_key(*states: dict) -> dict[str, int]:
    """Telegram calls per logical key, read from every host's receipts."""
    seen: dict[str, dict] = {}
    for state in states:
        seen.update(state["delivery_receipts"])
    counts: dict[str, int] = {}
    for receipt in seen.values():
        for key in receipt["keys"]:
            counts[key] = counts.get(key, 0) + 1
    return counts


def test_the_losing_host_does_not_send_and_its_work_stays_pending(
    tmp_path, monkeypatch
):
    ref = SharedRef(DEFAULT_STATE)
    local = host(tmp_path / "local.json", ref, "local:mac")
    cloud = host(tmp_path / "cloud.json", ref, "cloud:run-1")
    calls: list[str] = []
    recording_sender(monkeypatch, calls)

    assert delivery.deliver_alert(local, alert(finding()), NOW) is True
    assert delivery.deliver_alert(cloud, alert(finding()), NOW) is False

    assert len(calls) == 1
    record = next(iter(cloud.state["outbox"].values()))
    assert record["status"] == "pending"
    assert cloud.state["alerts"] == {}  # no baseline moved on the loser
    assert load_state(tmp_path / "cloud.json")["outbox"] == cloud.state["outbox"]


def test_differently_grouped_findings_still_send_each_key_once(tmp_path, monkeypatch):
    """The Mac groups {a, b}; the cloud, from the same snapshot, {b, c}. The
    arbitration is per member key, so b cannot go out twice, and c still goes
    out once the Mac's receipt shows b was covered."""
    ref = SharedRef(DEFAULT_STATE)
    base = deepcopy(ref.state)
    local = host(tmp_path / "local.json", ref, "local:mac")
    cloud = host(tmp_path / "cloud.json", ref, "cloud:run-1")
    calls: list[str] = []
    recording_sender(monkeypatch, calls)
    a, b, c = finding("new_show:a"), finding("new_show:b"), finding("new_show:c")

    assert delivery.deliver_alert(local, alert(a, b), NOW) is True
    assert delivery.deliver_alert(cloud, alert(b, c), NOW) is False
    assert len(calls) == 1

    ref.sync(local, base)
    cloud_base = deepcopy(base)
    cloud_base = ref.sync(cloud, cloud_base)
    later = host(tmp_path / "cloud.json", ref, "cloud:run-2")
    assert delivery.recover(later, NOW + timedelta(minutes=15)) is True

    assert len(calls) == 2
    assert receipts_per_key(local.state, later.state) == {
        "new_show:a": 1,
        "new_show:b": 1,
        "new_show:c": 1,
    }


def test_reminder_failover_with_both_hosts_eligible_sends_the_rung_once(
    tmp_path, monkeypatch
):
    ref = SharedRef(DEFAULT_STATE)
    target = (NOW + timedelta(hours=1)).isoformat()
    rung = {"target": target, "offset": 120}
    cloud = host(tmp_path / "cloud.json", ref, "cloud:run-1")
    local = host(tmp_path / "local.json", ref, "local:mac")
    calls: list[str] = []
    recording_sender(monkeypatch, calls)

    assert delivery.deliver_reminder(cloud, rung, NOW) is True
    assert delivery.deliver_reminder(local, rung, NOW) is False
    assert len(calls) == 1
    assert local.state["reminders_sent"] == {}


def test_a_blocked_unit_does_not_hold_up_independent_work(tmp_path, monkeypatch):
    """Cloud news keeps flowing while the Mac holds a different reservation."""
    ref = SharedRef(DEFAULT_STATE)
    local = host(tmp_path / "local.json", ref, "local:mac")
    cloud = host(tmp_path / "cloud.json", ref, "cloud:run-1")
    calls: list[str] = []
    recording_sender(monkeypatch, calls)
    news = finding("news:leak-1", kind="NEWS_LEAD", title="Press lead")

    assert delivery.deliver_alert(local, alert(finding()), NOW) is True
    assert delivery.deliver_alert(cloud, alert(finding()), NOW) is False
    assert delivery.deliver_alert(cloud, alert(news), NOW) is True
    assert len(calls) == 2
    assert "news:leak-1" in cloud.state["alerts"]


def test_work_published_elsewhere_is_not_resent_after_a_failed_pre_run_sync(
    tmp_path, monkeypatch
):
    """The Mac wakes, its pre-run sync fails, and it still holds a snapshot in
    which the rung looks unsent. The reservation reads the live ref, which
    already carries the cloud's receipt."""
    ref = SharedRef(DEFAULT_STATE)
    stale = deepcopy(ref.state)
    base = deepcopy(ref.state)
    target = (NOW + timedelta(hours=1)).isoformat()
    rung = {"target": target, "offset": 120}
    cloud = host(tmp_path / "cloud.json", ref, "cloud:run-1")
    calls: list[str] = []
    recording_sender(monkeypatch, calls)
    assert delivery.deliver_reminder(cloud, rung, NOW) is True
    ref.sync(cloud, base)

    woken = context(tmp_path / "local.json", state=stale)
    woken.coordinator = RefCoordinator(ref, "local:mac")
    assert delivery.deliver_reminder(woken, rung, NOW + timedelta(minutes=30)) is False
    assert len(calls) == 1
    assert next(iter(woken.state["outbox"].values()))["status"] == "pending"


def test_an_unconfirmed_reservation_does_not_send_and_releases_its_token(
    tmp_path, monkeypatch
):
    """A push that timed out may or may not have landed. Nothing is sent, the
    work stays pending, and the release this process publishes outranks the
    held entry if the push did land — so the work is not stranded."""
    from watcher.state_sync import StateSyncTransportError

    ref = SharedRef(DEFAULT_STATE)
    base = deepcopy(ref.state)
    fail = StateSyncTransportError("git push did not answer within 45s")
    local = host(tmp_path / "local.json", ref, "local:mac", fail=fail, landed=True)
    calls: list[str] = []
    recording_sender(monkeypatch, calls)

    assert delivery.deliver_alert(local, alert(finding()), NOW) is False
    assert calls == []
    assert next(iter(local.state["outbox"].values()))["status"] == "pending"
    (held,) = ref.state["reservations"].values()
    assert held["status"] == "held"
    assert [e["status"] for e in local.state["reservations"].values()] == ["released"]

    ref.sync(local, base)
    assert [e["status"] for e in ref.state["reservations"].values()] == ["released"]
    cloud = host(tmp_path / "cloud.json", ref, "cloud:run-1")
    assert delivery.recover(cloud, NOW + timedelta(minutes=15)) is True
    assert len(calls) == 1
    (retaken,) = ref.state["reservations"].values()
    assert retaken["generation"] == 2 and retaken["holder"] == "cloud:run-1"


def test_a_refused_or_absent_ref_never_permits_a_send(tmp_path, monkeypatch):
    from watcher.state_sync import StateSyncError, StateSyncRefAbsentError

    calls: list[str] = []
    recording_sender(monkeypatch, calls)
    for index, fail in enumerate(
        (
            StateSyncRefAbsentError("shared state ref is missing", established=True),
            StateSyncError("shared state is incompatible or invalid"),
            OSError("disk full"),
        )
    ):
        ref = SharedRef(DEFAULT_STATE)
        ctx = host(tmp_path / f"state-{index}.json", ref, "local:mac", fail=fail)
        assert delivery.deliver_alert(ctx, alert(finding()), NOW) is False
        assert next(iter(ctx.state["outbox"].values()))["status"] == "pending"
    assert calls == []


def test_a_definite_failure_releases_the_reservation_for_either_host(
    tmp_path, monkeypatch
):
    ref = SharedRef(DEFAULT_STATE)
    base = deepcopy(ref.state)
    local = host(tmp_path / "local.json", ref, "local:mac")
    calls: list[str] = []
    recording_sender(monkeypatch, calls, status="failed")
    assert delivery.deliver_alert(local, alert(finding()), NOW) is False
    assert [e["status"] for e in local.state["reservations"].values()] == ["released"]
    ref.sync(local, base)

    recording_sender(monkeypatch, calls)
    cloud = host(tmp_path / "cloud.json", ref, "cloud:run-1")
    assert delivery.recover(cloud, NOW + timedelta(minutes=15)) is True
    assert len(calls) == 2  # the refused attempt, then exactly one delivery
    assert "new_show:dune-imax" in cloud.state["alerts"]


@pytest.mark.parametrize("outcome", ["uncertain", "crash", "receipt-write"])
def test_an_ambiguous_attempt_is_quarantined_on_every_host(
    tmp_path, monkeypatch, outcome
):
    ref = SharedRef(DEFAULT_STATE)
    base = deepcopy(ref.state)
    calls: list[str] = []
    writes = 0

    def fail_receipt_write(path, state):
        nonlocal writes
        writes += 1
        if outcome == "receipt-write" and not state["outbox"] and state["alerts"]:
            raise OSError("disk full after Telegram confirmation")
        save_state(path, state)

    local = host(tmp_path / "local.json", ref, "local:mac", writer=fail_receipt_write)
    if outcome == "crash":

        def crash(cfg, text, **kwargs):
            calls.append(text)
            raise RuntimeError("connection reset mid-request")

        monkeypatch.setattr(notify, "send_telegram", crash)
    else:
        recording_sender(
            monkeypatch, calls, "uncertain" if outcome == "uncertain" else "confirmed"
        )

    with pytest.raises(Exception) if outcome != "uncertain" else _nothing():
        delivery.deliver_alert(local, alert(finding()), NOW)
    assert len(calls) == 1
    durable = load_state(tmp_path / "local.json")
    assert [r["status"] for r in durable["outbox"].values()] == ["uncertain"]
    assert [e["status"] for e in durable["reservations"].values()] == ["uncertain"]

    local.state.clear()
    local.state.update(durable)
    ref.sync(local, base)
    recording_sender(monkeypatch, calls)
    later = NOW + timedelta(days=2)
    for name, holder in (("cloud", "cloud:run-1"), ("local-next", "local:mac")):
        again = host(tmp_path / f"{name}.json", ref, holder, clock=Clock(later))
        delivery.deliver_alert(again, alert(finding()), later)
        delivery.recover(again, later)
    assert len(calls) == 1


class _nothing:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_a_lease_spent_before_the_send_began_sends_nothing(tmp_path, monkeypatch):
    """A laptop that slept between winning the reservation and calling Telegram
    wakes to a spent lease: it does not send late, keeps the work pending and
    releases the reservation for the next attempt."""
    ref = SharedRef(DEFAULT_STATE)
    clock = Clock()

    def sleep_through_the_lease(path, state):
        save_state(path, state)
        if any(r["status"] == "sending" for r in state["outbox"].values()):
            clock.now = NOW + delivery.RESERVATION_LEASE

    local = host(
        tmp_path / "local.json", ref, "local:mac", clock=clock,
        writer=sleep_through_the_lease,
    )
    calls: list[str] = []
    recording_sender(monkeypatch, calls)

    assert delivery.deliver_alert(local, alert(finding()), NOW) is False
    assert calls == []
    durable = load_state(tmp_path / "local.json")
    assert [r["status"] for r in durable["outbox"].values()] == ["pending"]
    assert [e["status"] for e in durable["reservations"].values()] == ["released"]


def test_a_stale_holder_cannot_race_the_store_that_took_over(tmp_path, monkeypatch):
    """The Mac wins a reservation and stalls before its `sending` save. The
    cloud, seeing the lease expire, still does not take over: that holder may
    have sent. The Mac's own next pass may, once the lease is over, because its
    live file shows the earlier pass never reached Telegram. The stalled pass
    then resumes past its lease and sends nothing: one message in total."""
    ref = SharedRef(DEFAULT_STATE)
    stalled_clock = Clock()
    after_lease = NOW + delivery.RESERVATION_LEASE + timedelta(minutes=1)
    calls: list[str] = []
    others: dict[str, bool] = {}
    state_path = tmp_path / "local.json"

    def meanwhile():
        cloud = host(tmp_path / "cloud.json", ref, "cloud:run-1", clock=Clock(after_lease))
        others["cloud"] = delivery.deliver_alert(cloud, alert(finding()), after_lease)
        # The store's next pass reads the live file the stalled one left.
        successor = context(state_path, state=load_state(state_path))
        successor.coordinator = RefCoordinator(ref, "local:mac")
        successor.clock = Clock(after_lease)
        others["successor"] = delivery.recover(successor, after_lease)
        stalled_clock.now = after_lease

    stalled = host(
        state_path, ref, "local:mac", clock=stalled_clock, after_confirm=meanwhile
    )
    recording_sender(monkeypatch, calls)

    assert delivery.deliver_alert(stalled, alert(finding()), NOW) is False
    assert others == {"cloud": False, "successor": True}
    assert len(calls) == 1
    (entry,) = ref.state["reservations"].values()
    assert entry["generation"] == 2 and entry["holder"] == "local:mac"
    # The stale pass rolled back to pending and released its own token only.
    (mine,) = stalled.state["reservations"].values()
    assert mine["status"] == "released" and mine["generation"] == 1


def test_a_crashed_holder_blocks_other_hosts_until_its_store_takes_over(
    tmp_path, monkeypatch
):
    """A process that dies after its reservation landed, before persisting
    `sending`, never called Telegram — but only its own store can prove that."""
    ref = SharedRef(DEFAULT_STATE)

    def die_before_sending(path, state):
        if any(r["status"] == "sending" for r in state["outbox"].values()):
            raise SystemExit("killed by the overall deadline")
        save_state(path, state)

    dying = host(tmp_path / "local.json", ref, "local:mac", writer=die_before_sending)
    calls: list[str] = []
    recording_sender(monkeypatch, calls)
    with pytest.raises(SystemExit):
        delivery.deliver_alert(dying, alert(finding()), NOW)
    assert calls == []
    durable = load_state(tmp_path / "local.json")
    assert [r["status"] for r in durable["outbox"].values()] == ["pending"]

    for when, holder in (
        (NOW + timedelta(minutes=5), "local:mac"),  # lease still running
        (NOW + timedelta(hours=3), "cloud:run-2"),  # any lease, another host
        (NOW - timedelta(hours=3), "cloud:run-3"),  # a clock skewed either way
    ):
        other = context(tmp_path / "other.json", state=deepcopy(durable))
        other.coordinator = RefCoordinator(ref, holder)
        other.clock = Clock(when)
        assert delivery.recover(other, when) is False
    assert calls == []

    later = NOW + delivery.RESERVATION_LEASE
    successor = context(tmp_path / "local.json", state=deepcopy(durable))
    successor.coordinator = RefCoordinator(ref, "local:mac")
    successor.clock = Clock(later)
    assert delivery.recover(successor, later) is True
    assert len(calls) == 1


def test_a_store_never_takes_over_an_attempt_that_may_have_sent(tmp_path, monkeypatch):
    """Killed mid-request: the ref still says `held` (the post-run sync never
    ran), and a regrouped finding has since retired the uncertain record. The
    reservation this store saved with `sending` still quarantines the key."""
    ref = SharedRef(DEFAULT_STATE)
    local = host(tmp_path / "local.json", ref, "local:mac")
    calls: list[str] = []

    def crash(cfg, text, **kwargs):
        calls.append(text)
        raise RuntimeError("process killed mid-request")

    monkeypatch.setattr(notify, "send_telegram", crash)
    with pytest.raises(RuntimeError):
        delivery.deliver_alert(local, alert(finding()), NOW)
    durable = load_state(tmp_path / "local.json")
    assert [e["status"] for e in ref.state["reservations"].values()] == ["held"]
    assert [e["status"] for e in durable["reservations"].values()] == ["uncertain"]
    durable["outbox"] = {}

    recording_sender(monkeypatch, calls)
    later = NOW + timedelta(hours=1)
    successor = context(tmp_path / "local.json", state=durable)
    successor.coordinator = RefCoordinator(ref, "local:mac")
    successor.clock = Clock(later)
    regrouped = alert(finding(), finding("new_show:dune-imax-sibling"))
    assert delivery.deliver_alert(successor, regrouped, later) is False
    assert len(calls) == 1


def test_a_reservation_git_child_that_survives_stops_every_later_reservation(
    tmp_path, monkeypatch
):
    from watcher.state_sync import StateSyncCleanupError

    ref = SharedRef(DEFAULT_STATE)
    calls: list[str] = []
    recording_sender(monkeypatch, calls)
    ctx = host(tmp_path / "local.json", ref, "local:mac")

    class Surviving(RefCoordinator):
        def reserve(self, claim):
            if self.blocked_exit is not None:
                raise delivery.state_sync.StateSyncError("blocked")
            claim(deepcopy(self.ref.state))
            self.blocked_exit = delivery.state_sync.UNCONFIRMED_TREE_EXIT
            raise StateSyncCleanupError("group 4242 survived", pgid=4242)

    ctx.coordinator = Surviving(ref, "local:mac")
    assert delivery.deliver_alert(ctx, alert(finding("new_show:a")), NOW) is False
    assert delivery.deliver_alert(ctx, alert(finding("new_show:b")), NOW) is False
    assert calls == []
    assert runner.execute(ctx, str(tmp_path / "local.json")) == (
        delivery.state_sync.UNCONFIRMED_TREE_EXIT
    )


def test_settled_and_long_expired_reservations_are_pruned(tmp_path, monkeypatch):
    ref = SharedRef(DEFAULT_STATE)
    local = host(tmp_path / "local.json", ref, "local:mac")
    calls: list[str] = []
    recording_sender(monkeypatch, calls, status="failed")
    news = finding("news:leak-1", kind="NEWS_LEAD", title="Press lead")
    delivery.deliver_alert(local, alert(news), NOW)
    assert [e["status"] for e in local.state["reservations"].values()] == ["released"]

    retained = NOW + timedelta(days=7, minutes=30)
    delivery.recover(local, retained - timedelta(days=1))
    assert local.state["reservations"]  # still inside the retention window
    delivery.recover(local, retained + timedelta(days=7))
    assert local.state["reservations"] == {}


def test_a_decline_after_an_earlier_push_of_the_token_releases_it(
    tmp_path, monkeypatch
):
    """Defence in depth for the same round-1 finding: whenever a reservation
    ends unconfirmed after this token was pushed at least once, the process
    publishes that it will not send under it, so the entry cannot strand the
    work for other holders."""
    ref = SharedRef(DEFAULT_STATE)
    base = deepcopy(ref.state)

    class LandsThenDeclines(RefCoordinator):
        def reserve(self, claim):
            entries = claim(deepcopy(self.ref.state))
            self.ref.state["reservations"].update(deepcopy(entries))  # landed
            # A later attempt that declines (here: the tip changed under it).
            self.ref.state["alerts"]["new_show:dune-imax"] = NOW.isoformat()
            assert claim(deepcopy(self.ref.state)) is None
            self.ref.state["alerts"].clear()
            return False

    local = host(tmp_path / "local.json", ref, "local:mac")
    local.coordinator = LandsThenDeclines(ref, "local:mac")
    calls: list[str] = []
    recording_sender(monkeypatch, calls)

    assert delivery.deliver_alert(local, alert(finding()), NOW) is False
    assert calls == []
    assert [e["status"] for e in load_state(tmp_path / "local.json")[
        "reservations"
    ].values()] == ["released"]
    ref.sync(local, base)
    cloud = host(tmp_path / "cloud.json", ref, "cloud:run-9")
    assert delivery.recover(cloud, NOW + timedelta(minutes=5)) is True
    assert len(calls) == 1
