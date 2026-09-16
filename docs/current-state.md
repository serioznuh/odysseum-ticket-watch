# Current state

<!-- Snapshot of the system TODAY. Never a changelog — history lives in docs/history/. -->

A single-user Telegram watcher covering **two independent targets**:

1. *Dune : Troisième partie* ticket-sale opening at Pathé Odysseum
   (Montpellier, IMAX 70 mm) — reads Pathé's public JSON API (which publishes
   `salesOpeningDatetime` in advance) plus Google News RSS, and sends
   deduplicated alerts: sale-date announcements, new listings, bookable-now, a
   24 h / 2 h / 15 min reminder ladder, strictly-filtered news leads, and
   supervision alerts (failure streak, stale state, weekly heartbeat).
   Configured for **IMAX 70mm on December 19–20, 2026**: each date alerts only
   on day-level bookability or an available session in that format. December 15,
   standard, 4DX and ordinary 70mm sessions cannot trigger a wanted-date alert.
   Generic book-now messages are replaced by these date alerts. Every
   message names its film and cinema on the first line, and findings that are
   one piece of news share one message rather than arriving as a burst.
2. *La odisea* (Nolan) **in IMAX at Cinesa Diagonal Mar, Barcelona** — watches
   the booking calendar for specific wanted dates and for IMAX leaving or
   returning. The film is already showing; what is watched is the schedule
   being extended past its current wall.

## Runtime shape

- **One pass = an ordered sequence of bounded jobs** (`watcher/runner.py` over
  `watcher/jobs.py`). Due reminders are checked **before** any request and
  again after fresh observations land; `reminders_sent` is the dedup record, so
  no rung goes out twice and a run that overruns its window still delivers the
  warning on time *and* the opening ping. Each polling job has an aggregate
  budget covering retries, feed loops and the token refresh (Pathé 120 s, news
  45 s, Cinesa 60 s; 240 s for all polling, inside one launchd firing
  interval), enforced at every blocking call, down to Chrome's launch and each
  CDP round trip; teardown has its own allowance, falls back to the profile's
  own lock when `ps` cannot find Chrome, and fails the mint rather than leave a
  locked profile behind. A crashing job exits non-zero but never costs the pass
  its reminders, supervision or state save, and banks nothing: Pathé analysis
  precedes the health bookkeeping it would strand.
- **Local half** — launchd agent `com.odysseum.ticket-watch` in the
  `~/.ticket-watch` clone fires `scripts/local-check.sh` every 5 min. An
  adaptive-cadence guard decides if a full Pathé + news check is due (≈4 h
  baseline, tightening to every firing around the announced opening). Pending
  future wanted dates override this: check every existing 5-min firing until
  their alerts are delivered or the dates pass. No sub-minute guarantee; sleep
  still pauses checks. It gates
  neither the **Cinesa check** — one small call, not bot-gated — nor the
  **reminder ladder**, both of which run on every firing. This half *owns* the
  ladder: 5-min firings give three chances inside a 15-min warning. Runs from a
  residential IP: Akamai blocks Pathé from datacenter IPs, and Cloudflare
  challenges Cinesa from them.
