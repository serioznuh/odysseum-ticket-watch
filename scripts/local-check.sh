#!/bin/bash
# Pathé + Cinesa check, run from a residential IP (both chains block or
# challenge datacenter IPs). Fired by the com.odysseum.ticket-watch
# LaunchAgent every 5 min; safe to run manually too.
#
# The adaptive guard still gates the Pathé + news half (≈4 h baseline) and
# still runs ahead of every call that half makes. It gates nothing else: the
# Cinesa half and the reminder ladder run on EVERY firing.
#
# Code deployment and live state use separate Git histories (OTW-21). main is
# fast-forwarded first and never contains local runtime writes. The pre/post
# state syncs use refs/heads/runtime-state through watcher.state_sync, retaining
# OTW-14's three-way receipt merge without rebasing the code checkout.
set -euo pipefail
cd "$(dirname "$0")/.."

# launchd normally serializes firings, but a manual run can overlap it. Hold
# one process lock across deploy, both state syncs and the watcher itself so two
# local owners can never read and write the live JSON concurrently.
#
# `locked` also supervises the whole firing under its documented overall
# deadline (state_sync.LOCAL_RUN_DEADLINE_SECONDS, two launchd intervals) plus a
# cleanup allowance. On that deadline it stops and reaps this run's own process
# tree before releasing the lock, then exits non-zero — so a hung Git child
# costs the reminder ladder two firings instead of every later one, and the next
# firing never races a writer that is still alive. The lock file is never
# deleted to break a lock.
if [ "${OTW_LOCAL_CHECK_LOCKED:-}" != "1" ]; then
  exec .venv/bin/python -m watcher.state_sync locked \
    --lock .cache/local-check.lock -- /bin/bash "$0" "$@"
fi

source .env

status=0

# Deploy code before touching shared runtime state. A broken/corrupt state ref
# can therefore neither block this fast-forward nor roll it back. Re-exec the
# newly deployed script once so script changes take effect in this firing; the
# parent Python process continues holding the overlap lock across the exec.
#
# The pull runs under `state_sync bounded`, the same stdlib boundary the state
# syncs use (macOS ships no `timeout` binary). A pull that never answers is
# stopped with its whole process tree and counts as a failed deployment, so this
# firing continues on the installed code instead of hanging until the overall
# deadline and losing the reminder check entirely.
#
# Mirrors watcher.state_sync.UNCONFIRMED_TREE_EXIT (a test pins the two): a
# bounded child left a process group running that nothing here could stop. That
# group may still write, so this firing stops at once instead of stacking more
# work beside it — the same class of hard stop as the missing-ref exit below, and
# never folded into "failed, carry on". The next firing is blocked by the record
# that child left next to the lock until that group is gone.
STATE_UNCONFIRMED_TREE_EXIT=5

if [ "${OTW_CODE_DEPLOYED:-}" != "1" ]; then
  deploy_status=0
  .venv/bin/python -m watcher.state_sync bounded \
    -- git pull --ff-only --quiet origin main || deploy_status=$?
  if [ "$deploy_status" -eq 0 ]; then
    export OTW_CODE_DEPLOYED=1
    exec /bin/bash "$0" "$@"
  fi
  if [ "$deploy_status" -eq "$STATE_UNCONFIRMED_TREE_EXIT" ]; then
    echo "ERROR: the deployment pull left a Git process group running; stopping" \
         "this firing rather than starting state work beside it" >&2
    exit "$deploy_status"
  fi
  echo "ERROR: code deployment from origin/main failed; running installed code" >&2
  status=1
fi

# Every Git invocation inside a sync waits a bounded time
# (state_sync.GIT_TIMEOUT_SECONDS) and a timeout stops that Git process tree and
# counts as a transport failure — the existing capped streak, no new alert path.
sync_state() {
  .venv/bin/python -m watcher.state_sync sync --store .cache/state-sync
}

# Mirrors watcher.state_sync.BOOTSTRAP_REQUIRED_EXIT (a test pins the two): the
# shared state ref is confirmed absent, so nothing here may send. Local receipts
# are no defence — a reminder the cloud failover sent while this Mac slept lived
# only in that ref, and this clone cannot see it. Creating the ref back is an
# explicit operator action (`state_sync init` for a new install, `recover` for an
# existing one); the tracked seed is not evidence of what was already sent.
STATE_BOOTSTRAP_REQUIRED_EXIT=3

# Pull shared state BEFORE the run. The watcher must see a reminder the cloud
# failover sent while this Mac slept, or it can re-send it. A failed sync keeps
# the last validated local copy; a transient network failure is retried
# silently for a few consecutive firings, while a genuinely unrecoverable
# (merge/schema) failure records the durable WATCHER_ERROR marker right away.
# Either way, the watcher still runs so it can surface that marker to Telegram.
# The one exception is the missing-ref condition above: there the firing stops
# before the watcher can deliver anything from an unverified history. The owner
# still learns of it — that sync leaves the durable marker, which the first pass
# after recovery delivers as one loud alert.
# A surviving Git group is the other hard stop, for the same reason: it may still
# be writing, and continuing would run the watcher and a second sync beside it.
pre_sync_status=0
sync_state || pre_sync_status=$?
if [ "$pre_sync_status" -eq "$STATE_BOOTSTRAP_REQUIRED_EXIT" ]; then
  echo "ERROR: shared runtime-state ref is missing; no send may happen until" \
       "'watcher.state_sync init' (new install) or 'recover' (existing one) runs" >&2
  exit "$pre_sync_status"
fi
if [ "$pre_sync_status" -eq "$STATE_UNCONFIRMED_TREE_EXIT" ]; then
  echo "ERROR: the pre-run state sync left a Git process group running; stopping" \
       "this firing before the watcher or a second sync runs beside it" >&2
  exit "$pre_sync_status"
fi

# Decides whether a Pathé + news check is due. The reminder ladder and Cinesa
# half still run on every firing. Capture failure so a delivered reminder's
# newly saved receipt is synchronized even when a later job failed.
.venv/bin/python -m watcher \
  --state .cache/state-sync/state.json \
  --mode check --adaptive-cadence || status=$?

# Pull/merge/push ALWAYS after the watcher. This retries a prior rejected push
# and preserves every local receipt before reporting the final run status.
sync_status=0
sync_state || sync_status=$?
if [ "$sync_status" -eq "$STATE_UNCONFIRMED_TREE_EXIT" ]; then
  # Never let this one be masked by an earlier ordinary failure: it is the status
  # that says a writer of this firing is still running.
  echo "ERROR: the post-run state sync left a Git process group running; the next" \
       "firing stops until that group is gone" >&2
  exit "$sync_status"
fi
if [ "$sync_status" -ne 0 ] && [ "$status" -eq 0 ]; then
  status=$sync_status
fi

exit "$status"
