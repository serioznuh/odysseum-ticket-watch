# odysseum-ticket-watch

Telegram watcher that detects **in advance** when tickets for *Dune : Troisième
partie* go on sale at **Pathé Odysseum (Montpellier)** — France's first and only
IMAX 70 mm (1.43:1) screen — and reminds you before the sale opens.

It does **not** scrape HTML and does **not** auto-buy tickets.

## How it works

Pathé's website is protected by Akamai Bot Manager (plain HTTP GET of any page
returns 403), but its public JSON API is open and, crucially, contains the
advance signal we want as a structured field:

| Signal | Endpoint | Meaning |
|---|---|---|
| `salesOpeningDatetime` | `/api/shows`, `/api/show/{slug}` | **Future** date/time when Pathé opens ticket sales (e.g. `2026-11-05T08:00:00+01:00`). This is what powers the "Ouverture des ventes le …" banner on their site. |
| new catalogue listing | `/api/shows` | Pathé creates *dedicated event listings* for 70 mm runs (they did for L'Odyssée: `l-odyssee-projection-imax-70mm-54413`). Any new listing matching the film patterns is alerted. |
| cinema programme | `/api/cinema/cinema-pathe-odysseum/shows` | The film appears at Odysseum, with `bookable`/`isBookable` flags. |
| bookable sessions | `/api/show/{slug}/showtimes/cinema-pathe-odysseum` | Actual sessions with `tags` (`imax`, …), auditorium and direct booking links — the "tickets are on sale now" proof. |
| news leads | Google News RSS (fr + en) | Press announcements (ticket waves are often announced globally before French ticketing opens). Phrase + film matching, low/medium confidence. |

Detection never relies on a generic "Réserver maintenant" button. Pathé
signals come from structured API fields; text-phrase matching ("réservations
ouvertes", "mise en vente", "IMAX 70mm", …) is only applied to news items and
requires a film match too.

**Alert types** (each sent once, deduplicated forever via `state/state.json`):

- 🎟️ `SALE_DATE` — a future sale opening was published → immediate alert + reminder ladder
- 🔁 `SALE_DATE_CHANGED` — the published opening moved
- 🆕 `NEW_LISTING` — new Pathé catalogue entry matching the film (e.g. "… : Projection IMAX 70mm")
- 📍 `CINEMA_LISTED` — film on the Odysseum programme, not bookable yet
- 🚨 `TICKETS_AVAILABLE` — bookable sessions exist at Odysseum (fallback if no advance date was ever published; re-alerts when a *new format* appears, e.g. IMAX 70 mm sessions added after standard ones)
- ⏰ reminders — 24 h / 2 h / 15 min before the sale opening, plus 🟢 at opening time
- 📰 `NEWS_LEAD` — external press match (no reminders are scheduled from news alone)
- ⚠️/✅ watcher error / recovery, 💤 weekly heartbeat ("still alive, nothing new")

Every alert includes: source URL, detected format (IMAX 70 mm / IMAX / other),
cinema, and confidence (HIGH = Pathé API, MEDIUM = news with explicit future
date, LOW = news phrase match).

## Setup

Requires **Python 3.9+** (tested on macOS system Python 3.9.6; CI uses 3.12).

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m pytest -q          # run the test suite
```

### 1. Create the Telegram bot

1. In Telegram, talk to [@BotFather](https://t.me/BotFather) → `/newbot` → pick a
   name/username → copy the **bot token** (`123456789:AA...`).
2. Open a chat with your new bot and send it any message (bots cannot message
   you first).
3. Get your **chat id**:
   ```bash
   curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" | python3 -m json.tool
   # → result[0].message.chat.id  (e.g. 123456789)
   ```

### 2. Environment variables

| Variable | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | token from BotFather |
| `TELEGRAM_CHAT_ID` | your chat id |

### 3. Run locally

```bash
export TELEGRAM_BOT_TOKEN="123456789:AA..."
export TELEGRAM_CHAT_ID="123456789"

python -m watcher --test-telegram          # send a test message
python -m watcher --mode check --dry-run   # full check, alerts logged, state untouched
python -m watcher --mode check             # full check, alerts sent, state saved
python -m watcher --mode remind            # reminders only (no network except Telegram)
```

`--dry-run` never sends and never writes state, so a later real run will send
everything the dry run showed. Logs go to stderr; add `--verbose` for debug.

### 4. Deploy with GitHub Actions

```bash
git init && git add . && git commit -m "ticket watcher"
gh repo create odysseum-ticket-watch --public --source . --push
gh secret set TELEGRAM_BOT_TOKEN
gh secret set TELEGRAM_CHAT_ID
```

The workflow ([.github/workflows/watch.yml](.github/workflows/watch.yml)) runs:

- a **daily full check** at 07:23 UTC (09:23 Paris in summer), and
- a **reminder pass every 15 min** — state-only, so the 24h/2h/15min ladder can
  fire between daily checks. It sends nothing unless a reminder is due.

State is persisted by committing `state/state.json` back to the repo, which
also keeps the scheduled workflow from being auto-disabled after 60 days of
repo inactivity. Trigger a manual run (Actions → ticket-watch → Run workflow)
to verify the setup; use `dry_run` for a safe first run.

> **Private repo?** The 15-min pass costs ~2900 free-tier minutes/month.
> Make the repo public (state contains no secrets), or relax the cron to
> `*/30` or hourly — see the comment in the workflow.

> **Caution:** GitHub's cron is best-effort (runs can lag several minutes at
> busy times), and datacenter IPs are more likely to be challenged by Akamai
> than home IPs. If checks start failing you'll get a ⚠️ Telegram alert after
> 3 consecutive failures — fall back to running locally (see below).

### Alternative: run locally via cron (no GitHub needed)

```cron
# crontab -e   (Mac must be awake)
23 9 * * *    cd ~/Projects/odysseum-ticket-watch && .venv/bin/python -m watcher --mode check  >> watch.log 2>&1
*/15 * * * *  cd ~/Projects/odysseum-ticket-watch && .venv/bin/python -m watcher --mode remind >> watch.log 2>&1
```

## Configuration

Everything lives in [config.toml](config.toml) — film slug and match patterns,
cinema slug, reminder offsets, news feeds, heartbeat cadence. To watch another
film, change `[film]`; to watch another cinema, change `[cinema]` (slugs come
from `https://www.pathe.fr/api/cinemas` or the pathe.fr URL).

## Example Telegram messages

```
🎟️ Ticket sale opening announced
Listing: Dune : Troisième partie : Projection IMAX 70mm
Sales open: Thu 05 Nov 2026, 08:00 (Paris time)
Watched cinema: Pathé Odysseum, Montpellier
Format of this listing: IMAX 70 mm (1.43:1)
Confidence: HIGH — official Pathé API field salesOpeningDatetime.
Note: opening time is national; popular IMAX 70 mm seats can sell out in minutes.
🔗 https://www.pathe.fr/evenements/dune-troisieme-partie-projection-imax-70mm-...
```

```
🚨 Tickets are bookable NOW
Listing: Dune : Troisième partie
Cinema: Pathé Odysseum, Montpellier
Formats bookable: IMAX 70 mm (1.43:1): 14 session(s); IMAX: 22 session(s)
Session dates: 2026-12-16 → 2026-12-27 (36 sessions)
Book (IMAX 70 mm (1.43:1)): https://s.pathe.fr/fr/.../booking
Confidence: HIGH — sessions returned by the Pathé booking API for this cinema.
🔗 https://www.pathe.fr/films/dune-troisieme-partie-50828
```

```
⏰ Reminder: ticket sale opens in ~15 minutes
🎬 Dune : Troisième partie
🗓️ Opening: Thu 05 Nov 2026, 08:00 (Paris time)
🏛️ Pathé Odysseum, Montpellier
Be ready: sign in on pathe.fr, save a payment method.
👉 https://www.pathe.fr/films/dune-troisieme-partie-50828
```

```
📰 News lead: possible ticket-sale info
Headline: Dune 3 : les réservations IMAX 70mm ouvriront le 5 novembre
Source: BoxOffice Pro, published 2026-10-28
Matched phrases: imax 70mm, reservations
Date(s) mentioned (possible sale date): 05 Nov 2026
Confidence: MEDIUM — external lead, verify on Pathé. No reminders are scheduled from news alone.
🔗 https://news.google.com/...
```

## Notes & limitations

- **Pathé "Ma liste"**: adding the film to your Pathé wishlist (with marketing
  emails + app push enabled) gets you Pathé's own release/booking notifications.
  Timing isn't documented and there's no cinema/format targeting — enable it as
  a *backup*, this watcher remains the precise/early channel.
- The first IMAX 70 mm ticket wave for Dune 3 (April 2026) was global, sold via
  imax.com/US chains, and sold out in hours. French/Odysseum ticketing goes
  through Pathé — exactly what this watcher monitors; the news layer covers any
  further global waves.
- If Pathé redesigns the API or extends bot protection to `/api`, the watcher
  alerts you after 3 failed daily checks instead of failing silently.
