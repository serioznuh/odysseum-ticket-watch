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
if [ "${OTW_CODE_DEPLOYED:-}" != "1" ]; then
  if git pull --ff-only --quiet origin main; then
    export OTW_CODE_DEPLOYED=1
    exec /bin/bash "$0" "$@"
  fi
  echo "ERROR: code deployment from origin/main failed; running installed code" >&2
  status=1
fi

sync_state() {
  .venv/bin/python -m watcher.state_sync sync --store .cache/state-sync
}

# Mirrors watcher.state_sync.BOOTSTRAP_REQUIRED_EXIT (a test pins the two):
# the shared state ref is confirmed absent and this clone holds no verified
# delivery history, so nothing here may send. Creating that ref is an explicit
# operator action (`state_sync init` for a new install, `recover` for an
# existing one) — the tracked seed is not evidence of what was already sent.
STATE_BOOTSTRAP_REQUIRED_EXIT=3

# Pull shared state BEFORE the run. The watcher must see a reminder the cloud
# failover sent while this Mac slept, or it can re-send it. A failed sync keeps
# the last validated local copy; a transient network failure is retried
# silently for a few consecutive firings, while a genuinely unrecoverable
# (merge/schema) failure records the durable WATCHER_ERROR marker right away.
# Either way, the watcher still runs so it can surface that marker to Telegram.
# The one exception is the missing-ref condition above: there the firing stops
# before the watcher can deliver historical alerts from an unverified seed.
pre_sync_status=0
sync_state || pre_sync_status=$?
if [ "$pre_sync_status" -eq "$STATE_BOOTSTRAP_REQUIRED_EXIT" ]; then
  echo "ERROR: shared runtime-state ref is missing with no verified local history;" \
       "run 'watcher.state_sync init' (new install) or 'recover' (existing one)" >&2
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
if [ "$sync_status" -ne 0 ] && [ "$status" -eq 0 ]; then
  status=$sync_status
fi

exit "$status"
