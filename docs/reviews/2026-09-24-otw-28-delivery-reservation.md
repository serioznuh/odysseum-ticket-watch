# OTW-28: atomic shared reservation before any Telegram send, no Mac/cloud duplicates
Flow 1 (/claude-build) · builder claude-opus-5.5/high · reviewer gpt-6-sol/xhigh · 2026-09-24
<!-- cross-review-loop-id: 50c58238-450d-4238-9a8b-74708bcd5ddf -->

## Task
OTW-28: atomic shared reservation before any Telegram send, no Mac/cloud duplicates

## Round 1 — VERDICT: REVISE
FINDINGS:
1. [P1] watcher/state_sync.py:903 — Recovery keeps the first token when different reservations share a generation, so an older released losing token can replace the winner's uncertain token and free work that may already have been sent. — FIXED in d16b945e
2. [P1] watcher/state_sync.py:1211 — A nonzero git push is treated as a definite rejection although the ref may have updated; the retry then declines against its own held reservation and can leave unsent work blocked for a fresh runner. — FIXED in d16b945e
NOTES: none

## Round 2 — VERDICT: REVISE · re-review @ high
FINDINGS:
1. [P1] watcher/delivery.py:595 — A new generation could be based on a locally released reservation whose push never landed, so after that release reconciled it could replace the winner's held reservation and let another host send the same notification. — FIXED in b0857e12
NOTES: none

## Round 3 — VERDICT: APPROVE · re-review @ high
FINDINGS: none
NOTES: none

## Outcome
<!-- cross-review-merge-state: APPROVED -->
Approved after 3 rounds by Codex (gpt-6-sol); eligible for merge pending GitHub confirmation. Done-when: met
Follow-ups: the `.github/workflows/watch.yml` step change (exit-code 5/6 handling; cron untouched) still needs the owner-approved live Actions check after merge; code still on schema 4 cannot sync the upgraded state ref, so deploy the Mac clone promptly; each send adds one extra commit to `runtime-state` (relevant to OTW-26); the BACKLOG prose still names the renamed overlap test.
