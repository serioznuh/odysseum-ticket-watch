# OTW-18: state validation/migration, fail-closed recovery instead of silent reset
Flow 2 (/codex-build) · builder gpt-5.6-sol/high · reviewer claude-opus-5/xhigh · 2026-09-15
<!-- cross-review-loop-id: 15939ab0-d74c-4b45-9a77-c7105a3dc0de -->

## Task
OTW-18: state validation/migration, fail-closed recovery instead of silent reset

## Round 1 — VERDICT: APPROVE
Architecture: right-sized, stdlib-only, numbered schema with explicit migrations and validation at both read and write boundaries; fail-closed with a separate bootstrap path is the correct trade for a single-user system whose worst outcome is replayed notifications.
NOTES:
1. watcher/__main__.py: a StateError from the post-delivery save_state call is uncaught and exits with a traceback, so that run's delivered alerts could be re-sent next firing; no evidence Pathe emits the malformed timestamp that could trigger it. [assigned-id: OTW-23]
2. scripts/local-check.sh and docs/BACKLOG.md's OTW-14 problem statement still described the old load_state behavior (rename-and-start-fresh) instead of the new fail-closed exit. [fixed] in 309af1b
3. docs/current-state.md was not updated for the new fail-closed / --bootstrap-state runtime shape. [fixed] in 309af1b
4. README.md shrank slightly while adding the recovery section, trimming some unrelated prose to stay in budget. [accepted]
5. watcher/state.py caught a bare ValueError around migrate_state, which could mask an unrelated internal bug as "state file is invalid". [fixed] in 309af1b
6. --test-telegram now requires a loadable state file since load_state runs before that branch; harmless since state is committed in both clones. [accepted]

## Outcome
<!-- cross-review-merge-state: APPROVED -->
Done-when: met

Fixtures cover unreadable JSON, wrong nested types, unsupported versions, older supported schemas, missing production state and explicit first use (--bootstrap-state); invalid state causes no Telegram sends, no empty-state overwrite and no dry-run mutation; migrations are lossless and idempotent (verified directly against the live production state.json); ruff and pytest pass. Approved after round 1 plus one no-new-round quick-fix pass (309af1b) addressing NOTES 2, 3 and 5. Eligible for merge pending GitHub confirmation.