# odysseum-ticket-watch

A small Telegram watcher that tells you **in advance** when tickets for
*Dune : Troisième partie* go on sale at **Pathé Odysseum** (Montpellier) —
France's only IMAX 70 mm / 1.43:1 screen — then counts you down to the
opening so you're ready the minute seats exist. It never auto-buys tickets,
and it can watch any film/cinema on pathe.fr by editing [config.toml](config.toml).

## What it sends you

- 🎟️ **Sale opening announced** — Pathé published the date/time sales open (the key early signal); 🔁 if that datetime changes
- ⏰ **Reminders** — 24 h / 2 h / 15 min before the opening, plus 🟢 "sales should be open NOW"
- 🆕 **New listing** — a matching catalogue entry appeared (Pathé creates dedicated event pages for 70 mm runs, each with its own sale opening)
- 📍 **Listed at your cinema** (not bookable yet) → 🚨 **Tickets bookable NOW**, with formats, session dates and a direct booking link
- 📰 **News lead** — early press hint via Google News (low/medium confidence, strictly filtered — see configuration)
- ⚠️ watcher failure / ✅ recovery / 💤 weekly "still alive" heartbeat

Every alert carries the source URL, detected format (IMAX 70 mm / IMAX /
other), cinema and a confidence level. Each distinct finding is sent **once**,
deduplicated forever via `state/state.json`. News leads, heartbeats and
recovery notes arrive **silently**; sale dates, tickets, reminders and
failures buzz (tune via `alerts.silent_kinds`).

## How it works

pathe.fr pages are bot-protected, but Pathé's public JSON API is open and
publishes `salesOpeningDatetime` *before* sales start — a structured advance
signal, so there is no HTML scraping and no guessing from "Réserver" buttons.
The daily check reads the catalogue, your cinema's programme and its bookable
sessions (endpoints documented in [watcher/pathe.py](watcher/pathe.py)), plus
Google News RSS for press leaks.

The runtime is **hybrid**, because Akamai blocks Pathé's API from GitHub's
datacenter IPs (verified: 403 from Actions, 200 from a home IP, same code):

| Where | What | Why |
|---|---|---|
| your Mac — launchd, daily 09:30 | full Pathé + news check; pushes `state/state.json` | needs a residential IP |
| GitHub Actions — every 15 min | reminder ladder + supervision, reading the shared state | needs 24/7 uptime; no Pathé access required |

Safety nets so it never dies silently: ⚠️ if the local check hasn't succeeded
for 72 h, ⚠️ after 3 consecutive Pathé failures, 💤 weekly heartbeat.

## Setup

