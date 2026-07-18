# Current state

<!-- Snapshot of the system TODAY. Never a changelog — history lives in docs/history/. -->

A single-user Telegram watcher for the *Dune : Troisième partie* ticket-sale
opening at Pathé Odysseum (Montpellier, IMAX 70 mm). It reads Pathé's public JSON
API (which publishes `salesOpeningDatetime` in advance) plus Google News RSS, and
sends deduplicated alerts: sale-date announcements, new listings, bookable-now,
a 24 h / 2 h / 15 min reminder ladder, strictly-filtered news leads, and
supervision alerts (failure streak, stale state, weekly heartbeat).

## Runtime shape

- **Local half** — launchd agent `com.odysseum.ticket-watch` in the
  `~/.ticket-watch` clone fires `scripts/local-check.sh` every 15 min; an
  adaptive-cadence guard decides if a full Pathé + news check is due (≈4 h
  baseline, tightening to every firing around the announced opening; zero
  network on idle firings). Runs from a residential IP because Akamai blocks
  Pathé's API from GitHub's datacenter IPs.
- **Cloud half** — `.github/workflows/watch.yml` cron `*/15`: reminder ladder +
  supervision only, reading shared state; never calls Pathé.
- **Shared state** — `state/state.json`, committed to `main` by both halves
  (`[skip ci]`); serves as dedup memory and reminder bookkeeping.
- **Code** — Python package `watcher/` (`pathe.py` API client, `news.py`,
  `detect.py`, `state.py`, `notify.py` Telegram, `config.py`, `__main__.py`
  CLI); config in `config.toml`; tests in `tests/` (42 passing).

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
- `logs/` and `.env` are local-only; `state/state.json` is the one runtime
  artifact that is committed and shared.
- The repo is public (Actions billing: a private repo at `*/15` would exceed
  the free tier).
