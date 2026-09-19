# Verification

Use the lowest-risk verification that proves the change.

## Fast local check

Use for docs, tests, or local logic changes:

```bash
.venv/bin/ruff check .
.venv/bin/python -m pytest -q
```

Expected current result: Ruff reports no errors and all collected tests pass.

For reverse cloud supervision and its read-only Telegram probe:

```bash
.venv/bin/python -m pytest -q tests/test_cloud_supervision.py tests/test_delivery.py tests/test_heartbeat.py tests/test_main.py tests/test_notify.py tests/test_config.py
```

These mocked tests cover stale, fresh/idle, API-failure, both cloud recovery branches,
heartbeat/outbox ordering and dedup without calling GitHub or Telegram. A real `--check-telegram` validates credentials but
sends no message; testing the edited Actions workflow still needs approval below.

## Watcher behavior check

Use when detection, news filtering, alert text, or cadence logic changes:

```bash
source .env
.venv/bin/python -m watcher --mode check --dry-run   # logs alerts, sends nothing, does not save state
```

Needs network access to `www.pathe.fr` — run from a residential IP (it is
Akamai-blocked from datacenter IPs). Read the logged alerts and confirm they
match expectations; precision beats recall (a suppressed alert is better than a
noisy one).

With `[cinesa] enabled = true` this also exercises the Cinesa half: expect a
`cinesa snapshot: N bookable day(s) …` line. If the cached token has expired it
first logs `minting a new Cinesa token via headed Chrome` and a Chrome window
opens offscreen for ~3 s — that is normal, and needs the Mac awake and logged
in. Cinesa is Cloudflare-challenged from datacenter IPs too, so this is
local-only as well.

To exercise only the Cinesa path without touching Pathé or Telegram:

```bash
.venv/bin/python -c "
from watcher import cinesa, detect
from watcher.config import load_config
cfg = load_config('config.toml'); snap = cinesa.fetch_snapshot(cfg)
print(len(snap.days), snap.days[0]['date'], '->', snap.days[-1]['date'])
print('IMAX days:', len(detect.imax_days(snap.days, cfg.cinesa_imax_attribute_id)))"
```

## Telegram delivery check — needs user approval

A real send reaches the user's phone:

```bash
source .env && .venv/bin/python -m watcher --test-telegram
```

## Scheduling / deploy checks — needs user approval

- **launchd** (plist or `scripts/local-check.sh` changes): deploy to
  `~/.ticket-watch`, then `launchctl kickstart gui/$(id -u)/com.odysseum.ticket-watch`
  and check `logs/`.
- **Actions** (`watch.yml` changes): Actions → *ticket-watch* → Run workflow with
  mode `test` (Telegram hello) or `remind` + dry-run; confirm the credential-check
  step succeeds before the watcher step.
- **State file edits** (live state at `.cache/state-sync/state.json`, or the
  tracked seed at `state/state.json`): approval required — wrong edits either
  re-send every past alert or silence future ones.

## PR verification notes

Every PR states what was verified and what could not be verified. Do not claim a fix
is complete without this.
