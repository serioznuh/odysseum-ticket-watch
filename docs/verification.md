# Verification

Use the lowest-risk verification that proves the change.

## Fast local check

Use for docs, tests, or local logic changes:

```bash
.venv/bin/python -m pytest -q
```

Expected current result: **42 passed**. No linter is configured yet (OTW-02).

## Watcher behavior check

Use when detection, news filtering, alert text, or cadence logic changes:

```bash
source .env
.venv/bin/python -m watcher --mode check --dry-run   # logs alerts, sends nothing, state untouched
```

Needs network access to `www.pathe.fr` — run from a residential IP (it is
Akamai-blocked from datacenter IPs). Read the logged alerts and confirm they
match expectations; precision beats recall (a suppressed alert is better than a
noisy one).

## Telegram delivery check — needs user approval

A real send reaches the user's phone:

```bash
.venv/bin/python -m watcher --test-telegram
```

## Scheduling / deploy checks — needs user approval

- **launchd** (plist or `scripts/local-check.sh` changes): deploy to
  `~/.ticket-watch`, then `launchctl kickstart gui/$(id -u)/com.odysseum.ticket-watch`
  and check `logs/`.
- **Actions** (`watch.yml` changes): Actions → *ticket-watch* → Run workflow with
  mode `test` (Telegram hello) or `remind` + dry-run.
- **State file edits** (`state/state.json`): approval required — wrong edits
  either re-send every past alert or silence future ones.

## PR verification notes

Every PR states what was verified and what could not be verified. Do not claim a fix
is complete without this.
