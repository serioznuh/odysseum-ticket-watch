"""The bounded units of work one watcher pass is made of (OTW-19).

Every job is a plain function over the shared `RunContext`. They are called one
after another by the coordinator in `watcher/runner.py`; nothing here starts a
thread, a process or a task, so there is still exactly **one writer** to
`ctx.state` per run — the guarantee the old monolithic `run()` had implicitly.

Polling jobs take a `Budget` (see `watcher/budget.py`): an *aggregate*
wall-clock allowance covering everything the job does, rather than a
per-request timeout. A source that fails slowly in many small steps used to be
able to burn a whole reminder window; now it runs out of budget, is recorded as
a Pathé/Cinesa failure through the existing supervision paths, and the run
moves on.

The reminder ladder is deliberately *not* budgeted: it makes at most one
Telegram call and is the one thing in a run whose correctness is measured in
minutes.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

from . import (
    alerts,
    cdp,
    cinesa,
    cloud,
    coalesce,
    delivery,
    detect,
    news,
    pathe,
    state_sync,
)
from . import state as state_mod
from .budget import Budget
from .detect import Finding

log = logging.getLogger("watcher.jobs")

# All polling in one run must finish inside a single launchd firing interval,
# with room left for the ladder and the state save — otherwise a slow run is
# still in flight when the interval that owns the next reminder rung comes up.
POLLING_BUDGET_SECONDS = 0.8 * state_mod.LOCAL_FIRING_INTERVAL_MINUTES * 60

# Per-job shares of that ceiling. They sum to less than the aggregate on
# purpose: each job is capped on its own *and* by whatever the jobs before it
# left over, so no ordering of failures can exceed POLLING_BUDGET_SECONDS.
PATHE_BUDGET_SECONDS = 120.0
NEWS_BUDGET_SECONDS = 45.0
CINESA_BUDGET_SECONDS = 60.0

CLOUD_STALE_PREFIX = "cloud_stale:"
CLOUD_RECOVERED_PREFIX = "cloud_recovered:"
CLOUD_EPISODE_PREFIX = f"{CLOUD_STALE_PREFIX}episode:"


@dataclass
class RunContext:
    """Everything one pass needs, and the only mutable state it shares.

    `clock` is injected rather than called directly so the CLI stays the single
    place that decides what "now" means (and so tests can script it).
    """

    cfg: Any
    state: dict
    clock: Callable[[], datetime]
    mode: str = "check"
    with_news: bool = False
    dry_run: bool = False
    adaptive_cadence: bool = False
    skip_if_checked_within: float = 0.0
    reminder_grace_minutes: float = 0.0
    state_sync_marker: str = state_sync.DEFAULT_MARKER_PATH
    state_path: str | None = None
    state_writer: Callable[[str, dict], None] | None = None
    monotonic: Callable[[], float] = time.monotonic
    sleeper: Callable[[float], None] = time.sleep
    # Process-local guard: source delivery and later outbox recovery share one
    # pass, but a definite failure must wait for the next firing rather than
    # being attempted twice back-to-back.
    delivery_attempts: set[str] = field(default_factory=set)
    # OTW-28: the shared-reservation handle (`state_sync.DeliveryCoordinator`).
    # None only where no shared state exists — dry runs and state files outside
    # the synchronized store — and then delivery keeps OTW-20's local claims.
    coordinator: Any = None

    def budget(self, seconds: float, label: str) -> Budget:
        return Budget(seconds, monotonic=self.monotonic, sleep=self.sleeper, label=label)


@dataclass
class PatheOutcome:
    snapshot: detect.Snapshot | None = None
    findings: list[Finding] = field(default_factory=list)
    error_key: str | None = None
    health: str | None = None


@dataclass
class CinesaOutcome:
    snapshot: detect.CinesaSnapshot | None = None
    findings: list[Finding] = field(default_factory=list)
    error_key: str | None = None
    health: str | None = None
    # A Chrome that may still hold the watcher profile lock. Unlike an ordinary
    # Cinesa outage, this needs the owner's hands and will break every later
    # mint, so it makes the run exit non-zero as well as feeding the streak.
    integrity_failure: bool = False
    # None means the profile lock could not be classified. Only True/False is
    # authoritative enough to preserve or retire pending owner advice.
    token_profile_stuck: bool | None = None


# ------------------------------------------------------------------ cadence

def pathe_due(ctx: RunContext, now: datetime) -> bool:
    """The adaptive-cadence guard, for the Pathé + news half only.

    Pure state arithmetic — no network — and it stays in front of everything
    that half fetches. It gates neither the Cinesa half nor the reminder
    ladder, which run on every firing by design.
    """
    threshold = ctx.skip_if_checked_within
    if ctx.adaptive_cadence:
        threshold = state_mod.adaptive_staleness_hours(ctx.state, ctx.cfg, now)
    if threshold > 0 and state_mod.is_check_fresh(ctx.state, threshold, now):
        log.info(
            "last successful Pathé check (%s) is newer than %.2fh%s — skipping it",
            ctx.state.get("last_check_ok"),
            threshold,
            " [adaptive tier]" if ctx.adaptive_cadence else "",
        )
        return False
    if ctx.adaptive_cadence:
        log.info("adaptive cadence tier: check when older than %.2fh — running", threshold)
    return True


# -------------------------------------------------------- supervision jobs


def run_state_sync_failure_job(ctx: RunContext, now: datetime) -> bool:
    """Deliver one loud alert for the unresolved local sync episode."""
    marker = state_sync.load_failure(ctx.state_sync_marker)
    if marker is None:
        return False
    finding = alerts.build_state_sync_failure_finding(ctx.cfg, marker)
    return deliver(ctx, [finding], now)


# ------------------------------------------------------------- source jobs

def run_pathe_job(
    ctx: RunContext, client: Any, now: datetime, budget: Budget | None
) -> PatheOutcome:
    """Fetch the Pathé snapshot and turn it into findings.

    Consumes OTW-13's explicit health results: a catalogue failure blinds the
    check, a partial (per-listing) failure refreshes the liveness pulse and
    feeds the same capped supervision streak, and only a fully healthy snapshot
    clears the outage bookkeeping.
    """
    out = PatheOutcome()
    try:
        snap = pathe.fetch_snapshot(client, ctx.cfg, budget=budget)
    except Exception as e:
        log.exception("Pathé check failed")
        out.health = "blind"
        finding = alerts.record_pathe_failure(ctx.cfg, ctx.state, str(e), now)
        if finding is not None:
            out.findings.append(finding)
            out.error_key = finding.key
        return out

    out.snapshot = snap
    # Analyse BEFORE the health bookkeeping below, deliberately. `analyze_pathe`
    # reads only the observation baselines (`shows_seen`, `sales`,
    # `formats_seen`, and `tickets_available` via `reminders_cover`), never the
    # health fields, so the order does not change a single finding — but if
    # analysis raises, the coordinator discards this outcome, and clearing
    # `error_alerted` first would bank a recovery whose alert was thrown away.
    # `build_recovered_finding` would then never fire again: the outage would
    # end silently, for good.
    analyzed = detect.analyze_pathe(snap, ctx.state, ctx.cfg, now)

    degradation = snap.degradation_summary()
    if degradation:
        out.health = "degraded"
        state_mod.refresh_catalogue_liveness(ctx.state, now)
        log.warning("%s", degradation)
        finding = alerts.record_pathe_failure(ctx.cfg, ctx.state, degradation, now)
        if finding is not None:
            out.findings.append(finding)
            out.error_key = finding.key
    else:
        # Reads `st` before the clear below, so the blind span is recoverable.
        if ctx.state.get("error_alerted"):
            out.findings.append(alerts.build_recovered_finding(ctx.cfg, ctx.state, now))
        ctx.state["failure_streak"] = 0
        ctx.state["error_alerted"] = False
        # Stale cause + spent stale keys must not survive into the next
        # outage: they would make the cloud pass report the wrong reason.
        ctx.state.pop("last_error", None)
        for spent in [k for k in ctx.state.get("alerts", {}) if k.startswith("stale:")]:
            ctx.state["alerts"].pop(spent, None)
        ctx.state["last_check_ok"] = now.isoformat()
        ctx.state["last_catalogue_ok"] = now.isoformat()
        out.health = "healthy"
    out.findings.extend(analyzed)
    return out


def run_news_job(
    ctx: RunContext,
    client: Any,
    now: datetime,
    budget: Budget | None,
    *,
    cloud: bool = False,
) -> list[Finding]:
    """News leads. Non-fatal by design: a dead feed must not blind the watch.

    Local selection still follows the Pathé cadence. Cloud selection is
    explicit and filters sources in ``news.fetch_news_items``.
    """
    if not ctx.cfg.news_enabled:
        return []
    try:
        items = news.fetch_news_items(client, ctx.cfg, budget=budget, cloud=cloud)
        return detect.analyze_news(items, ctx.cfg, ctx.state, now)
    except Exception:
        log.exception("news check failed (non-fatal)")
        return []


def track_profile_leak(
    ctx: RunContext,
    out: CinesaOutcome,
    now: datetime,
    budget: Budget | None,
) -> None:
    """Keep a leak visible until the profile is actually free again.

    A counter cannot do this job. The cached token keeps working for hours after
    a leak, those runs succeed, and `update_from_cinesa` resets `failure_streak`
    to zero — so a leak that recurs every 30 min (the mint backoff) never reaches
    the alert threshold and never fires (round-6 review).

    So the condition is re-tested instead of counted: `leak_since` is stamped
    once when a leak is seen and cleared only when Chrome demonstrably no longer
    holds the profile. That reconciliation runs after every Cinesa outcome, not
    only after another ChromeLeakError. An unknown answer is not "resolved" and
    leaves it set. While it is set every run exits non-zero, and once it has
    outlasted what the failure threshold means in wall-clock time, it alerts.
    The first alert actually delivered for this episode is loud; later daily
    reminders are silent.

    State is written twice per episode at most (stamped, then cleared), so a
    long leak does not churn `state.json` at the 5-min cadence.
    """
    cin = ctx.state.setdefault("cinesa", {})
    status = cdp.profile_lock_status(ctx.cfg.cinesa_chrome_profile, budget)
    out.token_profile_stuck = status
    if status is False:
        if cin.pop("leak_since", None):
            log.info("cinesa: the watcher profile is free again — leak cleared")
        return

    # True is a verified live watcher Chrome; None is a present lock whose
    # owner cannot be determined. Both must start an episode when there is no
    # prior stamp: requiring ChromeLeakError here would miss a leftover process
    # after an unrelated fetch failure or a successful cached-token poll.
    cin.setdefault("leak_since", now.isoformat())

    out.integrity_failure = True
    since = detect.as_aware(detect.parse_iso(cin["leak_since"]) or now)
    # The same wall-clock meaning the failure threshold has for the local half:
    # this many consecutive firings of the condition.
    threshold = timedelta(
        minutes=state_mod.LOCAL_FIRING_INTERVAL_MINUTES * ctx.cfg.failure_streak_threshold
    )
    if now - since < threshold:
        return
    # Like stale alerts, periods begin at the eligibility threshold and last a
    # full 24 hours. Midnight is irrelevant. Including the episode stamp means
    # a resolved leak followed by another one the same day gets a fresh key.
    period = (now - since - threshold).days
    episode = cin["leak_since"]
    episode_prefix = f"cinesa_leak:{episode}:"
    key = f"{episode_prefix}{period}"
    delivered = any(
        alert_key.startswith(episode_prefix)
        for alert_key in ctx.state.get("alerts", {})
    )
    day = period + 1 if delivered else 1
    out.findings.append(alerts.build_cinesa_leak_finding(ctx.cfg, since, key, day=day))


def run_cinesa_job(
    ctx: RunContext, now: datetime, budget: Budget | None
) -> CinesaOutcome:
    """The Cinesa half: one small API call, behind a token minted by a real
    headed Chrome. The budget covers that refresh too, so a Mac that cannot
    open Chrome right now cannot hold the whole run open."""
    out = CinesaOutcome()
    if not ctx.cfg.cinesa_enabled:
        return out

    cin = ctx.state.setdefault("cinesa", {})
    try:
        snap = cinesa.fetch_snapshot(ctx.cfg, budget=budget)
    except Exception as e:
        log.exception("Cinesa check failed")
        out.health = "blind"
        # Capped at the alert threshold: nothing reads a larger value,
        # and a counter that kept growing would rewrite state.json on
        # every firing of a long outage, commit and push included.
        cin["failure_streak"] = min(
            cin.get("failure_streak", 0) + 1, ctx.cfg.failure_streak_threshold
        )
        if cin["failure_streak"] >= ctx.cfg.failure_streak_threshold:
            # Stamped once, when the outage is first confirmed — not per
            # run, which at 5-min cadence would push state ~288x a day.
            cin.setdefault("blind_since", now.isoformat())
            started = detect.parse_iso(cin.get("blind_since")) or now
            # The key is already day-stamped, so it yields exactly one
            # alert per day; day 1 buzzes, later days repeat silently.
            day = (now.date() - detect.as_aware(started).date()).days + 1
            out.error_key = f"cinesa_error:{now:%Y-%m-%d}"
            out.findings.append(
                alerts.build_cinesa_error_finding(
                    ctx.cfg,
                    e,
                    out.error_key,
                    day=day,
                    since=alerts.short_dt(started) if day > 1 else None,
                )
            )
    else:
        out.snapshot = snap
        out.health = "healthy"
        if cin.get("error_alerted"):
            out.findings.append(alerts.build_cinesa_recovered_finding(ctx.cfg, now))
        cin.pop("blind_since", None)
        out.findings.extend(detect.analyze_cinesa(snap, ctx.state, ctx.cfg, now))

    # Reconcile independently of the fetch result. A successful cached-token
    # poll does not prove the profile is free, an ordinary outage does not prove
    # a prior leak persists, and a fresh cleanup error can already have resolved
    # by the time this exact profile check runs.
    track_profile_leak(ctx, out, now, budget)
    return out


# ---------------------------------------------------------------- delivery

def deliver(
    ctx: RunContext,
    findings: list[Finding],
    now: datetime,
    force_keys: set[str] | None = None,
) -> bool:
    """Filter what was already sent, merge one piece of news into one message,
    send, and mark every member key of a merged message — or none of them."""
    pending: list[Finding] = []
    force_keys = force_keys or set()
    for f in findings:
        if f.key not in force_keys and state_mod.already_sent(ctx.state, f.key):
            log.debug("suppressed duplicate alert %s", f.key)
            continue
        if any(p.key == f.key for p in pending):
            # Same key twice in one pass — a listing the catalogue returned
            # twice. Harmless before merging (the first send marked it), but
            # it would repeat itself inside a merged message.
            log.debug("dropped repeated finding %s", f.key)
            continue
        pending.append(f)

    sent_any = False
    for alert in coalesce.merge(pending, ctx.cfg):
        f = alert.finding
        if alert.merged:
            log.info(
                "merged %d findings into one alert (keys=%s)",
                len(alert.keys),
                " ".join(alert.keys),
            )
        log.info("alert [%s] %s (key=%s)", f.kind, f.title, f.key)
        # One loud member is enough to buzz: merging must never silence an
        # alert that would have arrived with sound on its own.
        if delivery.deliver_alert(
            ctx, alert, now, force=bool(set(alert.keys) & force_keys)
        ):
            sent_any = True
    return sent_any


def advance_baselines(
    ctx: RunContext,
    pathe_out: PatheOutcome,
    cinesa_out: CinesaOutcome,
    findings: list[Finding],
    now: datetime,
) -> None:
    """Move every baseline whose alert was actually delivered, and no other."""
    # The error flag flips only once the alert really went out, so a failed
    # send retries on the next run instead of being silently swallowed.
    if pathe_out.error_key and state_mod.already_sent(ctx.state, pathe_out.error_key):
        ctx.state["error_alerted"] = True
    if cinesa_out.error_key and state_mod.already_sent(ctx.state, cinesa_out.error_key):
        ctx.state.setdefault("cinesa", {})["error_alerted"] = True

    if cinesa_out.snapshot is not None:
        # Same rule for the IMAX baseline: advancing it after a failed send
        # would make analyze_cinesa agree with the new reality and never
        # re-raise the transition, losing the alert for good.
        imax_delivered = all(
            state_mod.already_sent(ctx.state, f.key)
            for f in findings
            if f.kind in ("CINESA_IMAX_GONE", "CINESA_IMAX_BACK")
        )
        state_mod.update_from_cinesa(
            ctx.state, cinesa_out.snapshot, ctx.cfg, now, advance_imax=imax_delivered
        )

    if pathe_out.snapshot is not None:
        # Advancing these baselines after a failed send would make the next
        # analysis agree with the new reality and lose one-shot alerts.
        one_shot_delivered = all(
            state_mod.already_sent(ctx.state, f.key)
            for f in findings
            if f.kind in ("NEW_LISTING", "TICKETS_AVAILABLE")
        )
        # `sales` is SALE_DATE's baseline and fails on its own: recording an
        # opening that was never announced retired the alert for good.
        sale_delivered = all(
            state_mod.already_sent(ctx.state, f.key)
            for f in findings
            if f.kind in ("SALE_DATE", "SALE_DATE_CHANGED")
        )
        state_mod.update_from_snapshot(
            ctx.state,
            pathe_out.snapshot,
            ctx.cfg,
            now,
            advance_one_shot=one_shot_delivered,
            advance_sales=sale_delivered,
        )


def run_heartbeat_job(
    ctx: RunContext,
    snap: detect.Snapshot | None,
    now: datetime,
    sent_any: bool,
    cloud_health: str = "disabled",
) -> None:
    """The weekly all-quiet status, suppressed on any run that already spoke.

    `sent_any` counts this pass's *check-half* sends only: reminders have never
    stood in for the heartbeat and must not start doing so now that they are
    also computed before the check.
    """
    if snap is None or sent_any or cloud_health in {"stale", "unknown"}:
        return
    if not alerts.heartbeat_due(ctx.state, now, ctx.cfg.heartbeat_days):
        return
    hb = alerts.build_heartbeat(ctx.cfg, snap, ctx.state, now)
    delivery.deliver_heartbeat(ctx, hb, now)


# --------------------------------------------------- reminders, supervision

def run_reminder_job(
    ctx: RunContext, now: datetime, *, retry_existing: bool = True
) -> bool:
    """Send whatever rung of the ladder is due at `now`.

    Called twice per run — once before any polling, once after fresh
    observations land — and it is safe to call twice because `reminders_sent`
    *is* the dedup record: `mark_reminder` writes the receipt, and
    `due_reminders` will not offer a rung that has one. The local/cloud
    ownership split is unchanged: the owner passes grace 0, the cloud failover
    passes a grace that `due_reminders` floors at the local firing interval so
    it can never reach a rung before the owner's worst-case first firing.
    """
    due = state_mod.due_reminders(
        ctx.state,
        ctx.cfg.reminder_offsets_minutes,
        now,
        ctx.reminder_grace_minutes,
        cfg=ctx.cfg,
    )
    sent = False
    for r in due:
        log.info("reminder due: %s before %s", r["offset"], r["target"])
        if delivery.deliver_reminder(
            ctx, r, now, retry_existing=retry_existing
        ):
            sent = True
    return sent


def _cloud_recovery_key(stale_key: str) -> str:
    return f"{CLOUD_RECOVERED_PREFIX}{stale_key.removeprefix(CLOUD_STALE_PREFIX)}"


def _cloud_stale_receipts(state: dict) -> list[str]:
    return sorted(
        key for key in state.get("alerts", {}) if key.startswith(CLOUD_STALE_PREFIX)
    )


def _rearm_cloud_outage(state: dict, now: datetime) -> None:
    """Close delivered outage episodes, including pre-OTW-27 timestamp keys."""
    for stale_key in _cloud_stale_receipts(state):
        recovery_key = _cloud_recovery_key(stale_key)
        if not state_mod.already_sent(state, recovery_key):
            state_mod.mark_sent(state, recovery_key, now)


def _cloud_outage_key(state: dict) -> str | None:
    """Return one stable key for the active episode, or None if already sent."""
    stale_keys = _cloud_stale_receipts(state)
    if any(
        not state_mod.already_sent(state, _cloud_recovery_key(key))
        for key in stale_keys
    ):
        return None

    episode_numbers = []
    for key in stale_keys:
        suffix = key.removeprefix(CLOUD_EPISODE_PREFIX)
        if key.startswith(CLOUD_EPISODE_PREFIX) and suffix.isdigit():
            episode_numbers.append(int(suffix))
    return f"{CLOUD_EPISODE_PREFIX}{max(episode_numbers, default=0) + 1}"


def run_cloud_supervision_job(ctx: RunContext, now: datetime) -> str:
    """Alert locally when successful scheduled cloud runs have gone stale.

    The public API is evidence, not a watched source: only a validated complete
    window with no recent success proves an outage; uncertainty stays quiet.
    """
    stale_hours = getattr(ctx.cfg, "cloud_stale_hours", 0)
    repository = getattr(ctx.cfg, "cloud_repository", "")
    workflow = getattr(ctx.cfg, "cloud_workflow", "")
    if (
        stale_hours <= 0
        or not repository
        or not workflow
        or alerts.running_in_ci()
    ):
        return "disabled"
    # This must happen even when the API call below fails: otherwise a pending
    # pre-upgrade heartbeat has no condition for unknown-health recovery to block.
    delivery.bind_cloud_health_conditions(ctx)
    now = detect.as_aware(now)
    stale_window = timedelta(hours=stale_hours)
    try:
        has_recent_success = cloud.has_successful_scheduled_run(
            repository,
            workflow,
            since=now - stale_window,
            until=now,
        )
    except cloud.CloudStatusError as exc:
        log.warning("cloud supervision unavailable (no alert): %s", exc)
        return "unknown"
    if has_recent_success:
        delivery.reconcile_cloud_health(ctx, "healthy")
        _rearm_cloud_outage(ctx.state, now)
        return "healthy"
    delivery.reconcile_cloud_health(ctx, "stale")
    key = _cloud_outage_key(ctx.state)
    if key is None:
        return "stale"
    finding = alerts.build_cloud_stale_finding(ctx.cfg, key, stale_hours)
    deliver(ctx, [finding], now)
    return "stale"


def run_supervision_job(ctx: RunContext, now: datetime) -> None:
    """Cloud-side dead-man's switch for the local catalogue pulse."""
    if ctx.adaptive_cadence:
        return
    if not state_mod.is_catalogue_check_stale(ctx.state, ctx.cfg.stale_check_hours, now):
        return
    catalogue_iso = state_mod.catalogue_check_iso(ctx.state)
    last_ok = detect.as_aware(detect.parse_iso(catalogue_iso))
    blind = now - last_ok
    # Alert once at the threshold, then every 24h for as long as it lasts —
    # a blind spell that goes quiet after one message is the failure mode
    # this exists to prevent. Periods are measured from the first alert, so
    # every repeat lands at the same clock time.
    period = alerts.stale_period(blind, ctx.cfg.stale_check_hours)
    key = f"stale:{catalogue_iso}:{period}"
    if state_mod.already_sent(ctx.state, key):
        return
    stale = alerts.build_stale_finding(ctx.cfg, ctx.state, blind, key, period + 1)
    deliver(ctx, [stale], now)
