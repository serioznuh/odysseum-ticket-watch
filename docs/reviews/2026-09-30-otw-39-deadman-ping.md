# OTW-39: ping a Healthchecks.io dead-man's switch at the end of every completed local firing (bounded, secret URL, no status change)
Flow 1 (/claude-build) · builder claude-opus-5.5/high · reviewer gpt-6.1-sol/xhigh · 2026-09-30
<!-- cross-review-loop-id: aff08a21-4e3b-4583-832f-60b60b246acf -->

## Task
OTW-39: ping a Healthchecks.io dead-man's switch at the end of every completed local firing (bounded, secret URL, no status change)

## Round 1 — VERDICT: REVISE
FINDINGS:
1. [P1] scripts/local-check.sh:186 — A post-run sync returning 3 still reaches the ping. If `runtime-state` disappears after the pre-run sync, the script pings and then exits 3, falsely reporting completion. Confirmed with the unchanged footer and a stubbed sync. Add the missing post-sync exit-3 guard and regression case.
2. [P1] scripts/local-check.sh:176 — The 10-second timeout excludes cleanup. The runner allows another 3 seconds to stop and reap the child; a targeted check requiring SIGKILL took 11.506 seconds. The new test permits 12.5 seconds despite claiming a 10-second ceiling. Reserve cleanup within the total budget and test that ceiling.
NOTES: [accepted] The small shell hook, existing supervisor, and system curl fit the project; no new dependency is needed. Accepted the recorded suite pass without rerunning it. No files changed. — acceptable because a positive review remark confirming the approach fits the project; nothing to change

## Round 2 — VERDICT: APPROVE · re-review @ high
NOTES: none
Dispositions of round 1 findings: 2 FIXED, 0 DISPUTED

## Outcome
<!-- cross-review-merge-state: APPROVED -->
Approved after 2 rounds by Codex; eligible for merge pending GitHub confirmation. Owner-side steps remain after deploy: the deploy check (docs/verification.md) and the deliberate-stop test.
Done-when: met
