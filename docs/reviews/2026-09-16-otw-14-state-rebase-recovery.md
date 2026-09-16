# OTW-14 state-rebase recovery: round 1 built+reviewed (REVISE, 2 P1s); round 2 blocked by a Codex usage-limit outage.
Flow 2 (/codex-build) · builder gpt-5.6-sol/high · reviewers Claude claude-opus-5/xhigh + Codex gpt-5.6-sol/xhigh · 2026-09-16
<!-- cross-review-loop-id: 7a0d5b4a-5f55-4220-99f9-a90ef06d0c50 -->

## Task
OTW-14 state-rebase recovery: round 1 built+reviewed (REVISE, 2 P1s); round 2 blocked by a Codex usage-limit outage.

## Round 1 — VERDICT: REVISE · reviewers Claude claude-opus-5/xhigh + Codex gpt-5.6-sol/xhigh
### Claude review — VERDICT: APPROVE
NOTES:
1. scripts/local-check.sh — a recovery failure at the pre-run pull exits the firing under set -e before the watcher runs, so the reminder ladder/Cinesa/Pathe checks are skipped that firing; not reachable in the shipped topology today (needs a manual cloud check dispatch racing an unpushed local commit), and the cloud reminder failover plus stale-check supervision still cover the user.
2. watcher/state_merge.py — an unreachable-guard bug: the earlier equality branch returns a copied sentinel that defeats the later identity check when both sides drop the same optional field (e.g. last_error) while another field forces recursion; fails closed (StateError, exit 2, rebase aborted, no corruption) but should be fixed.
3. Doc cleanup dropped the pinned test-count anchor from AGENTS.md/verification.md/current-state.md with no replacement.

### Codex review — VERDICT: REVISE
FINDINGS:
1. [P1] scripts/local-check.sh:55 — a merge failure only logs to launchd's log and exits; no durable, user-facing alert names the state-rebase recovery failure, so OTW-14's explicit failure-path acceptance criterion ("fails in a way that names itself in an alert") is unmet.
2. [P1] watcher/state_merge.py:192 — sale_target is picked by delivery timestamp rather than derived from the merged sales data, so a same-pass divergence (upstream records an earlier opening, local a later one) can make the merged sale_target skip the earlier reminder ladder.
NOTES: shell/Python split is a sound first OTW-21 increment; the same _MISSING identity bug Claude flagged is reproducible.

## Round 2 — BLOCKED (not started)
Three Codex dispatch attempts (resume x2, fresh x1) to fix round 1's findings all failed before producing model output. Diagnosis: `codex exec` returned `ERROR: You've hit your usage limit ... try again at Sep 17th, 2026 12:20 AM.` This is an external Codex-provider quota outage, not a defect in the round-1 diff or the dispatch payloads.

## Outcome
<!-- cross-review-merge-state: CAPPED -->
Stopped at cap — needs a human call on: resume this loop (or re-run round 2) once the Codex usage limit resets (~2026-09-17 00:20), to fix Codex's two P1 findings (missing failure-path alert; sale_target reconciliation across a merged state) plus the shared _MISSING identity-check note, before this can be approved and merged. Round-1 diff (commit d925205620699ec12961fa8ee8f1a309b506ae5a) is on branch loop/otw-14-20260916, with a passing authoritative test-gate run; not yet reviewer-approved end to end.
