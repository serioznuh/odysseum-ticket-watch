#!/bin/bash
# Pathé + Cinesa check, run from a residential IP (both chains block or
# challenge datacenter IPs). Fired by the com.odysseum.ticket-watch
# LaunchAgent every 15 min; safe to run manually too.
#
# The adaptive guard still gates the Pathé + news half (≈4 h baseline) and
# still runs ahead of every call that half makes. It gates nothing else: the
# Cinesa half and the reminder ladder run on EVERY firing. Cinesa is one small
# call to an API that is neither bot-gated nor rate-limited, and the point is
# catching a schedule release within minutes; it writes state only when the
# schedule actually changes, so unchanged firings stay commit-free and the git
# sync below still only pushes on real news.
#
# The sync brackets the run — pull, check, commit, pull, push — so this clone
# sees what the cloud failover did while the Mac was asleep before it decides
# what to alert (OTW-15).
set -euo pipefail
cd "$(dirname "$0")/.."

source .env

# Pull BEFORE the run as well as after it. The watcher reads state/state.json
# at startup, so a clone that has not pulled cannot see a reminder the cloud
# failover sent while this Mac was asleep: it would re-send it, and if the two
# halves marked *different* offsets the reminders_sent hunks conflict, the
# rebase below aborts, and the local commit sits unpushed until a human turns
# up (OTW-14's wedge, which then reads as a stale/blind watcher). Same recovery
# as the post-run pull: a dirty tree or a conflict leaves this clone untouched
# and the next firing retries. It also lands a deploy one firing sooner.
git pull --rebase --quiet origin main || git rebase --abort || true

# Decides whether a check is due (≈4 h baseline, tightening to every firing
# around the announced sale opening) and exits instantly otherwise.
# To force a full check right now: .venv/bin/python -m watcher --mode check
.venv/bin/python -m watcher --mode check --adaptive-cadence

if [ -n "$(git status --porcelain state/state.json)" ]; then
  git add state/state.json
  git commit -q -m "state: local check $(date -u +%FT%TZ) [skip ci]"
fi

# Pull ALWAYS, push only when there is something to push. This second pull
# rebases the commit just made onto anything the cloud pushed *during* the run.
# The pull used to be inside the push gate, which deadlocked on 2026-09-03: a
# blind run writes byte-identical state, so nothing was committed, so nothing
# was pushed, so nothing was pulled — and the fix for the outage could never
# reach this clone. A deploy must not depend on the watcher being healthy
# enough to write state.
# A swallowed rebase conflict would leave conflict markers in state.json, and
# load_state renames an unparseable state file and starts fresh — which re-sends
# every past alert and loses sale_target. Abort back to a clean tree instead and
# let the next firing retry.
git pull --rebase --quiet origin main || git rebase --abort || true
if [ -n "$(git log --oneline '@{u}..HEAD' 2>/dev/null)" ]; then
  git push --quiet origin main
fi
