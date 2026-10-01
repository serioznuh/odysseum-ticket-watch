"""OTW-38: the snapshot tap records each Pathé poll and never touches alerts."""

from __future__ import annotations

import errno
import gzip
import json
import os
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from watcher import __main__ as cli
from watcher import detect, jobs, notify, pathe, tap
from watcher.config import load_config
from watcher.detect import TZ_PARIS, Snapshot
from watcher.state import DEFAULT_STATE

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=TZ_PARIS)
PRIMARY = "dune-troisieme-partie-50828"
EVENT = "dune-troisieme-partie-projection-imax-70mm-55289"
CINEMA = "cinema-pathe-odysseum"
SALE = "2026-11-05T08:00:00+01:00"
BLOCK = (
    "Pathé API request failed for https://www.pathe.fr/api/shows: "
    "Client error '403 Forbidden' for url 'https://www.pathe.fr/api/shows'\n"
    "For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/403"
)


# Captured at import: `run_job` replaces `pathe.fetch_snapshot` with a fake.
REAL_FETCH = pathe.fetch_snapshot


@pytest.fixture(autouse=True)
def _no_politeness_sleeps(monkeypatch):
    monkeypatch.setattr(pathe.time, "sleep", lambda _s: None)


def handler_for(*, bookable_day: str = "2026-12-19", showtimes_status: int = 200):
    """A Pathé that lists the film and its IMAX 70 mm event, with the event's
    wanted date bookable and an unrelated film in the programme."""
    catalogue = {
        "shows": [
            {"slug": PRIMARY, "title": "Dune : Troisième partie", "isMovie": True,
             "salesOpeningDatetime": SALE},
            {"slug": EVENT, "title": "Dune : Troisième partie : Projection IMAX 70mm",
             "isMovie": False, "salesOpeningDatetime": SALE},
            {"slug": "another-film-1", "title": "Another film"},
        ]
    }
    programme = {
        "days": {"2026-10-01": {}},
        "shows": {
            PRIMARY: {"bookable": False, "days": {}},
            EVENT: {"isBookable": True, "days": {bookable_day: {"bookable": True}}},
            "another-film-1": {"bookable": True, "days": {"2026-10-02": {"bookable": True}}},
        },
    }
    sessions = {"2026-12-16": [{"time": "2026-12-16T20:00:00+01:00", "tags": ["imax"],
                                "status": "available", "refCmd": "https://example/booking"}]}

    def handler(request):
        path = request.url.path
        if path.endswith("/api/shows"):
            return httpx.Response(200, json=catalogue)
        if path.endswith(f"/cinema/{CINEMA}/shows"):
            return httpx.Response(200, json=programme)
        if EVENT in path and "/showtimes/" in path:
            return httpx.Response(
                403, headers={"content-type": "application/json"}, text='"No movie allowed !"'
            )
        if "/showtimes/" in path:
            if showtimes_status != 200:
                return httpx.Response(showtimes_status)
            return httpx.Response(200, json=sessions)
        return httpx.Response(200, json={"slug": path.rsplit("/", 1)[-1]})

    return handler


def fetch(**kwargs) -> Snapshot:
    client = httpx.Client(
        transport=httpx.MockTransport(handler_for(**kwargs)), base_url=pathe.BASE
    )
    return REAL_FETCH(client, load_config("config.toml"))


def make_ctx(**kwargs) -> jobs.RunContext:
    return jobs.RunContext(
        cfg=load_config("config.toml"),
        state=deepcopy(DEFAULT_STATE),
        clock=lambda: NOW,
        **kwargs,
    )


def run_job(monkeypatch, result, now=NOW, ctx=None):
    """One Pathé job; `result` is a snapshot to return or an exception to raise."""

    def fake_fetch(client, cfg, **_budget):
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(pathe, "fetch_snapshot", fake_fetch)
    ctx = ctx or make_ctx()
    return ctx, jobs.run_pathe_job(ctx, object(), now, None)


def saved_files():
    return sorted(p.name for p in tap.SNAPSHOT_DIR.glob("*.json.gz"))


def index_lines():
    index = tap.SNAPSHOT_DIR / tap.INDEX_NAME
    return index.read_text(encoding="utf-8").splitlines() if index.exists() else []


