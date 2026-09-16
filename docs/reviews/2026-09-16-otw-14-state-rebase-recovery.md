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
# OTW-14 state-rebase recovery: approved after fixing a sale_target regression found in re-review; all NOTES triaged.
Flow 2 (/codex-build) · builder gpt-5.6-sol/high · reviewers Claude claude-opus-5/xhigh + Codex gpt-5.6-sol/xhigh · 2026-09-17
<!-- cross-review-loop-id: 21db3617-5ee7-4793-807e-88cd5ddd98a0 -->

## Task
OTW-14 state-rebase recovery: approved after fixing a sale_target regression found in re-review; all NOTES triaged.

## Round 1 — VERDICT: REVISE · reviewers Claude claude-opus-5/xhigh + Codex gpt-5.6-sol/xhigh · re-review @ high
Built on the prior loop's commit d925205, fixing its two outstanding P1 findings (missing failure-path alert; naive sale_target reconciliation) and the shared _MISSING identity-check note. Committed at e738305.
### Claude review — VERDICT: REVISE
FINDINGS:
1. [P1] watcher/state_merge.py:207-212 — `_merge_sale_target` filtered candidates to `> now`, so a state-rebase recovery in the 0-6h window after a sale opened set `sale_target` to `None` even when both sides agreed on it — the "should be open NOW" reminder was then never sent, and nothing later restores a past target.
The failure-alert path and the _MISSING identity fix were confirmed correct and properly integrated with existing dedup/alert-kind conventions.
NOTES:
1. `watcher/jobs.py`'s `# ---- source jobs` banner appears twice (the new job is supervision, not a source). — [fixed]
2. `runner.py`'s docstring folds local-sync supervision into the polling-budget sentence, which is misleading. — [fixed]
3. The `state_sync_error:` receipt lives in the unpushed local commit; discarding those commits to resolve the wedge would repeat the alert once. — [accepted]
4. The pinned `269 passed` test-count anchor removed from AGENTS.md/verification.md/current-state.md has no replacement. — [accepted]
### Codex review — VERDICT: REVISE
FINDINGS:
1. [P1] watcher/state_merge.py:207 — same regression, independently reproduced: `_merge_sale_target` filters out targets at or before now, losing the unsent "open now" ping with no later recovery path.
The other two fixes (failure alert, _MISSING) were confirmed sound and correctly integrated.
NOTES: none

## Round 2 — VERDICT: APPROVE · reviewers Claude claude-opus-5/xhigh + Codex gpt-5.6-sol/high · re-review @ light/high
Fixed the sale_target regression: a lone agreed-or-carried-forward candidate is now retained (even if past) as long as it remains in the merged sales data, matching `update_from_snapshot`'s own invariant; multiple divergent elapsed candidates still fail closed. Committed at 7b81a75.
### Claude review — VERDICT: APPROVE
Verified the fix against the exact regression scenario (target survives, `due_reminders` fires the open-now ping, 6h cutoff intact, no duplicate ping) and against the two guard cases (no resurrection of a genuinely superseded target; ambiguous elapsed candidates still raise).
NOTES:
1. A pre-existing (not new) edge case where a deselected-then-still-carried target could ping once before self-correcting within one firing. — [accepted]
2. An unreachable slug-removal branch in `_merge_sales`. — [accepted]
3. The still-open doc-anchor note from round 1. — [accepted]
### Codex review — VERDICT: APPROVE
Independently confirmed the same three properties (survival, non-resurrection, fail-closed ambiguity) directly against the code.
NOTES:
1. The doc-anchor note remains non-blocking. — [accepted]

## NOTES triage
1. scripts/local-check.sh pre-run `pull_with_state_recovery` failure propagating under `set -e` — [fixed] in e738305: the pre-run call is now `|| true` (post-run call stays strict) and a failure instead records a durable local marker that surfaces as a loud, deduplicated `WATCHER_ERROR` once state sync succeeds.
2. watcher/state_merge.py `_MISSING` identity-check bug — [fixed] in e738305: the both-`_MISSING` guard now runs before the equality branches, so the sentinel is never deep-copied into the merged result.
3. Pinned `269 passed` test-count anchor removed from AGENTS.md/docs/verification.md/docs/current-state.md with no replacement — [accepted]: de-brittling docs that would otherwise drift on every test addition; no functional risk.
4. `watcher/jobs.py` duplicate `# ---- source jobs` banner above the new supervision job — [fixed] in 94a80b3 (re-labelled `# ---- supervision jobs`).
5. `watcher/runner.py` docstring implying local-sync supervision runs under the polling aggregate time budget — [fixed] in 94a80b3 (clarified: no network, no budget).
6. An unpushed `state_sync_error:` receipt could repeat its alert once if a human resolves the wedge by discarding local commits — [accepted]: documented, bounded (one repeat), and the scenario this branch exists to recover from in the first place.
7. A deselected-then-still-carried-forward sale target could ping "open now" once before self-correcting within one firing — [accepted]: pre-existing shape of the union rule (identical for future targets before this loop), needs a config edit inside a 6h window, self-corrects automatically.
8. Unreachable slug-removal branch in `_merge_sales` (upstream removing a slug keeps the local value) — [accepted]: dead code path given `update_from_snapshot` only ever adds/overwrites `sales`; harmless if it were ever reached.
9. `tests/test_state_merge.py`'s shell-boundary test uses a fake `git` shim rather than a real conflicting rebase, so the actual `git rebase`/`--continue`/`--skip`/`--abort` sequence is unverified by the automated suite — [assigned-id: OTW-25].

## Outcome
<!-- cross-review-merge-state: APPROVED -->
Approved after 2 rounds (continuing a prior capped loop's round 1); eligible for merge. Final head eef750a82da9e8150c3defd418638a510f70912f, authoritative test gate: pass (pytest, 2107ms). All review findings resolved; all NOTES triaged (4 fixed, 4 accepted, 1 assigned-id: OTW-25).
A state-rebase conflict now resolves itself within one firing via a domain-aware merge (preserving alerts/reminders_sent/their baselines) without a human, and an unresolvable conflict fails loudly through a durable local marker that reaches a deduplicated user-facing WATCHER_ERROR alert naming the state-sync recovery failure, rather than only launchd's log.
Done-when: met
