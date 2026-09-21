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

from watcher import state_sync
from watcher.state import DEFAULT_STATE, load_state, save_state
from watcher.state_sync import (
    BOOTSTRAP_REQUIRED_EXIT,
    DEFAULT_MARKER_PATH,
    DEFAULT_STATE_REF,
    DEFAULT_STORE_PATH,
    TRANSPORT_FAILURE_FILE,
    TRANSPORT_FAILURE_THRESHOLD,
    StateSyncError,
    StateSyncRefAbsentError,
    failure_key,
    initialize,
    load_failure,
    load_transport_failure,
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


def bootstrap_origin(tmp_path: Path) -> Path:
    """A bare origin carrying code and the tracked seed, but no state ref."""
    origin = tmp_path / "origin.git"
    bootstrap = tmp_path / "bootstrap"
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
    return origin


def clone_of(tmp_path: Path, origin: Path, name: str) -> Path:
    clone = tmp_path / name
    git(tmp_path, "clone", str(origin), str(clone))
    configure_git(clone)
    return clone


def remote_state_commit(origin: Path) -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", DEFAULT_STATE_REF],
        cwd=origin,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


@pytest.fixture
def uninitialized(tmp_path):
    """A first clone of an installation whose shared state ref does not exist."""
    origin = bootstrap_origin(tmp_path)
    return origin, clone_of(tmp_path, origin, "first")


@pytest.fixture
def two_clones(tmp_path):
    """A bare origin plus independent local-Mac and cloud working clones."""
    origin = bootstrap_origin(tmp_path)
    local = clone_of(tmp_path, origin, "local")
    cloud = clone_of(tmp_path, origin, "cloud")
    # The shared ref only ever comes from an explicit first-run initialization
    # (OTW-30), so ordinary syncs below always face an existing ref.
    initialize(local)
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
        state["delivery_receipts"]["local-attempt"] = {
            "delivery_id": "telegram:local",
            "keys": ["local-delivery"],
            "delivered_at": STAMP,
            "telegram_message_id": 101,
        }
        state["reminders_sent"][TARGET] = ["15"]
        state["last_check_ok"] = STAMP
        state["last_catalogue_ok"] = STAMP
        state.pop("last_error")

    def update_cloud(state):
        state["alerts"]["cloud-delivery"] = STAMP
        state["delivery_receipts"]["cloud-attempt"] = {
            "delivery_id": "telegram:cloud",
            "keys": ["cloud-delivery"],
            "delivered_at": STAMP,
            "telegram_message_id": 102,
        }
        state["reminders_sent"][TARGET] = ["120"]

    change_state(local, update_local)
    change_state(cloud, update_cloud)
    synchronize(local)
    synchronize(cloud)
    synchronize(local)

    merged = load_state(live_path(local))
    assert set(merged["alerts"]) == {"local-delivery", "cloud-delivery"}
    assert set(merged["delivery_receipts"]) == {"local-attempt", "cloud-attempt"}
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


# ---------------------------------------------------------------------------
# OTW-30: creating the shared ref is an explicit operation, never ordinary work
# ---------------------------------------------------------------------------

HISTORIC_ALERT = "sale:dune-troisieme-partie:2026-11-02T10:00:00+01:00"
IN_FLIGHT_ID = "telegram:news-leak-42"
IN_FLIGHT = {
    "keys": ["news:leak-42"],
    "kinds": ["NEWS_LEAD"],
    "text": "Dune : Troisième partie · Pathé Odysseum — press lead",
    "silent": True,
    "force": False,
    "created_at": STAMP,
    "topics": ["news"],
    "status": "sending",
    "ack": {"type": "alerts", "keys": ["news:leak-42"]},
    "claim": {"owner": "local", "token": "abc123", "at": STAMP},
}


def deliver(repo: Path, key: str, attempt: str) -> None:
    """Record one confirmed Telegram delivery the way a real pass would."""

    def update(state: dict) -> None:
        state["alerts"][key] = STAMP
        state["delivery_receipts"][attempt] = {
            "delivery_id": f"telegram:{key}",
            "keys": [key],
            "delivered_at": STAMP,
            "telegram_message_id": 4242,
        }

    change_state(repo, update)


def delete_state_ref(origin: Path) -> None:
    git(origin, "update-ref", "-d", DEFAULT_STATE_REF)


def test_fresh_clone_cannot_reseed_a_deleted_state_ref(tmp_path, two_clones):
    """The reproduced OTW-30 hazard: a new runner must not resurrect the ref
    from the frozen seed, which would make every past alert eligible again."""
    origin, local, _ = two_clones
    deliver(local, HISTORIC_ALERT, "attempt-historic")
    synchronize(local)
    delete_state_ref(origin)

    fresh = clone_of(tmp_path, origin, "fresh")
    assert (fresh / "state" / "state.json").exists()  # the seed is right there
    assert run(["sync", "--repo", str(fresh)]) == BOOTSTRAP_REQUIRED_EXIT

    # Nothing to deliver from, nothing pushed: no live file materializes, and
    # the absent ref stays absent until an operator acts.
    assert not live_path(fresh).exists()
    assert not (fresh / DEFAULT_STORE_PATH / "base.json").exists()
    assert remote_state_commit(origin) is None
    assert load_failure(fresh / DEFAULT_MARKER_PATH) is None


def test_existing_clone_preserves_live_and_base_when_the_ref_disappears(two_clones):
    origin, local, _ = two_clones
    deliver(local, HISTORIC_ALERT, "attempt-historic")
    synchronize(local)
    live_before = live_path(local).read_bytes()
    base_before = (local / DEFAULT_STORE_PATH / "base.json").read_bytes()
    delete_state_ref(origin)

    with pytest.raises(StateSyncRefAbsentError) as absent:
        synchronize(local)
    assert absent.value.established is True

    # Local receipts are not permission to keep sending: a reminder the cloud
    # delivered while this Mac slept lived only in the ref. Delivery stops here
    # too, and the ref is not recreated behind the owner's back.
    assert run(["sync", "--repo", str(local)]) == BOOTSTRAP_REQUIRED_EXIT
    assert live_path(local).read_bytes() == live_before
    assert (local / DEFAULT_STORE_PATH / "base.json").read_bytes() == base_before
    assert remote_state_commit(origin) is None
    marker = load_failure(local / DEFAULT_MARKER_PATH)
    assert marker is not None
    assert "is missing" in marker["detail"]
    # A working transport that answers "no such ref" is not an outage streak.
    assert not (local / DEFAULT_STORE_PATH / TRANSPORT_FAILURE_FILE).exists()


def test_transport_outage_stays_distinct_from_a_confirmed_absence(tmp_path):
    origin = bootstrap_origin(tmp_path)
    offline = clone_of(tmp_path, origin, "offline")
    git(offline, "remote", "set-url", "origin", str(tmp_path / "vanished.git"))

    assert run(["sync", "--repo", str(offline)]) == 1
    streak = load_transport_failure(offline / DEFAULT_STORE_PATH / TRANSPORT_FAILURE_FILE)
    assert streak is not None and streak["count"] == 1
    assert load_failure(offline / DEFAULT_MARKER_PATH) is None
    assert not live_path(offline).exists()


def test_explicit_initialization_creates_the_shared_ref_once(tmp_path, uninitialized):
    origin, first = uninitialized
    assert run(["sync", "--repo", str(first)]) == BOOTSTRAP_REQUIRED_EXIT

    assert run(["init", "--repo", str(first)]) == 0
    created = remote_state_commit(origin)
    assert created is not None
    assert load_state(live_path(first)) == DEFAULT_STATE

    # Ordinary syncs now work and neither rewrite nor re-create that history.
    assert run(["sync", "--repo", str(first)]) == 0
    assert remote_state_commit(origin) == created

    # A second host joins through ordinary sync, with no init of its own.
    second = clone_of(tmp_path, origin, "second")
    assert run(["sync", "--repo", str(second)]) == 0
    assert load_state(live_path(second)) == DEFAULT_STATE


def test_initialization_refuses_an_installation_with_delivery_history(two_clones):
    origin, local, _ = two_clones
    deliver(local, HISTORIC_ALERT, "attempt-historic")
    synchronize(local)
    delete_state_ref(origin)

    assert run(["init", "--repo", str(local)]) == 1
    assert remote_state_commit(origin) is None
    assert HISTORIC_ALERT in load_state(live_path(local))["alerts"]


def test_concurrent_initialization_leaves_the_independent_ref_intact(
    tmp_path, uninitialized, monkeypatch
):
    origin, first = uninitialized
    second = clone_of(tmp_path, origin, "second")
    assert run(["init", "--repo", str(first)]) == 0
    deliver(first, "first-host-delivery", "attempt-first")
    synchronize(first)
    created = remote_state_commit(origin)

    # `second` looked before `first` pushed, so its own creating push loses the
    # race. The independently created history must survive untouched, and the
    # loser must adopt it rather than keep an unverified seed.
    real_remote_state = state_sync._remote_state
    calls = {"count": 0}

    def racing_remote_state(repo, remote, state_ref):
        calls["count"] += 1
        if calls["count"] == 1:
            return None, None
        return real_remote_state(repo, remote, state_ref)

    monkeypatch.setattr(state_sync, "_remote_state", racing_remote_state)
    assert run(["init", "--repo", str(second)]) == 0
    assert calls["count"] >= 2
    assert remote_state_commit(origin) == created
    assert "first-host-delivery" in load_state(live_path(second))["alerts"]


def test_recovery_preserves_confirmed_receipts_and_quarantines_uncertainty(
    tmp_path, two_clones
):
    origin, local, cloud = two_clones
    synchronize(cloud)
    # The cloud delivered an alert and a reminder rung but never pushed them;
    # its live file is the only surviving proof, taken here as a backup.
    def cloud_work(state: dict) -> None:
        state["alerts"]["cloud-delivery"] = STAMP
        state["delivery_receipts"]["attempt-cloud"] = {
            "delivery_id": "telegram:cloud-delivery",
            "keys": ["cloud-delivery"],
            "delivered_at": STAMP,
            "telegram_message_id": 102,
        }
        state["reminders_sent"][TARGET] = ["120"]

    change_state(cloud, cloud_work)
    backup = tmp_path / "cloud-backup-state.json"
    backup.write_bytes(live_path(cloud).read_bytes())

    def local_work(state: dict) -> None:
        state["alerts"]["local-delivery"] = STAMP
        state["delivery_receipts"]["attempt-local"] = {
            "delivery_id": "telegram:local-delivery",
            "keys": ["local-delivery"],
            "delivered_at": STAMP,
            "telegram_message_id": 101,
        }
        state["reminders_sent"][TARGET] = ["15"]
        state["outbox"][IN_FLIGHT_ID] = deepcopy(IN_FLIGHT)

    change_state(local, local_work)
    delete_state_ref(origin)
    # Ordinary sync refuses and blocks delivery until the owner recovers.
    assert run(["sync", "--repo", str(local)]) == BOOTSTRAP_REQUIRED_EXIT

    assert run(["recover", "--repo", str(local), "--from", str(backup)]) == 0
    recovered = load_state(live_path(local))
    assert set(recovered["alerts"]) == {"cloud-delivery", "local-delivery"}
    assert set(recovered["delivery_receipts"]) == {"attempt-cloud", "attempt-local"}
    assert recovered["reminders_sent"][TARGET] == ["120", "15"]
    # The interrupted attempt stays quarantined: recovery may not turn an
    # unknown Telegram outcome into a replay.
    assert recovered["outbox"][IN_FLIGHT_ID]["status"] == "uncertain"
    assert recovered["outbox"][IN_FLIGHT_ID]["claim"] == IN_FLIGHT["claim"]

    # Normal delivery resumes on the reconciled history, so a runner that
    # joins afterwards can re-send none of it.
    joined = clone_of(tmp_path, origin, "joined")
    assert run(["sync", "--repo", str(joined)]) == 0
    rejoined = load_state(live_path(joined))
    assert set(rejoined["alerts"]) == {"cloud-delivery", "local-delivery"}
    assert rejoined["reminders_sent"][TARGET] == ["120", "15"]
    assert rejoined["outbox"][IN_FLIGHT_ID]["status"] == "uncertain"


def test_recovery_refuses_the_tracked_seed_as_evidence(uninitialized):
    origin, first = uninitialized
    assert not live_path(first).exists()

    assert run(["recover", "--repo", str(first)]) == 1
    assert remote_state_commit(origin) is None
    assert not live_path(first).exists()


def test_recovery_leaves_a_concurrently_recreated_ref_intact(tmp_path, two_clones):
    origin, local, cloud = two_clones
    synchronize(cloud)
    deliver(local, "local-delivery", "attempt-local")
    delete_state_ref(origin)
    # Another host recreated the ref from its own surviving store first.
    deliver(cloud, "cloud-delivery", "attempt-cloud")
    assert run(["recover", "--repo", str(cloud)]) == 0
    recreated = remote_state_commit(origin)

    assert run(["recover", "--repo", str(local)]) == 0
    assert remote_state_commit(origin) == recreated
    # The loser keeps its own evidence and unions it upstream on the next sync.
    assert "local-delivery" in load_state(live_path(local))["alerts"]
    assert run(["sync", "--repo", str(local)]) == 0
    unioned = load_state(live_path(local))
    assert {"local-delivery", "cloud-delivery"} <= set(unioned["alerts"])
    synchronize(cloud)
    assert {"local-delivery", "cloud-delivery"} <= set(load_state(live_path(cloud))["alerts"])


def install_local_check(repo: Path) -> Path:
    """The shipped launchd script, with a Python stub that records a watcher run."""
    (repo / "scripts").mkdir(exist_ok=True)
    script = repo / "scripts" / "local-check.sh"
    script.write_text(
        (ROOT / "scripts" / "local-check.sh").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    script.chmod(0o755)
    (repo / ".env").write_text("", encoding="utf-8")
    (repo / ".venv" / "bin").mkdir(parents=True)
    stub = repo / ".venv" / "bin" / "python"
    stub.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "-m" ] && [ "$2" = "watcher.state_sync" ]; then\n'
        '  exec "$REAL_PYTHON" "$@"\n'
        "fi\n"
        "printf '%s\\n' \"$*\" >> watcher-invocations\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return script


def test_local_check_stops_before_the_watcher_when_the_ref_is_missing(
    tmp_path, two_clones
):
    """The pre-run sync tolerates transport failures and runs the watcher anyway.
    A confirmed absence is the exception: this clone would otherwise deliver
    from the tracked seed, re-sending history nobody has receipts for here."""
    origin, local, _ = two_clones
    deliver(local, HISTORIC_ALERT, "attempt-historic")
    synchronize(local)
    delete_state_ref(origin)

    fresh = clone_of(tmp_path, origin, "fresh")
    script = install_local_check(fresh)
    env = os.environ.copy()
    env.update({"PYTHONPATH": str(ROOT), "REAL_PYTHON": sys.executable})
    fired = subprocess.run(
        ["/bin/bash", str(script)],
        cwd=fresh,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert fired.returncode == BOOTSTRAP_REQUIRED_EXIT
    assert "missing" in fired.stderr
    assert not (fresh / "watcher-invocations").exists()
    assert not live_path(fresh).exists()
    assert remote_state_commit(origin) is None


def test_a_surviving_base_alone_still_proves_an_existing_installation(two_clones):
    """The live file can be lost on its own — a wiped `.cache` entry, a failed
    write — while `base.json` still records what was delivered. Seeding over
    that would make every alert it holds eligible again, so `init` must refuse
    and point at recovery, which reads that base as a store."""
    origin, local, _ = two_clones
    deliver(local, HISTORIC_ALERT, "attempt-historic")
    synchronize(local)
    base = local / DEFAULT_STORE_PATH / "base.json"
    assert HISTORIC_ALERT in load_state(base)["alerts"]
    live_path(local).unlink()
    delete_state_ref(origin)

    assert run(["sync", "--repo", str(local)]) == BOOTSTRAP_REQUIRED_EXIT
    assert run(["init", "--repo", str(local)]) == 1
    assert remote_state_commit(origin) is None
    assert not live_path(local).exists()  # no seed materialized behind the owner

    assert run(["recover", "--repo", str(local)]) == 0
    assert HISTORIC_ALERT in load_state(live_path(local))["alerts"]
    joined = clone_of(local.parent, origin, "joined-after-base-recovery")
    assert run(["sync", "--repo", str(joined)]) == 0
    assert HISTORIC_ALERT in load_state(live_path(joined))["alerts"]


def test_local_check_stops_before_the_watcher_even_with_local_receipts(two_clones):
    """The established clone is blocked at the wrapper too. Its own receipts say
    nothing about what the other half delivered into the ref, so continuing
    would re-send exactly the reminder the cloud already sent."""
    origin, local, _ = two_clones
    deliver(local, HISTORIC_ALERT, "attempt-historic")
    synchronize(local)
    delete_state_ref(origin)

    script = install_local_check(local)
    env = os.environ.copy()
    env.update({"PYTHONPATH": str(ROOT), "REAL_PYTHON": sys.executable})
    fired = subprocess.run(
        ["/bin/bash", str(script)],
        cwd=local,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert fired.returncode == BOOTSTRAP_REQUIRED_EXIT
    assert not (local / "watcher-invocations").exists()
    assert HISTORIC_ALERT in load_state(live_path(local))["alerts"]
    # The owner still learns about it: one durable marker, delivered as a loud
    # alert by the first pass that runs after recovery.
    assert load_failure(local / DEFAULT_MARKER_PATH) is not None
