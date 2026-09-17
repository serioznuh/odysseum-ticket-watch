"""Real-Git integration tests for the runtime-state boundary (OTW-21)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from copy import deepcopy
from pathlib import Path

import pytest

from watcher.state import DEFAULT_STATE, load_state, save_state
from watcher.state_sync import (
    DEFAULT_MARKER_PATH,
    DEFAULT_STATE_REF,
    DEFAULT_STORE_PATH,
    TRANSPORT_FAILURE_FILE,
    TRANSPORT_FAILURE_THRESHOLD,
    StateSyncError,
    failure_key,
    load_failure,
    run,
    synchronize,
)

ROOT = Path(__file__).parents[1]
STAMP = "2026-09-17T12:00:00+02:00"
TARGET = "2026-12-01T09:00:00+01:00"


def git(repo: Path, *args: str, input_text: str | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        input=input_text,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def configure_git(repo: Path) -> None:
    git(repo, "config", "user.name", "OTW integration test")
    git(repo, "config", "user.email", "otw-test@example.invalid")


@pytest.fixture
def two_clones(tmp_path):
    """A bare origin plus independent local-Mac and cloud working clones."""
    origin = tmp_path / "origin.git"
    bootstrap = tmp_path / "bootstrap"
    local = tmp_path / "local"
    cloud = tmp_path / "cloud"
    origin.mkdir()
    bootstrap.mkdir()
    git(origin, "init", "--bare")
    git(bootstrap, "init", "-b", "main")
    configure_git(bootstrap)
    (bootstrap / "state").mkdir()
    save_state(bootstrap / "state" / "state.json", deepcopy(DEFAULT_STATE))
    (bootstrap / "code.txt").write_text("version 1\n", encoding="utf-8")
    (bootstrap / "behavior.py").write_text("print('version 1')\n", encoding="utf-8")
    git(bootstrap, "add", "state/state.json", "code.txt", "behavior.py")
    git(bootstrap, "commit", "-m", "bootstrap")
    git(bootstrap, "remote", "add", "origin", str(origin))
    git(bootstrap, "push", "-u", "origin", "main")
    git(origin, "symbolic-ref", "HEAD", "refs/heads/main")
    git(tmp_path, "clone", str(origin), str(local))
    git(tmp_path, "clone", str(origin), str(cloud))
    configure_git(local)
    configure_git(cloud)
    return origin, local, cloud


def live_path(repo: Path) -> Path:
    return repo / ".cache" / "state-sync" / "state.json"


def change_state(repo: Path, update) -> dict:
    state = load_state(live_path(repo))
    update(state)
    save_state(live_path(repo), state)
    return state


def push_raw_state(repo: Path, text: str) -> None:
    git(repo, "fetch", "origin", DEFAULT_STATE_REF)
    parent = git(repo, "rev-parse", "FETCH_HEAD")
    blob = git(repo, "hash-object", "-w", "--stdin", input_text=text)
    tree = git(repo, "mktree", input_text=f"100644 blob {blob}\tstate.json\n")
    commit = git(repo, "commit-tree", tree, "-p", parent, "-m", "corrupt state")
    git(repo, "push", "origin", f"{commit}:{DEFAULT_STATE_REF}")


def test_two_workers_merge_receipts_with_local_owner_health_update(two_clones):
    _, local, cloud = two_clones
    synchronize(local)
    synchronize(cloud)
    change_state(local, lambda state: state.__setitem__("last_error", "old failure"))
    synchronize(local)
    synchronize(cloud)

    def update_local(state):
        state["alerts"]["local-delivery"] = STAMP
        state["reminders_sent"][TARGET] = ["15"]
        state["last_check_ok"] = STAMP
        state["last_catalogue_ok"] = STAMP
        state.pop("last_error")

    def update_cloud(state):
        state["alerts"]["cloud-delivery"] = STAMP
        state["reminders_sent"][TARGET] = ["120"]

    change_state(local, update_local)
    change_state(cloud, update_cloud)
    synchronize(local)
    synchronize(cloud)
    synchronize(local)

    merged = load_state(live_path(local))
    assert set(merged["alerts"]) == {"local-delivery", "cloud-delivery"}
    assert merged["reminders_sent"][TARGET] == ["120", "15"]
    assert merged["last_check_ok"] == STAMP
    assert merged["last_catalogue_ok"] == STAMP
    assert "last_error" not in merged


def test_unresolvable_owner_conflict_marks_then_recovers_without_losing_receipt(
    two_clones,
):
    _, local, cloud = two_clones
    synchronize(local)
    synchronize(cloud)
    change_state(local, lambda state: state.__setitem__("last_error", "local failure"))
    change_state(cloud, lambda state: state.__setitem__("last_error", "cloud failure"))
    synchronize(local)

    assert run(["sync", "--repo", str(cloud), "--push-attempts", "1"]) == 1
    marker_path = cloud / DEFAULT_MARKER_PATH
    marker = load_failure(marker_path)
    assert marker is not None
    conflicted = load_state(live_path(cloud))
    assert conflicted["last_error"] == "cloud failure"

    conflicted["last_error"] = "local failure"
    conflicted["alerts"][failure_key(marker)] = STAMP
    save_state(live_path(cloud), conflicted)
    assert run(["sync", "--repo", str(cloud)]) == 0
    assert not marker_path.exists()
    synchronize(local)
    recovered = load_state(live_path(local))
    assert failure_key(marker) in recovered["alerts"]
    assert recovered["last_error"] == "local failure"


def test_rejected_push_preserves_local_receipt_and_retries_next_run(two_clones):
    origin, local, cloud = two_clones
    synchronize(local)
    synchronize(cloud)
    change_state(
        local,
        lambda state: state["alerts"].__setitem__("receipt-before-reject", STAMP),
    )
    hook = origin / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)

    argv = ["sync", "--repo", str(local), "--push-attempts", "1"]
    for _ in range(TRANSPORT_FAILURE_THRESHOLD - 1):
        assert run(argv) == 1
        assert load_failure(local / DEFAULT_MARKER_PATH) is None
        assert "receipt-before-reject" in load_state(live_path(local))["alerts"]
    assert run(argv) == 1
    assert load_failure(local / DEFAULT_MARKER_PATH) is not None

    hook.unlink()
    assert run(["sync", "--repo", str(local)]) == 0
    assert not (local / DEFAULT_STORE_PATH / TRANSPORT_FAILURE_FILE).exists()
    synchronize(cloud)
    assert "receipt-before-reject" in load_state(live_path(cloud))["alerts"]


def test_overlapping_local_invocations_are_serialized_by_process_lock(two_clones):
    _, local, _ = two_clones
    synchronize(local)
    worker = local / "overlap_worker.py"
    worker.write_text(
        """import sys, time
