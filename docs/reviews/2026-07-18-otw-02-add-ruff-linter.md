# Add a linter (ruff) — OTW-02
Flow 2 (/codex-build) · builder gpt-5.6-sol/high · reviewer claude-opus-4.8/xhigh · 2026-07-18

## Task
Implement backlog item OTW-02: no linter was configured; AGENTS.md's verification
tier only had pytest. Add `ruff` to requirements.txt, a minimal ruff.toml config, a
`ruff check .` lint step in the CI workflow, and update AGENTS.md + docs/verification.md
to mention it. Done when `ruff check .` passes locally and in CI, and the docs mention it.

## Round 1 — VERDICT: APPROVE
Test gate: `ruff check .` — all checks passed; `.venv/bin/python -m pytest -q` — 44
passed. Reviewer verified: `ruff.toml`'s `target-version = "py39"` with the default
rule set (pyflakes + pycodestyle E4/E7/E9) is a sane minimal first-time config,
matching the project's Python 3.9 constraint; the CI step is correctly wired after
`pip install` and before pytest, so lint failures actually break the build; the
`ruff>=0.8` pin is reasonable; doc updates (AGENTS.md, docs/verification.md) are
accurate with no stale line counts, both within budget; BACKLOG.md's OTW-02 checkbox
correctly flipped to done.

NOTES:
- ruff.toml relies on the implicit default rule selection rather than an explicit
  `[lint] select = [...]`. Fine for a first pass; an explicit list would only matter
  if the team wants rule-set stability across future ruff upgrades — non-blocking.

## Outcome
Approved after 1 round, no fix rounds needed.
