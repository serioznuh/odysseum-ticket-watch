# odysseum-ticket-watch — Backlog

**How to use:** every item has a stable ID (`OTW-nn`). In a new session, say
*"implement OTW-01 from backlog"* — each item is self-contained (problem, fix
sketch, file paths, done-when). IDs are never renumbered or reused; new items get the
next free number in whichever section fits. Completion is tracked **only** in the Done
column of the index table below.
An explicitly superseded item is closed with `[x]` and its replacement named;
parked or deferred items stay unchecked and state their restart condition.

Priorities: **P0** broken/urgent · **P1** high value · **P2** nice to have · **P3** someday.
Effort: S (≤ half day) · M (a day-ish) · L (multi-day).

## Index (stable ID order)

| ID | Title | Priority | Effort | Section | Done |
|----|-------|----------|--------|---------|------|
| OTW-01 | Docs-contract test in CI (superseded by OTW-37) | P2 | S | Infra, tooling & docs | [x] |
| OTW-02 | Add a linter (ruff) | P2 | S | Infra, tooling & docs | [x] |
| OTW-03 | 403 alert VPN wording wrong on manual CI dispatch | P3 | S | Bugs | [x] |
| OTW-04 | Cinesa alert: include session times + booking link | P2 | S | Features | [ ] |
| OTW-05 | Confirm Cinesa token behaviour with screen locked/asleep | P2 | S | Infra, tooling & docs | [x] |
| OTW-06 | Pathé failure_streak churns state; baseline can lose an alert | P2 | S | Bugs | [x] |
| OTW-07 | Stale-check alert says "Pathé" but the whole local half is down | P3 | S | Bugs | [x] |
| OTW-08 | Run the news half from the cloud pass to cover Mac-asleep windows | P1 | M | Features | [x] |
| OTW-09 | Supervision is one-directional — nothing watches the cloud half | P2 | M | Features | [x] |
| OTW-10 | Cinesa VPN 403 repeatedly launches headed Chrome | P1 | S | Bugs | [x] |
| OTW-11 | Make Cinesa Chrome refresh normally imperceptible | P2 | S | UX & design | [x] |
| OTW-12 | Keep reminder promises aligned with current observations (superseded by OTW-37) | P3 | S | Bugs | [x] |
| OTW-13 | A persistent per-listing Pathé failure is reported as healthy | P2 | S | Bugs | [x] |
| OTW-14 | An aborted state rebase can wedge the push until a human intervenes | P3 | S | Bugs | [x] |
| OTW-15 | Reminders ride a cloud cron that fires ~11% of its schedule | P0 | M | Bugs | [x] |
| OTW-16 | One run fans out a burst of near-identical alerts | P1 | S | Bugs | [x] |
| OTW-17 | A merged sale message mixing new and moved openings reads oddly (superseded by OTW-37) | P3 | S | UX & design | [x] |
| OTW-18 | Validate state and make recovery explicit | P1 | M | Infra, tooling & docs | [x] |
| OTW-19 | Split orchestration into bounded jobs | P2 | M | Infra, tooling & docs | [x] |
| OTW-20 | Persist a notification outbox and delivery receipts | P1 | L | Infra, tooling & docs | [x] |
| OTW-21 | Separate deployment from runtime-state synchronization | P1 | L | Infra, tooling & docs | [x] |
| OTW-22 | Move the local owner to an always-on residential host (superseded by OTW-39) | P2 | L | Infra, tooling & docs | [x] |
| OTW-23 | Validate source timestamps and handle final state-save failures | P2 | S | Bugs | [x] |
| OTW-24 | Guarantee Cinesa leak tracking when outcome-building raises | P3 | S | Bugs | [ ] |
| OTW-25 | Test legacy rebase recovery (superseded by OTW-21) | P3 | S | Infra, tooling & docs | [x] |
| OTW-26 | Bound runtime-state history fetched by ephemeral runners (superseded by OTW-41) | P3 | M | Infra, tooling & docs | [x] |
| OTW-27 | Cloud supervision trusts an unstable one-row Actions response and false-alerts | P0 | S | Bugs | [x] |
| OTW-28 | Coordinate Mac/cloud delivery before sending shared notifications | P1 | L | Infra, tooling & docs | [x] |
| OTW-29 | Bound Git operations and the local run's lifetime | P1 | M | Infra, tooling & docs | [x] |
| OTW-30 | Require explicit bootstrap when the shared runtime-state ref is missing | P2 | M | Infra, tooling & docs | [x] |
| OTW-31 | A blocked Pathé check stays silent for 6 h while a wanted date is pending | P1 | S | Bugs | [x] |
| OTW-32 | A GitHub outage holds back alerts only the Mac can produce (hedge if v2 slips) | P2 | S | Infra, tooling & docs | [ ] |
| OTW-33 | Cloud supervision polls GitHub every firing and is rate-limited a third of the day (superseded by OTW-39) | P2 | S | Bugs | [x] |
| OTW-34 | Per-firing liveness timestamps commit to the shared ref ~280 times a day (superseded by OTW-41) | P3 | S | Infra, tooling & docs | [x] |
| OTW-35 | Write the v1 switch-off checklist; Cinesa stays, disabled | P2 | S | Infra, tooling & docs | [ ] |
| OTW-36 | Let a workflow change be verified without touching production state (superseded by OTW-41) | P3 | S | Infra, tooling & docs | [x] |
| OTW-37 | Build v2 (`onsale-watch`): forked detection, single-writer runtime | P1 | L | Infra, tooling & docs | [ ] |
| OTW-38 | Snapshot tap: save what each Pathé poll saw, for the v2 shadow | P1 | S | Infra, tooling & docs | [x] |
| OTW-39 | External dead-man's switch for a dark Mac (Healthchecks.io) | P1 | S | Infra, tooling & docs | [x] |
| OTW-40 | Shadow-run v2 for 14 days and prove parity | P1 | M | Infra, tooling & docs | [ ] |
| OTW-41 | Take over with v2 and stop v1 | P1 | S | Infra, tooling & docs | [ ] |
| OTW-42 | Port the Cinesa source into v2 as a disabled adapter | P2 | M | Features | [ ] |

## Recommended next work (reviewed 2026-09-30)

The 2026-09-25 review found the live system healthy for its one remaining job,
the IMAX 70 mm alert for 19–20 December, and far larger than that job needs. On
2026-09-30 the owner decided to replace the runtime with a fork, v2
(`onsale-watch`), and to freeze v1 meanwhile. The order below protects the alert
first, then builds and proves v2, then hands over. The index above is the
completion record; estimates include implementation and verification.

| Order | ID | Work and reason for this position | Effort estimate |
| --- | --- | --- | --- |
| 1 | OTW-39 | Dead-man's switch on v1 now, inherited by v2: a dark Mac becomes loud within hours, and within the hour in December, instead of after 18 h. | S · about 2 h |
| 2 | OTW-38 | Snapshot tap on v1, so the v2 shadow replays real polls without adding Pathé traffic; it must run before the shadow starts. | S · about 3 h |
| 3 | OTW-37 | Build v2 in its own repository: forked Pathé and detection code, a new single-writer runtime. | L · 16–24 h |
| 4 | OTW-40 | Shadow-run v2 for 14 days against the parity gates; running by 2026-10-15. | M · about 6 h, plus 14 days |
| 5 | OTW-41 | Take over with v2 and stop v1; done by 2026-11-15. | S · about 4 h |
| 6 | OTW-35 | The v1 switch-off checklist that OTW-41 is run from; written while the shadow runs. | S · about 1 h |
| 7 | OTW-42 | Port the Cinesa source into v2 once a film is named, or after 2026-12-20. | M · 8–12 h |
| 8 | OTW-32 | Hedge only: built if a kill date is missed, so a GitHub outage cannot hold the wanted-date alert on v1. | S · about 4 h |

