# Close the survivor-report gap after a watchdog timeout and simplify the OTW-29 supervision code without behavior change.
Flow 1 (/claude-build) · builder claude-opus-5.5/high · reviewer gpt-6-sol/xhigh · 2026-09-24
<!-- cross-review-loop-id: 3ecd1b7f-d684-4616-a4df-76068a918827 -->

## Task
Close the survivor-report gap after a watchdog timeout and simplify the OTW-29 supervision code without behavior change.
## Round 1 — VERDICT: APPROVE
Codex approved with no findings; the post-approval Claude pass (loop-reviewer, xhigh) also approved.
FINDINGS: none
NOTES:
1. Codex: moving the supervision helpers out of state_sync.py would ease navigation. [accepted] Non-blocking; a module split is a separate change and OTW-28 will show which guarantees need to stay together.
2. Claude: a few-millisecond unguarded window exists between confirming a stop and deferring signals, reachable only if Git survives SIGKILL and a stop signal lands in the same instant. [accepted] Hypothetical double fault; the next firing is still blocked by any recorded marker.
3. Claude: in the watchdog path a recorded nested marker yields exit 4 instead of 5. [accepted] The marker still blocks the next firing; only the launchd log is less precise.
4. Claude: locked's own signal handler can overwrite a nested named record with the shell group's record. [accepted] Requires the shell group itself to survive SIGKILL; hypothetical.
5. Claude: an OSError inside the on_signal callback would turn death-by-signal into exit 1. [accepted] Still a non-zero exit that the wrapper treats as failure; no lock is released early.
6. Claude: EPERM on Linux may report a zombie-only group as a survivor. [accepted] Only reachable in CI where .cache is not persisted, so a false survivor is harmless.
7. Claude: Git children run in their own session, so manual terminal runs cannot prompt for credentials. [accepted] Deliberate; launchd runs never had a terminal.
8. Claude: the operator procedure for clearing a survivor record lives in stderr messages and current-state.md only, and the new current-state bullet is one long line. [accepted] The message names the exact file to clear; README wording can follow with OTW-28's operator notes.
## Outcome
<!-- cross-review-merge-state: APPROVED -->
Approved after 1 round by Codex, plus a post-approval Claude pass; eligible for merge pending GitHub confirmation.
Done-when: met

# Claude review of the OTW-29 Git and run-lifetime bounds
Flow 4 (/claude-review) · fixer gpt-6-sol/high · reviewer claude-opus-5.5/xhigh · 2026-09-24
<!-- cross-review-loop-id: 56b97c64-1ee0-4ade-b9f6-ff2bede0b6a7 -->

## Task
Review PR 30 (OTW-29: bound Git operations and the local run's lifetime) for real bugs.
## Round 1 — VERDICT: APPROVE
Claude (loop-reviewer, xhigh) approved with no findings, including the architecture pass; the authoritative gate passed on the reviewed head.
FINDINGS: none
NOTES:
1. A recorded survivor's pgid reused later by an unrelated group keeps blocking until that group exits. [accepted] Needs a prior double fault; a blocked firing is the safe failure and the record names the file to clear.
2. A 600 s deadline SIGTERM during a Cinesa pass leaves the `open`-started Chrome behind. [accepted] The Cinesa half is currently disabled; revisit before re-enabling it.
3. `locked` now polls through `communicate(timeout=600)` for the whole firing. [accepted] Negligible energy cost next to the watcher pass itself.
4. A nested level returning 6 for a group that already exited still reports 'needs a human'. [accepted] Only the log wording is less precise; the block itself is correct.
5. The current-state bullet is one long line and README has no operator steps for clearing a survivor record. [accepted] The stderr messages name the exact file; README wording can follow with OTW-28's operator notes.
6. Moving the supervision helpers out of `watcher/state_sync.py` would ease review. [accepted] A module split is a separate change; OTW-28 will show which guarantees must stay together.
## Outcome
<!-- cross-review-merge-state: APPROVED pr=30 -->
Approved after 1 round by Claude with no findings; eligible for merge pending GitHub confirmation.
Done-when: met