def test_a_healthy_poll_writes_one_file_named_by_its_utc_time(monkeypatch):
    snap = fetch()
    assert snap.healthy

    run_job(monkeypatch, snap)

    assert saved_files() == ["20261001T100000Z.json.gz"]
    assert index_lines() == []
    record = tap.read_file(tap.SNAPSHOT_DIR / "20261001T100000Z.json.gz")
    assert record["poll_time"] == "2026-10-01T12:00:00+02:00"
    assert record["health"] == "healthy"
    content = record["snapshot"]
    # The whole programme, not only the matched entries: a replay sees what
    # this poll saw, other films included.
    assert "another-film-1" in content["cinema_programme"]["shows"]
    assert content["listing_results"][EVENT]["showtimes"]["health"] == "expected_refusal"
    assert content["listing_results"][PRIMARY]["showtimes"]["data"]["2026-12-16"]


def test_an_identical_next_poll_writes_no_file_and_appends_same(monkeypatch):
    run_job(monkeypatch, fetch())
    run_job(monkeypatch, fetch(), now=NOW + timedelta(minutes=5))

    assert saved_files() == ["20261001T100000Z.json.gz"]
    assert index_lines() == ["2026-10-01T12:05:00+02:00 same 20261001T100000Z.json.gz"]


def test_a_changed_poll_writes_a_new_file(monkeypatch):
    run_job(monkeypatch, fetch())
    run_job(monkeypatch, fetch(bookable_day="2026-12-20"), now=NOW + timedelta(minutes=5))

    assert saved_files() == ["20261001T100000Z.json.gz", "20261001T100500Z.json.gz"]
    assert index_lines() == []


def test_a_degraded_poll_is_saved_with_its_diagnostic(monkeypatch):
    snap = fetch(showtimes_status=500)
    assert not snap.healthy

    run_job(monkeypatch, snap)

    record = tap.read_file(tap.SNAPSHOT_DIR / saved_files()[0])
    assert record["health"] == "degraded"
    result = record["snapshot"]["listing_results"][PRIMARY]["showtimes"]
    assert result["health"] == "unexpected_failure"
    assert "500 Internal Server Error" in result["diagnostic"]


def test_a_failed_poll_appends_its_error_on_one_line(monkeypatch):
    run_job(monkeypatch, RuntimeError(BLOCK))

    assert saved_files() == []
    assert index_lines() == [
        "2026-10-01T12:00:00+02:00 failed: HTTP 403 Forbidden from https://www.pathe.fr/api/shows"
    ]


def test_a_long_multiline_error_stays_one_bounded_line(monkeypatch):
    run_job(monkeypatch, RuntimeError("boom\n" + "x" * 2000))

    (line,) = index_lines()
    assert line.startswith("2026-10-01T12:00:00+02:00 failed: boom x")
    assert len(line) <= len("2026-10-01T12:00:00+02:00 failed: ") + tap.MAX_SUMMARY


def test_a_saved_file_rebuilds_a_snapshot_with_the_same_findings(monkeypatch):
    original = fetch()
    run_job(monkeypatch, original)

    rebuilt = tap.load_snapshot(tap.SNAPSHOT_DIR / saved_files()[0])

    assert rebuilt == original
    assert rebuilt.cinema_programme == original.cinema_programme
    cfg = load_config("config.toml")
    want = detect.analyze_pathe(original, deepcopy(DEFAULT_STATE), cfg, NOW)
    got = detect.analyze_pathe(rebuilt, deepcopy(DEFAULT_STATE), cfg, NOW)
    assert got == want
    # Not a vacuous comparison: the wanted IMAX 70 mm date is in there.
    assert any(f.kind == "PATHE_TARGET_DATE" for f in want)


@pytest.mark.parametrize("result", ["snapshot", "failure"])
def test_a_dry_run_writes_nothing(monkeypatch, result):
    outcome = fetch() if result == "snapshot" else RuntimeError(BLOCK)

    run_job(monkeypatch, outcome, ctx=make_ctx(dry_run=True))

    assert list(tap.SNAPSHOT_DIR.iterdir()) == []


def _write_old_file(age: timedelta, content: str = "old") -> str:
    name = (NOW - age).astimezone(timezone.utc).strftime(tap.NAME_FORMAT) + tap.SUFFIX
    record = {"version": tap.FORMAT_VERSION, "digest": content, "snapshot": {}}
    (tap.SNAPSHOT_DIR / name).write_bytes(gzip.compress(json.dumps(record).encode()))
    return name


