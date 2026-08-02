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
| OTW-05 | Confirm Cinesa token behaviour with screen locked/asleep | P2 | S | Infra, tooling & docs | [x] |
| OTW-06 | Pathé failure_streak churns state; baseline can lose an alert | P2 | S | Bugs | [x] |
| OTW-07 | Stale-check alert says "Pathé" but the whole local half is down | P3 | S | Bugs | [ ] |
| OTW-08 | Run the news half from the cloud pass to cover Mac-asleep windows | P1 | M | Features | [ ] |
| OTW-09 | Supervision is one-directional — nothing watches the cloud half | P2 | M | Features | [ ] |
| OTW-10 | Cinesa VPN 403 repeatedly launches headed Chrome | P1 | S | Bugs | [ ] |
| OTW-11 | Make Cinesa Chrome refresh normally imperceptible | P2 | S | UX & design | [ ] |

## 1. Critical — security & breakage

## 2. Bugs

### OTW-07 · Stale-check alert says "Pathé" but the whole local half is down
**Priority:** P3 · **Effort:** S
**Problem:** The dead-man's-switch alert in `watcher/__main__.py` (the
`is_check_stale` block) is titled "No successful Pathé check recently" and its
body says "new Pathé signals are NOT being watched". But it is keyed on
`last_check_ok`, which goes stale whenever the *local half* stops — and that
half runs Cinesa and the news feeds too. The most likely cause (Mac asleep,
lid shut) takes all three down, so naming only Pathé understates the outage and
points the user at the wrong subsystem. Found while sizing `stale_check_hours`
down from 72 h to 18 h.
**Fix:** Retitle to something like "Local checks have stopped" and list what is
actually dark (Pathé + news + Cinesa, conditional on `cfg.cinesa_enabled`),
keeping the "cloud reminders still run" reassurance. Key format must not
change — `stale:{last_check_ok}` is dedup memory (AGENTS.md "Conventions").
**Done when:** the alert body names every half that the local script owns, a
unit test asserts Cinesa is mentioned when enabled, and the `Finding.key`
format is byte-identical to today's.

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

### OTW-10 · Cinesa VPN 403 repeatedly launches headed Chrome
**Priority:** P1 · **Effort:** S
**Problem:** `watcher/cinesa.py::_get_json` treats both 401 and 403 as a rejected
token, and `fetch_snapshot` responds with `get_token(force=True)`. The forced
path bypasses the proactive-refresh backoff and valid-token fallback. With the
owner's VPN enabled on 2026-08-01/02, the data API returned 403 while the same
cached token worked again after the VPN was disabled; meanwhile every 15-min
firing launched headed Chrome, often leaving it alive for the full 60 s on
Cloudflare's `Attention Required!` page. This is noisy, cannot repair an
IP-level block, and the eventual alert misdiagnoses it as a Chrome/GUI problem.
**Fix:** Preserve the response status and distinguish an authentication failure
from a likely network/IP rejection. A 401 may force an immediate token mint. A
403 may try one forced mint, but a failed mint must record a cooldown in the
git-ignored credential cache while preserving the still-unexpired token; later
firings should retry the API but must not reopen Chrome inside that cooldown.
When the API accepts the cached token again, clear the incident naturally. Add
VPN/proxy guidance to the Cinesa error alert. Never put the token, cooldown, or
per-run timestamps in `state/state.json` or logs.
**Done when:** a test simulating repeated 15-min 403s plus a hard-blocked mint
launches Chrome at most once per cooldown window (at least 60 min), turning the
VPN off lets the original cached token recover without another mint, a 401 still
forces renewal, and the three-failure alert says to disable VPN/proxy and wait
for the automatic retry.

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

### OTW-08 · Run the news half from the cloud pass to cover Mac-asleep windows
**Priority:** P1 · **Effort:** M
**Problem:** The structural hole behind OTW-05: while the Mac sleeps (lid shut,
on battery, away for a day) the local half runs nothing, and the cloud pass is
remind-only — so a sale announcement landing overnight is not seen until the
lid opens. Pathé genuinely cannot move to the cloud (Akamai 403s datacenter
IPs), but the **news feeds can**: `watcher/news.py` only reads Google News RSS,
which is not IP-gated, and the measured worst ordinary blind window is ~13 h
overnight — long enough to miss an announcement outright.
**Fix:** Give the cloud pass a news-capable mode (e.g. `--mode remind
--with-news`, or a `news` mode) that runs the news half and its NEWS_LEAD /
sale-detection findings but skips Pathé and Cinesa entirely, and wire it into
`.github/workflows/watch.yml`. News matching must stay strict (AGENTS.md
"Conventions") — this widens *when* it runs, never *what* it matches. Watch for
double-sending: the local half runs the same feeds, so dedup must be shared
through `state/state.json`, which both halves already commit.
**Done when:** a scheduled cloud run raises a news finding with the Mac off,
the same finding is not re-sent by the next local run, the cloud pass still
never touches `www.pathe.fr`, and `--mode remind` without the flag behaves
exactly as today.

