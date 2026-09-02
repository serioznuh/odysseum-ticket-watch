# 2026-09 — Alert message redesign and the outage drumbeat

## 2026-09-02 — Pathé 403 outage exposed two problems

A local 403 outage started at 11:45 (last good check 07:11). Pathé's API
returned `{"error":"Error from IP 79.116.217.215"}` from an Akamai Barcelona
edge — the ISP's own IP, with no VPN in play — while `www.pathe.fr` itself
still served 200. The alert that fired at 13:31 advised disabling a VPN the
user did not have, and buried the one fact that mattered ("BLIND") on line 4
under a 100-character API URL.

Reviewing the alert surfaced a second, larger problem: **an outage of any
length produced exactly two messages, ever.** The local alert latched on
`error_alerted`; the cloud stale alert was keyed `stale:{last_check_ok}`, which
is frozen during an outage, so dedup silenced every repeat; and the weekly
heartbeat is nested under `if snap is not None`, so it stops precisely when
the watcher is blind. Three weeks of downtime would have meant two messages on
day one and then silence.

## What changed

- **All 19 message types rewritten.** State first, cause second, action last.
  `Confidence: HIGH — …` boilerplate dropped everywhere except news leads,
  where confidence genuinely varies. The two urgent messages
  (`TICKETS_AVAILABLE`, the "open now" ping) became the shortest rather than
  the longest. The three reminder offsets stopped sharing one template: 24 h
  prepares, 2 h warns, 15 min says be at the keyboard.
- **Every alert names its film and cinema on the first line**, so a second
  watch target can never be mistaken for this one.
- **The outage reminder repeats every 24 h** until it recovers, silently after
  the first. Owned by the cloud pass, so it survives the Mac being off — which
  is the likelier cause of a long blind spell than Pathé blocking an IP.
- **The cloud pass can now say *why* it is blind.** It never calls Pathé, so it
  reads a `last_error` the local half records — written only when the text
  changes, so a steady outage still writes state once rather than every 15 min.
  `error_alerted` distinguishes "local half is failing" from "the Mac never
  reported", which need opposite responses.
- **Cinesa outages repeat daily too**, on the local half (the cloud pass cannot
  watch Cinesa — it needs Chrome). `blind_since` is stamped once per outage.
- Closed **OTW-07** (stale alert now names Pathé + news + Cinesa) and **OTW-03**
  (CI gets its own 403 wording).

## Notes for later

- `WATCHER_STILL_BLIND` buzzed on the first simulation run: `config.toml`
  *replaces* `DEFAULT_SILENT_KINDS` rather than extending it, so a kind added
  only in code still notifies loudly. `test_config.py` now enforces the two
  lists agree.
- `summarize_pathe_error` had to be made idempotent — its output is stored in
  state and re-parsed by the cloud pass, and the summarised form no longer
  matched the original regex, silently losing the status code.
- `html.escape` was rewriting every apostrophe as `&#x27;`. Telegram's HTML
  mode only needs `& < >` escaped in text content; `notify.esc` now uses
  `quote=False`.
- The stale key gained a `:{period}` suffix, deliberately breaking OTW-07's
  byte-identical requirement. `state.migrate_stale_keys` rewrites the old key
  on load so a currently-blind watcher gets no duplicate alert on deploy.

## Review round (Opus max, PR #11)

Three P1 defects, all confirmed by reproduction before fixing:

- **The stale-key migration corrupted two-digit periods.**
  `datetime.fromisoformat` accepts sub-minute UTC offsets, so
  `stale:<iso>:37` parses as a valid timestamp with a `+02:00:37` offset.
  Using "does it parse?" as the old-format test meant every period from 10 to
  99 — outage days 11 to 100 — was renamed after being sent, so `already_sent`
  missed and the alert re-fired on every cloud pass: ~96 messages, commits and
  pushes a day. The discriminator is now a `fullmatch` on the exact legacy
  shape (`LEGACY_STALE_KEY`), and a simulation covering days 9-14 with real
  `load_state`/`save_state` round-trips confirms one message per day.
- **The cloud alert blamed CI for the Mac's outage.** `pathe_cause` branched
  on `running_in_ci()`, but the cloud pass always runs in Actions while the
  cause it reports was recorded by the Mac — so every residential 403 read as
  "Pathé blocks GitHub datacenter IPs", i.e. the known, dismissable artifact.
  The CI branch is only sound for an error the current process caught, so it
  is now an explicit `ci=` argument that `build_stale_finding` never sets.
- **NEW_LISTING denied a sale date the next alert announced.** The branch fires
  on first sight of a listing regardless of `salesOpeningDatetime`, and a
  dedicated 70 mm event page usually arrives with its opening already set —
  the project's headline scenario. The line is now conditional.

Also fixed from the review notes: "Reminders set: …" was an unconditional
promise, but the ladder tracks a single `sale_target` (the earliest future
opening) and stops once tickets are bookable, so it was false for any later
listing — `reminders_cover()` now gates the claim; `TICKETS_AVAILABLE` titled
the best format *present* rather than the one that just appeared, re-announcing
IMAX 70 mm when standard sessions were the news; `last_error` no longer stores
the failing URL, so an outage flapping between endpoints cannot rewrite state
every 15 min; and NEWS_LEAD carries its confidence again, so low and medium
leads are distinguishable.