**Two tracks:** v1, this repository, protects the Dune alert until it is
delivered or v2 takes over. It receives only OTW-38 and OTW-39, plus OTW-32 if a
kill date is missed. v2 is tracked here until its repository exists (OTW-37);
from its first commit its work is tracked in its own backlog, and this one keeps
the v1 side and the hand-over.

**Why a fork:** measured on 2026-09-25, `watcher/` had 10,381 lines and `tests/`
12,809. About 4,170 code and 5,170 test lines keep the Mac and the cloud from
double-sending and move state through Git; 1,262 lines are the disabled Cinesa
half. All 35 Telegram messages in 81 days came from the Mac and the cloud sent
none; `outbox` and `reservations` were empty in all 2,329 state-ref commits. The
Actions cron fired 5–8 times a day, with a median gap of 177–277 minutes in its
last four weeks. Pushing to `main` deploys within one firing, so v1 cannot be
simplified in place in stages. Since then OTW-31 raised its first alert on
2026-09-29 (block from 15:57, alert at 16:29, recovery at 16:35), and the OTW-28
reservation path ran three times without a fault.

**Owner decisions (2026-09-30):** (A) Healthchecks.io's free plan as the
dead-man's switch; (B) a fork in a new repository, `onsale-watch`; (C) sends use
an intent record before the POST and a receipt after it, so at most one
duplicate ever and one silent note for an unknown outcome; (D) Cinesa stays in
place, disabled, and is ported to v2 by OTW-42; (E) v2 includes the reminder
ladder; (F) the kill dates below, a separate Telegram test bot for the shadow,
and the Mac kept awake from 2026-12-01.

**Kill dates and fallback:** the shadow (OTW-40) runs by 2026-10-15 and the
takeover (OTW-41) is done by 2026-11-15. If either date is missed, v2 is
abandoned for this watch: OTW-32 lands on v1, OTW-39 stays, and the items closed
below as superseded by OTW-37 or OTW-41 are reassessed as new items.

**Owner involvement:** run OTW-39's deploy check and deliberate-stop test;
approve the launchd and workflow steps of the takeover; from 2026-12-01 keep
the Mac awake and tighten the Healthchecks grace to 60 minutes. Already in
place since 2026-09-30: the `onsale-watch` repository (private, cloned at
`~/Documents/Projects/onsale-watch`); the shadow's test bot, whose credentials
are in `~/.onsale-watch-shadow.env` and delivered a test message; the
Healthchecks.io check with Telegram linked (its test notification arrived) and
`HEALTHCHECK_PING_URL` in `~/.ticket-watch/.env`. Everything else is built and
merged by the review loops under the approval list in AGENTS.md.

**Loop safety:** in `serioznuh/cross-llm-review`, CR-112 and CR-113 (merged
2026-09-24) removed the failure behind OTW-28's merge, which passed its approval,
merge and backlog gates with an uncommitted review-log edit. No `watch.yml`
change is planned before the workflow is disabled at the takeover, so OTW-36 is
closed.

**Change freeze:** pushing to `main` deploys to the Mac. From 2026-12-10 until
both wanted dates have passed, merge only P0 fixes, on whichever version owns
the alert.

**Deferred:** OTW-04 and OTW-24 stay open until the Cinesa port (OTW-42). They
are built on v1 only if a Cinesa film is named while v1 still owns the watch.

**Closed as superseded (2026-09-30):** OTW-01, OTW-12 and OTW-17 by OTW-37;
OTW-22 and OTW-33 by OTW-39; OTW-26, OTW-34 and OTW-36 by OTW-41. Each item
states why. Earlier: OTW-25's legacy rebase path no longer exists; OTW-21
already covers the replacement synchronization mechanism with real-Git tests.

## 1. Critical — security & breakage

## 2. Bugs

### OTW-27 · Cloud supervision trusts an unstable one-row Actions response and false-alerts
**Priority:** P0 · **Effort:** S
**Problem:** The first production firings after OTW-09 deployed on 2026-09-21
sent two loud, false "Cloud checks have stopped" alerts six minutes apart.
Actions was healthy: scheduled run `35569229823` had succeeded that morning.
The exact anonymous request in `watcher/cloud.py` asks for
`event=schedule&status=success&per_page=1`, then treats `workflow_runs[0]` as
the newest success. GitHub's endpoint does not document that ordering, and the
request returned inconsistent old rows: 2026-09-13 on the 10:16 local firing,
then 2026-09-06 on the 10:22 firing; an authenticated listing and an anonymous
ten-row listing both showed the current run. Each old timestamp also produced
a fresh `cloud_stale:{last_success}` key, so dedup turned the changing bad
evidence into repeated alerts rather than containing it. This violates the
watcher's precision-first alert policy and can buzz every five minutes.
**Fix sketch:** Do not infer an outage from the first row of an unordered
response. Query a bounded `created` window covering the 18-hour health
threshold, request enough rows for every possible scheduled firing in that
window, validate each row, and treat any qualifying success as proof of
health. If no recent success exists, obtain historical context separately or
word the alert as a bounded absence rather than claiming an unverified exact
"last" run. Key/dedup the outage episode independently of whichever historical
candidate the API happens to return, and re-arm only after positive recovery.
API errors, malformed/partial pages and contradictory results must fail quiet.
**Files:** `watcher/cloud.py`, `watcher/jobs.py`, `watcher/alerts.py`,
`tests/test_cloud_supervision.py`.
**Done when:** fixtures reproducing both anonymous responses above cannot
raise an alert while a recent scheduled success exists; alternating old rows
during a real outage produce one alert for the episode; a confirmed recovery
re-arms the next outage; API uncertainty stays silent; ruff and pytest pass.

### OTW-17 · A merged sale message mixing new and moved openings reads oddly
**Priority:** P3 · **Effort:** S
**Disposition:** closed as superseded by OTW-37 on 2026-09-30. This watch's sale
openings passed on 2026-09-09, so the wording is not worth a v1 change. v2 forks
the merging code unchanged, so the quirk moves to v2's backlog as a known
low-priority item. The scope below is historical.
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

### OTW-31 · A blocked Pathé check stays silent for 6 h while a wanted date is pending
**Priority:** P1 · **Effort:** S
**Problem:** `alerts.record_pathe_failure` raises its loud blind/degraded alert
only when the failure streak reaches `failure_streak_threshold` **and** no fully
healthy snapshot exists for 6 h (`is_check_fresh(st, 6.0, now)`). The 6 h was
sized for the 4 h baseline cadence. While a wanted date is pending,
`state.adaptive_staleness_hours` returns 0 and every 5-min firing checks Pathé,
yet a block of up to 6 h still says nothing. Production log: on 2026-09-18
`/api/shows` answered `403 Forbidden` from 19:29 to 21:48 (27 failed checks) and
no alert was sent; on 2026-09-03 a block ran from about 05:00 to 21:46. The
19–20 December IMAX 70 mm sessions can open at any time, and the usual cause —
Akamai refusing the Mac's current route, such as a VPN — is something the owner
can fix within minutes once told.
**Fix sketch:** derive the blind tolerance from the active cadence tier instead
of a fixed 6 h. While `adaptive_staleness_hours` is at the every-firing floor (a
pending wanted date or the opening window), alert once consecutive failed
firings span about 30 minutes; slower tiers keep the 6 h rule. Reuse the
existing loud alert and its 403/VPN wording, one alert per episode, the silent
recovery and the degraded→blind re-arming; add no new alert kind. Ship with a
30-minute default; if it proves noisy, raise it later — no sign-off needed.
**Files:** `watcher/alerts.py`, `watcher/state.py` or `watcher/jobs.py` for the
tier, `config.toml` if the tolerance becomes configurable, `tests/test_main.py`,
`README.md` alert catalogue, `docs/current-state.md`.
**Done when:** with a wanted date pending, failures spanning 30 minutes send
exactly one loud alert and a continuing outage stays quiet; a baseline-tier
outage keeps the 6 h rule; recovery re-arms and degraded→blind still escalates;
wanted-date alerts are unchanged. Ruff, pytest and an affected-flow dry-run pass.

