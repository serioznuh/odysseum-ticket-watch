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
  45 s, Cinesa 60 s; 240 s for all polling, under one launchd firing interval),
  enforced at every blocking call, down to Chrome's launch and each CDP call; teardown has its own
  allowance and falls back to the profile's own lock when `ps` cannot find Chrome. A Chrome that may
  still hold that lock exits the run non-zero and no token fallback absorbs it, as does a crashing
  job — neither ever costs the pass its reminders, supervision or state save, and Pathé analysis
  precedes the bookkeeping a crash would strand. The final bookkeeping save is the same kind of contained failure: an invalid field or a filesystem error reports its cause, exits non-zero and leaves the last validated file untouched, because confirmed receipts were already durable and an uncertain attempt is never replayed on its own.
- **Local half** — launchd agent `com.odysseum.ticket-watch` in the
  `~/.ticket-watch` clone fires `scripts/local-check.sh` every 5 min. An
  adaptive-cadence guard decides if a full Pathé + news check is due (≈4 h
  baseline, tightening to every firing around the announced opening). Pending
  future wanted dates override this: check every existing 5-min firing until
  their alerts are delivered or the dates pass. No sub-minute guarantee; sleep
  still pauses checks. It gates neither the **Cinesa check** — one small call,
  not bot-gated — nor the **reminder ladder**, both of which run on every
  firing. This half *owns* the ladder: 5-min firings give three chances inside a 15-min warning.
  Before replay/heartbeat it validates a bounded public Actions result: any success proves health, uncertainty stays quiet, and a proven 18-hour absence alerts once until positive recovery.
  Runs from a residential IP: Akamai blocks Pathé from datacenter IPs, and Cloudflare challenges Cinesa from them.
- **Cloud half** — `.github/workflows/watch.yml` cron `*/15`: cloud-safe news,
  supervision, and reminders as a **failover** rather than their owner. It passes
  `--reminder-grace-minutes 25` (> the local 5-min interval), so it only sends
  a reminder the Mac demonstrably missed; that wait is floored at the local
  firing interval, so the failover can never reach a rung before the owner's
  worst-case first firing. The cloud grace exceeds the 15-min warning window,
  so that rung still belongs to the Mac alone; a sleeping Mac is covered by the
  opening-time ping instead. Two writers stay off `reminders_sent` because of
  that ordering *and* because `local-check.sh` pulls before it runs — a Mac
  waking from sleep sees what the cloud sent before deciding. Measured to
  2026-09-03 this cron fired 10.9% of its schedule (median gap 58 min, max 11.5
  h), which is why the ladder is no longer cloud-owned (OTW-15). Scheduled
  `remind --with-news` passes read Google News plus opted-in `cloud_extra_pages`;
  they never call Pathé/Cinesa. Every run validates its bot/chat read-only after failover work, so a failed probe cannot cost a reminder and success proves credentials.
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
  state do not. It links to the selected format's event page and names degraded
  listing calls, but is withheld unless current cloud health is also proven.
- **Shared state boundary** — live JSON is `.cache/state-sync/state.json`; Git transports it on `refs/heads/runtime-state`, separate from `main`. Both halves call `watcher/state_sync.py` before and after a pass; Git plumbing commits the state ref without checking it out, so state never dirties the code worktree. Reconciliation unions receipts/baselines; conflicts fail closed and pushes retain receipts. Every Git invocation in a sync waits a bounded time; a timeout stops that Git process tree and is the existing transport failure — the capped streak, no new alert path — never a confirmed absence, and the last validated live/base copy plus any quarantined attempt survive it untouched.
  Creating that ref is never ordinary work. A confirmed absence is reported before anything local is touched, so live/base evidence survives it, and it always makes `sync` exit 3: both startup wrappers stop there and nothing is delivered until an operator acts. Local receipts do not buy an exception — a receipt that lived only in the ref, such as a reminder the cloud sent while the Mac slept, is invisible here — and the tracked `state/state.json` is only a first-run seed, never proof of what was sent. An established install (evidence in live *or* base) also leaves the durable marker, so the first pass after recovery raises one loud alert. A transport failure stays distinct and still continues on the last validated copy. `state_sync init` creates the ref once for a genuinely new install and adopts an independently created one instead of replacing it; owner-run `recover` unions every surviving store's receipts and keeps in-flight attempts quarantined as uncertain, folding in whatever the ref holds when it writes — a host that recreated it meanwhile may be the only record of an uncertain attempt — without rewriting that history (procedure in README).
