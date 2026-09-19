# odysseum-ticket-watch

A small Telegram watcher that tells you **in advance** when *Dune : Troisième partie* tickets go
on sale at **Pathé Odysseum** (Montpellier), then counts down. It never auto-buys; edit [config.toml](config.toml) for another watch.

## What it sends you

- 🎟️ **Sale opening announced** — Pathé published the date/time sales open (the key early signal); 🔁 if that datetime changes
- ⏰ **Reminders** — 24 h / 2 h / 15 min before the opening, plus an unconfirmed-opening reminder at the scheduled time
- 🆕 **New listing** — a matching catalogue entry appeared (Pathé creates dedicated event pages for 70 mm runs, each with its own sale opening)
- 🎫 **Your wanted Pathé date opened** — IMAX 70mm on December 19 or 20; one alert per date, combined if both open together
- 📍 **Listed at your cinema** (not bookable yet); without wanted dates configured, 🚨 **Tickets bookable NOW** reports new formats
- 📰 **News lead** — early press hint via Google News (low/medium confidence, strictly filtered — see configuration)
- 🔴 watcher blind or persistently degraded, then a silent daily repeat until it recovers / ✅ recovery / 💤 weekly heartbeat

From the second watch target (*La odisea* in IMAX at **Cinesa Diagonal Mar**,
Barcelona — see [Cinesa target](#cinesa-target)):

- 🎫 **A watched date opened in IMAX** — one of your `target_dates` is now bookable with IMAX on it
- 🗓️ *(silent)* that date opened, but **without** IMAX — you still get the loud 🎫 if IMAX appears for it later
- 📉 **IMAX disappeared** (confirmed over two checks) / 📈 **IMAX is back**

Every alert names its **film and cinema** first, so a second watch target is
never mistaken for this one, plus its source URL and format. Findings are sent
**once**, deduplicated forever via the shared runtime state — bar the outage
reminder, daily by design. Same-run findings that are one piece of news share
**one message**. News leads, heartbeats, recoveries and "still blind" repeats
are **silent** (`alerts.silent_kinds`).

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
| your Mac — launchd, every 5 min | Pathé + news check (adaptive cadence); **owns the reminder ladder**; syncs the `runtime-state` ref | needs a residential IP |
| GitHub Actions — cron `*/15`, ~11% reliable | cloud-safe news + supervision + reminder **failover** (25 min grace), sharing dedup state | covers a sleeping Mac; never calls Pathé or Cinesa |

Not every 403 is a block: the showtimes endpoint serves only `isMovie` films, so
event listings (the 70 mm ones) always answer `"No movie allowed !"`, and their
bookability is read off the cinema programme. Unexpected detail/showtimes failures
degrade health without discarding the rest of the snapshot.

Safety nets: 🔴 after 3 consecutive Pathé failures (including partial listing failures once full coverage has been absent for 6 h),
🔴 if the local catalogue liveness pulse stops for 18 h — **then every 24 h**, so
a long outage cannot fall out of mind — and 💤 a weekly heartbeat. Each names
the cause it can prove (IP block, CI range, origin refusal, or a silent Mac).

### Cinesa target

The second target watches a film that is **already showing**, so the signal is
the booking calendar extending. You name the dates you want and only those buzz.

Cinesa runs Vista's Omnia/Connect platform, split across two hosts:

| Host | Status | Role |
|---|---|---|
| `www.cinesa.es` | Cloudflare managed challenge (403 to any plain client) | mints the 12 h API token |
| `vwc.cinesa.es/WSVistaWebClient` | open — plain `httpx`, clean JSON | every actual check |

The data API runs on **every** 5-min firing. Only its token needs a browser:
[watcher/cdp.py](watcher/cdp.py) drives real, headed Chrome offscreen about twice a
day. Headless never clears the challenge; no stealth or challenge-solving is used.

**Requirements:** Chrome installed, Mac logged in and awake — a locked screen is fine (verified); only system sleep or a login window blocks it.
Chrome self-activates on launch, so focus is handed back explicitly. Refreshes are normally imperceptible, not a 100% guarantee of invisibility; absolute zero laptop impact requires a separate always-on home machine.

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
.venv/bin/python -m watcher --mode remind --with-news --dry-run  # cloud-safe news; plain remind stays request-free
```

### State bootstrap and recovery

State contains permanent alert receipts and reminder rungs, so a missing or
invalid file stops the watcher before network access or Telegram delivery. The
watcher never renames, replaces, or silently restores it; dry-runs are read-only.

For a genuinely new installation with no state file, initialize it once:

```bash
.venv/bin/python -m watcher --bootstrap-state
```

Bootstrap refuses to overwrite any existing file. A missing production file is
**not** a new installation. To recover, first stop launchd and disable the Actions
workflow so neither state writer can send or synchronize. Preserve the damaged
file, then collect the production clone, remote history, and any backups. Start
from the newest valid copy and reconcile every known Telegram receipt: take the
union of `alerts` keys and, per sale timestamp, the union of `reminders_sent`;
also retain the newest `sales`, `formats_seen`, and `shows_seen` baselines. Never
resume directly from an older backup—it may omit recent sends and replay them.

Install the reconciled candidate at `general.state_file`, then validate it without
sending or rewriting it using `python -m watcher --mode remind --dry-run`. Ensure
that exact repaired state reaches both the production clone and `main` before
re-enabling Actions and launchd. If delivery history cannot be reconciled, keep
the watcher stopped rather than risk duplicate historical notifications.

## Deploy

**Cloud half** (cloud-safe news + reminder failover + supervision):

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

The agent fires every 5 minutes with adaptive cadence: every 4 h normally, 2 h
in the last week, 30 min in the last 48 h, every firing around opening, then 6 h
once bookable. Wanted dates remain at 5 min until announced. Sleep pauses checks;
failures retry on wake. Both halves sync a dedicated `runtime-state` ref; code
deployment on `main` is independent of that state history.

## Configuration reference (config.toml)

| Key | Default | What it does |
|---|---|---|
| `film.primary_slug` | *(required)* | Film slug, taken from its pathe.fr URL. |
| `film.title` | slug | Display name used in alerts. |
| `film.page_url` | derived from slug | Link shown in alerts and reminders. |
| `film.target_format` | `""` | Optional `imax70`, `imax` or `other` filter for listings, sale announcements and reminders. IMAX 70mm requires both IMAX and 70mm evidence; 1.43:1 alone is insufficient. |
| `film.target_dates` | `[]` | Wanted `YYYY-MM-DD` dates; requires `target_format`. Replaces generic book-now alerts with date-specific alerts, using day-level bookability or available sessions. |
| `film.target_page_url` | film page | Reminder link to the dedicated format page. |
| `film.release_date` | `""` | `YYYY-MM-DD`. News mentioning this date isn't mistaken for a sale date. |
| `film.match_patterns` | *(see file)* | Regexes (matched on lowercase, accent-stripped slug+title) that catch extra listings, e.g. a dedicated "… : Projection IMAX 70mm" event page. |
| `cinema.slug` | *(required)* | Cinema slug from `https://www.pathe.fr/api/cinemas`. |
| `cinema.name`, `cinema.city` | slug, `""` | Shown in alerts; also used as venue words for news filtering. |
| `reminders.offsets_minutes` | `[1440, 120, 15]` | When to remind before the sale opening. At the scheduled time, an unconfirmed-opening reminder is due unless the selected format is already available. |
| `news.enabled` | `true` | `false` switches the news channel off entirely. Pathé API alerts (sale date, listings, sessions) are unaffected. |
| `news.min_confidence` | `"low"` | `"low"`: sale wording (réservations, billets, tickets, on sale…), or format keywords (70mm/IMAX) together with a venue mention. `"medium"`: only leads with sale wording **and** an explicit future date — quieter, but a dateless "tickets just went on sale" headline would be dropped. |
| `news.max_age_days` | `10` | Ignore news older than this. |
| `news.max_alerts_per_run` | `3` | Cap on news alerts per check. |
| `news.google_news_queries` | *(see file)* | Google News RSS search URLs to scan. |
| `news.extra_pages`, `news.cloud_extra_pages` | `[]`, `[]` | Extra URLs scanned locally, and the separate explicit allow-list scanned from the cloud. Cloud mode otherwise reads only `news.google.com` RSS and always refuses `pathe.fr` hosts. |
| `alerts.heartbeat_days` | `7` | 💤 "alive" summary when nothing was alerted for N days. `0` = off. |
| `alerts.failure_streak_threshold` | `3` | ⚠️ after N consecutive failed Pathé checks. |
| `alerts.stale_check_hours` | `18` | Cloud pass ⚠️ when the last successful check is older than this (local job died, or the Mac stayed shut). `0` = off. Sized from measured gaps: 4 h median, 12.9 h worst ordinary overnight — below ~16 h, normal nights trip it. |
| `alerts.silent_kinds` | `["HEARTBEAT", "NEWS_LEAD", "RECOVERED", "CINESA_TARGET_NO_IMAX", "WATCHER_STILL_BLIND"]` | Alert kinds delivered silently (no sound/vibration). Everything else buzzes; reminders and the 🟢 "open now" ping always buzz. This list **replaces** the built-in default rather than extending it, so a new quiet kind must be added here too (a test enforces this). |
| `cadence.baseline_hours` | `4.0` | Check frequency while nothing is announced. |
| `cadence.within_week_hours` | `2.0` | …when the sale opening is ≤ 7 days away. |
| `cadence.final_48h_hours` | `0.5` | …when it's ≤ 48 h away. |
| `cadence.opening_window_minutes` | `15` | …from 4 h before to 6 h after the opening (every launchd firing). |
| `cadence.after_tickets_hours` | `6.0` | …once tickets are bookable (still watching for new waves/formats). |
| `cinesa.enabled` | `false` | Master switch for the Cinesa Diagonal Mar target. `false` skips it entirely — no API call, no browser. |
| `cinesa.film_id` | *(required when enabled)* | Vista HO code, straight from the cinesa.es film URL (e.g. `HO00003228`). |
| `cinesa.site_id` | *(required when enabled)* | Vista site id. Diagonal Mar is `032`; the full list is in `/api/omnia/v1/pageList?friendly=/cines/&properties=vistaCinema`. |
| `cinesa.film_title`, `cinesa.site_name`, `cinesa.site_city` | ids, `""` | Display names used in alerts. |
| `cinesa.page_url` | `https://www.cinesa.es/` | Link shown in alerts. |
| `cinesa.target_dates` | `[]` | `YYYY-MM-DD` dates to watch. Each buzzes 🎫 once it is bookable **with IMAX**; bookable without IMAX is reported silently 🗓️. Invalid dates fail at startup. |
| `cinesa.imax_attribute_id` | `"0000000086"` | Vista showtime attribute marking an IMAX session. |
| `cinesa.token_url` | `page_url` | Page loaded purely to mint a token; any Cinesa page works. |
| `cinesa.api_base` | `https://vwc.cinesa.es/WSVistaWebClient` | The open data host. |
| `cinesa.token_cache` | `.cache/cinesa-token.json` | Cached 12 h token, mode 0600. **Git-ignored — it is a credential.** |
| `cinesa.token_refresh_before_hours` | `3.0` | Refresh the token once it has less than this much life left, instead of at expiry. Gives a multi-hour retry window if the Mac is locked/asleep; a failed refresh keeps using the token in hand rather than failing the check. Retries are backed off to 30 min apart. A data-API 403 also preserves the token and suppresses another Chrome mint for at least 60 min; disable any VPN/proxy and wait for the automatic retry. |
| `cinesa.chrome_path`, `cinesa.chrome_profile` | macOS Chrome, `.cache/chrome-profile` | Chrome binary for the token step, and a throwaway profile — never your own. |
| `general.state_file` | `.cache/state-sync/state.json` | Validated live state materialized from the dedicated Git ref. |

Secrets are env-only (never in config.toml): `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.

## Notes & limitations

- Pathé's own "Ma liste" wishlist notifications are a reasonable **backup**
  (release/booking pushes, timing undocumented, no format targeting) — this
  watcher remains the precise/early channel.
- If Pathé redesigns the API or extends bot protection, you get a ⚠️ alert
  after 3 failed checks instead of silence.
