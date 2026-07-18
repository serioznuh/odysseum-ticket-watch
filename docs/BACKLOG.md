# odysseum-ticket-watch — Backlog

**How to use:** every item has a stable ID (`OTW-nn`). In a new session, say
*"implement OTW-01 from backlog"* — each item is self-contained (problem, fix
sketch, file paths, done-when). IDs are never renumbered or reused; new items get the
next free number in whichever section fits. Completion is tracked **only** in the Done
column of the index table below.

Priorities: **P0** broken/urgent · **P1** high value · **P2** nice to have · **P3** someday.
Effort: S (≤ half day) · M (a day-ish) · L (multi-day).

## Index (sorted by priority)

| ID | Title | Priority | Effort | Section | Done |
|----|-------|----------|--------|---------|------|
| OTW-01 | Docs-contract test in CI | P2 | S | Infra, tooling & docs | [ ] |
| OTW-02 | Add a linter (ruff) | P2 | S | Infra, tooling & docs | [ ] |

## 1. Critical — security & breakage

## 2. Bugs

## 3. Features

## 4. UX & design

## 5. Infra, tooling & docs

### OTW-01 · Docs-contract test in CI
**Priority:** P2 · **Effort:** S
**Problem:** The docs standard (AGENTS.md "Documentation maintenance") defines line
budgets and required sections, but nothing enforces them — docs can silently drift.
**Fix:** Add `tests/test_docs_contract.py` (pytest, runs in the existing
`.github/workflows/tests.yml`): fail on missing required sections, broken local
markdown links, docs over budget (AGENTS.md 180 · README.md 220 ·
docs/current-state.md 180 · topic docs 150 · docs/history.md 80 · history archives
260), and dates/changelog phrasing leaking into current-state.md.
**Done when:** the test passes on the current tree, and deliberately breaking a link
or exceeding a budget makes `python -m pytest -q` fail.

### OTW-02 · Add a linter (ruff)
**Priority:** P2 · **Effort:** S
**Problem:** No linter is configured; AGENTS.md's verification tier only has pytest.
**Fix:** Add `ruff` to requirements (or a dev-requirements file), a minimal
`ruff.toml`/`pyproject.toml` config, a lint step in `.github/workflows/tests.yml`,
and update AGENTS.md + docs/verification.md commands.
**Done when:** `ruff check .` passes locally and in CI, and the docs mention it.
