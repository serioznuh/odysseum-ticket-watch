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
