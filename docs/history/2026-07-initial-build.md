# 2026-07 — Initial build and hardening

## 2026-07-06 — Watcher created, then split into hybrid

- Initial Dune 3 ticket-sale watcher for Pathé Odysseum: Pathé JSON API polling
  (`salesOpeningDatetime` as the key early signal), Google News RSS leads,
  Telegram alerts, dedup state (`1349fd1`).
- Discovered Akamai blocks Pathé's API from GitHub datacenter IPs (403 from
  Actions, 200 from home, same code). Split into the hybrid runtime: local
  daily Pathé check + cloud reminder/supervision pass sharing
  `state/state.json` via git (`1049e22`).
- Moved the production clone to `~/.ticket-watch` — macOS TCC blocks launchd
  agents under `~/Documents` (`36da591`).

## 2026-07-07 — Precision and cadence hardening

- Tightened news matching: sale wording required; format keywords (70mm/IMAX)
  only count together with a venue mention (`dcf5e48`); raised news alerts to
  `min_confidence=medium`, date-bearing leads only (`e27e7c4`). Driven by the
  no-noisy-alerts rule.
- Notification loudness tiers: quiet kinds (news leads, heartbeat, recovery)
  sent with `disable_notification` (`5c615c1`).
- Added a 15:00 retry slot with a freshness guard (`915ae75`), then replaced
  fixed slots with adaptive cadence — ≈4 h baseline tightening to every launchd
  firing around the announced opening (`af37205`), with the guard running
  before any network/git activity so idle firings are zero-network (`b1b0a6b`).
- README restructured for first-time readers with a config reference table
  (`138960c`).

## 2026-07-18 — Documentation standard adopted

- Instantiated the two-tier docs standard (grown tier): AGENTS.md operating
  contract, CLAUDE.md pointer stub, stable-ID backlog (`OTW-nn`),
  docs/current-state.md, docs/verification.md, this history, docs/reviews/.
