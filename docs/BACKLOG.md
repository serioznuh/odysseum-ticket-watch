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
| OTW-03 | 403 alert VPN wording wrong on manual CI dispatch | P3 | S | Bugs | [x] |
| OTW-04 | Cinesa alert: include session times + booking link | P2 | S | Features | [ ] |
| OTW-05 | Confirm Cinesa token behaviour with screen locked/asleep | P2 | S | Infra, tooling & docs | [x] |
| OTW-06 | Pathé failure_streak churns state; baseline can lose an alert | P2 | S | Bugs | [x] |
| OTW-07 | Stale-check alert says "Pathé" but the whole local half is down | P3 | S | Bugs | [x] |
| OTW-08 | Run the news half from the cloud pass to cover Mac-asleep windows | P1 | M | Features | [ ] |
| OTW-09 | Supervision is one-directional — nothing watches the cloud half | P2 | M | Features | [ ] |
| OTW-10 | Cinesa VPN 403 repeatedly launches headed Chrome | P1 | S | Bugs | [x] |
| OTW-11 | Make Cinesa Chrome refresh normally imperceptible | P2 | S | UX & design | [x] |
| OTW-12 | `reminders_cover` can over-promise on two same-pass events | P3 | S | Bugs | [ ] |
| OTW-13 | A persistent per-listing Pathé failure is reported as healthy | P2 | S | Bugs | [x] |
| OTW-14 | An aborted state rebase can wedge the push until a human intervenes | P3 | S | Bugs | [ ] |
| OTW-15 | Reminders ride a cloud cron that fires ~11% of its schedule | P0 | M | Bugs | [x] |
| OTW-16 | One run fans out a burst of near-identical alerts | P1 | S | Bugs | [x] |
| OTW-17 | A merged sale message mixing new and moved openings reads oddly | P3 | S | UX & design | [ ] |
| OTW-18 | Validate state and make recovery explicit | P1 | M | Infra, tooling & docs | [x] |
| OTW-19 | Split orchestration into bounded jobs | P2 | M | Infra, tooling & docs | [x] |
| OTW-20 | Persist a notification outbox and delivery receipts | P1 | L | Infra, tooling & docs | [ ] |
| OTW-21 | Separate deployment from runtime-state synchronization | P1 | L | Infra, tooling & docs | [ ] |
| OTW-22 | Move the local owner to an always-on residential host | P2 | L | Infra, tooling & docs | [ ] |
| OTW-23 | An uncaught save_state failure after delivery can re-send alerts | P2 | S | Bugs | [ ] |
| OTW-24 | Harden Cinesa leak tracking against a builder exception, and always persist state in CI | P3 | S | Bugs | [ ] |

## Architecture implementation sequence

1. **OTW-18** — validate state before changing how it is stored or delivered.
2. **OTW-13** — expose partial Pathé failures and preserve evidence quality.
3. **OTW-19** — extract jobs and protect reminders from slow polling.
4. **OTW-21 + OTW-14** — separate synchronization from deployment and resolve
   conflicts without losing delivery history. Design the OTW-20 delivery
   ownership contract here; implement it with the outbox in the next step.
5. **OTW-20** — persist pending notifications and confirmed delivery receipts.
6. **OTW-08** — add cloud news coverage using the shared delivery contract.
7. **OTW-09** — add reverse supervision of the cloud half.
8. **OTW-22** — migrate to an approved always-on home host. This can move
   earlier once OTW-18, OTW-21 and OTW-09 are ready and a host is available.

Remaining independent work: **OTW-12**, **OTW-17**, then **OTW-01**;
**OTW-04** becomes useful when Cinesa is enabled again. These are not
dependencies of the architecture sequence. New items remain unchecked until
their individual acceptance criteria are met.

## 1. Critical — security & breakage

## 2. Bugs

### OTW-17 · A merged sale message mixing new and moved openings reads oddly
**Priority:** P3 · **Effort:** S
**Problem:** When one listing's opening is announced for the first time and
another listing *moves* to that same minute, `coalesce` merges them (same
`sale_datetime`). The title says CHANGED, and the body carries both
"Reminders set: …" and "Reminders rescheduled automatically.", plus a "Was: …"
line that does not say which listing it belongs to. Every fact is true and the
actionable content — time, formats, link — is correct, so this is readability,
not a wrong alert. Raised in review of OTW-16 and reproduced. Also applies to
several listings moving from *different* previous times.
**Fix sketch:** either split the group by whether the member changed (two
honest messages, since "just announced" and "moved" are different news), or
give the sale group its own body builder that attributes each varying line to
its format label. `watcher/coalesce.py` (`group_of`, `_merge_bodies`).
Splitting alone does not fix several listings moving from different times.
**Done when:** no merged sale message states two contradictory reminder lines,
and a "Was: …" line names the listing it describes.


