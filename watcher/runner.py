"""Coordinator: one watcher pass, as an ordered sequence of bounded jobs.

The order *is* the contract (OTW-19):

1. **Due reminders, before any network call.** The ladder is the one thing in a
   run whose correctness is measured in minutes, and it used to wait behind all
   polling and retries. Re-reading the clock afterwards fixed the *wording* of a
   late reminder but could not give back a warning window a slow source had
   already eaten.
2. **The adaptive-cadence guard**, still in front of everything the Pathé + news
   half fetches, then the Pathé, news and Cinesa polling jobs. Each runs under
   an aggregate time budget, and each is guarded, so a job that is skipped,
   disabled or outright broken cannot take the rest of the pass with it.
3. **Delivery** of this pass's findings (dedup, coalescing, sending), then the
   baselines those alerts gate.
4. **Due reminders again**, recomputed against a fresh clock and the
   observations that just landed. `reminders_sent` is the dedup record, so a
   rung sent in step 1 cannot be sent twice.
5. **Supervision**, then exactly one state save.

Everything runs in this process, one job after another. There is exactly one
writer to `ctx.state` per run — the guarantee the monolithic `run()` had
implicitly, and which splitting it up must not quietly drop.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable

from . import jobs, pathe
from . import state as state_mod
from .budget import Budget
from .detect import Finding
from .jobs import RunContext

log = logging.getLogger("watcher.runner")


def _guard(failed: list[str], name: str, fn: Callable, *args):
    """Run a job; an unexpected failure is recorded, never propagated.

    A crash inside one job must not cost the run its reminders, its supervision
    or its state save — that is the whole reason these are separate jobs. The
    names collected here make `execute` exit non-zero, so a bug still surfaces
    in launchd's log and in Actions rather than passing for a healthy pass.
    """
    try:
        return fn(*args)
    except Exception:
        log.exception("%s job failed unexpectedly", name)
        failed.append(name)
        return None


def _run_source_jobs(
    ctx: RunContext, now: datetime, polling: Budget, failed: list[str]
) -> None:
    """The check half: poll the sources, deliver what they found, move the
    baselines those alerts gate."""
    try:
        due = jobs.pathe_due(ctx, now)
        if not due and not ctx.cfg.cinesa_enabled:
            # Nothing to poll. The original zero-network no-op, and it still
            # falls through to the ladder, which the local half owns (OTW-15).
            return
        client = pathe.make_client()
    except Exception:
        log.exception("source setup failed")
        failed.append("sources")
        return

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

    sent_any = pathe_out.sent
    delivered = _guard(failed, "delivery", jobs.deliver, ctx, findings, now)
    sent_any = bool(delivered) or sent_any

    _guard(
        failed, "baselines", jobs.advance_baselines, ctx, pathe_out, cinesa_out, findings, now
    )
    _guard(failed, "heartbeat", jobs.run_heartbeat_job, ctx, pathe_out.snapshot, now, sent_any)


def execute(ctx: RunContext, state_path: str) -> int:
    """Run one pass and persist its state. Returns the process exit code."""
    # The run-start reading. Every piece of bookkeeping uses it on purpose —
    # `last_check_ok`, the dedup keys and the staleness arithmetic all record
    # *when this batch ran*, and a single run has to agree with itself.
    now = ctx.clock()
    failed: list[str] = []

    # Reminders first, before a single request. See this module's docstring.
    _guard(failed, "reminder", jobs.run_reminder_job, ctx, now)

    if ctx.mode == "check":
        polling = ctx.budget(jobs.POLLING_BUDGET_SECONDS, "polling")
        _run_source_jobs(ctx, now, polling, failed)

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