### OTW-33 · Cloud supervision polls GitHub every firing and is rate-limited a third of the day
**Priority:** P2 · **Effort:** S
**Disposition:** closed as superseded by OTW-39 on 2026-09-30. The external
dead-man's switch replaces Actions-based supervision, and v2 has no cloud half.
A rate-limited firing answers "unknown" and the next answering firing settles
it, so this was noise rather than a blind spell: 279 warnings from 2026-09-21 to
09-25, 3 from 09-26 to 09-30. v1 keeps polling until it is switched off
(OTW-41). The scope below is historical.
**Problem:** `jobs.run_cloud_supervision_job` calls
`cloud.has_successful_scheduled_run` on every 5-min firing — 12 unauthenticated
GitHub API requests an hour, by design without a credential. The unauthenticated
limit (60 an hour) is shared by everything on the owner's public IP, and since
2026-09-21 the job logged `403 rate limit exceeded` 238 times (101 of 288 firings
on 2026-09-22, 105 on 2026-09-23). Each one is "unavailable (no alert)", so the
supervision OTW-09/OTW-27 built is silently blind about a third of the day. Its
threshold is `cloud.stale_hours = 18`; polling every 5 minutes buys nothing.
**Fix sketch:** poll at most once per configurable interval (30–60 min is ample
for an 18 h threshold) and reuse the last conclusive answer in between. After a
rate-limit refusal, wait until the reset time GitHub returns before asking
again. Keep the throttle bookkeeping in a local git-ignored cache under
`.cache/`, never in shared state, so it adds no ref commits. Unchanged: any
recent success proves health, uncertainty stays quiet, and a proven 18 h absence
alerts once until positive recovery. Stay credential-free unless throttling
proves insufficient.
**Files:** `watcher/cloud.py`, `watcher/jobs.py`, `config.toml`,
`tests/test_cloud_supervision.py`, `docs/current-state.md`.
**Done when:** twelve consecutive firings make at most one API request per
interval; a rate-limited answer defers the next request until its reset; the
alert/recovery decisions in the existing supervision tests are unchanged; a
simulated shared-IP exhaustion no longer blinds more than one interval. Ruff,
pytest and an affected-flow dry-run pass.

## 3. Features

### OTW-04 · Cinesa alert: include session times + booking link
**Priority:** P2 · **Effort:** S
**Scheduling:** deferred. Cinesa stays in place, disabled (owner decision
2026-09-30), and this item is built as part of the v2 port (OTW-42). Build it
here only if a Cinesa film is named while v1 still owns the watch (estimate:
3–4 h including verification).
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

### OTW-42 · Port the Cinesa source into v2 as a disabled adapter
**Priority:** P2 · **Effort:** M (8–12 h including a live verification)
**Scheduling:** deferred. Start when the owner names a Cinesa film, or after
2026-12-20, whichever comes first. If a film is named before the takeover
(OTW-41), this item moves ahead of it.
**Problem:** the owner will watch future releases at Cinesa Diagonal Mar
(decision 2026-09-30: Cinesa stays in place, disabled, and is not retired). v1
can do that today from `[cinesa]` in `config.toml`. After the takeover v1 is off
and v2 has no Cinesa source, so until this port Cinesa cannot be enabled by
configuration alone.
**Fix sketch:** port `watcher/cinesa.py`, `watcher/cdp.py` and their tests
(`tests/test_cinesa.py`, `tests/test_cdp.py`) into v2 as one adapter behind the
OTW-37 contract: the `cinesa_target:<site>:<film>:<date>` facts and the silent
no-IMAX note, IMAX gone and back with the two-check confirmation, and its own
health states (blind, token step stuck). Keep every boundary in AGENTS.md: a
real headed Chrome on a throwaway profile, never headless; no stealth, TLS
impersonation, cookie replay or CAPTCHA solving; the 12 h token is a credential
in a git-ignored 0600 cache and never reaches state, logs or commits; an empty
snapshot is a blip, never evidence; no per-run timestamp in state. Fold in
OTW-24 (leak reconciliation runs even when outcome-building raises) and OTW-04
(session times and a booking link in the target-date alert). A new film is then
one `[[targets]]` table: film id, title, page URL and dates.
**Files:** the v2 repository; here only the closing notes on OTW-04 and OTW-24.
**Done when:** with a real film on sale, an enabled target mints a token through
headed Chrome, reads the calendar and raises the expected findings in a dry
run; disabled, the adapter makes no request and launches no browser; the ported
tests pass; OTW-04 and OTW-24 are closed here with their v2 references.

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
**Disposition:** closed as superseded by OTW-37 on 2026-09-30. v1 is frozen until
it is switched off (OTW-41) and v2 starts with its own small docs, so a
docs-contract test here no longer pays for itself. The scope below is
historical.
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

### OTW-12 · Keep reminder promises aligned with current observations
**Priority:** P3 · **Effort:** S
**Disposition:** closed as superseded by OTW-37 on 2026-09-30. The ladder is
dormant for this watch: `formats_seen` already holds the wanted format, so
`due_reminders` returns nothing even for a new opening. v2 derives the promise
and the ladder from one rule. The scope below is historical.
**Problem:** `detect.reminders_cover()` reads delivered format evidence from
the state before the current snapshot advances it. On 2026-09-21 a synthetic
snapshot with a future opening and newly bookable IMAX 70mm reproduced a
"Reminders set" promise followed by an empty reminder ladder after the
observation was applied. The wording and effective scheduler policy disagree.
**Scope update:** the original second scenario — a withdrawn listing retained
in `state["sales"]` pinning the ladder — is already fixed by OTW-20:
`update_from_snapshot` derives `sale_target` from current observations and
preserves an older target only when evidence is incomplete. Do not apply the
old suggestion to union historical `sales` back into the target calculation.
**Fix sketch:** derive the promise and effective ladder from the same
selected-format, post-observation policy. Preserve delivered-baseline gates
and distinguish complete observations from degraded/unknown evidence; a
failed fetch must not invent either availability or withdrawal. Keep existing
dedup keys, reminder offsets and local/cloud grace semantics.
**Files:** `watcher/detect.py`, `watcher/state.py`, `watcher/jobs.py` if needed,
`tests/test_detect.py`, `tests/test_state.py`, `tests/test_main.py`.
**Done when:** differential tests cover same-pass selected-format booking,
booking in another format, an authoritatively withdrawn earlier listing and
incomplete observations that preserve an older target. Announcement wording
agrees with the resulting reminder policy in each case; the already-fixed
withdrawal behavior stays intact; ruff, pytest and an affected-flow dry-run pass.

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
**Follow-up:** OTW-28 owns prevention of concurrent Mac/cloud sends; this
completed item preserves receipts and documents the race but does not close it.

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
**Follow-ups:** OTW-28 adds delivery coordination, OTW-29 bounds transport and
local-run lifetime, and OTW-30 makes missing-ref bootstrap explicit.

### OTW-22 · Move the local owner to an always-on residential host
**Priority:** P2 · **Effort:** L
**Disposition:** closed as superseded by OTW-39 on 2026-09-30. Hosting stays the
owner's MacBook (constraint restated 2026-09-25: no new hardware), and the
dead-man's switch makes a dark laptop loud instead. File a new item if another
host becomes available. The scope below is historical.
**Scheduling:** parked by the owner on 2026-09-21. The current MacBook is the
only available host. Resume only after another approved host becomes available;
this item does not block the active queue. Re-estimate effort for that host.
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
**Dependencies:** OTW-18, OTW-21 and OTW-09; scheduling additionally requires
another approved host. Purchasing hardware and
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