Requires Python 3.9+.

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -q        # optional: run the test suite
```

**1. Telegram bot** — talk to [@BotFather](https://t.me/BotFather) → `/newbot`
→ copy the token. Open your new bot's chat, send it any message, then:

```bash
curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" | python3 -m json.tool
# your chat id is at result[..].message.chat.id
```

**2. Secrets** — provide `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` as
environment variables (locally: a git-ignored `.env` file with `export` lines).

**3. Try it:**

```bash
source .env
.venv/bin/python -m watcher --test-telegram          # sends a hello message
.venv/bin/python -m watcher --mode check --dry-run   # full check; logs alerts, sends nothing, state untouched
.venv/bin/python -m watcher --mode check             # real run: alerts sent, state saved
```

## Deploy

**Cloud half** (reminders + supervision):

```bash
gh repo create odysseum-ticket-watch --public --source . --push
gh secret set TELEGRAM_BOT_TOKEN
gh secret set TELEGRAM_CHAT_ID
```

Keep the repo public — on a private repo the 15-min pass costs ~2900
free-tier Actions minutes/month (or relax the cron to `*/30` in
[watch.yml](.github/workflows/watch.yml)). To verify: Actions → *ticket-watch*
→ Run workflow with mode `test` — you should get a Telegram message.

**Local half** (the daily Pathé check). The clone must live **outside
`~/Documents`/`~/Desktop`** — macOS blocks launchd agents there
("Operation not permitted"):

```bash
git clone https://github.com/<you>/odysseum-ticket-watch.git ~/.ticket-watch
cp .env ~/.ticket-watch/ && cd ~/.ticket-watch
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
mkdir -p logs ~/Library/LaunchAgents
cp scripts/com.odysseum.ticket-watch.plist ~/Library/LaunchAgents/    # fix the absolute paths inside if yours differ
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.odysseum.ticket-watch.plist
launchctl kickstart gui/$(id -u)/com.odysseum.ticket-watch            # run once now to test
```

launchd runs a missed 09:30 job on the next wake; only a fully powered-off
Mac skips a day. Both halves commit `state/state.json`, so run
`git pull --rebase` before editing any working copy.

## Configuration reference (config.toml)

| Key | Default | What it does |
|---|---|---|
| `film.primary_slug` | *(required)* | Film slug, taken from its pathe.fr URL. |
| `film.title` | slug | Display name used in alerts. |
| `film.page_url` | derived from slug | Link shown in alerts and reminders. |
| `film.release_date` | `""` | `YYYY-MM-DD`. News mentioning this date isn't mistaken for a sale date. |
| `film.match_patterns` | *(see file)* | Regexes (matched on lowercase, accent-stripped slug+title) that catch extra listings, e.g. a dedicated "… : Projection IMAX 70mm" event page. |
| `cinema.slug` | *(required)* | Cinema slug from `https://www.pathe.fr/api/cinemas`. |
| `cinema.name`, `cinema.city` | slug, `""` | Shown in alerts; also used as venue words for news filtering. |
| `reminders.offsets_minutes` | `[1440, 120, 15]` | When to remind before the sale opening. The 🟢 "open now" ping always fires at opening time. |
| `news.enabled` | `true` | `false` switches the news channel off entirely. Pathé API alerts (sale date, listings, sessions) are unaffected. |
| `news.min_confidence` | `"low"` | `"low"`: sale wording (réservations, billets, tickets, on sale…), or format keywords (70mm/IMAX) together with a venue mention. `"medium"`: only leads with sale wording **and** an explicit future date — quieter, but a dateless "tickets just went on sale" headline would be dropped. |
| `news.max_age_days` | `10` | Ignore news older than this. |
| `news.max_alerts_per_run` | `3` | Cap on news alerts per check. |
| `news.google_news_queries` | *(see file)* | Google News RSS search URLs to scan. |
| `news.extra_pages` | `[]` | Extra URLs scanned with the same phrase rules. |
| `alerts.heartbeat_days` | `7` | 💤 "alive" summary when nothing was alerted for N days. `0` = off. |
| `alerts.failure_streak_threshold` | `3` | ⚠️ after N consecutive failed Pathé checks. |
| `alerts.stale_check_hours` | `72` | Cloud pass ⚠️ when the last successful check is older than this (local job died). `0` = off. |
| `alerts.silent_kinds` | `["HEARTBEAT", "NEWS_LEAD", "RECOVERED"]` | Alert kinds delivered silently (no sound/vibration). Everything else buzzes; reminders and the 🟢 "open now" ping always buzz. |
| `general.state_file` | `state/state.json` | Dedup/reminder state location. |

Secrets are env-only (never in config.toml): `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.

## Example alerts

```
🎟️ Ticket sale opening announced
Listing: Dune : Troisième partie : Projection IMAX 70mm
Sales open: Thu 05 Nov 2026, 08:00 (Paris time)
Watched cinema: Pathé Odysseum, Montpellier
Format of this listing: IMAX 70 mm (1.43:1)
Confidence: HIGH — official Pathé API field salesOpeningDatetime.
🔗 https://www.pathe.fr/evenements/dune-troisieme-partie-projection-imax-70mm-...
```

```
⏰ Reminder: ticket sale opens in ~15 minutes
🎬 Dune : Troisième partie
🗓️ Opening: Thu 05 Nov 2026, 08:00 (Paris time)
🏛️ Pathé Odysseum, Montpellier
Be ready: sign in on pathe.fr, save a payment method.
👉 https://www.pathe.fr/films/dune-troisieme-partie-50828
```

## Notes & limitations

- Pathé's own "Ma liste" wishlist notifications are a reasonable **backup**
  (release/booking pushes, timing undocumented, no format targeting) — this
  watcher remains the precise/early channel.
- If Pathé redesigns the API or extends bot protection, you get a ⚠️ alert
  after 3 failed checks instead of silence.
- Context: the first global IMAX 70 mm ticket wave for Dune 3 (April 2026,
  not France) sold out within hours — when the Odysseum opening is announced,
  expect minutes, not days.
