"""State-aware recovery for local-check's conflicted state rebase."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from watcher.state import DEFAULT_STATE, load_state
from watcher.state_merge import StateMergeError, merge_states, run


def fresh_state() -> dict:
    return deepcopy(DEFAULT_STATE)


def test_merge_preserves_both_delivery_histories_and_acknowledged_baselines():
    base = fresh_state()
    upstream = fresh_state()
    local = fresh_state()
    target = "2026-12-01T09:00:00+01:00"

    upstream["alerts"] = {
        f"sale:cloud-listing:{target}": "2026-10-01T10:00:00+02:00",
        "cloud-only": "2026-10-01T10:01:00+02:00",
    }
    upstream["reminders_sent"] = {target: ["1440", "120"]}
    upstream["shows_seen"] = ["cloud-listing"]
    upstream["formats_seen"] = {"cloud-listing": ["imax70"]}
    upstream["sales"] = {"cloud-listing": target}
    upstream["sale_target"] = target

    local["alerts"] = {
        "tickets:local-listing:imax70": "2026-10-01T10:02:00+02:00",
        "local-only": "2026-10-01T10:03:00+02:00",
    }
    local["reminders_sent"] = {target: ["15"]}
    local["shows_seen"] = ["local-listing"]
    local["formats_seen"] = {"local-listing": ["imax70"]}
    local["tickets_available"] = True
    local["last_check_ok"] = "2026-10-01T10:03:00+02:00"

    merged = merge_states(base, upstream, local)

    assert set(merged["alerts"]) == {
        f"sale:cloud-listing:{target}",
        "cloud-only",
        "tickets:local-listing:imax70",
        "local-only",
    }
    assert merged["reminders_sent"] == {target: ["120", "1440", "15"]}
    assert merged["shows_seen"] == ["cloud-listing", "local-listing"]
    assert merged["formats_seen"] == {
        "cloud-listing": ["imax70"],
        "local-listing": ["imax70"],
    }
    assert merged["sales"] == {"cloud-listing": target}
    assert merged["sale_target"] == target
    assert merged["tickets_available"] is True
    assert merged["last_check_ok"] == local["last_check_ok"]


def test_merge_unions_same_receipt_keys_and_keeps_earliest_delivery_proof():
    base = fresh_state()
    upstream = fresh_state()
    local = fresh_state()
    target = "2026-12-01T09:00:00+01:00"
    upstream["alerts"]["same"] = "2026-10-01T08:05:00Z"
    local["alerts"]["same"] = "2026-10-01T10:04:00+02:00"
    upstream["reminders_sent"][target] = ["1440", "120"]
    local["reminders_sent"][target] = ["120", "15"]
    upstream["formats_seen"]["dune"] = ["other", "imax70"]
    local["formats_seen"]["dune"] = ["imax", "imax70"]

    merged = merge_states(base, upstream, local)

    assert merged["alerts"]["same"] == "2026-10-01T10:04:00+02:00"
    assert merged["reminders_sent"][target] == ["120", "1440", "15"]
    assert merged["formats_seen"]["dune"] == ["imax", "imax70", "other"]


def test_sales_baseline_is_not_lost_when_only_one_side_still_has_it():
    target = "2026-12-01T09:00:00+01:00"
    base = fresh_state()
    base["alerts"][f"sale:dune:{target}"] = "2026-10-01T10:00:00+02:00"
    base["sales"]["dune"] = target
    base["sale_target"] = target
    upstream = deepcopy(base)
    upstream["alerts"].clear()
    upstream["sales"].clear()
    local = deepcopy(base)

    merged = merge_states(base, upstream, local)

    assert merged["alerts"] == base["alerts"]
    assert merged["sales"] == base["sales"]
    assert merged["sale_target"] == target


def test_concurrent_sale_changes_keep_the_most_recently_acknowledged_baseline():
    old = "2026-11-01T09:00:00+01:00"
    upstream_sale = "2026-11-02T09:00:00+01:00"
    local_sale = "2026-11-03T09:00:00+01:00"
    base = fresh_state()
    base["sales"] = {"dune": old}
    base["sale_target"] = old
    upstream = deepcopy(base)
    upstream["sales"]["dune"] = upstream_sale
    upstream["sale_target"] = upstream_sale
    upstream["alerts"][f"sale:dune:{upstream_sale}"] = "2026-10-01T10:00:00+02:00"
    local = deepcopy(base)
    local["sales"]["dune"] = local_sale
    local["sale_target"] = local_sale
    local["alerts"][f"sale:dune:{local_sale}"] = "2026-10-01T10:05:00+02:00"

    merged = merge_states(base, upstream, local)

    assert merged["sales"] == {"dune": local_sale}
    assert merged["sale_target"] == local_sale
    assert set(merged["alerts"]) == {
        f"sale:dune:{upstream_sale}",
        f"sale:dune:{local_sale}",
    }


def test_unsafe_concurrent_scalar_change_fails_instead_of_guessing():
    base = fresh_state()
    upstream = fresh_state()
    local = fresh_state()
    upstream["last_error"] = "upstream failure"
    local["last_error"] = "local failure"

    with pytest.raises(StateMergeError, match="last_error changed differently"):
        merge_states(base, upstream, local)


def test_cli_failure_names_recovery_and_does_not_replace_output(tmp_path, capsys):
    base = fresh_state()
    upstream = fresh_state()
    local = fresh_state()
    upstream["last_error"] = "upstream failure"
    local["last_error"] = "local failure"
    paths = {}
    for name, state in (("base", base), ("upstream", upstream), ("local", local)):
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(state), encoding="utf-8")
        paths[name] = path
    output = tmp_path / "state.json"
    output.write_text(json.dumps(base), encoding="utf-8")
    before = output.read_bytes()

    status = run(
        [
            "--base",
            str(paths["base"]),
            "--upstream",
            str(paths["upstream"]),
            "--local",
            str(paths["local"]),
            "--output",
            str(output),
        ]
    )

    assert status == 2
    assert "state rebase recovery failed" in capsys.readouterr().err
    assert output.read_bytes() == before
    assert load_state(output) == base


def test_local_check_recovers_a_state_rebase_within_the_same_firing(tmp_path):
    """Exercise the shell boundary with a fake Git; no repository is touched."""
    project = tmp_path / "project"
    (project / "scripts").mkdir(parents=True)
    (project / "state").mkdir()
    (project / ".venv" / "bin").mkdir(parents=True)
    (project / ".env").write_text("", encoding="utf-8")
    source_script = Path(__file__).parents[1] / "scripts" / "local-check.sh"
    local_check = project / "scripts" / "local-check.sh"
    local_check.write_text(source_script.read_text(encoding="utf-8"), encoding="utf-8")

    data = tmp_path / "git-data"
    data.mkdir()
    base = fresh_state()
    upstream = fresh_state()
    local = fresh_state()
    target = "2026-12-01T09:00:00+01:00"
    upstream["alerts"]["cloud"] = "2026-10-01T10:00:00+02:00"
    upstream["reminders_sent"][target] = ["120"]
    local["alerts"]["local"] = "2026-10-01T10:01:00+02:00"
    local["reminders_sent"][target] = ["15"]
    for name, state in (("base", base), ("upstream", upstream), ("local", local)):
        (data / f"{name}.json").write_text(json.dumps(state), encoding="utf-8")

    python_wrapper = project / ".venv" / "bin" / "python"
    python_wrapper.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "-m" ] && [ "$2" = "watcher.state_merge" ]; then\n'
        '  exec "$REAL_PYTHON" "$@"\n'
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    python_wrapper.chmod(0o755)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_git = fake_bin / "git"
    fake_git.write_text(
        """#!/bin/sh
