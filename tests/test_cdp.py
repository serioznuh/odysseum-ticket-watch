"""Lifecycle tests for the headed Chrome token step."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from watcher import cdp

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def test_launch_background_uses_hidden_offscreen_args(monkeypatch):
    calls = []
    previous = "/Applications/Notes.app"

    monkeypatch.setattr(cdp, "_frontmost_app", lambda: previous)

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
    def __init__(self, _url):
        self.closed = False

    def call(self, _method, params):
        if params["expression"] == "token":
            return {"result": {"result": {"value": "fresh-token"}}}
        raise AssertionError("the page title is not needed after a token is ready")

    def close(self):
        self.closed = True


def _patch_evaluation(monkeypatch, tmp_path, websocket=None):
    chrome = tmp_path / "Chrome"
    chrome.write_text("", encoding="utf-8")
    profile = tmp_path / "watcher-profile"
    previous = "/Applications/Notes.app"
    events = []
    launches = []

    monkeypatch.setattr(cdp, "_frontmost_app", lambda: previous)
    monkeypatch.setattr(cdp, "_free_port", lambda: 4321)

    def fake_launch(path, args, **kwargs):
        launches.append((path, args, kwargs))
        return previous

    monkeypatch.setattr(cdp, "_launch_background", fake_launch)
    monkeypatch.setattr(
        cdp,
        "_terminate_by_profile",
        lambda path: events.append(("cleanup", path)),
    )
    monkeypatch.setattr(
        cdp,
        "_restore_focus",
        lambda app: events.append(("focus", app)),
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
        def __init__(self, _url):
            pass

        def call(self, _method, _params):
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
        def __init__(self, _url):
            pass

        def call(self, _method, params):
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
    monkeypatch.setattr(cdp, "_profile_pids", lambda _profile: {101})
    monkeypatch.setattr(cdp, "CLEANUP_WAIT_SECONDS", 0.0)
    monkeypatch.setattr(cdp.os, "kill", lambda _pid, _signal: None)

    with caplog.at_level(logging.WARNING, logger=cdp.log.name):
        cdp._terminate_by_profile(str(tmp_path / "watcher-profile"))

    assert "did not exit" in caplog.text
