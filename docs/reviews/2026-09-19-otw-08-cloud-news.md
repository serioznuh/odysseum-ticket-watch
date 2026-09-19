# OTW-08: cloud news coverage via remind --with-news, sharing dedup with the local half
Flow 2 (/codex-build) · builder gpt-5.6-sol/high · reviewer claude-opus-5/xhigh · 2026-09-19
<!-- cross-review-loop-id: 9363f432-7e9f-4723-809b-8377b5fd5d15 -->

## Task
OTW-08: cloud news coverage via remind --with-news, sharing dedup with the local half

## Round 1 — VERDICT: REVISE
Claude review: APPROVE. Codex double-review pass: REVISE.
1. [P1] watcher/runner.py:181 — cloud news is delivered without a remotely synchronized claim, so overlapping local and cloud runs can both send the same finding — DISPUTED: the OTW-20/OTW-21 delivery contract documents that the outbox is not a distributed lock and no alert ownership mechanism exists; sequential and synchronized-overlap cases are deduplicated and tested; limitation now stated in docs/current-state.md
2. [P1] watcher/news.py:57 — cloud extra-page filter rejected Pathé hosts but allowed Cinesa hosts, so a configured cloud page could make the cloud pass request Cinesa — FIXED in round 2 (cinesa.es and subdomains refused, tests added)
NOTES:
- [fixed] AGENTS.md said the cloud pass is remind-only; corrected in round 2.
- [accepted] the cloud news client sends default httpx headers and does not follow redirects, because redirects could bypass the host guard and the shipped extra-page list is empty; acceptable until a page is opted in.
- [accepted] simultaneous unsynchronized local and cloud runs can still both send, because the delivery layer is documented as not a distributed lock (OTW-20/OTW-21) and that limit is stated in current-state.md.
- [accepted] the cloud news helper returns an unused bool, which is acceptable because it is harmless and consistent with sibling jobs.

## Round 2 — VERDICT: APPROVE · re-review @ high
Claude re-review: APPROVE. Codex re-review: APPROVE (dispute on round 1 finding 1 accepted).
NOTES:
- [accepted] a cloud-side news grace could be added later if the residual duplicate window ever bites; it is out of scope because it would be a new ownership mechanism beyond OTW-08.

## Outcome
<!-- cross-review-merge-state: APPROVED -->
Approved after 2 rounds by Claude and Codex (double review). Done-when: met — remind --with-news runs only the news half plus delivery, never Pathé or Cinesa (Pathé and Cinesa hosts refused, feeds limited to Google News, extra pages cloud opt-in); a cloud-delivered finding is suppressed on the next local run and vice versa; quiet passes do not dirty state; plain remind is unchanged. Residual: simultaneous unsynchronized local and cloud runs can still both send, the documented OTW-20/OTW-21 limit, now stated in current-state.md.
