#!/bin/bash
# Pathé + Cinesa check, run from a residential IP (both chains block or
# challenge datacenter IPs). Fired by the com.odysseum.ticket-watch
# LaunchAgent every 5 min; safe to run manually too.
#
# The adaptive guard still gates the Pathé + news half (≈4 h baseline) and
# still runs ahead of every call that half makes. It gates nothing else: the
# Cinesa half and the reminder ladder run on EVERY firing. Cinesa is disabled
# in the shipped configuration; when enabled it is one small
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

cleanup_state_merge_tmp() {
  rm -f "$1/base.json" "$1/upstream.json" "$1/local.json"
  rmdir "$1"
}

abort_state_rebase() {
  echo "ERROR: state rebase recovery failed; aborting rebase (OTW-14)" >&2
  git rebase --abort || true
  return 1
}

recover_state_rebase() {
  local conflicts tmp action
  while true; do
    conflicts="$(git diff --name-only --diff-filter=U)"
    if [ "$conflicts" != "state/state.json" ]; then
      echo "ERROR: pull failed with conflicts outside state/state.json; not auto-merging" >&2
      abort_state_rebase
      return 1
    fi

    echo "WARNING: state/state.json rebase conflict; running domain-aware recovery (OTW-14)" >&2
    tmp="$(mktemp -d "${TMPDIR:-/tmp}/otw-state-merge.XXXXXX")"
    if ! git show ':1:state/state.json' > "$tmp/base.json" \
      || ! git show ':2:state/state.json' > "$tmp/upstream.json" \
      || ! git show ':3:state/state.json' > "$tmp/local.json" \
      || ! .venv/bin/python -m watcher.state_merge \
        --base "$tmp/base.json" \
        --upstream "$tmp/upstream.json" \
        --local "$tmp/local.json" \
        --output state/state.json; then
      cleanup_state_merge_tmp "$tmp"
      abort_state_rebase
      return 1
    fi
    cleanup_state_merge_tmp "$tmp"
    git add state/state.json

    # The replay can become empty when upstream already contains every local
    # receipt. Skipping is safe only when the entire commit is empty; local
    # state commits contain no unrelated files, and the index check enforces it.
    action=--continue
    if git diff --cached --quiet; then
      action=--skip
    fi
    if GIT_EDITOR=true git rebase "$action"; then
      echo "state rebase recovery completed" >&2
      return 0
    fi
    # A replay with more than one local commit may stop on the next state
    # conflict. Loop only for that exact case; every other failure aborts below.
    if [ "$(git diff --name-only --diff-filter=U)" != "state/state.json" ]; then
      abort_state_rebase
      return 1
    fi
  done
}

pull_with_state_recovery() {
  if git pull --rebase --quiet origin main; then
    return 0
  fi
  if [ "$(git diff --name-only --diff-filter=U)" = "state/state.json" ]; then
    recover_state_rebase
    return
  fi
  # Preserve the established retry behavior for dirty trees, network errors,
  # and non-state failures. Only the known state rebase wedge is auto-merged.
  git rebase --abort || true
}

# Pull BEFORE the run as well as after it. The watcher reads state/state.json
# at startup, so a clone that has not pulled cannot see a reminder the cloud
# failover sent while this Mac was asleep: it would re-send it, and if the two
# halves marked *different* offsets the reminders_sent hunks can conflict.
# State-only conflicts are merged by delivery/baseline semantics and the rebase
# continues in this firing; unrelated pull failures retain the normal retry.
# It also lands a deploy one firing sooner.
pull_with_state_recovery

# Decides whether a check is due (≈4 h baseline, tightening to every firing
# around the announced sale opening) and exits instantly otherwise.
# To force a full check right now: .venv/bin/python -m watcher --mode check
#
# Capture the status instead of letting `set -e` abort here. A failing run can
# still have saved state — a delivered reminder's receipt, for one — and under
# `set -e` that state was left modified but uncommitted, which then made the
# NEXT firing's pre-run pull fail on a dirty tree. A deploy must not depend on
# the watcher being healthy (OTW-14's wedge class), so sync first and report
# the watcher's status to launchd at the end.
status=0
.venv/bin/python -m watcher --mode check --adaptive-cadence || status=$?

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
# A state-only conflict is recovered here without dropping either half's
# delivery receipts or the observation baselines those receipts acknowledge.
pull_with_state_recovery
if [ -n "$(git log --oneline '@{u}..HEAD' 2>/dev/null)" ]; then
  git push --quiet origin main
fi

exit "$status"