### OTW-16 · One run fans out a burst of near-identical alerts
**Priority:** P1 · **Effort:** S
**Problem:** Findings are analysed per listing and per date, and `run()` sends
one Telegram message per finding. Nothing merges findings raised in the *same*
pass that a human reads as one piece of news, so a single run buzzes the phone
several times with near-identical messages. Observed twice, both confirmed
from `state/state.json` timestamps: 2026-09-03 21:46 sent four messages (two
"Sale opens Wed 9 Sep, 09:00" differing only by listing, two "New listing:
IMAX 70 mm"), and 2026-08-06 15:08 sent two identical "IMAX tickets open"
messages, one per watched Cinesa date. Per-key dedup across runs was working;
the gap is within a run.
**Fix:** Merge after the already-sent filter so a group only holds findings
about to go out, and mark every member key on success (none on failure).
Dedup keys and per-item analysis stay exactly as they are — changing a key
format would re-send every past alert of that shape.
**Done when:** the two bursts above produce two messages and one message
respectively, every original key is still written to state, a failed send
retries the whole group, and merging cannot silence an alert that would
otherwise have buzzed.
**Done (2026-09-04):** added `watcher/coalesce.py`, merging same-datetime sale
openings, new listings, bookable-now formats and Cinesa target dates.
`Finding.merge_item` names each item in the merged text. Also fixed a bug the
work exposed: `state["sales"]` advanced even when the SALE_DATE send failed,
which retired the sale announcement — the watcher's whole point — permanently
(`advance_sales`). Dropped the Cinesa `👉` line that repeated the URL
`render_finding` already appends.


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
**Done (2026-09-02):** retitled to "Local checks have stopped — {duration}";
the body names Pathé + news, and Cinesa when enabled. The byte-identical-key
requirement was **deliberately superseded** in the same change: the alert now
repeats every 24h while blind, which needs the period in the key
(`stale:{last_check_ok}:{period}`). `state.migrate_stale_keys` rewrites the old
key on load so a currently-blind watcher gets no duplicate.

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
**Done (2026-09-02):** `running_in_ci()` checks `GITHUB_ACTIONS`; in CI the 403
reads "Pathé blocks GitHub datacenter IPs" / "run the check locally instead",
with no retry promise. The launchd log pointer and the VPN advice were dropped
from the local variant too — the 2 Sep outage proved the VPN hint wrong when
the block is the ISP's own IP.

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

### OTW-13 · A persistent per-listing Pathé failure is reported as healthy

**Problem:** `fetch_snapshot` now swallows per-listing failures so one listing
cannot blind the watch (the 2026-09-02 outage). The `degraded` list it builds
reaches only a `log.info` — nothing in state, the heartbeat or any alert. Since
`__main__.run` refreshes `last_check_ok`, zeroes `failure_streak`, clears
`error_alerted` and may send RECOVERED whenever the catalogue calls succeed, a
*permanently* failing showtimes call reports full health forever. The commit
that introduced it claimed the degradation "can only delay a real alert by one
firing", which holds for a transient failure but not a persistent one.

Today this is masked: `analyze_pathe` fires TICKETS_AVAILABLE on `days` **or**
the programme entry's `isBookable`, and that entry comes from a still-fatal
catalogue call — so the sale signal survives. What is silently lost is session
detail and the `refCmd` deep booking link.

**Fix sketch:** carry `degraded` out of `fetch_snapshot` on `detect.Snapshot`,
and either name the affected listings in the weekly heartbeat or raise a
supervision finding once a listing has degraded for N consecutive checks.
Two traps: expected refusals (`origin_refusal`, which returns cleanly and is
never added to `degraded`) must not count, or the 70 mm listings alert forever;
and `show_detail()` swallows its own failures inside the helper, so they never
reach `degraded` at all — a permanently failing *detail* call is invisible too,
and carrying only `degraded` out would miss it.

**Related, same area:** a swallowed showtimes failure can also *invent* an
alert. `analyze_pathe` and `update_from_snapshot` fall back to
`classify_format(title, slug)` when `days` is empty but the entry is bookable,
so a format never actually seen can enter `present` and fire TICKETS_AVAILABLE
off a failure rather than a change. Narrow today (`dune-troisieme-partie-50828`
classifies as `other`, so it needs "no standard sessions ever at Odysseum"),
but it contradicts the claim that degradation can never invent an alert.

**Files:** `watcher/pathe.py` (`fetch_snapshot`), `watcher/detect.py`
(`Snapshot`), `watcher/__main__.py` (`build_heartbeat`).

**Done when:** a persistent per-listing failure is visible to the user without
reading logs, and a test covers "catalogue healthy + one listing failing
forever" not reporting unqualified health.

**Architecture link:** introduce an explicit result carrying data, health and
diagnostics, distinguishing authoritative empty data, unexpected failure and
expected refusal. OTW-19 should consume that result without treating catalogue
success as proof that every listing is healthy. Keep repeated degradation quiet
unless it changes or meets the existing supervision policy.

### OTW-14 · An aborted state rebase can wedge the push until a human intervenes

**Problem:** `local-check.sh` now aborts a failed rebase rather than leaving
conflict markers in `state.json` (which now make `load_state` exit non-zero with
an actionable diagnostic). Correct, but the local state commit survives unpushed, so
the following `git push` is rejected non-fast-forward and `set -e` exits the
script 1. The same conflict then recurs on every firing and local state stops
reaching origin until someone resolves it by hand.

Not urgent: measured, a realistic divergence (cloud adds an `alerts` key while
the Mac updates `last_check_ok`) auto-merges cleanly, so this needs both halves
touching adjacent keys. It also degrades to a *self-announcing* failure — the
cloud pass sees a frozen `last_check_ok` and fires its stale alert — rather
than the silent state-destroying one it replaced.

**Fix sketch:** on a rebase abort, log the conflict loudly and recover through
a tested, domain-aware state merge, preserving `alerts`, `reminders_sent` and
the baselines they acknowledge. An unpushed delivery record is not a disposable
cache: never drop its commit and assume polling can reconstruct what was sent.
OTW-21 owns the broader synchronization boundary; this item owns recovery from
the specific rebase wedge and can ship as its first increment.

**Files:** `scripts/local-check.sh`; possibly a `.gitattributes` merge driver.

**Done when:** a conflicting state rebase resolves itself within one firing
without a human, or fails in a way that names itself in an alert.

### OTW-15 · Reminders ride a cloud cron that fires ~11% of its schedule
**Priority:** P0 · **Effort:** M
**Problem:** `run()` computed reminders as `[] if args.adaptive_cadence else
due_reminders(...)`, and the local half always passes `--adaptive-cadence` — so
the ladder was deliberately cloud-only, keeping two writers off
`state["reminders_sent"]`. But `.github/workflows/watch.yml` does not run on
its `*/15` schedule: measured over the 9.6 days to 2026-09-03 it fired 100
times where the cron implies 920 (10.9%), median gap 58 min, mean 139 min, max
693 min (11.5 h); only 2 of 99 gaps were ≤ 20 min. `due_reminders` returns at
most one reminder per call, so the 15-min warning was likely to be skipped
outright and a single bad gap could span the sale opening — 2026-09-09 09:00,
six days out when this was found. Second defect in the same path:
`render_reminder` built its countdown from the offset *label*, so a reminder
delivered late announced "Sale opens in 2 hours" with minutes left.
**Fix:** Invert ownership instead of trying to make the cron reliable. The
local half owns the ladder (launchd fires every 15 min — the resolution a
15-min warning needs) and passes no grace. `due_reminders` gains
`grace_minutes`: it reports only a reminder whose window opened at least that
long ago, which makes a caller a *failover* rather than a second owner. The
workflow passes `--reminder-grace-minutes 25`, comfortably above the local
firing interval, so the cloud sends only what the Mac missed. `notify._countdown`
renders the real remaining time (floored, so it never promises time that is
gone, with the leftover minutes spelled out below a day), falling back to the
offset label when `now` is unknown.
**Three defects the review caught in that fix:** (1) a flat grace swallows a rung
narrower than itself — at grace 25 the 15-min warning became eligible at
`dt + 10 min`, past the opening, where the 'open' branch takes over, so the most
time-critical rung had *no* cloud failover at all. Handing the failover the
second half of every window (round 2) restored reachability but broke the
ordering the grace exists for: half of a 15-min window is 7.5 min, ahead of the
owner's worst-case first firing at 15 min, so the cloud could beat the Mac to
the rung it was only meant to cover for. `_failover_eligible_at` now floors the
wait at the local firing interval and caps it at the rung's own width, which
settles every rung by construction — rungs wider than the interval keep the
whole grace, and the 15-min rung, exactly one interval wide with no slack to
share, is the owner's alone with the 'open' ping as its failover cover. The
tests read all three production numbers (offsets from `config.toml`, grace from
`watch.yml`, interval from the launchd plist) and assert the ordering for
offsets nobody has configured yet, so a new rung cannot reintroduce the race.
(2) Grace is temporal separation, not exclusion: `scripts/local-check.sh` ran
the watcher *before* pulling, so a Mac waking from sleep could not see a
reminder the cloud had sent and would re-send it — or, having marked a different
offset, conflict on the rebase and leave its commit unpushed (OTW-14's wedge).
The script now pulls before the run as well as after. (3) The ladder was worded
from the `now` captured before the Pathé/news/Cinesa block, which can burn
minutes on retries — a run starting at T-14 and reaching the ladder at T+1 would
send the 15-min warning after the sale had opened. The ladder re-reads the
clock; every other `now` stays the run-start reading so a single run's
bookkeeping still agrees with itself.
**Trap found while implementing:** the cadence guard's `return 0` — taken when
Pathé is fresh and Cinesa is off, and `config.toml` has `cinesa.enabled =
false` — sat *before* the reminder block, so the local half would have kept
swallowing the ladder on most firings. The check block is now guarded by that
same condition rather than returning, preserving the zero-network no-op while
letting control reach the reminders.
**Done when:** the local half sends reminders on a firing where the adaptive
guard skips Pathé and Cinesa is off; a grace larger than the elapsed time
suppresses a reminder for a failover caller and releases it once that time has
passed; no configured offset lets the failover act before the owner's worst-case
first firing, and none is left with no failover cover at all; grace never widens
the 'open' ping's 6 h cutoff, which stays anchored to the sale time; a reminder
reports the time actually left when it is actually sent; and the local half sees
the cloud's state before deciding what to send.
**Done (2026-09-04):** all of the above, with regression tests in
`tests/test_state.py` (grace semantics, including grace 0 ≡ the old call, the
owner-first ordering read from production, and the same ordering as a property
of the formula), `tests/test_notify.py` (countdown granularity and the
late-reminder wording) and `tests/test_main.py` (a `run()` firing that the
cadence guard used to return out of, and a slow run whose ladder must use the
clock it finished on). Verified by dry-run against the real `config.toml`: a
fresh Pathé check skips the network and still fires the reminder that is due,
worded from the actual remaining time.
**Residual risk:** the cloud can still duplicate a reminder the local half sent
but has not yet pushed — the window is the local run's own duration plus its
push, and the 25-min grace covers all but a pathological case. A cloud send
whose *own* push fails is likewise invisible to the Mac. A state-file merge
driver (OTW-14) repairs conflicting records but cannot undo duplicate messages
already sent. OTW-20 and OTW-21 address durable delivery and coordination;
the existing grace and two-sided pull remain necessary until that replacement
has been verified.

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

**Architecture link:** follow OTW-19, OTW-21 and OTW-20 so the cloud can select
the news job and share delivery ownership with the local half. Test overlapping
runs as well as a sequential cloud run followed by a local run. Cloud-safe
source selection must also cover configured extra pages, not just RSS URLs.

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

**Architecture link:** use OTW-19's supervision job and OTW-21's ownership
rules. After OTW-15, a cloud outage removes reminder failover and supervision;
local reminders can still run. Distinguish process completion, source health
and notification delivery health: a quiet successful workflow does not prove
its Telegram credentials work. OTW-22 should retain this reverse supervision.

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

### OTW-12 · `reminders_cover` can over-promise on two same-pass events
**Priority:** P3 · **Effort:** S
**Problem:** `detect.reminders_cover()` gates the "Reminders set: …" line on the
opening being the earliest future one and tickets not yet bookable. It agrees
with `due_reminders` in every ordinary case (verified by differential test over
five scenarios), but has two narrow disagreements, both needing two independent
Pathé events inside a single poll:
1. It reads `state["tickets_available"]`, which `analyze_pathe` sees one run
   stale — `update_from_snapshot` sets it afterwards. If one listing's sessions
   become bookable in the *same* pass that another first announces a future
   opening, the claim is made and the ladder is then switched off.
2. It derives "earliest future opening" from `snap.matched_shows`, while
   `update_from_snapshot` derives `sale_target` from `state["sales"]`, which
   never prunes slugs that left the catalogue. If a previously-seen listing
   with an earlier opening disappears from `/shows` while a later opening is
   announced in the same pass, the claim is made while the ladder still targets
   the vanished listing.
Both self-correct from the next pass on, and both are strictly narrower than
the unconditional promise they replaced (2026-09-02 review, PR #11).
**Fix:** Union the snapshot's openings with `state["sales"]` inside
`reminders_cover`, and take `tickets_available` from the snapshot being analysed
rather than from state.
**Done when:** the differential test in `tests/test_detect.py` is extended with
both same-pass scenarios and `reminders_cover` agrees with `due_reminders` in
each.

### OTW-18 · Validate state and make recovery explicit
**Priority:** P1 · **Effort:** M
**Problem:** `load_state` turns unreadable JSON into `DEFAULT_STATE` and renames
the original file. This loses alert/reminder memory and can resend historical
notifications; valid JSON with an invalid shape or unsupported version is not
validated either. Even a dry-run currently reaches this mutating recovery path.
**Fix sketch:** validate top-level and nested field types, supported versions,
and timestamps before any detection or delivery. Add explicit, tested migrations
for older supported schemas. Preserve the original evidence on failure and exit
with an actionable diagnostic instead of starting with empty dedup memory.
Provide a documented bootstrap path for a genuinely new installation and an
explicit recovery path for a damaged or missing production state. An older
backup may lack recent receipts, so do not silently restore it and resume sends.
Preserve existing dedup key formats and delivery records through migrations;
retain the tested legacy stale-key migration. Keep dry-runs read-only.
**Dependencies:** none; foundation for OTW-20 and OTW-21.
**Files:** `watcher/state.py`, `watcher/__main__.py`, `tests/test_state.py`,
`tests/test_main.py`; document bootstrap/recovery in `README.md`.
**Done when:** fixtures cover unreadable JSON, wrong nested types, unsupported
versions, older supported schemas, missing production state and explicit first
use. Invalid state causes no Telegram sends, no empty-state overwrite and no
dry-run mutation. Valid migrations retain all delivery history and are
idempotent; the documented recovery procedure reconciles receipts before
notifications resume. Ruff and pytest pass.

### OTW-19 · Split orchestration into bounded jobs
**Priority:** P2 · **Effort:** M
**Problem:** `run()` owns fetching, analysis, delivery, reminders and supervision.
Reminders wait behind all polling and retries; re-reading the clock fixes their
wording but cannot recover a warning window consumed by slow requests. News
selection is also tied to the Pathé cadence, complicating cloud news coverage.
**Fix sketch:** keep a thin CLI and extract explicit Pathé, news, Cinesa,
reminder and supervision jobs under one coordinator. Give polling aggregate
time budgets covering request retries, feed loops and token refresh. Check due
reminders before polling and recompute them after fresh observations, using
current time and selected-format evidence without sending a rung twice.
Keep state mutation coordinated; extracting jobs must not introduce concurrent
writers. Consume OTW-13's explicit health results. Preserve default cadence and
source selection until OTW-08 deliberately enables cloud news. Keep Pathé's
guard before its network activity, Cinesa's real headed-browser lifecycle,
and the existing local-owner/cloud-grace ordering.
**Dependencies:** OTW-13; OTW-18 supplies validated input state.
**Files:** `watcher/__main__.py`, proposed `watcher/runner.py` and
`watcher/jobs.py`, source clients and `watcher/state.py` as needed,
`tests/test_main.py`, `tests/test_state.py`; owner doc `docs/current-state.md`.
**Done when:** fake-clock tests prove slow or failing polling cannot consume an
entire reminder window, a newly discovered opening is evaluated in the same
run, and skipped/disabled jobs do not block reminders. Default remind mode
makes no source requests, cadence/grace tests still pass, Python 3.9 works,
and no framework is introduced. Ruff, pytest and an affected-flow dry-run pass.

### OTW-20 · Persist a notification outbox and delivery receipts
**Priority:** P1 · **Effort:** L
**Problem:** Telegram sends happen before the final state save. A later crash
can discard successful-send memory; retrying currently also depends on several
flags that freeze observation baselines. Local memory is not sufficient to
coordinate delivery with the cloud half.
**Fix sketch:** persist pending notification records before delivery and save
confirmed receipts immediately afterward through OTW-21's state boundary.
Separate current observations from pending work and delivered baselines;
alert-affecting baselines still advance only after confirmed delivery. Retain
every existing `Finding.key`; merged messages acknowledge all member keys
atomically. Store the non-secret delivery receipt, including the Telegram
message ID when available, and preserve the existing silent/loud policy.
Exclude tokens, chat IDs and raw Telegram responses from shared records.
Define expiry and supersession for moved openings, obsolete reminders and
changed availability so a recovered queue does not replay stale advice.
Design ownership and failover jointly with OTW-21. A timeout or crash can occur
after a send succeeds but before its receipt is saved: represent uncertain
outcomes explicitly and document their recovery policy. An outbox alone must
not be described as exactly-once delivery or cross-host exclusion.
**Dependencies:** OTW-18, OTW-19 and OTW-21; settle the shared delivery contract
during OTW-21 before implementing this item. OTW-08 follows it.
**Files:** `watcher/state.py`, `watcher/notify.py`, `watcher/coalesce.py`,
the runner/jobs from OTW-19, proposed `watcher/delivery.py`, related state,
notification and runner tests; owner doc `docs/current-state.md`.
**Done when:** fault-injection tests cover restart before send, confirmed send
followed by a later crash, uncertain send outcomes, failed receipt persistence,
partial merged-group failure and overlapping local/cloud attempts. Confirmed
receipts survive restart and synchronization; superseded work is retired
without changing historical keys; undelivered baselines stay eligible. The
remaining uncertainty policy is explicit. Ruff, pytest and a dry-run pass.

### OTW-21 · Separate deployment from runtime-state synchronization
**Priority:** P1 · **Effort:** L
**Problem:** `local-check.sh` uses the same checkout and pull/rebase cycle for
code deployment and live state shared with GitHub Actions. A state conflict can
wedge synchronization (OTW-14) and interfere with later code updates. Runtime
state mixes delivery history with mutable observations and health fields that
cannot all be merged by the same rule.
**Fix sketch:** introduce a small state-store/synchronization boundary and
separate code updates from runtime synchronization. Choose the simplest
transport that meets the contract; JSON/Git may remain behind the boundary,
with a separate state ref/worktree if needed. Assign ownership to observation
and health domains, merge coherent owner snapshots, and preserve confirmed
delivery records. Account for intentional removals such as recovery clearing
an error: generic recursive merging or taking the newest individual fields
can resurrect an outage or create an inconsistent snapshot.
Implement OTW-14's recovery without dropping unpushed receipts. Define local
overlap protection, code/schema compatibility, failed-push recovery, and the
delivery claim/failover contract used by OTW-20. A merge is reconciliation after
the fact, not permission for two workers to send simultaneously. Keep the
current pre-run synchronization and grace protections until their replacements
are verified; keep credentials out of all shared state and Git history.
**Dependencies:** OTW-18 and OTW-19; OTW-14 is the first implementation increment,
not a replacement ID. Design OTW-20's contract here and implement its outbox next.
**Files:** `scripts/local-check.sh`, `.github/workflows/watch.yml`,
`watcher/state.py`, proposed `watcher/sync.py`, new temporary-repository
integration tests; owner doc `docs/current-state.md`.
**Done when:** two temporary clones exercise conflicting receipts, owner-state
updates, error recovery, failed pushes, overlapping local invocations and a
code update while state synchronization is broken. No confirmed receipt is
lost; conflicts resolve or produce an actionable failure; code deployment is
independent of a clean runtime-state worktree. Tests document the remaining
send race and OTW-20's coordination contract without promising exactly-once
delivery. Ruff, pytest and dry-runs pass; live scheduling/deploy checks follow
the explicit-approval rules in `docs/verification.md`.

### OTW-22 · Move the local owner to an always-on residential host
**Priority:** P2 · **Effort:** L
**Problem:** laptop sleep stops source checks and the local reminder owner.
Cloud failover cannot fetch Pathé and its scheduled runs have measured long
gaps. Refactoring the watcher cannot make a sleeping laptop perform checks.
**Fix sketch:** use an owner-approved always-on machine on a residential
connection for the local jobs, retaining cloud supervision and reminder
failover. Confirm host OS compatibility with the scripts before choosing it;
scope any portability work explicitly. Cinesa is currently disabled: keep
that setting unless asked otherwise. If enabled, its token step still needs
the supported real headed Chrome/GUI environment, with no stealth, headless
replacement or challenge-solving. Provision credentials privately and keep
token caches private; add startup/restart handling and a cutover/rollback
procedure. Stop the old local owner, reconcile its final receipts, and then
synchronize verified state before enabling delivery on the replacement, so
there is never a second active local sender.
Retain OTW-09's reverse supervision and update host-specific diagnostics.
**Dependencies:** OTW-18, OTW-21 and OTW-09; may move earlier in the sequence
when those are ready and an approved host exists. Purchasing hardware and
changing production scheduling/cutover require the owner's explicit approval.
**Files:** `scripts/`, deployment/configuration instructions in `README.md`,
runtime ownership in `docs/current-state.md`, host-specific messages and tests
in the runner; never commit credentials or a replacement production state.
**Done when:** an approved cutover preserves dedup and reminder history, the
old laptop can remain asleep through an overnight observation period while
checks continue at the configured cadence, and restart recovery is verified.
Cloud supervision observes the new owner, reverse supervision remains active,
rollback is documented, and Cinesa's GUI step is verified if enabled. Ruff,
pytest, source dry-runs and the approved operational checks pass.

### OTW-23 · An uncaught save_state failure after delivery can re-send alerts
**Priority:** P2 · **Effort:** S
**Problem:** OTW-18 made `load_state`/`migrate_state` fail closed with a clean
diagnostic exit, but `__main__.run`'s call to `save_state` after delivery is
not wrapped: a `StateError` there (for example from a malformed upstream
timestamp reaching `_parse_timestamp`, which since OTW-18 requires a UTC
offset) exits with a traceback and never persists the run's updated dedup
memory. Alerts already sent in that run would then be re-sent on the next
firing, because the delivery baseline never reached disk. No evidence today
that Pathé emits such a timestamp (production has always been offset-aware),
so this is hardening rather than an active bug.
**Fix sketch:** catch `StateError` around the post-delivery `save_state` call
in `watcher/__main__.py`, log it, and exit with an actionable diagnostic
instead of an uncaught traceback. Consider also validating/dropping a
malformed `salesOpeningDatetime` at ingestion in `watcher/state.py` so a bad
upstream value cannot become fatal only after delivery.
**Files:** `watcher/__main__.py`, `watcher/state.py`, `tests/test_main.py`.
**Done when:** a test simulating a `StateError` from the post-delivery
`save_state` call exits with a clear diagnostic (no traceback) and the run's
already-sent alerts are not silently lost from the next diagnostic; ruff and
pytest pass.

### OTW-24 · Harden Cinesa leak tracking against a builder exception, and always persist state in CI
**Priority:** P3 · **Effort:** S
**Problem:** Raised in dual review (Claude + Codex) of OTW-19's Cinesa
Chrome-cleanup/leak-detection rework. Two narrow, non-blocking gaps:
1. `jobs.run_cinesa_job` calls `track_profile_leak(ctx, out, now, budget)`
   after the `try/except/else` that builds `CinesaOutcome`, not in a
   `finally`. If `detect.analyze_cinesa` or `build_cinesa_error_finding` ever
   raised, `_guard` would discard the whole outcome and the leak check for
   that run would never happen — a latent bug needing another bug to trigger,
   since both are pure builders over already-parsed data today.
2. `.github/workflows/watch.yml`'s "Persist state" step has no `if: always()`.
   A run that now legitimately exits 1 (a detected Cinesa leak, or any other
   guarded job failure) still saves useful state locally but skips the commit
   step, matching the old pre-OTW-19 behavior (an uncaught exception also
   skipped it) rather than regressing — but `if: always()` would be strictly
   better now that a failing run can carry state worth persisting.
**Fix sketch:** move the `track_profile_leak` call into a `finally` (or an
equivalent guarantee) around the Cinesa job body so leak reconciliation runs
even if outcome-building itself raises; add `if: always()` to the "Persist
state" step in `watch.yml` so a failing run's state still reaches origin.
**Files:** `watcher/jobs.py`, `.github/workflows/watch.yml`, `tests/test_main.py`.
**Done when:** a test simulating an exception from Cinesa outcome-building
still records/clears the leak episode; the workflow persists state on a
failing run; ruff and pytest pass.
