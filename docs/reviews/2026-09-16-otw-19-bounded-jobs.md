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

# OTW-19 second continuation: 4 more Cinesa cleanup gaps found, capped pending owner decision
Flow 1 (/claude-build) · builder claude-opus-5/high · reviewer gpt-5.6-sol/xhigh · 2026-09-16
<!-- cross-review-loop-id: 4a635635-fafa-457d-bce8-e10336334ef7 -->

## Task
OTW-19 second continuation: 4 more Cinesa cleanup gaps found, capped pending owner decision

## Round 1 — VERDICT: REVISE
1. [P1] watcher/cdp.py:522 — cleanup returns `True` (clean teardown) when post-SIGTERM process discovery is unavailable, and also when PIDs survive the deadline — so `evaluate_on_page` never raises `ChromeLeakError` for these cases, and a Chrome that may still hold the profile lock produces no leak signal, no non-zero exit, and no `leak_since` stamp.
2. [P1] watcher/jobs.py:257 — leak tracking is conditional on the current exception being `ChromeLeakError`, while the fresh-profile check is additionally disabled when `detected=True` — an existing live leak followed by an ordinary Cinesa failure exits zero and stops its dedicated alert; cleanup uncertainty with an actually-free profile can also remain permanently stamped when required mints repeatedly fail.
3. [P1] watcher/cdp.py:563 — `_locked_profile_pid` collapses every `readlink` error or unparsable lock target to `None`, which `profile_lock_status` treats as demonstrably free — a permission/I/O failure while a real lock exists silently clears `leak_since`, violating the tri-state contract that unanswerable checks must return `None` (unknown), not `False` (free).
4. [P1] watcher/jobs.py:238 — alert loudness is derived from calendar-day distance rather than whether an alert was previously delivered — a leak detected shortly before midnight first becomes eligible after midnight with `day == 2`, so its first-ever alert is incorrectly silent (`WATCHER_STILL_BLIND`-shaped) instead of loud.
No fixes applied yet — the loop was capped here rather than sent to another fix round, pending owner review.
NOTES: the except BaseException/else cleanup-once structure, exception chaining, the unique `cinesa_leak:` key, its first-loud/repeat-silent config, the tri-state SIGTERM refusal on None, and the headed/offscreen/non-evasive Chrome design were all otherwise confirmed correct.

## Required verification
Not scheduled — no machinery/ paths changed in this diff.

## Outcome
<!-- cross-review-merge-state: CAPPED pr=21 -->
Capped after one round of this second continuation, deliberately WITHOUT sending a further fix round: the user pre-authorized up to 5 review rounds for the Cinesa-cleanup thread, all 5 were used in the prior continuation (loop_id 7661c956-...), and this fresh round (needed only because the prior round-5 review was blocked by a Codex outage, not because of new authorization) immediately surfaced 4 more real P1s in the same area — a pattern of each fix uncovering another gap one layer deeper (tri-state leak detection now interacts with SIGTERM-deadline handling, job-level streak/backoff coupling, filesystem-level lock parsing, and calendar-vs-delivery-history alert timing). Rather than keep spending rounds unilaterally past the authorized budget, this loop stops here for an owner decision: authorize a bounded number of further rounds to close these out, accept the current unfixed state and merge only the earlier (already-reviewed and fixed) commits, or take a different approach to this cleanup path's design.

# OTW-19: Codex rewrites Cinesa Chrome-cleanup, dual review (Claude+Codex) closes it
Flow 2 (/codex-build) · builder gpt-5.6-sol/high · reviewers Claude claude-opus-5/xhigh + Codex gpt-5.6-sol/xhigh · 2026-09-16
<!-- cross-review-loop-id: 88677a59-de90-4a48-bdbf-14b7d70e13b1 -->

## Task
OTW-19: Codex rewrites Cinesa Chrome-cleanup, dual review (Claude+Codex) closes it

