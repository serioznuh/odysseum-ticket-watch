#!/bin/bash
# Pathé check, run from a residential IP (GitHub's datacenter IPs are
# blocked by Akamai). Fired by the com.odysseum.ticket-watch LaunchAgent
# every 15 min; safe to run manually too.
#
# Order matters: the adaptive guard runs FIRST, so idle firings touch the
# network zero times (the guard only needs locally-written state). git sync
# happens only on runs that actually checked, or that have unpushed backlog
# left by an earlier offline run.
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

# Push local commits if any; rebase first so cloud commits (reminder marks)
# merge cleanly. Also retries a push an offline earlier run failed to make.
if [ -n "$(git log --oneline '@{u}..HEAD' 2>/dev/null)" ]; then
  git pull --rebase --quiet origin main || true
  git push --quiet origin main
fi
