# Improve Pathé 403 diagnostics for VPN failures
Flow 4 (/claude-review) · fixer gpt-5.6-sol/high · reviewer claude-opus-4.8/xhigh · 2026-07-18

## Task
Review PR #3 (branch `codex/improve-403-diagnostics`): condense httpx's multiline
status error into one alert line, identify HTTP 403 as network/IP rejection with
"disable VPN/proxy" guidance, point troubleshooting at the local launchd log,
document the behavior, and add regression tests for 403 and generic failures.
No changes to retry cadence, alert thresholds, scheduler config, or state semantics.

## Round 1 — VERDICT: APPROVE
Test gate: 44 passed. No findings. Reviewer verified the summarizer against a real
httpx 0.28.1 `HTTPStatusError` (returns `('HTTP 403 Forbidden from
https://www.pathe.fr/api/shows', 403)`), Python 3.9 annotation compatibility
(`from __future__ import annotations`), and that the `error:` dedup key format is
unchanged (no re-send risk). The `state/state.json` hunk is a routine runtime-state
commit that rode along on the branch.

NOTES:
- 403 guidance (VPN hint + launchd log path) is local-specific; a manual `check`
  dispatch on GitHub Actions would misattribute the datacenter-IP block to a VPN.
  Rare owner-only path, gated by the failure streak — filed as OTW-03.
- The status-line regex assumes no single quote in reason phrase/URL; standard
  HTTP reason phrases have none.

## Outcome
Approved after 1 round, no fix rounds needed. One substantive note filed as
backlog item OTW-03; PR #3 merged.
