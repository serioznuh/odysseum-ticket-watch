# OTW-30: require explicit bootstrap when the shared state ref is missing
Flow 1 (/claude-build) · builder claude-opus-5/high · reviewer gpt-5.6-sol/xhigh · 2026-09-22
<!-- cross-review-loop-id: e9e6fae3-3556-40dd-a75c-3c06d3b6f820 -->

## Task
OTW-30: require explicit bootstrap when the shared state ref is missing

## Round 1 — VERDICT: REVISE
- [P1] watcher/state_sync.py — recovery did not reconcile an independently recreated remote ref before dropping base.json, so remote-only sending/uncertain outbox work lost its quarantine and could be re-sent — FIXED in 9a8952d
- [P1] watcher/state_sync.py — confirmed-ref-absence handling could return exit 1 if best-effort cleanup raised OSError, and the wrappers only block on the dedicated exit code — FIXED in 9a8952d
NOTES: none

## Round 2 — VERDICT: APPROVE · re-review @ high
NOTES: none


## Outcome
<!-- cross-review-merge-state: APPROVED -->
Approved after 2 rounds; eligible for merge pending GitHub confirmation.
Done-when: met
Evidence: confirmed ref absence always blocks ordinary delivery (transport failure stays distinct), local live/base state survives untouched, explicit init/recover replace silent reseeding, a concurrently created remote ref is preserved and folded rather than overwritten, recovery reconciles confirmed receipts and quarantines uncertain attempts, both wrappers (scripts/local-check.sh, .github/workflows/watch.yml) honor the block, ruff and pytest pass (445), and isolated real-Git dry-runs cover fresh clone, existing clone, init, concurrent creation, and recovery.
