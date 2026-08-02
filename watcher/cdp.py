"""Read one value out of a page rendered by a real, headed Chrome.

Why this exists: www.cinesa.es sits behind a Cloudflare managed challenge that
serves the short-lived API token. A normal headed Chrome clears it on its own
in ~3 s; `--headless=new` is challenged and never settles (measured: token in
2.7 s headed, "Just a moment…" and no token after 45 s headless). So the token
step drives a real browser window — offscreen, on a throwaway profile. No
stealth, fingerprint spoofing or challenge solving is involved or wanted: if
Chrome itself stops clearing the challenge, this must fail loudly, not escalate.

Stdlib only (the watcher's dependency budget is httpx and nothing else), so the
CDP transport is a ~90-line websocket client. It only ever talks to a localhost
DevTools port with small text frames.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import logging
import os
import shlex
import socket
import struct
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
HARD_BLOCK_TITLE = "Attention Required!"
CLEANUP_WAIT_SECONDS = 3.0
CLEANUP_POLL_SECONDS = 0.1
_MISSING = object()


class CDPError(RuntimeError):
    """Chrome could not be driven, or the page never yielded the value."""


class _WebSocket:
    """Minimal RFC 6455 client: text frames, no extensions, no TLS (localhost)."""

    def __init__(self, url: str, timeout: float = 30.0):
        _, _, rest = url.partition("://")
        hostport, _, path = rest.partition("/")
        host, _, port = hostport.partition(":")
        self.sock = socket.create_connection((host, int(port or 80)), timeout=timeout)
        self.sock.settimeout(timeout)

        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall(
            (
                f"GET /{path} HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n"
            ).encode()
        )
        expect = base64.b64encode(
            hashlib.sha1((key + WS_GUID).encode()).digest()
        ).decode()

        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise CDPError("websocket handshake closed early")
            buf += chunk
        head, _, tail = buf.partition(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n")[0] or expect.encode() not in head:
            raise CDPError(f"websocket handshake rejected: {head[:120]!r}")
        self._buf = tail
        self._id = 0

    def _read(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise CDPError("websocket closed mid-frame")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _frame(self) -> tuple[bool, int, bytes]:
        b0, b1 = self._read(2)
        fin, opcode = bool(b0 & 0x80), b0 & 0x0F
        masked, length = b1 & 0x80, b1 & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._read(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._read(8))[0]
        mask = self._read(4) if masked else b""
        data = self._read(length)
        if masked:
            data = bytes(c ^ mask[i % 4] for i, c in enumerate(data))
        return fin, opcode, data

    def _recv(self) -> str:
        """One full message; answers pings, reassembles continuation frames."""
        payload = b""
        while True:
            fin, opcode, data = self._frame()
            if opcode == 0x9:  # ping
                self._send(0xA, data)
                continue
            if opcode == 0xA:  # stray pong
                continue
            if opcode == 0x8:
                raise CDPError("websocket closed by Chrome")
            payload += data
            if fin:
                return payload.decode("utf-8", "replace")

    def _send(self, opcode: int, payload: bytes) -> None:
        mask = os.urandom(4)
        n = len(payload)
        header = bytes([0x80 | opcode])
        if n < 126:
            header += bytes([0x80 | n])
        elif n < 65536:
            header += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            header += bytes([0x80 | 127]) + struct.pack(">Q", n)
        self.sock.sendall(
            header + mask + bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
        )

    def call(self, method: str, params: dict | None = None, timeout: float = 30.0) -> dict:
        self._id += 1
        mid = self._id
        self._send(
            0x1, json.dumps({"id": mid, "method": method, "params": params or {}}).encode()
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            msg = json.loads(self._recv())
            if msg.get("id") == mid:  # otherwise an unsolicited CDP event
                return msg
        raise CDPError(f"no CDP reply for {method} within {timeout}s")

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._send(0x8, b"")
        with contextlib.suppress(Exception):
            self.sock.close()


def _free_port() -> int:
    with contextlib.closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _frontmost_app() -> str | None:
    """Bundle path of the app that currently has focus, if it can be read."""
    try:
        asn = subprocess.run(
            ["lsappinfo", "front"], capture_output=True, text=True, timeout=5, check=True
        ).stdout.strip()
        if not asn:
            return None
        out = subprocess.run(
            ["lsappinfo", "info", "-only", "bundlepath", asn],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout
        path = out.partition("=")[2].strip().strip('"')
        return path or None
    except (OSError, subprocess.SubprocessError):
        return None


def _restore_focus(bundle_path: str | None) -> None:
    if not bundle_path:
        return
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(
            ["open", "-a", bundle_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=True,
        )


def _launch_background(
    chrome_path: str,
    args: list[str],
    *,
    previous_app: str | None | object = _MISSING,
) -> str | None:
    """Start Chrome hidden/backgrounded; returns the app that had focus.

    Chrome activates itself on launch even under `open -g -j` (measured: our
    own PID became frontmost), and on a twice-daily schedule that means Chrome
    grabbing the keyboard mid-sentence. `-g -j` still helps — the window starts
    hidden and offscreen — but focus has to be handed back explicitly, and only
    *after* Chrome's last activation point, or it simply steals it again.
    The window is still a real one, which is what clears the challenge;
    nothing here touches the challenge itself.
    """
    previous = _frontmost_app() if previous_app is _MISSING else previous_app
    bundle = chrome_path.split("/Contents/MacOS/")[0]
    subprocess.run(
        ["open", "-g", "-j", "-n", "-a", bundle, "--args", *args],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=30,
    )
    return previous


def _profile_pids(profile_dir: str) -> set[int] | None:
    """Return running Chrome PIDs whose exact profile argument is ours."""
    marker = f"--user-data-dir={os.path.abspath(profile_dir)}"
    try:
        listing = subprocess.run(
            ["ps", "-Ao", "pid=,command="],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        log.warning(
            "could not inspect Chrome processes for watcher profile %s;"
            " termination not confirmed",
            profile_dir,
        )
        return None

    pids = set()
    for line in listing.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        try:
            pid = int(parts[0])
            command = shlex.split(parts[1])
        except (ValueError, OSError):
            continue
        if marker in command:
            pids.add(pid)
    return pids


def _terminate_by_profile(profile_dir: str) -> None:
    """Terminate only our Chrome profile and confirm it goes away.

    `open` detaches, so there is no child PID to wait on. The profile path is
    unique to this watcher, so the user's own Chrome can never match. A short
    bounded wait catches a Chrome that ignored SIGTERM without holding the
    watcher open indefinitely.
    """
    pids = _profile_pids(profile_dir)
    if not pids:
        return

    deadline = time.monotonic() + CLEANUP_WAIT_SECONDS
    while pids:
        for pid in pids:
            try:
                os.kill(pid, 15)
            except (ProcessLookupError, PermissionError):
                continue

        current = _profile_pids(profile_dir)
        if current is None or not current:
            return
        pids = current
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(CLEANUP_POLL_SECONDS, remaining))

    if pids:
        log.warning(
            "Chrome processes for watcher profile %s did not exit within %.1fs: %s",
            profile_dir,
            CLEANUP_WAIT_SECONDS,
            ", ".join(str(pid) for pid in sorted(pids)),
        )


def _page_title(ws: _WebSocket) -> str:
    """Read the title without turning a transient CDP error into a failure."""
    try:
        value = (
            ws.call(
                "Runtime.evaluate",
                {"expression": "document.title", "returnByValue": True},
            )
            .get("result", {})
            .get("result", {})
            .get("value", "")
        )
    except (AttributeError, CDPError, OSError, TypeError, ValueError):
        return ""
    return value if isinstance(value, str) else ""


def _devtools(url: str, method: str = "GET", timeout: float = 5.0) -> Any:
    req = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def evaluate_on_page(
    url: str,
    expression: str,
    *,
    chrome_path: str = DEFAULT_CHROME,
    profile_dir: str = ".cache/chrome-profile",
    wait_seconds: float = 60.0,
    poll_seconds: float = 1.5,
) -> Any:
    """Load `url` in a real headed Chrome and poll `expression` until truthy.

    The window is placed far offscreen so a scheduled run does not steal focus
    or flash on screen. Chrome runs on a throwaway profile directory, never the
    user's own, and is always terminated before returning.
    """
    if not os.path.exists(chrome_path):
        raise CDPError(f"Chrome not found at {chrome_path} — set [cinesa] chrome_path")

    os.makedirs(profile_dir, exist_ok=True)
    profile = os.path.abspath(profile_dir)
    port = _free_port()
    previous_app = _frontmost_app()
    try:
        _launch_background(
            chrome_path,
            [
                f"--remote-debugging-port={port}",
                f"--user-data-dir={profile}",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-background-networking",
                "--window-size=1200,900",
                "--window-position=-32000,-32000",
                "about:blank",
            ],
            previous_app=previous_app,
        )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                _devtools(f"http://127.0.0.1:{port}/json/version")
                break
            except (urllib.error.URLError, OSError):
                time.sleep(0.3)
        else:
            raise CDPError("Chrome DevTools endpoint never came up")

        tab = _devtools(
            f"http://127.0.0.1:{port}/json/new?{urllib.parse.quote(url, safe=':/?=&%')}",
            method="PUT",
        )
        # Opening the tab is Chrome's last activation point, so hand focus back
        # now — doing it earlier just lets Chrome take it again.
        _restore_focus(previous_app)
        ws = _WebSocket(tab["webSocketDebuggerUrl"])
        try:
            deadline = time.monotonic() + wait_seconds
            title = ""
            while time.monotonic() < deadline:
                reply = ws.call(
                    "Runtime.evaluate", {"expression": expression, "returnByValue": True}
                )
                value = reply.get("result", {}).get("result", {}).get("value")
                if value:
                    return value
                title = _page_title(ws)
                if title.strip().startswith(HARD_BLOCK_TITLE):
                    raise CDPError(
                        f"Cloudflare hard block: page title is {title.strip()!r}"
                    )
                time.sleep(poll_seconds)
            if not title:
                title = _page_title(ws)
            raise CDPError(
                f"page never produced the value within {wait_seconds:.0f}s"
                f" (last page title: {title!r})"
            )
        finally:
            ws.close()
    finally:
        try:
            _terminate_by_profile(profile)
        except Exception:
            log.exception("unexpected error while cleaning up watcher Chrome")
        finally:
            _restore_focus(previous_app)
