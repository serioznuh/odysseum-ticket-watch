# Local half alerts when the cloud half stops, with no state churn.
Flow 2 (/codex-build) · builder gpt-5.6-sol/high · reviewers Claude claude-opus-5/xhigh + Codex gpt-5.6-sol/xhigh · 2026-09-20
<!-- cross-review-loop-id: 49ec4627-98fc-42d6-8f14-26d4907ec460 -->

## Task
Local half alerts when the cloud half stops, with no state churn.

## Round 1 — VERDICT: REVISE · reviewers Claude claude-opus-5/xhigh + Codex gpt-5.6-sol/xhigh
### Claude review — VERDICT: APPROVE
NOTES: workflow credential probe could gate reminder failover; wording of the README bullet; 18 h threshold sizing; warning-log volume during a sustained API outage.

### Codex review — VERDICT: REVISE
FINDINGS:
1. [P1] watcher/jobs.py:533 — Reverse cloud supervision runs after the weekly heartbeat — during a prolonged cloud outage, the local half still sends "All checks healthy" every seven days, contradicting and masking the outage OTW-09 is meant to surface. — FIXED in 51bfe94
2. [P1] watcher/jobs.py:526 — The `cloud_stale:` alert has no outbox condition or expiry, while pending work is recovered before the next cloud probe — if its first Telegram attempt fails and the cloud recovers before the next local run, the stale outage alert is replayed loudly after recovery. — FIXED in 51bfe94

## Round 2 — VERDICT: REVISE · reviewers Claude claude-opus-5/high + Codex gpt-5.6-sol/high · re-review @ high
### Claude review — VERDICT: REVISE
FINDINGS:
1. [P1] watcher/runner.py:252,256 — the cloud branches call `delivery.recover(ctx, now)` without cloud-health reconciliation or blocked condition domains, while the outbox is shared across halves — a `cloud_stale:` record left pending by a failed local send is replayed by the next successful cloud run, sending a loud stopped-cloud alert from the live cloud half after the outage ended. — FIXED in eefd815
NOTES: workflow credential probe ordering; no recovery counterpart to `cloud_stale:`; 18 h threshold sizing undocumented; a dropped residential-IP sentence in docs/current-state.md; cloud probe not under the adaptive-cadence guard.

### Codex review — VERDICT: REVISE
FINDINGS:
1. [P1] watcher/runner.py:252 — Cloud-mode recovery ignores `cloud-health` conditions — a failed local stale-cloud alert can sync into the outbox and be sent by the first recovered scheduled run, producing an obsolete alert after recovery. — FIXED in eefd815
2. [P1] watcher/jobs.py:521 — API-error handling returns before legacy outbox records receive cloud-health conditions — during an API blip, a pending pre-upgrade heartbeat lacks the tag checked by `blocked_condition_domains` and can replay "All checks healthy" while cloud health is unknown or dead. — FIXED in eefd815

## Round 3 — VERDICT: REVISE · reviewers Claude claude-opus-5/high + Codex gpt-5.6-sol/high · re-review @ high
### Claude review — VERDICT: APPROVE
NOTES: a manual `workflow_dispatch` in check mode takes the unguarded recovery path (small window, only wrong if the cloud recovered before the local half observed it); withholding the heartbeat on a stale cloud yields one alert then silence; the GitHub API call is not under the adaptive-cadence guard (about 12 req/h against a 60/h per-IP limit).

### Codex review — VERDICT: REVISE
FINDINGS:
1. [P1] docs/verification.md:73 — verification says the credential check must succeed "before the watcher step," contradicting the required and implemented post-watcher ordering — the procedure is impossible to follow and could encourage restoring the probe ahead of reminder failover.
NOTES: the round-2 runtime fixes otherwise appear correct.

## Outcome
<!-- cross-review-merge-state: CAPPED -->
stopped at cap — needs human call on: whether to accept a one-line docs fix outside the round cap. The only open finding is docs/verification.md:73, which still says the credential check runs before the watcher step; the workflow runs it after. Claude approved the head; Codex's sole remaining finding is that doc contradiction. The authoritative gate passed at every round head. Recommended: change "before" to "after" on that line, then merge.
