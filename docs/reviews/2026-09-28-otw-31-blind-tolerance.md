# Report a blocked Pathé check within about 30 minutes while a wanted date is pending (OTW-31).
Flow 1 (/claude-build) · builder claude-opus-5.5/xhigh · reviewer gpt-6-sol/xhigh · 2026-09-28
<!-- cross-review-loop-id: a8088f40-b63b-4be7-8a05-ffaf789dcf93 -->

## Task
Report a blocked Pathé check within about 30 minutes while a wanted date is pending (OTW-31).

## Round 1 — VERDICT: REVISE
FINDINGS:
1. [P1] watcher/alerts.py:154 — the error key changed from error:<date> to error:<failing_since> without a migration — an old-key receipt persisted before error_alerted was saved, or an old pending outbox item delivered during recovery, leaves error_alerted false, so the new key sends a second loud alert for the same outage 30 minutes later.
2. [P1] watcher/state.py:95 — failing_since is written into shared version-5 state while older code rejects unknown fields — an Actions run that checked out pre-deploy code can fetch Mac-written state, fail its pre-sync, and lose the cloud reminder failover; the rollout needs to be state-compatible.
NOTES: [obsolete] An episode start timestamp is a reasonable basis for the 30-minute rule. — superseded by round 2 (23dcc6d1) replaced the recorded episode-start timestamp with a consecutive-failure count plus freshness check on existing state fields

## Round 2 — VERDICT: APPROVE · re-review @ high
NOTES: none
Dispositions of round 1 findings: 2 FIXED, 0 DISPUTED

## Outcome
<!-- cross-review-merge-state: APPROVED -->
Approved after 2 rounds by Codex (gpt-6-sol); eligible for merge pending GitHub confirmation. Round 2 dropped the new state field and kept the first outage of a day on the historical `error:<date>` key, so the change writes only existing version-5 fields and needs no state migration or staged rollout.
Done-when: met
