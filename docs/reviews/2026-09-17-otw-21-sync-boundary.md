# OTW-21 sync boundary: approved after fixing a transient-failure alerting regression found in round 1; all NOTES triaged.
Flow 2 (/codex-build) · builder gpt-5.6-sol/high · reviewers Claude claude-opus-5/xhigh + Codex gpt-5.6-sol/xhigh · 2026-09-17
<!-- cross-review-loop-id: e84a9a58-32f2-47f9-afe6-3fe5d6f398cc -->

## Task
OTW-21 sync boundary: approved after fixing a transient-failure alerting regression found in round 1; all NOTES triaged.

## Round 1 — VERDICT: REVISE · reviewers Claude claude-opus-5/xhigh + Codex gpt-5.6-sol/xhigh
Added a dedicated `refs/heads/runtime-state` git ref, transported via raw git plumbing (hash-object/mktree/commit-tree/push, never checked out) with compare-and-swap push semantics; moved the live state file to a git-ignored `.cache/state-sync/state.json` with tracked `state/state.json` now only an initial seed; reused OTW-14's `merge_states` and schema validation; added overlap locking (flock) to `scripts/local-check.sh`; reordered `local-check.sh` to fast-forward and activate code BEFORE touching state sync; updated `.github/workflows/watch.yml` to the same boundary; added `tests/test_sync_integration.py` with real-git (not mocked) integration coverage of two temporary clones sharing one origin. Committed at f2b561a.
### Claude review — VERDICT: REVISE
FINDINGS:
1. [P1] watcher/state_sync.py:374 (with 98-100, scripts/local-check.sh:52) — every `synchronize()` failure, including a single transient network error with no retry/backoff, wrote the durable failure marker and fired a loud, unwithdrawable `WATCHER_ERROR` alert — a Mac waking from sleep or a GitHub blip would reliably buzz the user, unlike this codebase's existing consecutive-failure gating pattern for transient failures elsewhere.
Architecture (git-plumbing ref transport, compare-and-swap push, seed-vs-live migration, overlap lock, code-first deployment ordering, and OTW-14 receipt preservation) was confirmed sound; the six done-when integration-test scenarios were confirmed genuinely covered with real git subprocesses.
NOTES:
1. `.github/workflows/watch.yml`'s "Synchronize runtime state (before)" step had no `continue-on-error`, so a sync failure there skipped the watcher step entirely, losing that pass's reminder failover. — [fixed]
2. The overlap lock has no lifetime bound; a wedged local run holds it indefinitely, silently disabling the local half until cloud grace/stale supervision notices. — [accepted]
3. `_remote_state` fetches the runtime-state ref with no depth bound; it grows ~288 commits/day, and a fresh GitHub Actions checkout re-fetches the whole history every run. — [assigned-id: OTW-26]
4. The tracked `state/state.json` seed is now frozen; if the runtime-state ref is ever deleted, a fresh checkout would reseed from a stale file and could re-send historical alerts — comparable blast radius to the old design. — [accepted]
5. The first firing after this merges runs against a not-yet-created `.cache/state-sync/state.json`, which self-heals on the next firing. — [accepted]
6. `docs/verification.md:63` still named the obsolete live-state path. — [fixed]
### Codex review — VERDICT: REVISE
FINDINGS:
1. [P1] watcher/state_sync.py:373 — same regression, independently reproduced: all transport, schema, and merge failures immediately created the same durable failure marker, with no retry delay or consecutive-failure gate for transient Git failures.
The same architecture pieces were confirmed sound independently; the same NOTES items Claude listed above were independently confirmed, with no new ones raised.
NOTES: none

