# Current state

<!-- Snapshot of the system TODAY. Never a changelog — history lives in docs/history/. -->

A single-user Telegram watcher covering **two independent targets**:

1. *Dune : Troisième partie* ticket-sale opening at Pathé Odysseum
   (Montpellier, IMAX 70 mm) — reads Pathé's public JSON API (which publishes
   `salesOpeningDatetime` in advance) plus Google News RSS, and sends
   deduplicated alerts: sale-date announcements, new listings, bookable-now, a
   24 h / 2 h / 15 min reminder ladder, strictly-filtered news leads, and
   supervision alerts (failure streak, stale state, weekly heartbeat).
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
- **Code** — Python package `watcher/` (`pathe.py` and `cinesa.py` API clients,
  `cdp.py` browser token step, `news.py`, `detect.py`, `state.py`, `notify.py`
  Telegram, `config.py`, `__main__.py` CLI); config in `config.toml`; tests in
  `tests/` (63 passing).

## Cinesa specifics

- Two hosts: `www.cinesa.es` is behind a **Cloudflare managed challenge** and
  only mints the 12 h API token; `vwc.cinesa.es/WSVistaWebClient` serves the
  actual data to plain `httpx` and is not bot-protected.
- The token step drives a **real headed Chrome** (offscreen, throwaway profile,
  ~3 s). `--headless=new` is challenged and never settles — measured, not
  assumed. No stealth or challenge-solving is used or wanted: if Chrome stops
  clearing the challenge on its own, the watcher must fail loudly instead.
- Chrome **self-activates on launch** even under `open -g -j`, so `cdp.py`
  captures the frontmost app and re-activates it after the tab is created
  (doing it any earlier just lets Chrome take focus again). Measured focus
  loss: 0–0.15 s per refresh, twice a day.
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
  active firing; Actions picks it up on the next cron tick.

## Boundaries

- Secrets (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`) are env-only: git-ignored
  `.env` locally, repo secrets in Actions.
- `logs/`, `.env` and `.cache/` (Cinesa token + Chrome profile) are local-only;
  `state/state.json` is the one runtime artifact that is committed and shared.
  The Cinesa token is a credential and must never be committed.
- The repo is public (Actions billing: a private repo at `*/15` would exceed
  the free tier).
