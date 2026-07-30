# Watch La odisea IMAX at Cinesa Diagonal Mar
Flow 3 (/codex-review) · fixer claude-opus-5/high · reviewer gpt-5.6-sol/xhigh · 2026-07-30
<!-- cross-review-loop-id: c27c8ce3-1def-4ff7-9c58-29e5259bb48e -->

## Task
Review PR #5 (branch `feat/cinesa-diagonal-mar-imax`): a second, independent
watch target — specific dates becoming bookable in IMAX at Cinesa Diagonal Mar
for a film already showing, plus IMAX leaving/returning. New watcher/cdp.py
(local Chrome DevTools token step) and watcher/cinesa.py (Vista OCAPI client),
wired into detect.py/state.py/config.py/notify.py/__main__.py, with tests in
tests/test_cinesa.py and tests/test_config.py. A prior, separate commit on the
same branch fixed an unrelated CI break (ruff 0.16.0 default-rule drift) and
added pytest.ini so the conductor's bare `pytest` test gate could resolve the
`watcher` package; those are tooling fixes, not part of the reviewed feature.

## Round 1 — VERDICT: REVISE
1. [P1] watcher/state.py:143 — `imax_absent_streak` grows unbounded after
   absence is confirmed, so `last_change`/state.json changes every firing while
   IMAX stays absent — FIXED in 3833fd2 (capped at the confirmation threshold).
2. [P1] watcher/__main__.py:329 — the Cinesa IMAX baseline advanced even when
   the Telegram send for that transition failed, silently losing the alert —
   FIXED in 3833fd2 (baseline now gated on confirmed delivery via already_sent).
3. [P1] watcher/__main__.py:297 — `failure_streak` grows unbounded after the
   alert threshold, causing a state commit every firing during an outage —
   FIXED in 3833fd2 (capped at the threshold).

NOTES:
- Create the token cache tmp file 0600 at creation rather than chmod'ing
  afterward, closing a brief world-readable window. [fixed] in 3833fd2.
- watcher/config.py's `cinesa_token_url` defaulted to "" when both `token_url`
  and `page_url` were omitted, contradicting the documented page_url fallback.
  [fixed] in 3833fd2.

## Round 2 — VERDICT: REVISE · re-review @ high
1. [P1] watcher/config.py:132 — the runtime default `silent_kinds` fallback
   omitted CINESA_TARGET_NO_IMAX, diverging from notify.DEFAULT_SILENT_KINDS —
   a config.toml omitting `[alerts] silent_kinds` would make the documented
   silent "bookable without IMAX" note buzz loudly — FIXED in ee10ae7
   (config.py now imports and reuses notify.DEFAULT_SILENT_KINDS directly,
   removing the duplicated literal instead of just patching it).

NOTES: none

## Round 3 — VERDICT: APPROVE · re-review @ high
Reviewer re-verified both prior fixes against the cumulative diff: counters
stabilize without state churn, the undelivered-IMAX-alert retry behaves
correctly, and the silent-kind defaults now share one source. No new findings.

## Outcome
Approved after 3 rounds; two rounds of real findings (state-churn/alert-loss
bugs, then a silent_kinds default drift), both fixed and re-verified. One
pre-existing, out-of-scope analog noted during the fix (Pathé's own
`failure_streak` has the same unbounded-growth shape, and its baseline update
is likewise unconditional after its send loop) — filed as backlog item
OTW-06 rather than fixed here, since it predates this PR. Eligible for merge
pending GitHub confirmation.
