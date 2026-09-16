"""Lifecycle tests for the headed Chrome token step."""

from __future__ import annotations

import logging
import os
import subprocess
from types import SimpleNamespace

import pytest

from watcher import cdp

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def test_launch_background_uses_hidden_offscreen_args(monkeypatch):
    calls = []
    previous = "/Applications/Notes.app"

    monkeypatch.setattr(cdp, "_frontmost_app", lambda budget=None: previous)

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace()

    monkeypatch.setattr(cdp.subprocess, "run", fake_run)

    returned = cdp._launch_background(
        CHROME,
        [
            "--remote-debugging-port=1234",
            "--user-data-dir=/tmp/watcher-profile",
            "--window-size=1200,900",
            "--window-position=-32000,-32000",
            "about:blank",
        ],
    )

    assert returned == previous
    command, options = calls[0]
    assert command[:8] == [
        "open",
        "-g",
        "-j",
        "-n",
        "-a",
        "/Applications/Google Chrome.app",
        "--args",
        "--remote-debugging-port=1234",
    ]
    assert "--user-data-dir=/tmp/watcher-profile" in command
    assert "--window-position=-32000,-32000" in command
    assert options["check"] is True
    assert options["timeout"] == 30


class _SuccessfulWebSocket:
    def __init__(self, _url, timeout=30.0, budget=None):
        self.closed = False

    def call(self, _method, params, timeout=30.0):
        if params["expression"] == "token":
            return {"result": {"result": {"value": "fresh-token"}}}
        raise AssertionError("the page title is not needed after a token is ready")

    def close(self):
        self.closed = True


def _patch_evaluation(monkeypatch, tmp_path, websocket=None, signalled=True):
    chrome = tmp_path / "Chrome"
    chrome.write_text("", encoding="utf-8")
    profile = tmp_path / "watcher-profile"
    previous = "/Applications/Notes.app"
    events = []
    launches = []

    monkeypatch.setattr(cdp, "_frontmost_app", lambda budget=None: previous)
    monkeypatch.setattr(cdp, "_free_port", lambda: 4321)

    def fake_launch(path, args, **kwargs):
        launches.append((path, args, kwargs))
        return previous

    monkeypatch.setattr(cdp, "_launch_background", fake_launch)
    monkeypatch.setattr(
        cdp,
        "_terminate_by_profile",
        lambda path, budget=None: events.append(("cleanup", path)) or signalled,
    )
    monkeypatch.setattr(
        cdp,
        "_restore_focus",
        lambda app, budget=None: events.append(("focus", app)),
    )
    monkeypatch.setattr(cdp, "_WebSocket", websocket or _SuccessfulWebSocket)

    return chrome, profile, previous, events, launches


def test_successful_refresh_returns_token_and_restores_after_cleanup(monkeypatch, tmp_path):
    chrome, profile, previous, events, launches = _patch_evaluation(monkeypatch, tmp_path)

    def fake_devtools(url, method="GET", timeout=5.0):
        assert timeout == 5.0
        if url.endswith("/json/version"):
            return {"Browser": "Chrome"}
        assert method == "PUT"
        return {"webSocketDebuggerUrl": "ws://127.0.0.1:4321/devtools/page/1"}

    monkeypatch.setattr(cdp, "_devtools", fake_devtools)

    assert (
        cdp.evaluate_on_page(
            "https://www.cinesa.es/",
            "token",
            chrome_path=str(chrome),
            profile_dir=str(profile),
        )
        == "fresh-token"
    )
    assert launches[0][2]["previous_app"] == previous
    assert events == [
        ("focus", previous),
        ("cleanup", str(profile.resolve())),
        ("focus", previous),
    ]


@pytest.mark.parametrize("failure", ["launch", "startup", "late"])
def test_failures_after_launch_restore_focus_after_profile_cleanup(
    monkeypatch, tmp_path, failure
):
    class FailingWebSocket:
        def __init__(self, _url, timeout=30.0, budget=None):
            pass

        def call(self, _method, _params, timeout=30.0):
            raise cdp.CDPError("late CDP failure")

        def close(self):
            pass

    websocket = FailingWebSocket if failure == "late" else _SuccessfulWebSocket
    chrome, profile, previous, events, _launches = _patch_evaluation(
        monkeypatch, tmp_path, websocket
    )

    if failure == "launch":
        def failing_launch(_path, _args, **_kwargs):
            raise cdp.CDPError("Chrome launch failed")

        monkeypatch.setattr(cdp, "_launch_background", failing_launch)

    def fake_devtools(url, method="GET", timeout=5.0):
        if failure == "startup":
            raise cdp.CDPError("Chrome DevTools endpoint failed")
        if url.endswith("/json/version"):
            return {"Browser": "Chrome"}
        return {"webSocketDebuggerUrl": "ws://127.0.0.1:4321/devtools/page/1"}

    monkeypatch.setattr(cdp, "_devtools", fake_devtools)

    with pytest.raises(cdp.CDPError):
        cdp.evaluate_on_page(
            "https://www.cinesa.es/",
            "token",
            chrome_path=str(chrome),
            profile_dir=str(profile),
        )

    assert events[-2:] == [
        ("cleanup", str(profile.resolve())),
        ("focus", previous),
    ]