## Round 1 — VERDICT: REVISE · reviewers Claude claude-opus-5/xhigh + Codex gpt-5.6-sol/xhigh
### Claude review — VERDICT: APPROVE
NOTES:
1. [fixed] stale `_mint_waits` docstring reference (function renamed to `_mint_plan`) — fixed in f3d63cc
2. [accepted] leak-alert day-numbering label has a cosmetic off-by-one near midnight; loudness itself is correct
3. [accepted] the ChromeLeakError branch's save_token rewrite drops mint_cooldown_until — pre-existing behavior, not introduced by this diff
4. [accepted] mint patience is now shorter than before by design (documented trade-off)
5. [accepted] a transient double `ps` failure can report a false-positive leak on an otherwise-successful mint — correctly biased trade-off (leak-suspected over silently-clean)
6. [accepted] architecture: watcher/cdp.py has grown to ~830 lines with many timeout constants and overlapping Chrome-liveness helpers — justified by real failures hit at 5-min cadence, but at the ceiling for a single-user hobby project; a NOTE, not a block

### Codex review — VERDICT: REVISE
FINDINGS:
1. [P1] watcher/jobs.py:304 — leak discovery still depended on `isinstance(error, ChromeLeakError)`; when no `leak_since` existed, `track_profile_leak` returned before inspecting the profile if that flag was false, so a locked profile alongside an ordinary Cinesa failure or a successful cached-token poll went unrecorded — FIXED in e2cfcbe
2. [P1] watcher/jobs.py:238 — the date-only `cinesa_leak:` key was not scoped to a leak episode, so same-day episodes could suppress each other and a first alert near midnight could get an unwanted daily repeat — FIXED in e2cfcbe
NOTES: overlaps Claude's round-1 NOTES above (stale docstring, day-numbering wording, pre-existing mint_cooldown_until drop); architecturally the jobs/budget split was appropriate, but leak truth was split across an exception subtype, a caller flag, and profile probing — addressed by round 2's unconditional reconciliation fix.

## Round 2 — VERDICT: APPROVE · reviewers Claude claude-opus-5/high (re-review) + Codex gpt-5.6-sol/high (re-review)
### Claude review — VERDICT: APPROVE
NOTES:
1. [assigned-id: OTW-24] watcher/jobs.py:295 — `track_profile_leak` runs after the try/except/else rather than in a finally; if outcome-building itself raised, the leak check for that run would be skipped (latent, needs another bug to trigger)
2. [accepted] leak-alert day-numbering cosmetic off-by-one persists (duplicate of round-1 note); loudness is correct
3. [assigned-id: OTW-24] .github/workflows/watch.yml's "Persist state" step lacks `if: always()`, so a now-legitimate non-zero exit (a detected leak) skips the state commit — matches prior behavior, not a regression, but worth hardening
4. [accepted] README's alert catalogue correctly left untouched — the leak alert reuses existing WATCHER_ERROR/WATCHER_STILL_BLIND kinds, needs no new silent_kinds entry
5. [accepted] `pathe.make_client()` in runner._run_source_jobs is still never closed — pre-existing, unchanged by this diff, outside scope

### Codex review — VERDICT: APPROVE
NOTES: none — both round-1 fixes confirmed complete, all four originally-open findings confirmed still closed, no other P0/P1 found.

A no-brainer fix (item 1 above) was applied after round 2's dual approval: commit f3d63cc, authoritative gate re-run and passed at that head, no new review round per protocol §8.1.

## Required verification
Not scheduled — no machinery/ paths changed in this diff.

## Outcome
<!-- cross-review-merge-state: APPROVED pr=21 -->
Done-when: met — fake-clock tests prove slow/failing polling cannot consume an entire reminder window, a newly discovered opening is evaluated in the same run, skipped/disabled jobs do not block reminders, default remind mode makes no source requests, cadence/grace tests pass, Python 3.9 works, no framework was introduced, and ruff/pytest/dry-run all pass (278 passed at the settled head, 279 including the notes-fix commit). Approved after 2 dual-review rounds (Claude + Codex, both required, both approved round 2) that closed all 4 outstanding Cinesa Chrome-cleanup findings via a Codex-authored refactor of the leak-detection reconciliation (unconditional profile check after every Cinesa outcome; an episode-scoped alert key) on top of the 9 prior rounds' narrower patches. Two minor hardening notes were promoted to a new backlog item, OTW-24, rather than blocking this merge. Eligible for merge pending GitHub confirmation.