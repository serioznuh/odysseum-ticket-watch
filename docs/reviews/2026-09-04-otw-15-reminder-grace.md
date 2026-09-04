# OTW-15: local half now owns the sale reminder ladder (cloud cron fires ~11% of scheduled times)
Flow 1 (/claude-build) · builder claude-opus-5/max · reviewers Claude claude-opus-5/xhigh + Codex gpt-5.6-sol/xhigh · 2026-09-04
<!-- cross-review-loop-id: 98a46eea-4c26-484f-807c-ebd1369ccf2c -->

## Task
OTW-15: local half now owns the sale reminder ladder (cloud cron fires ~11% of scheduled times)

## Round 1 — VERDICT: REVISE · reviewers Claude claude-opus-5/xhigh + Codex gpt-5.6-sol/xhigh
### Claude review — VERDICT: APPROVE
NOTES:
1. Residual two-writer path: `scripts/local-check.sh` pulls only after the watcher run, so a Mac waking from sleep can duplicate or rebase-conflict against a reminder the cloud sent during the sleep window (interacts with OTW-14). Suggested a pre-run pull.
2. Hour-flooring on the 2h rung is coarse ("in 1 hour" for 100 min left) — direction is safe, granularity could improve.
3. `test_grace_ignores_a_negative_value` is vacuous — passes with or without the `max(0.0, grace_minutes)` clamp.
4. Truncated sentence in `.github/workflows/watch.yml`'s header comment.
5. `AGENTS.md` still says the cloud half "owns" reminders without the failover nuance.

### Codex review — VERDICT: REVISE
FINDINGS:
1. [P1] watcher/__main__.py:599 — Grace is temporal separation, not mutual exclusion: `local-check.sh` runs the watcher before pulling remote state, so a resumed local run after sleep can see stale `reminders_sent` and duplicate a cloud-sent reminder.
2. [P1] watcher/state.py:258 — The shipped 25-minute grace makes the 15-minute rung mathematically unreachable from the cloud failover: eligibility `dt-15+25 = dt+10` is past the point `due_reminders` permanently switches to the open-ping branch.
## Round 2 — VERDICT: REVISE · reviewers Claude claude-opus-5/xhigh + Codex gpt-5.6-sol/high · re-review @ high (Codex)
### Claude review — VERDICT: APPROVE
NOTES: see round 3 NOTES for the residual items carried forward; round 2's own review confirmed the grace-window and pull-ordering fixes were correct as far as they went.

### Codex review — VERDICT: REVISE
FINDINGS:
1. [P1] watcher/state.py:236 — For the 15-minute rung, grace 25 is capped to 7.5 min (half-window), making cloud eligibility T-7.5; the local owner's worst-case guaranteed first firing is only by T. The cloud can beat or overlap the owner; the pre-run pull cannot prevent duplicates when the cloud has sent but not yet pushed.
2. [P1] watcher/__main__.py:605 — `now` is captured before the potentially lengthy Pathé/news network checks and reused for reminder selection/wording; a slow run can send a stale-timed or mis-selected reminder.
NOTES: pre-run pull correctly placed and equivalent to the existing recovery pattern; no adaptive-cadence regression found; the two new reachability tests would fail against round 1 but assert only reachability, not owner-first ordering; the formerly-vacuous negative-grace test and the workflow typo are genuinely fixed.

FIXED in c3fba52 — round 3 replaced the half-window cap with a floor at the local firing interval (`LOCAL_FIRING_INTERVAL_MINUTES`, so a rung no wider than the interval gets zero pre-opening cloud eligibility) and re-captured the clock (`ladder_now`) immediately before reminder computation/rendering.
## Round 3 — VERDICT: APPROVE · reviewers Claude claude-opus-5/high (light) + Codex gpt-5.6-sol/high · re-review @ high
### Claude review — VERDICT: APPROVE
NOTES:
1. The `open` ping's eligibility (`dt + grace`) is not floored the same way as the offset rungs; at the shipped grace (25) it stays safely after the owner's worst case (T+15), but a hypothetical grace below 15 would not. No invariant test sweeps this branch. Cheap to close by reusing the same floor.
2. Eligibility uses `>=`, so at exactly `window_open + interval` both halves are simultaneously eligible rather than strictly ordered — measure-zero in practice, covered by the pre-run pull.
3. `scripts/local-check.sh` now pulls while bash is still reading the script from disk — safe (git unlinks before rewriting), and the post-run pull already had this shape; noted only so it isn't mistaken for new exposure.

### Codex review — VERDICT: APPROVE
- Production offsets behave correctly: 1440 and 120 min retain the 25-min grace; the 15-min rung has no pre-opening cloud eligibility; the open ping becomes eligible at T+25.
- Synthetic tests cover varied offset sets including a 5-min rung and enforce owner-first ordering; production tests additionally enforce reachability.
- The plist test correctly reads `StartInterval=900`, converts to 15 minutes, compares against `LOCAL_FIRING_INTERVAL_MINUTES`.
- `ladder_now` drives both reminder selection and rendering; both clock-order-sensitive tests would fail under the former single-clock behavior.
- The concurrent-send-before-push race remains unchanged and is accurately documented as residual risk.
- Sandbox note: Codex's own read-only review sandbox could not write temp files for 4 state-I/O tests (its own environment restriction, not a code defect); the conductor's authoritative gate independently recorded 144/144 passing.

## Required verification
Not scheduled — no path in this change matches `required_verification_paths` (machinery/ prefix); this repository has no such surface.

## Outcome
<!-- cross-review-merge-state: APPROVED -->
Approved after 3 rounds. Combined round-3 verdict: APPROVE (Claude) + APPROVE (Codex). Eligible for merge pending GitHub confirmation.
Done-when: met
Every clause in docs/BACKLOG.md's OTW-15 entry is satisfied and independently verified by both reviewers across three rounds: the local half sends reminders on a cadence-skipped firing; grace suppresses then releases a failover reminder; no configured offset lets the failover act before the owner's worst-case first firing and none is left with zero cover; the 'open' ping's 6h cutoff stays anchored to the sale time regardless of grace; a reminder reports the time actually left; the local half pulls cloud state before deciding what to send.
