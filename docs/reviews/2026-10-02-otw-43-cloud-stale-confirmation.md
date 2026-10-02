# OTW-43: confirm a cloud-stale verdict across firings before sending the Cloud checks have stopped alert
Flow 3 (/codex-review) · fixer claude-opus-5.5/high · reviewer gpt-6.1-sol/xhigh · 2026-10-02
<!-- cross-review-loop-id: 007972c5-feff-44eb-aee4-3cf76b56cec2 -->

## Task
OTW-43: confirm a cloud-stale verdict across firings before sending the Cloud checks have stopped alert
Required checks: ["test"] (source: versioned)

## Round 1 — VERDICT: REVISE
FINDINGS:
1. [P1] watcher/jobs.py:602 — dry-runs write the cloud stale-confirmation marker, and a healthy dry-run (line 596) deletes it — a stale rehearsal can serve as prior evidence for a real alert 30 minutes later, and a healthy rehearsal erases real evidence; dry-runs must not write or clear it
NOTES: none

## Round 2 — VERDICT: APPROVE · re-review @ high
NOTES: none
Dispositions of round 1 findings: 1 FIXED, 0 DISPUTED

## Outcome
<!-- cross-review-merge-state: APPROVED pr=37 -->
Approved after 2 rounds; eligible for merge pending GitHub confirmation.
Done-when: met
