# Current state

<!-- Snapshot of the system TODAY. Never a changelog — history lives in docs/history/. -->

A single-user Telegram watcher covering **two independent targets**:

1. *Dune : Troisième partie* ticket-sale opening at Pathé Odysseum
   (Montpellier, IMAX 70 mm) — reads Pathé's public JSON API (which publishes
   `salesOpeningDatetime` in advance) plus Google News RSS, and sends
   deduplicated alerts: sale-date announcements, new listings, bookable-now, a
   24 h / 2 h / 15 min reminder ladder, strictly-filtered news leads, and
   supervision alerts (failure streak, stale state, weekly heartbeat). Every
   message names its film and cinema on the first line, and findings that are
   one piece of news share one message rather than arriving as a burst.
2. *La odisea* (Nolan) **in IMAX at Cinesa Diagonal Mar, Barcelona** — watches
   the booking calendar for specific wanted dates and for IMAX leaving or
   returning. The film is already showing; what is watched is the schedule
   being extended past its current wall.

## Runtime shape

- **Local half** — launchd agent `com.odysseum.ticket-watch` in the
  `~/.ticket-watch` clone fires `scripts/local-check.sh` every 15 min. An
  adaptive-cadence guard decides if a full Pathé + news check is due (≈4 h
  baseline, tightening to every firing around the announced opening). The
  **Cinesa check runs on every firing** — one small call, not bot-gated. Runs
  from a residential IP: Akamai blocks Pathé from datacenter IPs, and
  Cloudflare challenges Cinesa from them.
- **Cloud half** — `.github/workflows/watch.yml` cron `*/15`: reminder ladder +
  supervision only, reading shared state; the scheduled pass never calls Pathé
  (a manual `check` dispatch would, but is 403'd from datacenter IPs). It never
  calls Cinesa either.
- **Shared state** — `state/state.json`, committed to `main` by both halves
  (`[skip ci]`); serves as dedup memory and reminder bookkeeping. The Cinesa
  half writes only on real change, so the 15-min cadence causes no commit churn.
  Failure streaks stop changing at their alert threshold, and every Pathé alert
  baseline — listings, formats and `sales` — advances only after the alert it
  gates was delivered, so one failed send cannot retire an announcement.
  `last_error` records the failure cause for the cloud pass and is rewritten
  only when the text changes, so a steady outage still writes state once.
- **Pathé failure model** — only the catalogue calls (`/shows`,
  `/cinema/…/shows`) are the health signal and can fail the check. Every
  per-listing call (detail and showtimes) is best-effort, so one listing can
  never blind the watch — that was the 2026-09-02 outage. The showtimes
  endpoint serves only `isMovie: true` listings and refuses every *event*
  listing with `403 "No movie allowed !"`; this is permanent, not a "not yet"
  (measured: an event listing bookable at Odysseum today still 403s), so the
  70 mm listings are watched through their cinema-programme `isBookable`
  entry, and no `refCmd` deep link is available for them. The refusal is
  matched on that message — the observed Akamai block is *also* JSON
  (`{"error":"Error from IP …"}`) — and is reported as an origin refusal
  rather than an IP block. A persistent per-listing failure is still reported
  as healthy (OTW-13).
- **Deploying needs no state change** — `local-check.sh` pulls on every firing.
  It used to pull only when it had a state commit to push, which deadlocked:
  a blind run writes identical state, so nothing was pushed and nothing pulled,
  and the fix for an outage could not reach the clone that needed it.
- **Code** — Python package `watcher/` (`pathe.py` and `cinesa.py` API clients,
  `cdp.py` browser token step, `news.py`, `detect.py`, `state.py`, `notify.py`
  Telegram, `config.py`, `__main__.py` CLI); config in `config.toml`; tests in
  `tests/` (127 passing).

## Cinesa specifics

- Two hosts: `www.cinesa.es` is behind a **Cloudflare managed challenge** and
  only mints the 12 h API token; `vwc.cinesa.es/WSVistaWebClient` serves the
  actual data to plain `httpx` and is not bot-protected.
- The token step drives a **real headed Chrome** (offscreen, throwaway profile,
  ~3 s). `--headless=new` is challenged and never settles — measured, not
  assumed. No stealth or challenge-solving is used or wanted: if Chrome stops
  clearing the challenge on its own, the watcher must fail loudly instead.
- Chrome **self-activates on launch** even under `open -g -j`, so `cdp.py`
  captures the frontmost app and hands focus back after the tab is created and
  again after profile-scoped cleanup (doing it any earlier just lets Chrome
  take focus again). Refreshes are normally imperceptible, but a headed
  browser has no absolute invisibility guarantee.
- A definitive Cloudflare `Attention Required!` title fails fast; the normal
  `Just a moment…` challenge is allowed to use the regular poll window. Cleanup
  signals only the watcher profile and warns if Chrome termination is not
  confirmed. Absolute zero laptop impact requires a separate always-on home
  machine.
- **A locked screen does not block the token step** — measured on the owner's
  Mac: eight mints over 13 min while locked (one with the display on, seven
  with it asleep, on AC) each returned a fresh ~12 h token in 2.3 s, against a
  2.5 s unlocked control. Neither the lock nor display sleep throttles the
  challenge. What does block it is losing the GUI session: system sleep, or a
  login window. Sleep is self-correcting — the LaunchAgent does not fire while
  asleep and the missed firing coalesces on wake. The Mac has one internal
  display and so no clamshell mode, meaning a closed lid is simply sleep.
- The token is refreshed **3 h before expiry**, not at it, and a failed refresh
  falls back to the token still in hand, so one blocked attempt cannot take the
  half down — it has ~12 firings to succeed, backed off to 30 min apart so a
  long outage does not mean a Chrome launch every 15 min. A data-API **403**
  is treated as a likely network/IP rejection: the watcher tries one forced mint,
  then keeps the still-valid token and records a one-hour cooldown in the
  git-ignored credential cache if minting fails. During that window it retries
  the API without reopening Chrome; a successful response clears the cooldown.
  A token that is actually dead (or 401-rejected) still forces renewal and fails
  loudly rather than going quiet.
- Verified IDs: film `HO00003228`, site `032` (Diagonal Mar), IMAX showtime
  attribute `0000000086`.
- **The booking wall is fixed, not rolling** — observed 2026-07-29→08-25
  (28 days) then 2026-07-30→08-25 (27 days): the trailing edge advances while
  the far edge stays put, so dates open in batches. This is why watching named
  dates is meaningful rather than an alert that fires every day.

## User workflow

- Passive: alerts arrive on Telegram; quiet kinds (news leads, heartbeat,
  recovery) are silent, time-critical ones buzz.
- Manual runs from a clone: `source .env && .venv/bin/python -m watcher
  --mode check [--dry-run]`; `--test-telegram` for a smoke test.
- Deploying = pushing to `main`: the `~/.ticket-watch` clone pulls on its next
  firing, healthy or not; Actions picks it up on the next cron tick.

## Boundaries

- Secrets (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`) are env-only: git-ignored
  `.env` locally, repo secrets in Actions.
- `logs/`, `.env` and `.cache/` (Cinesa token + Chrome profile) are local-only;
  `state/state.json` is the one runtime artifact that is committed and shared.
  The Cinesa token is a credential and must never be committed.
- The repo is public (Actions billing: a private repo at `*/15` would exceed
  the free tier).