## Round 2 — VERDICT: APPROVE · reviewers Claude claude-opus-5/xhigh + Codex gpt-5.6-sol/high · re-review @ light/high
Fixed the alerting-precision regression: added a `StateSyncTransportError` subclass raised only at the three network boundaries (ls-remote, fetch, push-exhaustion), gated behind a consecutive-failure streak (threshold 3, mirroring the existing `failure_streak_threshold` pattern) before marking/alerting; integrity failures (merge conflict, schema rejection, git-plumbing failure, OSError) still mark and alert immediately on first occurrence. Committed at 213bbf7.
### Claude review — VERDICT: APPROVE
Verified the transient/integrity classification, the streak gating and reset-on-success, and that new tests prove both halves (N-1 transient failures produce nothing, the Nth marks; one integrity failure marks immediately); confirmed no regression to the round-1-approved architecture.
NOTES:
1. The cloud runner has no persistent cache, so the transport streak there is always 1 (not a regression from round 1, but effectively local-only gating). — [accepted]
2. The streak counts sync invocations, and `local-check.sh` syncs twice per firing, so an outage now alerts after ~5-10 min rather than ~15 — still spans more than one firing. — [accepted]
3. A stale comment in `scripts/local-check.sh`'s pre-run sync block described only the old (always-immediate) marking behavior. — [fixed]
4. `resolve_failure`'s require-delivery-first semantics mean a marker written during an outage still produces one loud alert after recovery — this is round-1/OTW-14 semantics, not new here. — [accepted]
### Codex review — VERDICT: APPROVE
Independently confirmed the same classification, gating, and test coverage, and that the real-git integration tests still cover receipt merging, rejected-push recovery, overlap locking, code-first deployment, and code/state-history separation.
NOTES:
1. The cloud runner's ephemeral cache makes transport streak gating effectively local-only; local-check.sh counts both pre- and post-run sync attempts. — [accepted]

## NOTES triage
1. Both round-1 reviewers' P1 (transient network failures alerting immediately, no consecutive-failure gate) — [fixed] in 213bbf7: a `StateSyncTransportError` subclass is gated behind a 3-failure streak mirroring the existing `failure_streak_threshold` pattern; integrity failures still mark immediately.
2. `.github/workflows/watch.yml` missing `continue-on-error` on the pre-run sync step — [fixed] in 6796950.
3. `docs/verification.md:63` naming the obsolete live-state path — [fixed] in 6796950.
4. Stale comment in `scripts/local-check.sh` describing only the old always-immediate marking behavior — [fixed] in 6796950.
5. Overlap lock has no lifetime bound (a hung run wedges later firings until it dies) — [accepted]: `flock` auto-releases on process death; only a live-but-hung run is affected, and existing cloud grace/stale-check supervision bounds the exposure.
6. Runtime-state git ref has unbounded history growth (~288 commits/day, no fetch depth bound) — [assigned-id: OTW-26].
7. A deleted runtime-state ref would fall back to the frozen tracked seed, risking resurrected historical alerts — [accepted]: documented residual risk, comparable blast radius to the pre-OTW-21 design.
8. First firing after this merges hits a not-yet-created live-state file — [accepted]: self-heals on the next firing.
9. Cloud runner's ephemeral cache makes the transport streak gating effectively local-only — [accepted]: not a regression (the marker was equally ephemeral pre-fix), and the cloud pass's own stale/supervision alerts still cover a persistent cloud-side outage.
10. Transport streak counts sync invocations (twice per local firing) rather than firings, so alerting after ~5-10 min instead of ~15 — [accepted]: still spans more than one firing, matching the fix's intent.
11. `resolve_failure`'s require-delivery-first semantics guarantee one loud alert after any marked outage recovers — [accepted]: pre-existing OTW-14 semantics, unchanged by this item.

## Outcome
<!-- cross-review-merge-state: APPROVED -->
Approved after 2 rounds; eligible for merge. Final head b471c75298e4a76642b5b1d830307b366802f176, authoritative test gate: pass (pytest, 6179ms). All review findings resolved; all NOTES triaged (4 fixed, 6 accepted, 1 assigned-id: OTW-26). One prior backlog item (OTW-25) was marked superseded, since this change replaced the rebase-based mechanism it targeted.
A state-rebase/state-sync conflict or failure can no longer revert, block, or delay a code update reaching this clone (code fast-forwards on `main` independently of the runtime-state ref); no confirmed delivery receipt is lost (OTW-14's merge semantics are reused unchanged); overlapping local invocations are locked; a lightweight schema/version check gates every state input; and failed pushes retry with compare-and-swap semantics without losing the receipts just produced. Six real-git integration scenarios (conflicting receipts, an owner/health-field update, error recovery, a failed push and its recovery, overlapping local invocations, and a code update landing while state sync is broken) are exercised in `tests/test_sync_integration.py`.
Done-when: met