def test_rotation_deletes_old_files_but_keeps_the_baseline_of_later_same_lines():
    very_old = _write_old_file(timedelta(days=40), "a")
    baseline = _write_old_file(timedelta(days=31), "b")
    recent = _write_old_file(timedelta(days=10), "c")
    unrelated = tap.SNAPSHOT_DIR / "notes.txt"
    unrelated.write_text("kept")

    tap.record_poll(fetch(), NOW)

    assert very_old not in saved_files()
    # Polls between the cutoff and `recent` said "same" as this one.
    assert baseline in saved_files()
    assert recent in saved_files()
    assert "20261001T100000Z.json.gz" in saved_files()
    assert unrelated.exists()


def test_index_lines_past_retention_are_trimmed_once_a_day_not_every_poll():
    index = tap.SNAPSHOT_DIR / tap.INDEX_NAME
    just_expired = (NOW - timedelta(days=30, hours=1)).isoformat()
    in_window = (NOW - timedelta(days=2)).isoformat()
    index.write_text(f"{just_expired} same x\n{in_window} failed: boom\n")

    tap.record_failure("again", NOW)
    # Within the slack: left alone rather than rewritten on every poll.
    assert index_lines()[0].startswith(just_expired)

    tap.record_failure("later", NOW + timedelta(days=1))
    assert index_lines() == [
        f"{in_window} failed: boom",
        "2026-10-01T12:00:00+02:00 failed: again",
        "2026-10-02T12:00:00+02:00 failed: later",
    ]


def test_nothing_is_written_once_the_directory_reaches_the_cap(monkeypatch, caplog):
    tap.record_poll(fetch(), NOW)
    size = sum(p.stat().st_size for p in tap.SNAPSHOT_DIR.iterdir())
    monkeypatch.setattr(tap, "SIZE_CAP_BYTES", size)

    with caplog.at_level("WARNING"):
        assert tap.record_poll(fetch(bookable_day="2026-12-20"), NOW + timedelta(minutes=5)) is None
        assert tap.record_failure(BLOCK, NOW + timedelta(minutes=10)) is False
        assert tap.record_poll(fetch(), NOW + timedelta(minutes=15)) is None

    assert saved_files() == ["20261001T100000Z.json.gz"]
    assert index_lines() == []
    assert "not recorded" in caplog.text


def test_rotation_runs_before_the_cap_so_old_files_free_room(monkeypatch):
    _write_old_file(timedelta(days=50), os.urandom(4000).hex())
    _write_old_file(timedelta(days=40), "b")
    monkeypatch.setattr(
        tap,
        "SIZE_CAP_BYTES",
        sum(p.stat().st_size for p in tap.SNAPSHOT_DIR.iterdir()) - 1,
    )

    assert tap.record_poll(fetch(), NOW) == "20261001T100000Z.json.gz"


def test_a_temporary_file_left_by_a_killed_firing_is_removed_later():
    stale = tap.SNAPSHOT_DIR / ".20260901T000000Z.json.gz.abc.tmp"
    fresh = tap.SNAPSHOT_DIR / ".20261001T100000Z.json.gz.def.tmp"
    stale.write_bytes(b"partial")
    fresh.write_bytes(b"in progress")
    hours_ago = os.stat(stale).st_mtime - 2 * tap.STALE_TMP_SECONDS
    os.utime(stale, (hours_ago, hours_ago))

    tap.record_failure("boom", NOW)

    assert not stale.exists()
    assert fresh.exists()


def test_a_failed_write_leaves_no_partial_file(monkeypatch):
    def broken_replace(src, dst):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(tap.os, "replace", broken_replace)

    with pytest.raises(OSError):
        tap.record_poll(fetch(), NOW)

    assert list(tap.SNAPSHOT_DIR.iterdir()) == []


# ---------------------------------------------------------- never the alert path

def _break_tap(monkeypatch, how: str) -> None:
    if how == "unwritable":
        # A file where the directory should be: mkdir fails, as on a
        # permission error, whoever runs the test.
        target = tap.SNAPSHOT_DIR.parent / "not-a-directory"
        target.write_text("")
        monkeypatch.setattr(tap, "SNAPSHOT_DIR", target / "pathe-snapshots")
    elif how == "full disk":
        def no_space(*_a, **_k):
            raise OSError(errno.ENOSPC, "No space left on device")

        monkeypatch.setattr(tap, "_atomic_write", no_space)
        monkeypatch.setattr(tap, "_append_index", no_space)
    elif how == "cap reached":
        monkeypatch.setattr(tap, "SIZE_CAP_BYTES", 0)
    elif how == "odd snapshot":
        monkeypatch.setattr(
            tap, "snapshot_content", lambda snap: {"x": object()}  # not JSON
        )