echo "$1:$2" >> "$FAKE_GIT_DATA/calls"
case "$1:$2" in
  pull:--rebase)
    [ -f "$FAKE_GIT_DATA/rebase-done" ] && exit 0
    exit 1
    ;;
  diff:--name-only)
    [ ! -f "$FAKE_GIT_DATA/rebase-done" ] && echo state/state.json
    exit 0
    ;;
  diff:--cached)
    exit 1
    ;;
  show::1:state/state.json)
    exec /bin/cat "$FAKE_GIT_DATA/base.json"
    ;;
  show::2:state/state.json)
    exec /bin/cat "$FAKE_GIT_DATA/upstream.json"
    ;;
  show::3:state/state.json)
    exec /bin/cat "$FAKE_GIT_DATA/local.json"
    ;;
  add:state/state.json)
    exit 0
    ;;
  rebase:--continue)
    : > "$FAKE_GIT_DATA/rebase-done"
    exit 0
    ;;
  rebase:--abort)
    : > "$FAKE_GIT_DATA/aborted"
    exit 0
    ;;
  status:--porcelain|log:--oneline|push:--quiet)
    exit 0
    ;;
esac
echo "unexpected fake git call: $*" >&2
exit 90
""",
        encoding="utf-8",
    )
    fake_git.chmod(0o755)

    env = os.environ.copy()
    env.update(
        {
            "FAKE_GIT_DATA": str(data),
            "PATH": f"{fake_bin}:{env['PATH']}",
            "PYTHONPATH": str(Path(__file__).parents[1]),
            "REAL_PYTHON": sys.executable,
        }
    )
    result = subprocess.run(
        ["/bin/bash", str(local_check)],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "state rebase recovery completed" in result.stderr
    assert not (data / "aborted").exists()
    assert "rebase:--continue" in (data / "calls").read_text(encoding="utf-8")
    merged = load_state(project / "state" / "state.json")
    assert set(merged["alerts"]) == {"cloud", "local"}
    assert merged["reminders_sent"][target] == ["120", "15"]
