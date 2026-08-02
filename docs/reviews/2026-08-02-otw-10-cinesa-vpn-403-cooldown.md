# OTW-10: distinguish 401 vs 403 in Cinesa API, add Chrome-launch cooldown for 403s
Flow 2 (/codex-build) · builder gpt-5.6-luna/max · reviewer claude-opus-5/xhigh · 2026-08-02
<!-- cross-review-loop-id: 380a858f-7f17-4361-9731-69c61ca842dd -->

## Task
OTW-10: distinguish 401 vs 403 in Cinesa API, add Chrome-launch cooldown for 403s
## Round 1 — VERDICT: APPROVE
NOTES:
1. [accepted] cinesa.py:349-353 — double-403 per firing during active cooldown; harmless, no behavior fix needed.
2. [accepted] cinesa.py:212/284 — up to 2 Chrome launches/hour via proactive-refresh + forced-mint paths; within OTW-10 spec (at most once per cooldown window ≥60 min).
3. [accepted] cinesa.py:212-218 — cooldown check precedes proactive refresh; rare edge (final hour of expiring token under active cooldown) yields bounded delay with VPN guidance shown.

## Outcome
Approved after 1 round; eligible for merge pending GitHub confirmation.
