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
| OTW-02 | Add a linter (ruff) | P2 | S | Infra, tooling & docs | [x] |
| OTW-03 | 403 alert VPN wording wrong on manual CI dispatch | P3 | S | Bugs | [ ] |
| OTW-04 | Cinesa alert: include session times + booking link | P2 | S | Features | [ ] |
| OTW-05 | Confirm Cinesa token behaviour with screen locked/asleep | P2 | S | Infra, tooling & docs | [ ] |
| OTW-06 | Pathé failure_streak churns state; baseline can lose an alert | P2 | S | Bugs | [x] |

## 1. Critical — security & breakage

## 2. Bugs

### OTW-03 · 403 alert VPN wording wrong on manual CI dispatch
**Priority:** P3 · **Effort:** S
**Problem:** The 403 error alert (`watcher/__main__.py`, `summarize_pathe_error` /
ERROR alert body) says "Disable any VPN or proxy" and points at
`~/.ticket-watch/logs/launchd.log`. Correct for the local Mac, but a manually
dispatched `check` run on GitHub Actions also gets 403 (datacenter IP, per
AGENTS.md) — there the VPN advice misattributes the cause and the log path
doesn't exist. Rare, owner-only path; the failure-streak gate makes it unlikely
to ever fire from a one-off dispatch (review note, PR #3).
**Fix:** Detect CI (e.g. `GITHUB_ACTIONS` env) and swap the hint text to
"GitHub datacenter IPs are blocked by Pathé — run the check locally", dropping
the launchd log pointer.
**Done when:** the ERROR alert body differs between local and CI contexts, with
a unit test covering both.

### OTW-06 · Pathé failure_streak churns state; baseline can lose an alert
**Priority:** P2 · **Effort:** S
**Problem:** Found while fixing the same two bugs on the newer Cinesa half
(PR #5 review). `watcher/__main__.py`'s top-level `st["failure_streak"]`
(Pathé) increments unconditionally on every failed check with no cap, so a
prolonged Pathé outage rewrites `state/state.json` — and triggers a
`local-check.sh` commit+push — on every 15-min firing, same as the Cinesa bug
fixed in PR #5. Separately, `state_mod.update_from_snapshot` runs
unconditionally after the Telegram send loop regardless of whether any given
finding's send succeeded, so a failed send for a one-shot alert (e.g.
`NEW_LISTING`) can have its underlying state already advanced before delivery
is confirmed, and never retry. Both predate PR #5; not fixed there since it
only touched the Cinesa half.
**Fix:** Mirror PR #5's fixes: cap `failure_streak` at
`cfg.failure_streak_threshold` (nothing reads a larger value); gate
`update_from_snapshot`'s alert-affecting fields on confirmed delivery the same
way `update_from_cinesa`'s new `advance_imax` parameter does, for whichever
Pathé finding kinds are genuinely one-shot dedup-keyed (not the sale-date/
sessions fields that are meant to always reflect current truth).
**Done when:** a simulated multi-firing Pathé outage leaves state byte-identical
after the cap, and a failed send for a one-shot Pathé alert kind retries on the
next run instead of being silently dropped — both with regression tests
mirroring `tests/test_cinesa.py`'s equivalents.

## 3. Features

### OTW-04 · Cinesa alert: include session times + booking link
**Priority:** P2 · **Effort:** S
**Problem:** The 🎫 "watched date opened in IMAX" alert
(`detect.analyze_cinesa`) says the date is bookable and links to the film page,
but not *which* IMAX sessions exist or their times — for a popular film the
user still has to hunt for the session and seats. `film-screening-dates` only
carries date + attribute ids, which is why v1 stops there.
**Fix:** On a firing target-date finding only (rare, so the extra call is
cheap), fetch `ocapi/v1/showtimes/by-business-date/{date}` from
`cfg.cinesa_api_base`, filter to the site/film and the IMAX attribute, and add
the session times plus a direct booking URL to the alert lines.
**Done when:** the 🎫 alert lists IMAX session times for the date, with a unit
test over a captured `by-business-date` payload, and a Cinesa failure on that
extra call still leaves the base alert intact.

## 4. UX & design

## 5. Infra, tooling & docs

### OTW-05 · Confirm Cinesa token behaviour with the screen locked / asleep
**Priority:** P2 · **Effort:** S
**Problem:** The token step (`watcher/cdp.py`) needs a real Chrome window and so
an active GUI session. The *resilience* half of this item is done: the token now
refreshes `cinesa.token_refresh_before_hours` (3 h) before expiry and falls back
to the cached token when a refresh fails, so a blocked attempt no longer blinds
the Cinesa half (`watcher/cinesa.py::get_token`, tests in `tests/test_cinesa.py`).
What is still unverified is the underlying question: **does Chrome actually
launch and clear Cloudflare while the screen is locked?** Testing it means
locking the owner's Mac, so it was not done unprompted.
**Fix:** With the owner's agreement, lock the screen and run
`.venv/bin/python -c "from watcher import cinesa; from watcher.config import
load_config; print(len(cinesa.get_token(load_config('config.toml'), force=True)))"`
via a delayed shell, then read the result on unlock. Asleep needs no test —
launchd does not fire at all, and the firing coalesces on wake.
**Done when:** the locked-screen result is recorded in docs/current-state.md,
and if it fails there, the ⚠️ guidance text names "unlock the Mac" explicitly.

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
