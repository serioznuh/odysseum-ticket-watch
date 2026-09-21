# OTW-27: stop cloud supervision false alerts from an unstable Actions response
Flow 2 (/codex-build) · builder gpt-5.6-sol/high · reviewer claude-opus-5/xhigh (+ Codex gpt-5.6-sol/xhigh double review) · 2026-09-21
<!-- cross-review-loop-id: 3b02f997-0861-4e60-a10d-f7e4329dca94 -->

## Task
OTW-27: stop cloud supervision false alerts from an unstable Actions response

## Round 1 — VERDICT: REVISE
Claude review: APPROVE. Codex review: REVISE.
- [P1] watcher/cloud.py:105 — page completeness was checked before inspecting returned runs, so a valid recent success in a partial response was discarded as unknown and the prior outage was never re-armed — FIXED in 90e7f65
NOTES (Claude):
- stale_hours above ~24h disables supervision (latent, shipped config is 18h) [accepted]
- Design relies on GitHub honoring the created= filter; failure direction is quiet [accepted]
- A single empty complete page is still authoritative absence; the episode key limits it to one alert [accepted]
- isdigit vs isdecimal on the episode suffix; unreachable, only this code writes those keys [accepted]
- BACKLOG Done checkbox not ticked; ticked by the post-merge backlog reconciliation [accepted]

## Round 2 — VERDICT: APPROVE · re-review @ high
Claude review (loop-reviewer-light): APPROVE. Codex review (gpt-5.6-sol/high): APPROVE.
NOTES: none

## Outcome
<!-- cross-review-merge-state: APPROVED -->
Approved after 2 rounds; eligible for merge pending GitHub confirmation. Done-when: met — both anonymous-response fixtures stay silent while a recent success exists, alternating old rows during an outage give one episode alert, confirmed recovery re-arms, API uncertainty stays silent, ruff and pytest pass.