@pytest.mark.parametrize("how", ["unwritable", "full disk", "cap reached", "odd snapshot"])
@pytest.mark.parametrize("result", ["snapshot", "failure"])
def test_a_broken_tap_leaves_findings_and_state_unchanged(monkeypatch, caplog, how, result):
    def outcome():
        return fetch() if result == "snapshot" else RuntimeError(BLOCK)

    real_poll, real_failure = tap.record_poll, tap.record_failure
    monkeypatch.setattr(tap, "record_poll", lambda *a, **k: None)
    monkeypatch.setattr(tap, "record_failure", lambda *a, **k: None)
    base_ctx, baseline = run_job(monkeypatch, outcome())
    monkeypatch.setattr(tap, "record_poll", real_poll)
    monkeypatch.setattr(tap, "record_failure", real_failure)
    _break_tap(monkeypatch, how)

    with caplog.at_level("WARNING"):
        ctx, got = run_job(monkeypatch, outcome())

    assert got.findings == baseline.findings
    assert (got.health, got.error_key) == (baseline.health, baseline.error_key)
    assert ctx.state == base_ctx.state
    if how == "cap reached":
        assert "not recorded" in caplog.text
    elif not (how == "odd snapshot" and result == "failure"):
        assert "snapshot tap failed" in caplog.text


CLI_CONFIG = """
[film]
primary_slug = "dune-troisieme-partie"

[cinema]
slug = "montpellier-multiplexe-odysseum"
name = "Pathé Odysseum"
city = "Montpellier"

[news]
enabled = false

[alerts]
heartbeat_days = 0
failure_streak_threshold = 3
stale_check_hours = 0
"""


@pytest.mark.parametrize("how", ["working", "unwritable", "full disk"])
def test_a_broken_tap_leaves_alerts_state_and_exit_status_unchanged(
    tmp_path, monkeypatch, how
):
    """End to end through the CLI: the same message, the same receipts, exit 0."""
    config = tmp_path / "config.toml"
    config.write_text(CLI_CONFIG, encoding="utf-8")
    state = tmp_path / "state.json"
    state.write_text(json.dumps(DEFAULT_STATE), encoding="utf-8")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:secret-bot-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "987654321")
    monkeypatch.setattr(pathe, "make_client", object)
    slug = "dune-troisieme-partie"
    show = {"slug": slug, "title": "Dune : Troisième partie",
            "salesOpeningDatetime": SALE, "isMovie": True}
    monkeypatch.setattr(
        pathe, "fetch_snapshot", lambda client, cfg, **kw: Snapshot(matched_shows=[show])
    )
    sent = []
    monkeypatch.setattr(notify, "send_telegram", lambda cfg, text, **kw: sent.append(text) or True)
    snapshot_dir = tap.SNAPSHOT_DIR
    if how != "working":
        _break_tap(monkeypatch, how)

    status = cli.run(["--config", str(config), "--state", str(state), "--mode", "check"])

    assert status == 0
    assert len(sent) == 1
    st = json.loads(state.read_text(encoding="utf-8"))
    assert f"sale:{slug}:{SALE}" in st["alerts"]
    assert st["sales"] == {slug: SALE}
    assert not any("snapshot" in key or "tap" in key for key in st)
    if how == "working":
        (saved,) = snapshot_dir.glob("*.json.gz")
        raw = gzip.decompress(saved.read_bytes()).decode()
        assert "secret-bot-token" not in raw
        assert "987654321" not in raw


def test_a_dry_run_through_the_cli_writes_nothing(tmp_path, monkeypatch):
    config = tmp_path / "config.toml"
    config.write_text(CLI_CONFIG, encoding="utf-8")
    state = tmp_path / "state.json"
    state.write_text(json.dumps(DEFAULT_STATE), encoding="utf-8")
    monkeypatch.setattr(pathe, "make_client", object)
    monkeypatch.setattr(
        pathe, "fetch_snapshot", lambda client, cfg, **kw: Snapshot(matched_shows=[])
    )

    status = cli.run(
        ["--config", str(config), "--state", str(state), "--mode", "check", "--dry-run"]
    )

    assert status == 0
    assert list(tap.SNAPSHOT_DIR.iterdir()) == []