### OTW-23 · Validate source timestamps and handle final state-save failures
**Priority:** P2 · **Effort:** S
**Problem:** `runner.execute` still calls the final `save_state` without
handling validation or filesystem failures, so the CLI can end with an
uncaught traceback. A synthetic `salesOpeningDatetime` without a UTC offset
was accepted into observation state and rejected only on save during the
2026-09-21 assessment. There is no evidence that production Pathé responses
currently contain that value; this is hardening of an untrusted input boundary.
**Scope update:** OTW-20 persists confirmed receipts immediately, so an ordinary
failure of the final bookkeeping save no longer discards those receipts or
automatically re-sends delivered alerts. Failed receipt writes already recover
as `uncertain`; preserve that precision-first policy.
**Fix sketch:** reject malformed or offset-free source timestamps before they
enter persisted observations or delivery decisions. Treat the invalid value
as unknown/degraded evidence, not proof that an opening was withdrawn. Handle
expected final-save failures with an actionable diagnostic and non-zero exit;
preserve the last validated file and never reset dedup state on failure.
**Files:** `watcher/runner.py`, `watcher/state.py`, `watcher/detect.py` or the
source boundary as needed, `watcher/__main__.py`, `tests/test_main.py`,
`tests/test_state.py`, `tests/test_delivery.py`.
**Done when:** tests cover malformed and offset-free source timestamps plus
validation/filesystem failure at the final save. The CLI reports a clear error
without an uncaught traceback, confirmed receipts remain recoverable, uncertain
attempts are not automatically replayed, and rejecting a bad date cannot retire
a valid existing reminder. Ruff, pytest and an affected-flow dry-run pass.

### OTW-24 · Guarantee Cinesa leak tracking when outcome-building raises
**Priority:** P3 · **Effort:** S
**Scheduling:** deferred. Cinesa stays in place, disabled (owner decision
2026-09-30), and this guarantee is built into the v2 port (OTW-42). Build it
here first if Cinesa is re-enabled on v1 before the takeover.
**Problem:** `jobs.run_cinesa_job` calls `track_profile_leak` after the
`try/except/else` that builds `CinesaOutcome`. If `detect.analyze_cinesa` or an
alert builder raises, `_guard` discards the outcome and that run skips leak
reconciliation. This is a narrow exceptional path; Cinesa is currently disabled.
**Already covered:** the second issue originally tracked here is fixed:
`.github/workflows/watch.yml` runs "Synchronize runtime state (after)" with
`always() && inputs.dry_run != true`. Preserve that behavior; no new CI
persistence implementation is needed.
**Fix sketch:** guarantee leak reconciliation even when outcome-building raises,
using `finally` or an equivalent structure. Ensure the episode bookkeeping
survives the runner discarding a failed outcome; merely updating a discarded
result is insufficient. Continue surfacing the original job failure.
**Files:** `watcher/jobs.py`, `watcher/runner.py` if needed, `tests/test_main.py`
or `tests/test_budget.py`; retain existing workflow behavior.
**Done when:** injected analysis and error-builder exceptions still record or
clear the leak episode correctly, the run reports the job failure, and existing
post-failure synchronization and dry-run behavior remain intact. Ruff, pytest
and an affected-flow dry-run pass. Complete before future Cinesa use.

### OTW-25 · Exercise OTW-14's rebase recovery against a real git rebase, not just a fake-Git test double
**Priority:** P3 · **Effort:** S
**Disposition:** closed as superseded by OTW-21; removed from the active queue
on 2026-09-21. The original scope below is historical and should not be
implemented against a code path that no longer exists.
**Problem:** Raised in dual review (Claude + Codex) of OTW-14's state-rebase
recovery. `tests/test_state_merge.py`'s shell-boundary test exercises
`scripts/local-check.sh`'s conflict-detection and merge-invocation logic
against a fake `git` shim, not a real conflicting rebase — so the actual
`git rebase`/`--continue`/`--skip`/`--abort` stage sequence, and cleanup on a
genuinely failed merge, are unverified by the automated suite (they were
verified manually, once, during review).
**Fix sketch:** add an integration test that builds two temporary git
repositories (or one repo with divergent branches) that produce a real
`state/state.json`-only rebase conflict, runs `scripts/local-check.sh`'s
recovery path against it with real `git`, and asserts the conflict resolves,
`--skip` is taken when appropriate, and an unresolvable conflict aborts
cleanly with the local commit intact.
**Files:** `tests/test_state_merge.py` or a new `tests/test_local_check.py`.
**Done when:** a real (not faked) conflicting rebase — including a resolvable
case and an unresolvable one — is exercised in the test suite, without
depending on the developer's own git config; ruff and pytest pass.
**Superseded (2026-09-17):** OTW-21 replaced the `git pull --rebase`-based
state-conflict mechanism this item targeted with a dedicated `runtime-state`
git ref synchronized via plumbing (never rebased), so `scripts/local-check.sh`
no longer has a rebase-recovery code path to test. `tests/test_sync_integration.py`
(added by OTW-21) already exercises the equivalent real-git scenarios —
conflicting receipts, failed pushes, overlapping invocations — against the
new mechanism.