- **Delivery boundary** — every alert, reminder and heartbeat is saved to an
  outbox before Telegram is called. Confirmation stores member keys and Telegram's message ID.
  Definite failures remain pending; a failed pre-send claim save rolls back to pending. A
  post-send timeout is `uncertain` and is not replayed automatically. Current observations
  stay independent: an uncertain sale alert cannot freeze its opening or reminder ladder.
  Only complete, positive contradictory evidence retires a member. Cloud passes defer
  cloud-health work; local proof retires recovered outage alerts and stale heartbeats.
  The next opening owns the ladder; booking retires old pings while recent openings keep war-room cadence.
  Overlapping local/cloud hosts can still send the same news finding: claims are not distributed locks.
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
  Payload trust is separate from request health: a published timestamp the watcher cannot read — malformed, or without a UTC offset — is dropped at the fetch boundary and only that field becomes unknown for that listing, so it reaches no alert, baseline or state file, the gap it leaves can retire neither a live opening nor its reminders, and everything the listing still reports correctly keeps its full weight.
- **Deployment is independent** — under the local process lock,
  `local-check.sh` fast-forwards `main` and re-execs the deployed script before
  its pre-run state sync. A corrupt state ref or rejected state push can fail and
  alert, but cannot block, rebase or roll back that code update.
- **Nothing in a local firing waits forever** — the deployment pull runs under the same stdlib boundary the syncs use (no platform `timeout` binary); a pull that never answers is a failed deployment, so the firing continues on the installed code. `locked` supervises the whole firing under an overall deadline of two launchd intervals plus a cleanup allowance, well clear of a healthy pass. On that deadline, or when launchd or Ctrl-C stops the supervisor, it stops and reaps only this run's own process tree — each child leads its own session, so a wedged `git-remote-https` or `ssh` goes too — and only then releases the lock, exiting 4. Each supervisor in the chain hands a stop it receives down to the tree it owns — a state sync forwards it to the Git child it started in a session of its own, inside the allowance the level above gives it — so no Git process outlives the firing that started it. The lock file is never deleted to break a lock, so the next firing cannot race a live writer, and a hung tree costs the ladder two firings instead of every later one. A tree that outlives repeated SIGKILL rounds is never *presumed* gone — an advisory lock dies with its holder, so the firing records that group next to the lock and exits 5, the next firing stops on that record rather than starting a second writer, and the record clears itself once the group is really gone (with one durable marker, so the owner hears about a machine in that state).
- **Code** — Python package `watcher/`: `pathe.py`/`cinesa.py` API clients,
  `cdp.py` token step, `news.py`, `detect.py`, `state.py`/`state_merge.py`/`state_sync.py`, `notify.py`,
  `coalesce.py`, `alerts.py`, `budget.py`/`jobs.py`/`runner.py` orchestration,
  `config.py`, thin `__main__.py` CLI; `config.toml`; pytest suite in `tests/`.

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
  long outage does not mean a Chrome launch every 5 min. A data-API **403** is
  treated as a likely network/IP rejection: the watcher tries one forced mint,
  then keeps the still-valid token and records a one-hour cooldown in the
  git-ignored credential cache if minting fails. During it the API is retried
  without reopening Chrome, and a good response clears the cooldown. A token
  that is actually dead (or 401-rejected) still forces renewal and fails loudly
  rather than going quiet. A mint that cannot confirm Chrome exited is the one
  failure no fallback absorbs: after every Cinesa outcome `cinesa.leak_since` is
  re-tested, clears only on proof the profile is free, and uses episode-scoped alerts.
- Verified IDs: film `HO00003228`, site `032` (Diagonal Mar), IMAX showtime
  attribute `0000000086`.
- **The booking wall is fixed, not rolling** — observed 2026-07-29→08-25
  (28 days) then 2026-07-30→08-25 (27 days): the trailing edge advances while
  the far edge stays put, so dates open in batches. This is why watching named
  dates is meaningful rather than an alert that fires every day.

## User workflow

- Passive: alerts arrive on Telegram; quiet kinds (news leads, heartbeat,
  recovery) are silent, time-critical and first local/cloud outage alerts buzz.
- Manual production-state runs: first `source .env && .venv/bin/python -m
  watcher.state_sync sync`; then run `.venv/bin/python -m watcher --state .cache/state-sync/state.json --mode check [--dry-run]` (`--test-telegram` smokes).
- Deploying = pushing to `main`: the `~/.ticket-watch` clone pulls on its next
  firing, healthy or not; Actions picks it up on the next cron tick.

## Boundaries

- Secrets (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`) are env-only: git-ignored
  `.env` locally, repo secrets in Actions.
- `logs/`, `.env` and `.cache/` (live state, Cinesa token and Chrome profile) are
  local-only; only the state ref's `state.json` is shared. The Cinesa token is a
  credential and must never enter either Git history.
- The repo is public (Actions billing: a private repo at `*/15` would exceed
  the free tier).