def test_hard_block_title_fails_without_waiting_for_normal_timeout(
    monkeypatch, tmp_path
):
    class HardBlockedWebSocket:
        def __init__(self, _url, timeout=30.0, budget=None):
            pass

        def call(self, _method, params, timeout=30.0):
            if params["expression"] == "token":
                return {"result": {"result": {"value": ""}}}
            assert params["expression"] == "document.title"
            return {
                "result": {
                    "result": {"value": "Attention Required! | Cloudflare"}
                }
            }

        def close(self):
            pass

    chrome, profile, _previous, _events, _launches = _patch_evaluation(
        monkeypatch, tmp_path, HardBlockedWebSocket
    )

    monkeypatch.setattr(
        cdp,
        "_devtools",
        lambda url, method="GET", timeout=5.0: (
            {"Browser": "Chrome"}
            if url.endswith("/json/version")
            else {"webSocketDebuggerUrl": "ws://127.0.0.1:4321/devtools/page/1"}
        ),
    )
    monkeypatch.setattr(
        cdp.time,
        "sleep",
        lambda _seconds: pytest.fail("hard-blocked page must not enter the poll wait"),
    )

    with pytest.raises(cdp.CDPError, match="Attention Required!"):
        cdp.evaluate_on_page(
            "https://www.cinesa.es/",
            "token",
            chrome_path=str(chrome),
            profile_dir=str(profile),
            wait_seconds=60,
        )


def test_profile_cleanup_targets_only_exact_watcher_profile(monkeypatch, tmp_path):
    watcher = tmp_path / "watcher-profile"
    own = tmp_path / "Chrome"
    sibling = tmp_path / "watcher-profile-other"
    listings = [
        (
            "101 /Applications/Google Chrome.app/Contents/MacOS/Google Chrome "
            f"--user-data-dir={watcher}\n"
            "202 /Applications/Google Chrome.app/Contents/MacOS/Google Chrome "
            f"--user-data-dir={own}\n"
            "303 /Applications/Google Chrome.app/Contents/MacOS/Google Chrome "
            f"--user-data-dir={sibling}\n"
        ),
        (
            "202 /Applications/Google Chrome.app/Contents/MacOS/Google Chrome "
            f"--user-data-dir={own}\n"
        ),
    ]
    killed = []

    def fake_run(command, **_kwargs):
        assert command == ["ps", "-Ao", "pid=,command="]
        return SimpleNamespace(stdout=listings.pop(0))

    monkeypatch.setattr(cdp.subprocess, "run", fake_run)
    monkeypatch.setattr(cdp.os, "kill", lambda pid, signal: killed.append((pid, signal)))

    cdp._terminate_by_profile(str(watcher))

    assert killed == [(101, 15)]
    assert listings == []


def test_profile_cleanup_warns_when_exit_cannot_be_confirmed(
    monkeypatch, caplog, tmp_path
):
    monkeypatch.setattr(cdp, "_profile_pids", lambda _profile, budget=None: {101})
    monkeypatch.setattr(cdp, "CLEANUP_WAIT_SECONDS", 0.0)
    monkeypatch.setattr(cdp.os, "kill", lambda _pid, _signal: None)

    with caplog.at_level(logging.WARNING, logger=cdp.log.name):
        cdp._terminate_by_profile(str(tmp_path / "watcher-profile"))

    assert "did not exit" in caplog.text


# ------------------------------------------- budget enforcement (OTW-19 r3)