### OTW-26 · Bound runtime-state history fetched by ephemeral runners
**Priority:** P3 · **Effort:** M
**Disposition:** closed as superseded by OTW-41 on 2026-09-30. The state ref is
retired at the takeover; it held 3,552 commits on 2026-09-30 and every runner
still fetches it whole. The scope below is historical.
**Problem:** Raised in dual review (Claude + Codex) of OTW-21. The dedicated
`refs/heads/runtime-state` ref carries `state.json`, synchronized locally to
`.cache/state-sync/state.json`. `watcher/state_sync.py` fetches it without a
depth bound, so each fresh Actions checkout downloads its entire growing
history. A changed merged payload creates another commit; unchanged syncs do
not. Download cost therefore grows with state history over the ref's lifetime.
**Scope clarification:** bound history transferred to ephemeral runners.
Shallow fetch does not prune history retained on the remote; remote retention
is a separate concern. The previous force-push compaction sketch conflicts
with the repository's preserve-pushed-history rule and is not the default fix.
**Fix sketch:** fetch only a bounded recent history for the state ref while
preserving normal fast-forward pushes, race/retry behavior and the local
`base.json` used for three-way reconciliation. Keep code-branch deployment
independent. Verify the chosen shallow strategy with real temporary Git
repositories, including repeated fetches and a competing push.
**Reference:** [Git fetch depth documentation](https://git-scm.com/docs/git-fetch).
**Files:** `watcher/state_sync.py`, `.github/workflows/watch.yml` if needed,
`tests/test_sync_integration.py` and relevant state-sync tests.
**Done when:** with a fixed current snapshot size, a fresh runner downloads a
bounded number of state-history commits regardless of the ref's age. Tests
cover a shallow initial fetch, later syncs, rejected concurrent pushes and
retry without losing confirmed receipts. Remote history is preserved, code
deployment remains independent, and ruff, pytest and an affected-flow dry-run
pass. Estimate: about one day including Git integration verification.
**Update (2026-09-24):** in practice the payload changes on every firing while a
wanted date is pending — the liveness timestamps — so the ref grew by 250–280
commits a day (OTW-34). A fresh runner still fetched the whole ref in about 1 s
(688 KiB), so this stays low urgency; revisit after OTW-34, or if that fetch
exceeds a few megabytes.

### OTW-28 · Coordinate Mac/cloud delivery before sending shared notifications
**Priority:** P1 · **Effort:** L (2–3 days including fault-injection tests)
**Problem:** the Mac and cloud can read the same unsent finding before either
publishes its receipt. `delivery._attempt` persists a claim only to that host's
local JSON; the other host can independently send the same message. Merging
receipts afterward preserves history but cannot undo the duplicate. This was
explicitly reproduced by
`test_overlapping_hosts_preserve_both_attempt_receipts_without_exactly_once_claim`
in `tests/test_delivery.py` (renamed by OTW-28 to
`test_uncoordinated_overlapping_hosts_preserve_both_attempt_receipts`, which now
covers uncoordinated hosts only) and is a documented limitation of completed
OTW-20/OTW-21, exercised by OTW-08's two news readers.
**Fix sketch:** establish authoritative permission to send before Telegram is
called. Prefer the existing shared-state transport if an atomic reservation
can satisfy the contract; ordinary three-way merging of local claims or a
time delay alone is insufficient. Arbitrate on logical member keys and their
existing episode identities, not only the merged message's ID: hosts may
group the same finding differently. A losing or unconfirmed reservation must
not send; keep definitely-unsent work pending without advancing its baseline.
Cover fresh findings and outbox recovery on both hosts, including reminder
failover when both hosts are eligible. Preserve cloud news while the Mac is
asleep, the existing reminder grace, member-key acknowledgement, and the
silent/loud policy. Bound coordination waits using OTW-29 and honor OTW-30's
missing-state guard. Existing keys and confirmed receipts must survive rollout.
Specify crash, failed-sync, wake-from-sleep and ownership-transfer behavior.
A lease expiring does not prove its previous holder stopped or that Telegram
rejected a request: prevent a resumed stale sender from racing a replacement,
and quarantine ambiguous attempts rather than automatically taking them over.
Keep the existing precision-first `uncertain` policy; do not promise exactly-once
delivery across an ambiguous Telegram response. No extra host is required.
**Dependencies:** OTW-20, OTW-21 and OTW-08 are complete. Implement OTW-29 and
OTW-30 before enabling shared reservations; OTW-22 is not a dependency.
**Files:** `watcher/delivery.py`, `watcher/state_sync.py`, `watcher/state.py`,
`watcher/state_merge.py`, runner/jobs and startup wrappers as needed;
`tests/test_delivery.py`, `tests/test_sync_integration.py`, reminder and news
tests; owner doc `docs/current-state.md`.
**Done when:** two independent temporary clones starting from the same state
and racing before either receipt is published make at most one mocked Telegram
call per logical notification, including differently grouped overlapping
findings. Tests cover rejected/unknown claim pushes, failed pre-run sync,
definite send failure, crashes before/after reservation and send, uncertain
receipt writes, clock skew, Mac wake/resume, and a stale holder after attempted
takeover. Known-unsent work remains safely retryable, ambiguous work stays
quarantined, confirmed member receipts survive reconciliation, and independent
news/reminder work still runs on the eligible host. Ruff, pytest and dry-runs
pass; live sends or scheduling changes follow the existing approval rules.

### OTW-29 · Bound Git operations and the local run's lifetime
**Priority:** P1 · **Effort:** M (about 1 day)
**Problem:** OTW-19 bounds source polling inside the watcher, but the local
wrapper first runs `git pull` and state synchronization under a process lock.
Neither `state_sync._git` nor the `locked` child wait has a timeout, and the
shell's deployment pull is also unbounded. A live but hung Git child can stop
the watcher before its first reminder check and hold the lock across later
firings. Cloud supervision can eventually report the outage but cannot resume
Pathé checks. The cloud job already has an eight-minute overall cap; this
does not protect the local wrapper.
**Evidence:** the 2026-09-21 isolated check used a stalled temporary Git shim:
the first locked run stayed waiting and a second firing skipped as overlapping.
The missing lifetime bound was previously accepted in the OTW-21 review.
**Fix sketch:** add bounded network/process waits for code deployment and both
state syncs, plus a documented overall local-run deadline and cleanup allowance.
Use the existing Python stdlib boundary rather than requiring a platform-specific
`timeout` binary. Preserve code-first deployment and the mandatory pre-run sync;
on timeout follow the existing fallback and notification policy, subject to
OTW-28's send-ownership rules and OTW-30's state-integrity guard. Classify Git
transport timeouts as transport failures without a new noisy alert path.
Terminate and reap only the invocation's owned process tree before releasing
the overlap lock; never delete a lock file to let a second writer race a live
first writer. Retain saved receipts and pending work when post-run sync stalls;
an interrupted send must retain its conservative `uncertain` recovery behavior.
**Dependencies:** OTW-19 and OTW-21 are complete. This supplies bounded waits
for OTW-28; it does not require OTW-22 or a cadence change.
**Files:** `scripts/local-check.sh`, `watcher/state_sync.py`,
`tests/test_state_sync.py`, `tests/test_sync_integration.py`; owner doc
`docs/current-state.md`.
**Done when:** controlled hung children exercise deployment, pre/post-run sync
and the overall watchdog. Each ends within the configured bound plus cleanup,
returns an actionable non-zero result, leaves validated state and confirmed
receipts intact, and allows the next firing to acquire the lock only after the
old process tree has stopped. A transient timeout respects existing alert
gating; healthy deployment, sync and reminder behavior remain intact. Ruff,
pytest and an affected-flow dry-run pass on the supported Python/macOS setup.

### OTW-30 · Require explicit bootstrap when the shared runtime-state ref is missing
**Priority:** P2 · **Effort:** M (about 1 day)
**Problem:** when `refs/heads/runtime-state` is absent and a fresh runner has no
local live file, `state_sync.synchronize` silently loads the frozen tracked
`state/state.json` seed and pushes a new state ref. It cannot distinguish a
genuinely new installation from deletion of an established production ref.
Receipts newer than the seed disappear from that runner's view, so historical
alerts can become eligible again. OTW-18's explicit recovery requirement does
not currently protect this OTW-21 bootstrap path.
**Evidence:** a 2026-09-21 real-Git check in temporary repositories published
a confirmed delivery, removed only the temporary remote state ref, and started
a fresh clone. Normal sync succeeded and recreated the ref without that alert
or delivery receipt. This is a reproduced recovery hazard, not evidence that
production state has been lost.
**Fix sketch:** normal scheduled synchronization must not silently create a
missing shared ref from the seed. Require an explicit initialization operation
for a genuinely new installation, separate from owner-approved recovery of an
existing one. On confirmed ref absence, preserve local live/base files, report
the condition, and prevent ordinary delivery from using an unverified seed;
startup wrappers must honor this even where pre-run sync currently allows
continuation after transport failure. Keep temporary transport failure and
confirmed absence distinct. Recovery must reconcile available confirmed
receipts and uncertain attempts from surviving stores before delivery resumes;
never treat an older seed or backup as proof that an alert was not sent.
Use normal history-preserving writes and leave an independently created remote
ref intact if initialization/recovery races another host. Document the operator
procedure without performing a production reset as part of implementation.
**Dependencies:** OTW-18 and OTW-21 are complete. This guards the startup state
used by OTW-28 and does not depend on OTW-22.
**Files:** `watcher/state_sync.py`, `watcher/__main__.py` if needed,
`scripts/local-check.sh`, `.github/workflows/watch.yml`,
`tests/test_state_sync.py`, `tests/test_sync_integration.py`, startup tests;
owner doc `README.md` for bootstrap/recovery instructions.
**Done when:** a fresh clone facing a deleted state ref cannot silently reseed
or send historical alerts; an existing clone preserves its live/base evidence.
Explicit first-time initialization works, concurrent creation is safe, and a
tested recovery preserves confirmed receipts and quarantined uncertainty before
normal sends resume. Transport outages remain distinct and the existing safe
fallback is preserved. Ruff, pytest and isolated dry-runs pass; production
state edits remain subject to the existing explicit-approval rule.

### OTW-32 · A GitHub outage holds back alerts only the Mac can produce
**Priority:** P2 · **Effort:** S (narrowed on 2026-09-30, about 4 h)
**Problem:** since OTW-28 every Telegram send first wins a reservation pushed to
`refs/heads/runtime-state`: no confirmed reservation, no send. While GitHub — or
only the Mac's route to it — is unavailable, nothing goes out. That includes
alerts only the Mac can produce, because the cloud never calls Pathé or Cinesa:
sale, new-listing, bookable and wanted-date findings, and Cinesa findings. For
those keys the only possible duplicate is the cloud recovering an outbox record
the Mac has already published. A GitHub incident in December would therefore
delay the one alert the watch exists for, while preventing no possible duplicate.
**Decision (2026-09-24, by default):** implement. It narrows OTW-28's rule for
one provable class of work only. If the tests cannot prove the premise below,
close this item as won't-do rather than weaken OTW-28.
**Decision (2026-09-30):** narrowed and demoted to a hedge. v2 (OTW-37) has no
reservation and so no GitHub dependency before a send. Build this item only if
the v2 shadow (OTW-40) is not running by 2026-10-15 or the takeover (OTW-41) has
not happened by 2026-11-15. Narrowed scope: the local owner sends a delivery
whose member keys are all wanted-date keys (`pathe_target:`) without a
reservation, keeping today's outbox, `sending`/`uncertain` transitions and
receipts; a cloud pass refuses to deliver a `pathe_target:` key, in fresh
findings and in outbox recovery alike. No publication marking. Narrowed done
when: with the state ref unreachable, a fresh wanted-date finding is sent once
on its first attempt and its receipt is kept; every other key still needs a
reservation; a cloud pass never sends a `pathe_target:` key; OTW-28's two-clone
tests still make at most one mocked Telegram call per logical notification. The
wider sketch and done-when below are historical.
**Fix sketch:** let a delivery skip the reservation only when all of these hold:
(a) this pass is the local owner; (b) every member key is Mac-origin (Pathé- or
Cinesa-derived); (c) its outbox record has never been part of a push attempt.
Mark a record "possibly published" before any post-run sync or reservation push
that could carry it, and treat unknown as published. Everything else keeps the
reservation, including a merged message that also carries shared work (news,
reminders). Enforce the premise in code instead of assuming it: a cloud pass,
including a manual check-mode dispatch, refuses to deliver Mac-origin keys. The
skip keeps today's outbox, `sending`/`uncertain` transitions and receipts.
**Files:** `watcher/delivery.py`, `watcher/coalesce.py` if grouping needs a
flag, `watcher/state_sync.py` (publication marking), `watcher/jobs.py`;
`tests/test_delivery.py`, `tests/test_sync_integration.py`;
`docs/current-state.md`.
**Done when:** with the state ref unreachable, a fresh Pathé finding is sent once
on its first attempt while news and reminders stay pending; once its record may
have been pushed it needs a reservation again; a cloud pass never delivers a
Mac-origin key; OTW-28's two-clone races still make at most one mocked Telegram
call per logical notification. Ruff, pytest and dry-runs pass.

### OTW-34 · Per-firing liveness timestamps commit to the shared ref ~280 times a day
**Priority:** P3 · **Effort:** S
**Disposition:** closed as superseded by OTW-41 on 2026-09-30. The commits are
harmless for the weeks v1 has left, and the ref is retired at the takeover. The
scope below is historical.
**Problem:** while a wanted date is pending, every 5-min firing runs a Pathé
check, so `last_check_ok` and `last_catalogue_ok` change on every firing and the
post-run sync commits and pushes them. `refs/heads/runtime-state` received 1,933
commits from 2026-09-17 to 2026-09-24 — 250–280 a day, each changing only those
two lines. AGENTS.md forbids exactly this for Cinesa (a per-run timestamp in
shared state). Each such push also competes with OTW-28's reservation pushes on
the same ref, so a cloud claim more often loses the compare-and-swap and has to
retry or decline.
**Fix sketch:** publish liveness at a bounded resolution. When a sync's only
difference from the ref is a liveness timestamp that moved by less than N
minutes, skip the push (N about 30, far below the 18 h staleness thresholds).
The live file keeps full precision for the cadence guard, and merges keep the
later timestamp. Receipts, baselines, reservations, outbox changes and a
liveness age past N still push in the same firing.
**Files:** `watcher/state_sync.py`, `watcher/state_merge.py` if merge rules
change, `tests/test_state_sync.py`, `tests/test_sync_integration.py`,
`docs/current-state.md`.
**Done when:** twelve firings that change only liveness produce at most one push
per N minutes; any other change still pushes in its firing; the cloud's
staleness check sees liveness no older than N; the Mac's cadence guard and
OTW-28's reservation tests are unchanged. Ruff, pytest and an affected-flow
dry-run pass. Cuts OTW-26's growth about tenfold.

### OTW-35 · Write the v1 switch-off checklist; Cinesa stays, disabled
**Priority:** P2 · **Effort:** S
**Decision (2026-09-30):** rescoped by the owner's decisions. (1) Cinesa is not
retired: it stays in place and disabled in v1 and is ported into v2 by OTW-42;
OTW-04 and OTW-24 stay open until then. (2) The end-of-watch behaviour moves to
v2 (`watch_until`, OTW-37). (3) What remains here is the v1 side of the
takeover: a checklist in README for stopping v1, reviewed before OTW-41 runs.
**Fix sketch:** document, in order: unload the LaunchAgent
`com.odysseum.ticket-watch`; disable `watch.yml`; what stays untouched until
2026-12-21 for rollback (the `~/.ticket-watch` clone, the plist file, the
`runtime-state` ref); the rollback steps and their caveat (once v2 has sent a
real alert, its sent keys must be merged into v1's state first, or v1 repeats
them); the Cinesa note above. The unload and the workflow change need the
owner's approval (AGENTS.md).
**Fallback:** if v2 is abandoned at a kill date, the watch ends on v1 in the
simplest form: on 2026-12-21 the owner unloads the LaunchAgent and disables
`watch.yml` by hand. No dormancy code is added to v1 during the freeze.
**Files:** `README.md`, `docs/current-state.md`.
**Done when:** the checklist is in README within its line budget and names every
owner-approved step, and OTW-41 has been run from it or the fallback has been
executed.
**Original scope (2026-09-24), kept for reference:**
**Problem:** the watch has a fixed end. Its remaining Pathé purpose is the IMAX
70 mm alert for `target_dates` 2026-12-19/20. The Cinesa target (*La odisea* at
Diagonal Mar) passed in August, and `cinesa.enabled = false` since. Nothing ends
either watch. After 20 December the launchd job would keep firing every 5
minutes, Pathé checks would continue at the post-ticket cadence, the Actions
cron and weekly heartbeat would continue, and the state ref would keep growing.
Meanwhile the dormant Cinesa half still carries the headed-Chrome token step, a
Chrome profile and a credential cache under `.cache/`, two open items (OTW-04,
OTW-24) and a large share of AGENTS.md and current-state.md.
**Fix sketch:** (1) Cinesa — default: retire it when the Pathé watch ends
(remove the half, its caches and docs; close OTW-04/OTW-24 as superseded). Keep
it only if the owner names a new target before then; OTW-24 then stays a
prerequisite. (2) Pathé — once every wanted date has passed, send one silent
"watch complete" summary, then go dormant on its own: no network call and no
message, heartbeats included. That is the whole stop; nothing waits for the
owner. Unloading the LaunchAgent and disabling `watch.yml` afterwards is
optional cleanup, documented in README and run by the owner whenever convenient
(both are launchd/workflow changes under AGENTS.md).
**Files:** `watcher/jobs.py`/`watcher/runner.py` (the dormant check),
`config.toml`, `README.md` (procedure), `docs/current-state.md`; `AGENTS.md`,
the Cinesa modules and their tests if Cinesa retires.
**Done when:** a dry run with the clock after 2026-12-20 makes no network call
and sends only the one-time summary; the decommission procedure is documented
and reviewed; the Cinesa disposition is recorded in this backlog. Ready by
2026-12-15; from then on nothing in the stop needs the owner.

### OTW-36 · Let a workflow change be verified without touching production state
**Priority:** P3 · **Effort:** S
**Disposition:** closed as superseded by OTW-41 on 2026-09-30. No `watch.yml`
change is planned before the workflow is disabled at the takeover; OTW-39
changes only the local script. The scope below is historical.
**Problem:** a change to `.github/workflows/watch.yml` can only be exercised by a
live run, which synchronizes — and may push — the shared `refs/heads/runtime-state`
ref and may send Telegram messages. So a workflow change ends with a request for
an owner-approved "live Actions check" (OTW-28 did), and no loop can test such a
change on its own.
**Fix sketch:** add a `sandbox` input to `workflow_dispatch` that runs the whole
job against a scratch copy of shared state. The before and after syncs read the
real ref but write only to a throwaway ref (for example
`refs/heads/runtime-state-sandbox-<run id>`, deleted at the end), and the
watcher runs with `--dry-run`. A loop can then dispatch its branch in sandbox
mode and read the result instead of asking the owner. The scheduled path and its
cron stay unchanged.
**Files:** `.github/workflows/watch.yml`, `watcher/state_sync.py` (state-ref
override), `tests/test_state_sync.py`, `README.md`.
**Done when:** a sandbox dispatch of a branch completes the whole job with no
Telegram call and no write to `refs/heads/runtime-state`; the scratch ref is
removed afterwards; scheduled runs are unchanged. Ruff, pytest and one sandbox
dispatch pass.

### OTW-37 · Build v2 (`onsale-watch`): forked detection, single-writer runtime
**Priority:** P1 · **Effort:** L (16–24 h including verification)
**Problem:** the watch has one job left, the IMAX 70 mm alert for 2026-12-19/20,
which needs under 600 lines of logic. On 2026-09-25 v1 was 10,381 code and 12,809
test lines: about 4,170 code and 5,170 test lines keep the Mac and the cloud from
double-sending and move state through a Git ref, and 1,262 lines are the disabled
Cinesa half. In 81 days all 35 Telegram messages came from the Mac, the cloud sent
none, and `outbox` and `reservations` were empty in all 2,329 state-ref commits.
Pushing to `main` deploys within one firing, so v1 cannot be simplified in place
in stages; the runtime is replaced in a fork while v1 stays frozen.
**Decisions (owner, 2026-09-30):** a new repository, `serioznuh/onsale-watch`
(private, created 2026-09-30 with a README and a .gitignore only); no cloud half
and no shared state; an intent record before each send and a receipt after it;
the reminder ladder is included; Cinesa is not part of this item (OTW-42).
**Fix sketch:**
- *Fork unchanged, with their tests:* `watcher/pathe.py` (request headers, three
  retries with backoff, the permanent `403 "No movie allowed !"` on event
  listings as a healthy refusal, the Akamai `{"error":"Error from IP …"}` body as
  a block, slug-pattern discovery of extra listings on the cinema programme);
  from `watcher/detect.py` `classify_format`, `target_date_findings`,
  `usable_source_timestamp` and the sale-date, new-listing and news matching;
  `watcher/coalesce.py`; from `watcher/notify.py` the send (429 `retry_after`,
  HTML escaping without quotes, `disable_notification`) and the renderers; the
  cause wording in `watcher/alerts.py` (`summarize_pathe_error`, `pathe_cause`).
  The wanted-date alert has never fired in production, so these rules are proven
  only by v1's fixtures (`tests/test_pathe_targets.py`, `tests/test_pathe.py`,
  `tests/test_detect.py`, `tests/test_coalesce.py`, `tests/test_notify.py`): do
  not rewrite them.
