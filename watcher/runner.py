"""Coordinator: one watcher pass, as an ordered sequence of bounded jobs.

The order *is* the contract (OTW-19):

1. **Due reminders, before any network call.** The ladder is the one thing in a
   run whose correctness is measured in minutes, and it used to wait behind all
   polling and retries. Re-reading the clock afterwards fixed the *wording* of a
   late reminder but could not give back a warning window a slow source had
   already eaten.
2. **Local-sync supervision** (no network, no budget), then the adaptive-cadence
   guard, still in front of everything the Pathé + news half fetches, then the
   Pathé, news and Cinesa polling jobs. Each polling job runs under an
   aggregate time budget, and each job here is guarded, so a job that is
   skipped, disabled or outright broken cannot take the rest of the pass with it.
3. **Delivery** of this pass's findings (dedup, coalescing, durable outbox,
   sending and immediate receipt persistence), then the baselines they gate.
   Fresh findings get the first chance to supersede stale pending work before
   safe pending records from an earlier run are recovered.
4. **Due reminders again**, recomputed against a fresh clock and the
   observations that just landed. `reminders_sent` is the dedup record, so a
   rung sent in step 1 cannot be sent twice.
5. **Supervision**, then a final state save for non-delivery bookkeeping.

Everything runs in this process, one job after another. There is exactly one
writer to `ctx.state` per run — the guarantee the monolithic `run()` had
implicitly, and which splitting it up must not quietly drop.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable

from . import delivery, jobs, pathe
from . import state as state_mod
from .budget import Budget
from .detect import Finding, Snapshot
from .jobs import RunContext

log = logging.getLogger("watcher.runner")


def _guard(failed: list[str], name: str, fn: Callable, *args, **kwargs):
    """Run a job; an unexpected failure is recorded, never propagated.

    A crash inside one job must not cost the run its reminders, its supervision
    or its state save — that is the whole reason these are separate jobs. The
    names collected here make `execute` exit non-zero, so a bug still surfaces
    in launchd's log and in Actions rather than passing for a healthy pass.
    """
    try:
        return fn(*args, **kwargs)
    except Exception:
        log.exception("%s job failed unexpectedly", name)
        failed.append(name)
        return None


def _run_source_jobs(
    ctx: RunContext,
    now: datetime,
    polling: Budget,
    failed: list[str],
) -> tuple[bool, Snapshot | None]:
    """The check half: poll the sources, deliver what they found, move the
    baselines those alerts gate."""
    try:
        due = jobs.pathe_due(ctx, now)
        if not due and not ctx.cfg.cinesa_enabled:
            # Nothing to poll. The original zero-network no-op, and it still
            # falls through to the ladder, which the local half owns (OTW-15).
            return False, None
        client = pathe.make_client()
    except Exception:
        log.exception("source setup failed")
        failed.append("sources")
        return False, None

    findings: list[Finding] = []
    pathe_out = jobs.PatheOutcome()
    cinesa_out = jobs.CinesaOutcome()

    if due:
        result = _guard(
            failed,
            "pathe",
            jobs.run_pathe_job,
            ctx,
            client,
            now,
            polling.child(jobs.PATHE_BUDGET_SECONDS, "Pathé check"),
        )
        if result is not None:
            pathe_out = result
            findings.extend(pathe_out.findings)

        news_findings = _guard(
            failed,
            "news",
            jobs.run_news_job,
            ctx,
            client,
            now,
            polling.child(jobs.NEWS_BUDGET_SECONDS, "news check"),
        )
        findings.extend(news_findings or [])

    result = _guard(
        failed,
        "cinesa",
        jobs.run_cinesa_job,
        ctx,
        now,
        polling.child(jobs.CINESA_BUDGET_SECONDS, "Cinesa check"),
    )
    if result is not None:
        cinesa_out = result
        findings.extend(cinesa_out.findings)
        if cinesa_out.integrity_failure:
            # A possibly-leaked Chrome still gets its alert through the normal
            # streak, but must not let the run report success: it needs the
            # owner, and every later mint will trip over the profile lock.
            failed.append("cinesa-cleanup")

    force_keys = {pathe_out.error_key} if pathe_out.error_key else set()
    delivered = _guard(
        failed, "delivery", jobs.deliver, ctx, findings, now, force_keys
    )
    sent_any = bool(delivered)

    _guard(
        failed, "baselines", jobs.advance_baselines, ctx, pathe_out, cinesa_out, findings, now
    )
    _guard(
        failed,
        "observation-reconciliation",
        delivery.reconcile_source_observations,
        ctx,
        now=now,
        pathe_snapshot=pathe_out.snapshot,
        pathe_health=pathe_out.health,
        cinesa_snapshot=cinesa_out.snapshot,
        cinesa_health=cinesa_out.health,
        cinesa_token_stuck=cinesa_out.token_profile_stuck,
    )
    return sent_any, pathe_out.snapshot


def execute(ctx: RunContext, state_path: str) -> int:
    """Run one pass and persist its state. Returns the process exit code."""
    # The run-start reading. Every piece of bookkeeping uses it on purpose —
    # `last_check_ok`, the dedup keys and the staleness arithmetic all record
    # *when this batch ran*, and a single run has to agree with itself.
    now = ctx.clock()
    failed: list[str] = []
    ctx.state_path = state_path

    # Reminders first, before a single request. On check runs, newly due work
    # still sends here, but a failed record from an earlier pass waits until
    # polling confirms that its opening has not moved.
    _guard(
        failed,
        "reminder",
        jobs.run_reminder_job,
        ctx,
        now,
        retry_existing=ctx.mode != "check",
    )

    # A failed pre-run rebase leaves a durable marker and then deliberately
    # lets this pass continue. Surface it before polling, but never ahead of a
    # due reminder: reminder timing remains the coordinator's first contract.
    _guard(failed, "state-sync-supervision", jobs.run_state_sync_failure_job, ctx, now)

    if ctx.mode == "check":
        polling = ctx.budget(jobs.POLLING_BUDGET_SECONDS, "polling")
        sent_now, pathe_snapshot = _run_source_jobs(ctx, now, polling, failed)
        # Polling and delivery of the current observations deliberately happen
        # before recovery. Their topic policy can retire a failed message whose
        # opening or availability changed while it was pending; replaying first
        # would send stale advice and then its correction back-to-back.
        recovered = _guard(failed, "outbox-recovery", delivery.recover, ctx, now)
        _guard(
            failed,
            "heartbeat",
            jobs.run_heartbeat_job,
            ctx,
            pathe_snapshot,
            now,
            sent_now or bool(recovered),
        )
    else:
        # Remind-only runs have no source observations that could supersede the
        # queue, so safe pending work can be recovered immediately.
        _guard(failed, "outbox-recovery", delivery.recover, ctx, now)

    # Read the clock AGAIN. Polling is budgeted but still not free, and a run
    # that started at T-16 and reaches this line at T+5 must send the "sale is
    # open" ping, not a 15-min warning worded from a clock that has expired.
    # A rung already sent above has its receipt in `reminders_sent`, so this
    # pass can only add what the fresh observations or the later clock made due.
    _guard(failed, "reminder", jobs.run_reminder_job, ctx, ctx.clock())

    _guard(failed, "supervision", jobs.run_supervision_job, ctx, now)

    if ctx.dry_run:
        log.info("dry-run: state NOT saved (%s)", state_path)
    else:
        state_mod.save_state(state_path, ctx.state)
        log.info("state saved to %s", state_path)

    if failed:
        log.error("run completed with failed job(s): %s", ", ".join(sorted(set(failed))))
        return 1
    return 0
