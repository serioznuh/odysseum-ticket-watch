# OTW-13: surface persistent per-listing Pathe failures, fix cloud supervision blind spots
Flow 2 (/codex-build) · builder gpt-5.6-sol/high · reviewer claude-opus-5/xhigh · 2026-09-16
<!-- cross-review-loop-id: 6ad7f752-cd4e-4123-952a-236832d9539d -->

## Task
OTW-13: surface persistent per-listing Pathe failures, fix cloud supervision blind spots

## Round 1 — VERDICT: REVISE
Architecture: the FetchResult/FetchHealth result type (data/health/diagnostics, distinguishing authoritative-empty, unexpected-failure and expected-refusal) is the right, proportionate foundation — stdlib only, both traps (origin_refusal, show_detail failures) handled correctly. One foundation gap on the state side: degradation reuses the blind channel (last_check_ok frozen, shared error_alerted), making degraded-but-alive and dark-Mac states indistinguishable to cloud supervision, and making a degraded-then-blind escalation mutually exclusive with re-alerting.
FINDINGS:
1. [P1] watcher/__main__.py:241-276 — build_stale_finding's partial branch asserted catalogue checks still work, but freezing last_check_ok while degraded makes a degraded-but-alive Mac and a dead/asleep Mac produce an identical cloud-supervision fingerprint, recreating the healthier-than-reality failure OTW-13 targets. [fixed] in b3f8efd
2. [P1] watcher/__main__.py:204-218 — record_pathe_failure gated on the single shared error_alerted, so an escalation from degraded to full catalogue blindness after the degradation alert fired raised nothing. [fixed] in b3f8efd

## Round 2 — VERDICT: APPROVE
Architecture: round-2 fix is foundational, not symptomatic — a genuinely separate liveness signal (last_catalogue_ok, schema v2→v3 migration) distinguishes degraded-but-alive from dark, and error_alerted re-arms on a strict partial→blind transition so escalation always re-alerts. Verified directly against the live production state.json: migration is lossless, no spurious stale alert on deploy, no Finding.key format changed.
NOTES:
1. A Pathe incident alternating between blind and degraded across successive runs could re-alert roughly every 10 min in the worst case; every message stays truthful and a 6h gate delays the first one — accepted as a trade-off, not a defect. [accepted]
2. A persistent degradation pins Pathe/news polling to the 5-min floor via adaptive_staleness_hours; moot under current config since target dates are pending and it already returns 0.0. [accepted]
3. is_check_stale had no remaining caller in watcher/. [fixed] in 04fca8d
4. The partial-degradation marker string was duplicated as two independent literals in __main__.py and detect.py. [fixed] in 04fca8d
5. build_recovered_finding labels recovery from only the last last_error, so a blind-then-partial-recovery outage reports 'Degraded' — cosmetic. [accepted]
6. Bumping CURRENT_STATE_VERSION to 3 fails closed loudly for at most one run if a half is still on v2 code, matching the v1→v2 precedent. [accepted]
7. refresh_catalogue_liveness adds up to ~24 state writes/day during a persistent degradation (vs ~288 for a per-run timestamp); deliberate and documented. [accepted]

## Outcome
<!-- cross-review-merge-state: APPROVED -->
Done-when: met

A persistent per-listing failure is now visible without reading logs (heartbeat names the affected endpoint/slug, plus loud local and cloud supervision alerts), and tests cover 'catalogue healthy + one listing failing forever' not reporting unqualified health, plus the round-2 escalation and degraded-vs-dark scenarios. Ruff and pytest pass. Approved after round 2 plus one no-new-round quick-fix pass (04fca8d) addressing NOTES 3 and 4. Eligible for merge pending GitHub confirmation.