- *New runtime, about 500 lines:* one process per launchd firing, every 5
  minutes; one flock; one local JSON state file; no Git at run time.
- *Facts, not baselines:* every run recomputes all facts from the snapshot; an
  alert is a fact whose key is not in `sent`. Key strings stay byte-identical to
  v1's (`pathe_target:…`, `sale:…`, `new_show:…`, `tickets:…`, `cinema_listed:…`,
  `news:…`), and same-run findings that are one piece of news stay one message.
- *Sending:* write an intent, POST, write the receipt; a merged message covers
  every member key or none. An intent without a receipt found on a later run is
  never re-sent automatically and raises one silent note naming its keys. A
  definite failure clears the intent, so the next firing retries.
- *Health:* per target, OTW-31's rule (30 minutes of consecutive failed polls
  while a wanted date is pending or an opening is near, 6 h otherwise), one loud
  alert per episode, silent recovery. A liveness ping to the OTW-39 check after
  every completed firing, whatever the poll result; each ping's HTTP result is
  recorded and six consecutive ping failures raise one silent note. A weekly
  silent heartbeat names the last acknowledged ping.
- *Reminders:* 24 h, 2 h, 15 min and an opening-time ping from the observed
  opening of the selected format; single owner, no failover.
- *End of watch:* `watch_until = "2026-12-20"`; after it, one silent summary,
  then no network call and no message.