from pathlib import Path
from watcher.state import load_state, save_state
from watcher.state_sync import synchronize

repo = Path(sys.argv[1])
key = sys.argv[2]
hits = repo / '.cache' / 'overlap-hits'
with hits.open('a', encoding='utf-8') as stream:
    stream.write(key + '\\n')
state_path = repo / '.cache' / 'state-sync' / 'state.json'
state = load_state(state_path)
state['alerts'][key] = '2026-09-17T12:00:00+02:00'
time.sleep(0.5)
save_state(state_path, state)
synchronize(repo)
""",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    lock = local / ".cache" / "local-check.lock"

    def command(key: str) -> list[str]:
        return [
            sys.executable,
            "-m",
            "watcher.state_sync",
            "locked",
            "--lock",
            str(lock),
            "--",
            sys.executable,
            str(worker),
            str(local),
            key,
        ]

    first = subprocess.Popen(command("first"), cwd=ROOT, env=env)
    hits = local / ".cache" / "overlap-hits"
    deadline = time.monotonic() + 5
    while not hits.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert hits.exists()
    second = subprocess.run(
        command("second"), cwd=ROOT, env=env, capture_output=True, text=True, check=False
    )
    assert second.returncode == 0
    assert "already running" in second.stderr
    assert first.wait(timeout=10) == 0
    assert hits.read_text(encoding="utf-8").splitlines() == ["first"]
    assert set(load_state(live_path(local))["alerts"]) == {"first"}


def test_code_fast_forward_succeeds_with_incompatible_state_ref(two_clones):
    _, local, cloud = two_clones
    synchronize(local)
    synchronize(cloud)
    incompatible = deepcopy(DEFAULT_STATE)
    incompatible["version"] = 999
    push_raw_state(cloud, json.dumps(incompatible))

    (cloud / "code.txt").write_text("version 2\n", encoding="utf-8")
    (cloud / "behavior.py").write_text("print('version 2')\n", encoding="utf-8")
    git(cloud, "add", "code.txt", "behavior.py")
    git(cloud, "commit", "-m", "deploy code v2")
    git(cloud, "push", "origin", "main")

    (local / "scripts").mkdir()
    local_check = local / "scripts" / "local-check.sh"
    local_check.write_text(
        (ROOT / "scripts" / "local-check.sh").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    local_check.chmod(0o755)
    (local / ".env").write_text("", encoding="utf-8")
    (local / ".venv" / "bin").mkdir(parents=True)
    python_wrapper = local / ".venv" / "bin" / "python"
    python_wrapper.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "-m" ] && [ "$2" = "watcher.state_sync" ]; then\n'
        '  exec "$REAL_PYTHON" "$@"\n'
        "fi\n"
        'exec "$REAL_PYTHON" behavior.py "$@"\n',
        encoding="utf-8",
    )
    python_wrapper.chmod(0o755)
    env = os.environ.copy()
    env.update({"PYTHONPATH": str(ROOT), "REAL_PYTHON": sys.executable})
    deployed = subprocess.run(
        ["/bin/bash", str(local_check)],
        cwd=local,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    # State sync is deliberately broken, so the firing reports failure only
    # after main fast-forwards and the newly deployed behavior has executed.
    assert deployed.returncode != 0
    assert "version 2" in deployed.stdout
    assert (local / "code.txt").read_text(encoding="utf-8") == "version 2\n"
    before = load_state(live_path(local))
    with pytest.raises(StateSyncError, match="unsupported state schema 999"):
        synchronize(local)
    assert load_state(live_path(local)) == before
    assert load_failure(local / DEFAULT_MARKER_PATH) is not None
    assert git(local, "status", "--porcelain", "--untracked-files=no") == ""


def test_merge_is_reconciliation_not_a_delivery_claim(two_clones):
    """OTW-20 must add claims/outcomes; state merging alone is not exactly-once."""
    _, local, cloud = two_clones
    synchronize(local)
    synchronize(cloud)
    # Both workers can decide to send from the same pre-delivery snapshot. The
    # boundary preserves the eventual receipt, but intentionally does not claim
    # work or promise cross-host exclusion.
    change_state(local, lambda state: state["alerts"].__setitem__("same-key", STAMP))
    change_state(cloud, lambda state: state["alerts"].__setitem__("same-key", STAMP))
    synchronize(local)
    synchronize(cloud)
    assert load_state(live_path(cloud))["alerts"]["same-key"] == STAMP
