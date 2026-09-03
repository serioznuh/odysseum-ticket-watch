#!/bin/bash
# Pathé + Cinesa check, run from a residential IP (both chains block or
# challenge datacenter IPs). Fired by the com.odysseum.ticket-watch
# LaunchAgent every 15 min; safe to run manually too.
#
# The adaptive guard still runs FIRST and still governs the Pathé + news
# half (≈4 h baseline). The Cinesa half runs on EVERY firing: it is one
# small call to an API that is neither bot-gated nor rate-limited, and the
# point is catching a schedule release within minutes. It writes state only
# when the schedule actually changes, so unchanged firings stay commit-free
# and the git sync below still only fires on real news.
set -euo pipefail
cd "$(dirname "$0")/.."

source .env

# Decides whether a check is due (≈4 h baseline, tightening to every firing
# around the announced sale opening) and exits instantly otherwise.
# To force a full check right now: .venv/bin/python -m watcher --mode check
.venv/bin/python -m watcher --mode check --adaptive-cadence

if [ -n "$(git status --porcelain state/state.json)" ]; then
  git add state/state.json
  git commit -q -m "state: local check $(date -u +%FT%TZ) [skip ci]"
fi

# Pull ALWAYS, push only when there is something to push. The pull used to be
# inside the push gate, which deadlocked on 2026-09-03: a blind run writes
# byte-identical state, so nothing was committed, so nothing was pushed, so
# nothing was pulled — and the fix for the outage could never reach this clone.
# A deploy must not depend on the watcher being healthy enough to write state.
git pull --rebase --quiet origin main || true
if [ -n "$(git log --oneline '@{u}..HEAD' 2>/dev/null)" ]; then
  git push --quiet origin main
fi