- *Modes:* `--dry-run`; `--replay DIR` over OTW-38 files; `--shadow`, which never
  calls Telegram for the owner's chat and logs would-send lines. Live mode
  refuses to start without the `imported_from_v1` marker in its state, or while
  the v1 LaunchAgent `com.odysseum.ticket-watch` is loaded.
- *Targets:* `[[targets]]` tables in `config.toml`. A source type is one adapter
  returning keyed facts and a health result, with one fixture test. The contract
  must fit a source with its own credential step (Cinesa, OTW-42) without being
  built for it.
- *Request pattern:* keep v1's unless parity is proven. The wanted-date flag is
  in the 74 KB `/api/cinema/{slug}/shows`, while each poll also downloads the
  785 KB `/api/shows`; caching the catalogue for up to an hour is adopted only
  if the shadow shows identical decisions.
- *Carried over:* the production clone lives outside `~/Documents` (macOS TCC),
  at `~/.onsale-watch`; secrets are env-only; every alert names film and cinema
  on its first line; times are Paris time; no stealth, TLS impersonation, cookie
  replay or CAPTCHA solving, ever.
**Files:** the new repository, with its own AGENTS.md, README and backlog; v2
work is tracked there from its first commit. Nothing in this repository changes.
**Done when:** the forked fixtures pass unchanged in v2; a replay of captured v1
snapshots sends nothing; a snapshot mutated so that `2026-12-19` is bookable on
the IMAX 70 mm listing yields exactly one loud message with key
`pathe_target:cinema-pathe-odysseum:dune-troisieme-partie-50828:imax70:2026-12-19`;
fault-injection tests cover a crash after the POST (no resend, one note), a
definite failure (retry on the next firing) and a failed state write; the import
guard and the v1-agent guard refuse live mode; a dry run dated after
`watch_until` makes no request; ruff and pytest pass on Python 3.9. About 2,400
code lines in total, about 1,900 of them forked; a build far above that comes
back to the owner.

### OTW-38 · Snapshot tap: save what each Pathé poll saw, for the v2 shadow
**Priority:** P1 · **Effort:** S (about 3 h)
**Problem:** the v2 shadow (OTW-40) must compare its decisions with v1's on the
same inputs without adding Pathé traffic, which would raise the risk of an
Akamai block. v1 keeps no record of what a poll returned: `run_pathe_job` builds
a `detect.Snapshot` in memory and drops it (`watcher/jobs.py`). Saving the raw
catalogue on every poll would cost about 220 MB a day (785 KB, about 280 polls).
**Fix sketch:** after each Pathé poll of a real run, record it under the
git-ignored `.cache/pathe-snapshots/`. A healthy or degraded poll writes one
gzipped JSON file with the poll time, the matched catalogue entries, the cinema
programme payload (74 KB) and each per-listing result (health, diagnostic,
showtimes body), but only when that content differs from the previous file. An
unchanged poll and a failed poll each append one line to `index.log` (time, and
"same" or the error summary), so a replay can reproduce every decision,
including blind spells. Delete files older than 30 days and stop writing above
200 MB. The tap must never affect the alert path: any error in it is logged as a
warning and swallowed, it touches no state, and dry runs write nothing.
**Files:** `watcher/jobs.py` or a small `watcher/tap.py`, `tests/test_main.py` or
a new `tests/test_tap.py`, `docs/current-state.md`.
**Done when:** a healthy poll writes one file; an identical next poll writes none
and appends "same"; a failed poll appends its error; an unwritable or full
directory leaves alerts, state and the exit status unchanged; rotation and the
size cap work; a saved file rebuilds a `detect.Snapshot` that yields the same
findings as the original; dry runs write nothing. Ruff, pytest and an
affected-flow dry-run pass.

