# OTW-23: reject bad source timestamps early and handle final state-save failures
Flow 1 (/claude-build) · builder claude-opus-5/high · reviewer gpt-5.6-sol/xhigh · 2026-09-21
<!-- cross-review-loop-id: a68e77ac-150c-45cb-8792-56f1e3e0bd2e -->

## Task
OTW-23: reject bad source timestamps early and handle final state-save failures

## Round 1 — VERDICT: REVISE
- [P1] watcher/detect.py:297 — any unreadable timestamp made the whole listing non-authoritative instead of only that field, so a valid opening beside a malformed display time announced reminders that were never armed — FIXED in 418a02b
NOTES:
- Final-save containment approach is sound and adds no dependency; nothing to change [accepted] Acceptable because it is praise only and needs no action.

## Round 2 — VERDICT: REVISE · re-review @ high
- [P1] watcher/detect.py:538 — NEW_LISTING collapsed a rejected opening into "no sale date published yet", a false absence claim — FIXED in 7adcee1
- [P1] watcher/alerts.py:443 — heartbeat collapsed rejected openings into an empty sales set and reported no upcoming opening while healthy — FIXED in 7adcee1

## Round 3 — VERDICT: APPROVE · re-review @ high
NOTES: none

## Outcome
<!-- cross-review-merge-state: APPROVED -->
Approved after 3 rounds; eligible for merge pending GitHub confirmation.
Done-when: met
Evidence: offset-free and malformed source timestamps are rejected per field as unknown evidence, a valid reminder ladder is never retired by a bad date, final-save validation and filesystem failures exit non-zero with a diagnostic while receipts stay durable, ruff and pytest pass and an isolated dry-run is clean.