class _FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def monotonic(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


class _StalledSocket:
    """A socket that never answers: every recv burns its whole timeout.

    Records the timeout in force for each read, which is the thing under test —
    a deadline that is only *checked* between reads cannot stop a recv that has
    already been armed with the connect-time ceiling.
    """

    def __init__(self, clock, handshake=b""):
        self.clock = clock
        self.timeouts: list[float] = []
        self._timeout = 0.0
        self._handshake = handshake

    def settimeout(self, seconds):
        self._timeout = seconds

    def sendall(self, _payload):
        pass

    def recv(self, _size):
        self.timeouts.append(self._timeout)
        self.clock.t += self._timeout
        if self._handshake:
            out, self._handshake = self._handshake, b""
            return out
        raise TimeoutError("stalled socket")

    def close(self):
        pass


def _stalled_websocket(monkeypatch, clock, budget):
    """A real `_WebSocket` over a stalled socket, past the handshake."""
    handshake = b"HTTP/1.1 101 x\r\n\r\n"
    sock = _StalledSocket(clock, handshake)
    monkeypatch.setattr(cdp.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(cdp.time, "sleep", clock.sleep)
    monkeypatch.setattr(cdp.socket, "create_connection", lambda *a, **kw: sock)
    monkeypatch.setattr(cdp, "_require_time", lambda *_a, **_kw: None)
    ws = cdp._WebSocket.__new__(cdp._WebSocket)
    ws._budget = budget
    ws._timeout = cdp.WEBSOCKET_TIMEOUT_SECONDS
    ws._deadline = None
    ws.sock = sock
    ws._buf = b""
    ws._id = 0
    # What __init__ arms at connect time, and what the read path must not keep
    # using once the budget or the call deadline has nearly run out.
    sock.settimeout(cdp.WEBSOCKET_TIMEOUT_SECONDS)
    sock.timeouts.clear()
    return ws, sock


def test_a_socket_read_is_rearmed_from_the_remaining_budget(monkeypatch):
    """The connect-time timeout is a ceiling, not a licence to block for it.

    A CDP call that starts with a couple of seconds left used to hand the
    already-armed 30 s socket timeout to `recv`, so one read could outlast the
    whole Cinesa job budget before the deadline was looked at again.
    """
    clock = _FakeClock()
    budget = cdp.Budget(
        3.0, monotonic=clock.monotonic, sleep=clock.sleep, label="Cinesa token mint"
    )
    ws, sock = _stalled_websocket(monkeypatch, clock, budget)

    with pytest.raises((cdp.CDPError, TimeoutError)):
        ws.call("Runtime.evaluate", {"expression": "token"}, timeout=30.0)

    assert sock.timeouts, "the read path was never exercised"
    # Every read was armed from what was actually left, never from the ceiling.
    assert max(sock.timeouts) < cdp.WEBSOCKET_TIMEOUT_SECONDS
    assert clock.t <= 3.0 + cdp.MIN_SOCKET_READ_SECONDS + 1.0


def test_an_unbudgeted_read_is_still_capped_by_the_calls_own_deadline(monkeypatch):
    """Even with no budget, a read must not outlive the RPC it belongs to."""
    clock = _FakeClock()
    ws, sock = _stalled_websocket(monkeypatch, clock, None)

    with pytest.raises((cdp.CDPError, TimeoutError)):
        ws.call("Runtime.evaluate", {"expression": "token"}, timeout=5.0)

    assert max(sock.timeouts) <= 5.0


def test_a_stalled_process_lookup_never_reads_as_nothing_to_kill(
    monkeypatch, caplog, tmp_path
):
    """`ps` is how Chrome is discovered for termination, and it is bounded like
    every other step, so it can time out. Treating that as "no processes" (it
    returns None, which is falsy) skipped SIGTERM and left a Chrome holding the
    watcher profile lock for the next mint."""
    attempts = []

    def stalled_ps(_command, **kwargs):
        attempts.append(kwargs["timeout"])
        raise subprocess.TimeoutExpired(cmd="ps", timeout=kwargs["timeout"])

    monkeypatch.setattr(cdp.subprocess, "run", stalled_ps)
    monkeypatch.setattr(cdp.os, "kill", lambda _pid, _sig: pytest.fail("no PID known"))

    with caplog.at_level(logging.WARNING, logger=cdp.log.name):
        cdp._terminate_by_profile(str(tmp_path / "watcher-profile"))

    # Retried rather than believed, then reported loudly instead of silently
    # passing for a clean exit.
    assert len(attempts) == cdp.PROFILE_LOOKUP_ATTEMPTS
    assert "could not determine whether Chrome is still running" in caplog.text
    assert any(record.levelno >= logging.ERROR for record in caplog.records)


def test_a_lookup_that_fails_only_after_the_kill_still_reports_uncertainty(
    monkeypatch, caplog, tmp_path
):
    """Discovery worked once, so SIGTERM went out. The follow-up lookup failing
    means "unconfirmed", not "exited"."""
    profile = tmp_path / "watcher-profile"
    listings = [f"101 /Chrome --user-data-dir={os.path.abspath(profile)}\n"]
    killed = []

    def flaky_ps(_command, **kwargs):
        if listings:
            return SimpleNamespace(stdout=listings.pop(0))
        raise subprocess.TimeoutExpired(cmd="ps", timeout=kwargs["timeout"])

    monkeypatch.setattr(cdp.subprocess, "run", flaky_ps)
    monkeypatch.setattr(cdp.os, "kill", lambda pid, _sig: killed.append(pid))

    with caplog.at_level(logging.WARNING, logger=cdp.log.name):
        cdp._terminate_by_profile(str(profile))

    assert killed == [101]
    assert "could not confirm" in caplog.text


def test_the_profile_lock_still_finds_chrome_when_ps_cannot(
    monkeypatch, caplog, tmp_path
):
    """`ps` is not the only way to find our Chrome: Chrome itself records the
    owning PID in the profile's SingletonLock, and reading a symlink cannot
    time out the way a process listing can. "Unknown" must not mean "give up"
    (round-4 review)."""
    profile = tmp_path / "watcher-profile"
    profile.mkdir()
    os.symlink("somehost-4242", profile / cdp.CHROME_PROFILE_LOCK)
    killed = []
    queried = []

    def ps(command, **kwargs):
        if command[:2] == ["ps", "-p"]:  # the narrow verification query
            queried.append(command)
            return SimpleNamespace(
                stdout=f"/Chrome --user-data-dir={os.path.abspath(profile)}\n"
            )
        raise subprocess.TimeoutExpired(cmd="ps", timeout=kwargs["timeout"])

    monkeypatch.setattr(cdp.subprocess, "run", ps)
    monkeypatch.setattr(cdp.os, "kill", lambda pid, _sig: killed.append(pid))
    monkeypatch.setattr(cdp, "_discover_profile_pids", lambda *_a: None)

    with caplog.at_level(logging.WARNING, logger=cdp.log.name):
        assert cdp._terminate_by_profile(str(profile)) is True

    assert killed == [4242]  # signalled, not merely logged about
    assert queried, "the candidate PID must be verified before signalling"
    assert "falling back to the profile lock" in caplog.text


def test_a_recycled_lock_pid_is_never_signalled(monkeypatch, caplog, tmp_path):
    """A stale lock can name a PID the OS has since handed to something else.
    Killing that would be far worse than leaking a Chrome, so an unverifiable
    candidate is reported instead of signalled."""
    profile = tmp_path / "watcher-profile"
    profile.mkdir()
    os.symlink("somehost-4242", profile / cdp.CHROME_PROFILE_LOCK)

    monkeypatch.setattr(
        cdp.subprocess,
        "run",
        lambda command, **_kw: SimpleNamespace(stdout="/usr/bin/some-other-app\n"),
    )
    monkeypatch.setattr(cdp.os, "kill", lambda *_a: pytest.fail("signalled a stranger"))
    monkeypatch.setattr(cdp, "_discover_profile_pids", lambda *_a: None)

    with caplog.at_level(logging.WARNING, logger=cdp.log.name):
        assert cdp._terminate_by_profile(str(profile)) is False

    assert "could not be confirmed as ours" in caplog.text


def test_a_mint_that_cannot_confirm_teardown_is_not_reported_as_clean(
    monkeypatch, tmp_path
):
    """The gap this closes: a token was read, cleanup could signal nothing, and
    the run still returned successfully — so a Chrome holding the profile lock
    broke the *next* mint with nothing in this run to explain it."""
    chrome, profile, _previous, events, _launches = _patch_evaluation(
        monkeypatch, tmp_path, signalled=False
    )
    monkeypatch.setattr(
        cdp,
        "_devtools",
        lambda url, method="GET", timeout=5.0: (
            {"Browser": "Chrome"}
            if url.endswith("/json/version")
            else {"webSocketDebuggerUrl": "ws://127.0.0.1:4321/devtools/page/1"}
        ),
    )

    with pytest.raises(cdp.CDPError, match="could not be confirmed terminated"):
        cdp.evaluate_on_page(
            "https://www.cinesa.es/",
            "token",
            chrome_path=str(chrome),
            profile_dir=str(profile),
        )

    # Cleanup and the focus handoff still ran; only the verdict changed.
    assert ("cleanup", str(profile.resolve())) in events
