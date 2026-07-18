# odysseum-ticket-watch — agent instructions

Telegram watcher that alerts (in advance) when tickets for *Dune : Troisième partie*
go on sale at Pathé Odysseum (Montpellier, IMAX 70 mm), then counts down to the
opening. Single user: the repo owner. Runs as a hybrid: local launchd job on the
owner's Mac (full Pathé + news check) + GitHub Actions every 15 min (reminders +
supervision). It never buys tickets.

Stack: Python 3.9+ stdlib + `httpx`, no framework. Package `watcher/` (entry:
`python -m watcher`), config in `config.toml`, dedup/reminder state in
`state/state.json` (committed by both halves), launchd bits in `scripts/`.

## Commands

- Lint: `.venv/bin/ruff check .`
- Tests: `.venv/bin/python -m pytest -q` (currently 44 passing)
- Manual run: `source .env && .venv/bin/python -m watcher --mode check --dry-run`
- Telegram smoke test: `source .env && .venv/bin/python -m watcher --test-telegram`
- Secrets are env-only: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` — locally in a
  git-ignored `.env` (export lines), in CI as repo secrets. Never in config.toml.

## Current system

Read [docs/current-state.md](docs/current-state.md) for the active project state.

Core facts agents need before editing:

- The **production clone is `~/.ticket-watch`**, not this working copy — launchd
  can't run from `~/Documents` (macOS TCC). Changes reach production via
  `git pull` there; pushing to `main` is deploying.
- Pathé's API is **blocked from GitHub datacenter IPs** (Akamai 403). Anything
  touching `www.pathe.fr` runs locally; the scheduled cloud pass is remind-only
  and never calls Pathé (a manual `check` dispatch would, and gets 403'd).
- Both halves **commit `state/state.json` to `main`** (`[skip ci]`). Always
  `git pull --rebase` before committing; never rewrite pushed history.
- The adaptive-cadence guard in `scripts/local-check.sh` must stay **before**
  any network or git activity — idle firings are zero-network no-ops.
- Alerts are precision-first: the user rejects noisy notifications. Bad alerts
  get fixed the same day; when in doubt, drop the alert rather than send it.

## Verification

Use the lowest-risk check that proves the change — details in
[docs/verification.md](docs/verification.md):

- Any change: `.venv/bin/ruff check .` and `.venv/bin/python -m pytest -q` must pass.
- Behavior changes: also a `--dry-run` check run; exercise the affected flow.
- Real Telegram sends, state-file edits, launchd/plist changes, and workflow-cron
  changes require explicit user approval first.

## Backlog workflow

- The backlog lives in `docs/BACKLOG.md`. Items have stable IDs (`OTW-01`,
  `OTW-02`, …) that are never renumbered or reused.
- When asked to implement an ID (e.g. "implement OTW-03"), read that item first —
  it contains the problem, fix sketch, file paths, and done-when criteria.
- After completing an item, tick its Done checkbox in the index table and
  reference the ID in the commit message.
- New items: append with the next unused number in the fitting section; never
  repurpose an existing ID.

## Conventions

- Alert dedup is keyed on `Finding.key` (values like `sale:…`, `new_show:…`),
  not `kind`. Changing a key's format re-sends every past alert of that shape —
  never change it without a state migration.
- Alert kinds buzz by default; only kinds in `alerts.silent_kinds` (default
  HEARTBEAT, NEWS_LEAD, RECOVERED) are silent. A new non-urgent kind must be
  added there or it will notify loudly.
- News matching stays strict (sale wording required; format keywords need a
  venue mention) — loosening it needs user approval.
- Times shown to the user are Paris time; state timestamps are Paris-local
  ISO-8601 (offset-aware, e.g. `+02:00`).
- Keep dependencies minimal (`httpx`, `pytest`, `ruff`, `tomli` back-compat only);
  Python 3.9 compatibility is required (the local Mac may run system Python).

## Safety

- Never commit secrets, tokens, private data, or `.env`.
- Preserve unrelated user changes; never reset, clean, or force-push without
  explicit approval.
- No real Telegram sends without approval — use `--dry-run` / `--test-telegram`.
- Don't hand-edit `state/state.json` (dedup memory) without approval; a wrong
  edit either re-sends everything or silences future alerts.
- Don't tighten the Actions cron below `*/15` or make the repo private without
  approval (billing: ~2900 min/month at 15-min on private repos).

## Documentation ownership

When behavior changes, update the owner doc in the same change:

| Change area | Owner doc |
| --- | --- |
| Active runtime shape, user workflow | [docs/current-state.md](docs/current-state.md) |
| Setup, config reference, alert catalogue | [README.md](README.md) |
| Verification tiers, PR verification notes | [docs/verification.md](docs/verification.md) |
| Chronological history | [docs/history.md](docs/history.md) |

## Documentation maintenance

- `AGENTS.md` is the agent operating contract; `README.md` is for human setup
  and high-level behavior.
- `docs/current-state.md` is a snapshot, not a changelog.
- Update only the owner doc for a behavior change; detailed history goes to
  dated archives under `docs/history/`.
- Line budgets (to be enforced by the docs-contract test, OTW-01): `AGENTS.md`
  180, `README.md` 220, `current-state.md` 180, topic docs 150, history index
  80, history archive 260.
- Agents do not run separate documentation audits; they read this file, update
  the owner doc, and run normal verification.

## GitHub workflow

- Branch from `main`, verify, commit, push, open a PR. PRs are for visibility
  and diff review, not a manual approval gate.
- Merge after successful verification unless asked to keep the PR open; delete
  the branch after merge (remotely and locally).
- Remember pushing to `main` deploys: the `~/.ticket-watch` clone pulls it and
  the Actions cron runs it. State commits from both halves may land between
  your push attempts — rebase, don't force.
- Cross-review loops (`/claude-build`, `/codex-build`) follow
  `~/.claude/cross-review-protocol.md` and keep their logs in `docs/reviews/`.
