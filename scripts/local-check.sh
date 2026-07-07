#!/bin/bash
# Daily Pathé check, run from a residential IP (GitHub's datacenter IPs are
# blocked by Akamai). Invoked by the com.odysseum.ticket-watch LaunchAgent;
# safe to run manually too. Pulls the shared state first (the cloud reminder
# pass also commits to it), runs the check, pushes the updated state back.
set -euo pipefail
cd "$(dirname "$0")/.."

source .env
git pull --rebase --quiet origin main || true

# The launchd job fires at 09:30 and again at 15:00 as a retry; the guard
# makes any slot exit instantly when data is already fresher than 5 h.
# To force a full check, run `.venv/bin/python -m watcher --mode check`.
.venv/bin/python -m watcher --mode check --skip-if-checked-within 5

if [ -n "$(git status --porcelain state/state.json)" ]; then
  git add state/state.json
  git commit -q -m "state: local check $(date -u +%FT%TZ) [skip ci]"
  git pull --rebase --quiet origin main || true
  git push --quiet origin main
fi
