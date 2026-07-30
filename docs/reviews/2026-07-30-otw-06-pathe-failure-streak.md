# OTW-06: cap Pathe failure_streak, gate one-shot alert baselines on delivery
Flow 2 (/codex-build) · builder gpt-5.6-sol/high · reviewer claude-opus-5/xhigh · 2026-07-30
<!-- cross-review-loop-id: 7cc042f6-14ee-4e19-aa63-cb734f0c0cbb -->

## Task
OTW-06: cap Pathe failure_streak, gate one-shot alert baselines on delivery

## Round 1 — VERDICT: APPROVE
NOTES:
1. [accepted] sales/sale_target remain ungated by design (OTW-06's own fix sketch excludes them), so a failed SALE_DATE send is still lost permanently; mitigated by the existing reminder ladder. Reviewer flagged this may deserve its own backlog item — logged as a possible follow-up, not actioned this loop.
2. [accepted] A rare coincidence (one delivered TICKETS_AVAILABLE alert + one undelivered one-shot finding in the same run, followed by a shrinking format set) could repeat an already-relevant "tickets bookable" alert once — same shape as the already-approved Cinesa gate; not a regression.
3. [accepted] build_error_finding's streak count in its message can understate the true failure count once capped (cosmetic, single alert per outage).

## Required verification
<!-- cross-review-required-verification: none -->

## Outcome
Approved after 1 round; eligible for merge pending GitHub confirmation.