### OTW-09 · Supervision is one-directional — nothing watches the cloud half
**Priority:** P2 · **Effort:** M
**Problem:** If the *local* half dies, the cloud pass says so (`is_check_stale`
in `watcher/__main__.py`, `alerts.stale_check_hours`, now 18 h). The reverse has
no cover: if the *cloud* half stops — Actions disabled, `TELEGRAM_*` secrets
rotated, workflow error, GitHub disabling the cron after 60 days of repo
inactivity — the reminder pings **and** the stale alert both vanish silently,
and nothing on the local side notices. The 7-day `heartbeat_days` is the only
positive liveness signal, and it is sent by the local half, so it keeps arriving
happily while the cloud is dead. Worst case is losing the countdown reminders
around the sale opening, which is the one moment the whole project exists for.
**Fix:** Have the cloud pass record its own liveness and the local pass alert on
it — the mirror of the existing stale check. Two hazards shape the design:
- **State churn.** A `last_cloud_run` timestamp written every 15 min would make
  the cloud commit and push ~96 times a day — the exact trap AGENTS.md calls out
  for the `cinesa` key. Bucket it (floor to the hour, or the day) so the value
  changes at most ~24 times daily, or keep it out of `state/state.json` and read
  the last successful run from the GitHub Actions API instead (the repo is
  public, so unauthenticated works).
- **Commit timestamps are not a substitute.** The cloud only commits state on
  real change, so quiet periods produce no cloud commits at all — the last 12
  state commits are all `local check`. Absence of a commit proves nothing.
Alert should reuse `WATCHER_ERROR` (buzzes by default) with a fresh key prefix,
and stay quiet while the cloud is merely idle rather than dead.
**Done when:** disabling the workflow (or pointing it at a bad token) produces
one loud alert from the local half within a bounded window, the fix adds no more
than ~24 state writes/day, and a normal week of both halves running raises
nothing. Note the irreducible limit: if both halves die, only the absence of the
7-day heartbeat is left — worth saying plainly in the README rather than solving.

## 4. UX & design

### OTW-11 · Make Cinesa Chrome refresh normally imperceptible
**Priority:** P2 · **Effort:** S
**Problem:** A headed browser is irreducible on this laptop: Chrome activates
itself even under `open -g -j`, so `watcher/cdp.py` restores the previously
frontmost app after opening the Cinesa tab. That works in the measured happy
path, but restoration is absent when startup or `/json/new` fails, restoration
errors are intentionally swallowed, Chrome termination is not confirmed, and a
definitive `Attention Required!` hard block can keep the hidden browser alive
for the full 60 s. Therefore the honest local target is *normally
imperceptible*, not 100% guaranteed invisible.
**Fix:** Keep the real headed, offscreen, throwaway-profile design. Restore the
captured app again from the outer `finally` after cleanup so every post-launch
exit path gets a best-effort hand-back; make a definitive Cloudflare hard-block
title fail fast while still allowing the normal `Just a moment…` challenge time
to settle; and verify watcher-profile Chrome processes exit with a short bounded
wait and warning. Add small mocked tests for launch arguments, early/late failure
focus restoration, and profile-scoped cleanup—do not add Selenium/Playwright,
stealth, CAPTCHA solving, headless mode, idle detection, or user-profile access.
Update README/current-state wording to promise only normally imperceptible
operation and state that absolute zero laptop impact requires a separate
always-on home machine.
**Done when:** successful refresh still yields a token, every simulated failure
after Chrome launch attempts final focus restoration and watcher-only cleanup, a
hard-block page exits within 10 s rather than 60 s, the user's own Chrome cannot
match the cleanup target, tests cover the lifecycle contract, and the owner docs
describe the realistic visibility boundary without claiming a 100% guarantee.

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
