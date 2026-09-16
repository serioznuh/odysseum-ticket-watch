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
<!-- cross-review-merge-state: CAPPED pr=21 -->
Stopped at cap after round 3: two completed review rounds found and got fixes for 3 real P1 bugs (Cinesa mint budget enforcement, twice, and a Pathé bookkeeping-before-analysis ordering bug), and the round-3 re-review of the final fix could not be completed because Codex's account-wide usage quota was exhausted, surviving both an immediate retry and a model substitution to gpt-5.6-terra. Needs a human call: retry the round-3 review once Codex quota resets (the provider message cited ~1:59 PM), or inspect the diff directly and merge manually if satisfied.

# OTW-19 continuation: Cinesa Chrome-cleanup hardening, blocked again on exhausted Codex quota
Flow 1 (/claude-build) · builder claude-opus-5/high · reviewer gpt-5.6-sol/xhigh · 2026-09-16
<!-- cross-review-loop-id: 7661c956-4438-444e-93fd-4c1766c37b82 -->

## Task
OTW-19 continuation: Cinesa Chrome-cleanup hardening, blocked again on exhausted Codex quota

## Round 1 — VERDICT: REVISE
1. [P1] watcher/cdp.py:195 — budget-clamped RPC deadline did not re-arm the socket's read timeout, so a read could still block for up to the original ceiling with little budget left — FIXED in b4ba96a
2. [P1] watcher/cdp.py:329 — a stalled/timed-out `ps` process lookup was read as "nothing to kill", silently skipping SIGTERM and risking a leaked throwaway Chrome profile lock — FIXED in b4ba96a
NOTES: real headed Chrome / offscreen / throwaway-profile design and no prohibited evasion mechanisms confirmed intact.

## Round 2 — VERDICT: REVISE · re-review @ high
1. [P1] watcher/cdp.py:393 — when both PID-discovery attempts failed, `_terminate_by_profile` still just logged and returned without ever attempting SIGTERM — FIXED in 0f6043b
NOTES: socket-read fix confirmed complete for handshake, frame reassembly and unbudgeted calls; review log noted as stale (documentation only, not a code bug).

## Round 3 — VERDICT: REVISE · re-review @ high
1. [P1] watcher/cinesa.py:319 — the proactive-refresh fallback caught the new cleanup-integrity error broadly and returned the cached token, silently absorbing a real Chrome-leak condition into an ordinary retry case — FIXED in ef334fd
NOTES: profile-lock discovery and PID verification confirmed sound; headed/offscreen/non-evasive design intact.

## Round 4 — VERDICT: REVISE · re-review @ high
1. [P1] watcher/cdp.py:698 — the cleanup verdict was only checked after the outer try/finally, so a simultaneous page-evaluation failure masked a concurrent cleanup-integrity failure — FIXED in 9e17ed8
2. [P1] watcher/cinesa.py:325 — a proactive-refresh leak's backoff let the condition hide forever behind a working cached token, since the ordinary failure-streak counter reset to zero on each successful cached-token run — FIXED in 9e17ed8
NOTES: exception ordering at all three Cinesa call paths, runner-to-CLI exit propagation, and the unchanged `cinesa_error:` dedup key confirmed correct.

## Round 5 — BLOCKED (no reviewer verdict obtained)
The round-5 Codex review dispatch failed three times against head 9e17ed8: the initial attempt, a retry after the protocol's ~2-minute backoff, and a substitution to gpt-5.6-terra/high all returned the provider error "You've hit your usage limit ... try again at 7:04 PM". This is the second Codex account-wide usage-quota exhaustion on this same backlog item (the first blocked the prior loop section at what was then round 3; quota reset once already, allowing rounds 1–4 of this continuation to complete). No reviewer verdict was obtained for round 5, so the loop stops here again rather than assuming approval. Every finding from rounds 1–4 was addressed; round 5's diff (9e17ed8) has not yet been independently reviewed.

## Required verification
Not scheduled — no machinery/ paths changed in this diff.

## Outcome
<!-- cross-review-merge-state: CAPPED pr=21 -->
Stopped at cap after round 5 of this continuation (round 8 overall on this backlog item): four completed rounds found and got fixes for 6 real P1 bugs, all in the Cinesa Chrome-mint cleanup/budget-enforcement chain (socket-timeout re-arming, SIGTERM-skip on stalled process lookup, profile-lock-based termination with PID verification, cleanup-integrity signal loss on the failing path, and leak-detection starvation by the cached-token fallback). The user pre-authorized up to 5 review rounds for this continuation; all 5 were used, with round 5 blocked purely on exhausted Codex quota rather than a code defect. Needs a human call: retry the round-5 review once Codex quota resets (provider message cited ~7:04 PM), authorize further rounds if another finding turns up, or inspect the diff directly and merge manually if satisfied.