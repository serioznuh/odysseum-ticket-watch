# OTW-19: split orchestration into bounded jobs, blocked at round 3 on exhausted Codex review quota
Flow 1 (/claude-build) · builder claude-opus-5/high · reviewer gpt-5.6-sol/xhigh · 2026-09-16
<!-- cross-review-loop-id: bb44d75b-84b0-4829-b4fb-3e412d198793 -->

## Task
OTW-19: split orchestration into bounded jobs, blocked at round 3 on exhausted Codex review quota

## Round 1 — VERDICT: REVISE
1. [P1] watcher/cinesa.py:265 — Cinesa token minting ignored the remaining job budget on cache-miss and forced-refresh paths, risking a mint that overruns the 60s Cinesa job budget and 240s polling budget — FIXED in 5b052d2
2. [P1] watcher/jobs.py:141 — healthy Pathé bookkeeping was mutated before analyze_pathe completed, so a crash in analysis after that mutation would permanently lose a pending recovery alert — FIXED in 5b052d2
NOTES: recorded test gate was not rerun by the reviewer; local-check.sh's nonzero-exit handling and a docs/current-state.md wording gap were flagged as non-blocking.

## Round 2 — VERDICT: REVISE · re-review @ high
1. [P1] watcher/cdp.py:385 — the round-1 fix shortened the intended wait durations but did not bound the underlying blocking calls (each WebSocket call, the Chrome launch step, and cleanup each kept their own fixed timeout independent of the remaining budget), so a stalled mint could still overrun both the Cinesa job budget and the aggregate polling budget — FIXED in 0bfd122
NOTES: the round-1 Pathé fix was confirmed correct — analysis completes before recovery/health bookkeeping mutate state.

## Round 3 — BLOCKED (no reviewer verdict obtained)
The round-3 Codex review dispatch failed twice against head 0bfd122: the initial attempt (gpt-5.6-sol/high) and a retry after the protocol's ~2-minute backoff both returned the provider error "You've hit your usage limit ... try again at 1:59 PM". Per the rate-limit substitution policy the same review was then dispatched on gpt-5.6-terra/high, which failed identically — the outage is an account-wide Codex usage-quota exhaustion, not specific to one model. No reviewer verdict was obtained for round 3, so the loop stops here rather than assuming approval. The code itself was never rejected by a completed round-3 review; both prior rounds' findings were addressed and round 2 confirmed the earlier fix.

## Required verification
Not scheduled — no machinery/ paths changed in this diff.

## Outcome
<!-- cross-review-merge-state: CAPPED pr=TBD -->
Stopped at cap after round 3: two completed review rounds found and got fixes for 3 real P1 bugs (Cinesa mint budget enforcement, twice, and a Pathé bookkeeping-before-analysis ordering bug), and the round-3 re-review of the final fix could not be completed because Codex's account-wide usage quota was exhausted, surviving both an immediate retry and a model substitution to gpt-5.6-terra. Needs a human call: retry the round-3 review once Codex quota resets (the provider message cited ~1:59 PM), or inspect the diff directly and merge manually if satisfied.