- **Cloud half** — `.github/workflows/watch.yml` cron `*/15`: supervision, plus
  reminders as a **failover** rather than as their owner. It passes
  `--reminder-grace-minutes 25` (> the local 5-min interval), so it only sends
  a reminder the Mac demonstrably missed; that wait is floored at the local
  firing interval, so the failover can never reach a rung before the owner's
  worst-case first firing. The cloud grace exceeds the 15-min warning window,
  so that rung still belongs to the Mac alone; a sleeping Mac is covered by the
  opening-time ping instead. Two writers
  stay off `reminders_sent` because of that ordering *and* because
  `local-check.sh` pulls before it runs — a Mac waking from sleep sees
  what the cloud sent before deciding. Measured to 2026-09-03 this cron fired
  10.9% of its schedule (median gap 58 min, max 11.5 h), which is why the
  ladder is no longer cloud-owned (OTW-15). The scheduled pass never calls
  Pathé (a manual `check` dispatch would, but is 403'd from datacenter IPs).
  It never calls Cinesa either.
- **Format-specific reminders** — standard tickets no longer cancel the IMAX
  ladder. Existing `formats_seen` provides the format evidence without manual
  state edits. The opening-time message says availability is unconfirmed and
  links to the dedicated event page. Existing dedup keys remain unchanged;
  wanted-date keys are independent of prior format announcements. Programme
  logs now record bookable dates for future availability investigations.
- **Heartbeat status** — reports current booking evidence for each wanted date
  in the selected format, using the same rules as the date alerts. Other dates
  and formats cannot confirm those bookings. Only future national sale openings
  from live selected listings appear; old or withdrawn dates retained in dedup
  state do not. The heartbeat links to the selected format's event page and
  explicitly names any detail/showtimes calls that are currently degraded.
- **Shared state** — `state/state.json`, committed to `main` by both halves
  (`[skip ci]`); serves as dedup memory and reminder bookkeeping. The Cinesa
  half writes only on real change, so the 5-min cadence causes no commit churn.
  State is schema-validated before detection or delivery: invalid, unreadable,
  or unsupported-version state makes `--mode check` exit non-zero with an
  actionable diagnostic and leaves the file unchanged. A genuinely new
  installation creates empty state explicitly with `--bootstrap-state`.
  Failure streaks stop changing at their alert threshold, and every Pathé alert
  baseline — listings, formats and `sales` — advances only after the alert it
  gates was delivered, so one failed send cannot retire an announcement.
  `last_error` records the failure cause for the cloud pass and is rewritten
  only when the text changes. `last_check_ok` means fully healthy;
  `last_catalogue_ok` is a separate, hourly-throttled local-liveness pulse that
  also advances on partial snapshots. Thus a steady degradation does not write
  every 5 min, and the cloud can still tell a live degraded Mac from a dark one.
- **Pathé failure model** — catalogue failures still blind the check, while
  every best-effort detail/showtimes result explicitly distinguishes data,
  authoritative emptiness, expected refusal and unexpected failure. One bad
  listing cannot discard the rest of the snapshot, but an unexpected failure
  no longer refreshes `last_check_ok`: it enters the existing capped failure
  streak, appears by name in the heartbeat, and raises one degraded-watch alert
  after the supervision threshold. An unchanged condition stays quiet, while a
  later catalogue-wide failure re-arms supervision and raises its own loud
  blind alert. If the local liveness pulse subsequently goes stale, cloud
  supervision reports the whole local half dark, never merely degraded.
  It also cannot turn a programme-wide `isBookable` bit into guessed format
  evidence, so degradation cannot invent a ticket alert or baseline.
  The showtimes endpoint serves only `isMovie: true` listings and refuses every
  *event* listing with `403 "No movie allowed !"`; this is permanent, not a
  "not yet" (measured: a bookable event still 403s), so the 70 mm listings use
  cinema-programme `isBookable`, without a `refCmd` deep link. That exact
  refusal is expected healthy state; the observed JSON Akamai block is not.
- **Deploying needs no state change** — `local-check.sh` pulls on every firing.
  It used to pull only when it had a state commit to push, which deadlocked:
  a blind run writes identical state, so nothing was pushed and nothing pulled,
  and the fix for an outage could not reach the clone that needed it.
- **Code** — Python package `watcher/`: `pathe.py`/`cinesa.py` API clients,
  `cdp.py` token step, `news.py`, `detect.py`, `state.py`, `notify.py`,
  `coalesce.py`, `alerts.py`, `budget.py`/`jobs.py`/`runner.py` orchestration,
  `config.py`, thin `__main__.py` CLI; `config.toml`; `tests/` (259 passing).

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
  half down — it has many firings to succeed, backed off to 30 min apart so a
  long outage does not mean a Chrome launch every 5 min. A data-API **403**
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