### OTW-39 · External dead-man's switch for a dark Mac (Healthchecks.io)
**Priority:** P1 · **Effort:** S (about 2 h, plus the owner's setup)
**Problem:** the wanted-date alert can only come from the Mac. When the Mac
sleeps or the job dies, the only signal today is the cloud's stale alert after
18 h without a catalogue check, sent by an Actions cron that fired 5–8 times a
day with gaps up to 11.5 h (measured to 2026-09-25). On the night of
2026-09-29/30 the lid was closed from 22:45; Power Nap still ran 41 healthy
checks, 10–38 minutes apart, and nothing was due to alert. A night without
those dark wakes would have stayed silent for 18 h.
**Decision (owner, 2026-09-30):** use Healthchecks.io's free plan with its
Telegram integration.
**Fix sketch:** ping a Healthchecks.io check at the end of every completed
firing, healthy or not: the ping says "the job ran", and Pathé health stays with
the local rule (OTW-31), so one block does not alert twice. In
`scripts/local-check.sh`, send the ping just before the final `exit "$status"`,
after the post-run sync; the hard stops (exits 3, 5 and 6) and a watchdog kill
leave before that line and so do not ping, which turns a firing that cannot
deliver into a loud condition. The request is bounded (10 s); its failure is
logged and never changes the exit status. The URL is a secret:
`HEALTHCHECK_PING_URL` in the git-ignored `.env`, never in `config.toml` or the
log; when it is unset the script behaves exactly as today. Owner-side settings:
period 5 minutes; grace 3 hours until 2026-12-01, then 60 minutes, which still
tolerates the closed-lid night above. Its messages come from Healthchecks.io's
own bot and carry only the check's name, so the check is named after the watch
("Dune ticket watch (Mac)"). The Telegram integration cannot mute recoveries,
so each dark episode also ends with one "is now UP" message; README says so.
The same URL moves to v2 at the takeover (OTW-41); v2 in shadow never pings this
check.
**Owner actions:** done on 2026-09-30: the account; the check `Dune ticket watch
(Mac)` with period 5 minutes and grace 3 hours; the Telegram link, switched on
for that check, with its test notification received; and
`HEALTHCHECK_PING_URL` in `~/.ticket-watch/.env`, which matches that check. The
next firing sourced the file and ran normally. The owner approved the script
change by asking for this item on 2026-09-30. Still needed once it is deployed:
the deploy check (docs/verification.md, scheduling checks), which shows the
check's first ping, and the deliberate-stop test in Done when.
**Files:** `scripts/local-check.sh`, the wrapper tests in
`tests/test_sync_integration.py` or `tests/test_state_sync.py`, `README.md`,
`docs/current-state.md`.
**Done when:** with the variable set, a completed firing sends exactly one ping;
a firing that stops on exit 3, 5 or 6 sends none; a failing or hanging ping
delays the firing by at most 10 s and changes neither its exit status nor state;
with the variable unset nothing changes; the URL never reaches the log; a test
pins the ping's position. Owner-side: a deliberately stopped job produces one
Telegram message after the grace. Ruff and pytest pass; the deploy check follows
docs/verification.md.

### OTW-40 · Shadow-run v2 for 14 days and prove parity
**Priority:** P1 · **Effort:** M (about 6 h, plus 14 days elapsed)
**Problem:** v2 must not own the alert before it is proven, and the alert that
matters (`pathe_target:`) has never fired, so "both versions stayed silent"
proves nothing on its own.
**Fix sketch:** install v2 at `~/.onsale-watch` with its own LaunchAgent in
`--shadow` mode: its own state file, no write to v1's store or to
`refs/heads/runtime-state`, no ping to the production Healthchecks check. Its
`.env` holds only the owner's separate test bot (token and chat id from
`~/.onsale-watch-shadow.env`), never the production bot's token, so the shadow
cannot reach the production chat whatever its code does. Inputs: a replay of the
OTW-38 files, plus one live programme poll per hour (about 1% of v1's Pathé
traffic). All gates are required:
1. *Replay:* for every tapped poll, v2's would-send keys equal what v1 sent
   (v1's log and `alerts`), including "nothing".
2. *Fixtures:* v1's Pathé, detection, merging and notify fixtures pass against
   the forked code in v2's repository.
3. *Injection:* a tapped snapshot mutated so that `days["2026-12-19"].bookable`
   is true on the IMAX 70 mm listing goes through v2 end to end with Telegram
   mocked and yields exactly one loud message with v1's key; the same for
   2026-12-20, and for both dates at once (one merged message); a snapshot with
   only 2026-12-15 yields none.
4. *Health:* on a Pathé block, live or replayed from `index.log`, v2's blind and
   recovered verdicts match v1's within one firing.
5. *Own client:* the hourly live poll succeeds with v2's headers, and one real
   message reaches the owner through the separate test bot.
6. *Import rehearsal:* v2 loads a copy of the live state's keys and sends
   nothing on the next replay.
**Kill date:** running by 2026-10-15; otherwise v2 is abandoned for this watch
and OTW-32 lands on v1.
**Done when:** 14 consecutive days with every gate green and no unexplained
divergence, recorded in a short parity report in v2's repository. A fix to
forked detection or to sending restarts the 14 days.

### OTW-41 · Take over with v2 and stop v1
**Priority:** P1 · **Effort:** S (about 4 h)
**Problem:** exactly one sender may exist at any time, and nothing v1 already
sent may be sent again.
**Fix sketch:** one sitting, from the OTW-35 checklist. (1) Let a v1 firing
finish, then unload the LaunchAgent `com.odysseum.ticket-watch`. (2) Disable
`watch.yml`. (3) Read the final `state.json` from `refs/heads/runtime-state` and
import its `alerts` keys, `reminders_sent` rungs and `last_heartbeat` into v2's
state together with the `imported_from_v1` marker; a dry run must show nothing
pending. (4) In v2's `.env`, replace the test bot's token with the production
`TELEGRAM_BOT_TOKEN` and add `HEALTHCHECK_PING_URL`; switch v2's LaunchAgent
from shadow to live, kickstart it, and confirm one ping and a quiet first run.
(5) Leave v1's clone, its plist file and the state ref untouched until
2026-12-21. Steps 1, 2 and 4 need the owner's approval (AGENTS.md: launchd and
workflow changes). Rollback: unload v2, reload v1, re-enable the workflow. It is
clean until v2 has sent a real alert; after that, v2's sent keys must first be
merged into v1's state, an owner-approved state edit, or v1 repeats them.
**Kill date:** done by 2026-11-15; otherwise v2 is abandoned for this watch and
OTW-32 lands on v1. From 2026-12-10 only P0 fixes, on whichever version owns the
alert. From 2026-12-01 the owner keeps the Mac awake and sets the Healthchecks
grace to 60 minutes.
**Done when:** v1's agent is not loaded and `watch.yml` is disabled; v2 is live,
its state holds the marker and every v1 key, and its first live run sends
nothing; the dead-man's check shows v2's pings; the rollback steps are in v2's
README; this backlog's remaining open items are closed or moved to v2's backlog.
