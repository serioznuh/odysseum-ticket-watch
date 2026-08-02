# OTW-11: finally-block focus restore, hard-block fast-fail, Chrome exit verify, mocked tests
Flow 2 (/codex-build) · builder gpt-5.6-luna/max · reviewer claude-opus-5/xhigh · 2026-08-02
<!-- cross-review-loop-id: 1edd7a93-5419-43c7-9979-60fe1d09d984 -->

## Task
OTW-11: finally-block focus restore, hard-block fast-fail, Chrome exit verify, mocked tests
## Round 1 — VERDICT: REVISE
FINDINGS:
1. [P1] watcher/cdp.py:384 — hard-block fast-fail uses `== "Attention Required!"` but real Cloudflare page title is `"Attention Required! | Cloudflare"` (confirmed in launchd.log); equality never matches, branch is dead in production, headed Chrome still runs for full 60s. Fix: use `startswith(HARD_BLOCK_TITLE)` and update test fixture to assert on the real observed title.
NOTES:
1. cdp.py:397-403 — long-path final `_restore_focus` fires ~60s after capture; acceptable given the item asks for restoration on every post-launch path.
2. cdp.py:225,249 — exact-argv match assumes profile path has no spaces; true for default `.cache/chrome-profile` but worth a comment.
3. cdp.py:307 — narrow suppress tuple could let `struct.error` escape `_page_title`; harmless in practice.
4. docs/current-state.md:55 — will be accurate once the comparison is fixed.
