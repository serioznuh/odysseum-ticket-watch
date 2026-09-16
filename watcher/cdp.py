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

from .budget import Budget

log = logging.getLogger(__name__)

DEFAULT_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
HARD_BLOCK_TITLE = "Attention Required!"
CLEANUP_WAIT_SECONDS = 3.0
CLEANUP_POLL_SECONDS = 0.1
_MISSING = object()

# Every blocking step's own ceiling — what it waits when there is no budget,
# i.e. exactly what it has always waited. With a budget, each one is clamped to
# whatever is left of it as well (`_step_timeout`), because shortening the two
# *phase* deadlines is not enough on its own: a single `open`, DevTools poll,
# socket read or `ps` can outlast the phase that contains it (OTW-19 round 2).
FOCUS_TIMEOUT_SECONDS = 5.0
LAUNCH_TIMEOUT_SECONDS = 30.0
RESTORE_FOCUS_TIMEOUT_SECONDS = 10.0
DEVTOOLS_TIMEOUT_SECONDS = 5.0
WEBSOCKET_TIMEOUT_SECONDS = 30.0
CDP_CALL_TIMEOUT_SECONDS = 30.0
PROCESS_LIST_TIMEOUT_SECONDS = 15.0
STARTUP_POLL_SECONDS = 0.3

# A socket read is re-armed before every recv rather than once at connect time,
# so it cannot block for the connect-time ceiling after the budget has run out.
# Floored so a read that straddles the deadline still fails by timeout rather
# than by turning the socket non-blocking.
MIN_SOCKET_READ_SECONDS = 0.25

# `ps` is how Chrome is discovered for termination, so a failed or timed-out
# listing is *unknown*, never "nothing to kill" — it gets another try, then the
# profile's own lock file as a fallback.
PROFILE_LOOKUP_ATTEMPTS = 2
PROCESS_QUERY_TIMEOUT_SECONDS = 5.0

# Chrome records the process holding a user-data-dir as a symlink to
# `<host>-<pid>`. Reading it needs no subprocess, so it survives the `ps`
# timeout that would otherwise leave us with no PID to signal.
CHROME_PROFILE_LOCK = "SingletonLock"

# Teardown gets its own allowance rather than a slice of the caller's: the
# throwaway profile must be torn down even when the mint ran out of time, and
# it must not then run unbounded. `evaluate_on_page` mints this in its
# `finally`, so it is a ceiling on cleanup no matter what happened before.
CLEANUP_BUDGET_SECONDS = 5.0


class CDPError(RuntimeError):
    """Chrome could not be driven, or the page never yielded the value."""


class ChromeLeakError(CDPError):
    """Chrome may still be running on the watcher profile.

    Deliberately its own type, because it means the opposite of every other
    CDPError. An ordinary mint failure — the challenge did not clear, Chrome is
    not installed, the budget ran out — leaves nothing behind, so a caller may
    safely fall back to the token it already holds. This one says the value may
    even have been read but a process was left holding the profile lock, which
    is what the *next* mint trips over. Falling back to a cached token here
    reports success and hides the leak until the watch has silently gone quiet,
    so every fallback must let this through (round-5 review).
    """


def _step_timeout(budget: Budget | None, ceiling: float) -> float:
    """One blocking step's timeout: its own ceiling, never past the budget."""
    return ceiling if budget is None else budget.timeout(ceiling)


def _require_time(budget: Budget | None, what: str) -> None:
    """Refuse to *start* a step there is no time left for.

    Paired with `_step_timeout`, this is what bounds the whole operation: no
    step begins after the deadline, and a step that begins just before it is
    clamped to the remainder.
    """
    if budget is not None and budget.expired():
        raise CDPError(f"Chrome step abandoned before {what}: {budget.exhausted_message()}")


def _nap(budget: Budget | None, seconds: float) -> None:
    if budget is None:
        time.sleep(seconds)
    else:
        budget.sleep(seconds)


class _WebSocket:
    """Minimal RFC 6455 client: text frames, no extensions, no TLS (localhost)."""

    def __init__(self, url: str, timeout: float = 30.0, budget: Budget | None = None):
        self._budget = budget
        self._timeout = timeout  # the ceiling; each read re-derives its own
        self._deadline: float | None = None  # set for the duration of one call
        _, _, rest = url.partition("://")
        hostport, _, path = rest.partition("/")
        host, _, port = hostport.partition(":")
        connect = _step_timeout(budget, timeout)
        self.sock = socket.create_connection((host, int(port or 80)), timeout=connect)
        self.sock.settimeout(connect)

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
            chunk = self._recv_bytes(4096, "the websocket handshake completing")
            if not chunk:
                raise CDPError("websocket handshake closed early")
            buf += chunk
        head, _, tail = buf.partition(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n")[0] or expect.encode() not in head:
            raise CDPError(f"websocket handshake rejected: {head[:120]!r}")
        self._buf = tail
        self._id = 0

    def _read_timeout(self) -> float:
        """Timeout for the next socket read, re-derived every time.

        The connect-time timeout is a *ceiling*, not a licence to block for it:
        clamping only the RPC deadline still let one `recv` that started with
        seconds left block for the original 30 s, overrunning the Cinesa and
        aggregate polling budgets (round-3 review).
        """
        limit = self._timeout
        if self._deadline is not None:
            limit = min(limit, self._deadline - time.monotonic())
        if self._budget is not None:
            limit = min(limit, self._budget.remaining())
        return max(MIN_SOCKET_READ_SECONDS, limit)

    def _recv_bytes(self, size: int, what: str) -> bytes:
        """One socket read, re-armed to whatever is actually left."""
        _require_time(self._budget, what)
        self.sock.settimeout(self._read_timeout())
        return self.sock.recv(size)

    def _read(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self._recv_bytes(65536, "the rest of a websocket frame")
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
        timeout = _step_timeout(self._budget, timeout)
        deadline = time.monotonic() + timeout
        # Published to the read path so each recv is bounded by this call's
        # remaining time as well, not just by the connect-time ceiling.
        self._deadline = deadline
        try:
            while time.monotonic() < deadline:
                msg = json.loads(self._recv())
                if msg.get("id") == mid:  # otherwise an unsolicited CDP event
                    return msg
        finally:
            self._deadline = None
        raise CDPError(f"no CDP reply for {method} within {timeout:.1f}s")

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._send(0x8, b"")
        with contextlib.suppress(Exception):
            self.sock.close()


def _free_port() -> int:
    with contextlib.closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _frontmost_app(budget: Budget | None = None) -> str | None:
    """Bundle path of the app that currently has focus, if it can be read."""
    try:
        asn = subprocess.run(
            ["lsappinfo", "front"],
            capture_output=True,
            text=True,
            timeout=_step_timeout(budget, FOCUS_TIMEOUT_SECONDS),
            check=True,
        ).stdout.strip()
        if not asn:
            return None
        out = subprocess.run(
            ["lsappinfo", "info", "-only", "bundlepath", asn],
            capture_output=True,
            text=True,
            timeout=_step_timeout(budget, FOCUS_TIMEOUT_SECONDS),
            check=True,
        ).stdout
        path = out.partition("=")[2].strip().strip('"')
        return path or None
    except (OSError, subprocess.SubprocessError):
        return None


def _restore_focus(bundle_path: str | None, budget: Budget | None = None) -> None:
    if not bundle_path:
        return
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(
            ["open", "-a", bundle_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_step_timeout(budget, RESTORE_FOCUS_TIMEOUT_SECONDS),
            check=True,
        )


def _launch_background(
    chrome_path: str,
    args: list[str],
    *,
    previous_app: str | None | object = _MISSING,
    budget: Budget | None = None,
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
    previous = (
        _frontmost_app(budget) if previous_app is _MISSING else previous_app
    )
    bundle = chrome_path.split("/Contents/MacOS/")[0]
    subprocess.run(
        ["open", "-g", "-j", "-n", "-a", bundle, "--args", *args],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=_step_timeout(budget, LAUNCH_TIMEOUT_SECONDS),
    )
    return previous


def _profile_pids(profile_dir: str, budget: Budget | None = None) -> set[int] | None:
    """Return running Chrome PIDs whose exact profile argument is ours."""
    marker = f"--user-data-dir={os.path.abspath(profile_dir)}"
    try:
        listing = subprocess.run(
            ["ps", "-Ao", "pid=,command="],
            capture_output=True,
            text=True,
            timeout=_step_timeout(budget, PROCESS_LIST_TIMEOUT_SECONDS),
            check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        # `None` means *unknown*, not "none running". The caller must not read a
        # failed or timed-out listing as "nothing to kill" (round-3 review).
        log.warning(
            "could not list Chrome processes for watcher profile %s", profile_dir
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


def _discover_profile_pids(
    profile_dir: str, budget: Budget | None
) -> set[int] | None:
    """PIDs on our profile, retrying a failed lookup. `None` means *unknown*.

    `ps` is the only way Chrome is discovered for termination, and it is now
    bounded like every other step, so it can time out. Treating that as "no
    processes" silently skipped SIGTERM and left a Chrome holding the profile
    lock — which the next mint then trips over (round-3 review).
    """
    for attempt in range(PROFILE_LOOKUP_ATTEMPTS):
        pids = _profile_pids(profile_dir, budget)
        if pids is not None:
            return pids
        if attempt + 1 == PROFILE_LOOKUP_ATTEMPTS or (budget and budget.expired()):
            break
        _nap(budget, CLEANUP_POLL_SECONDS)
    return None


def _locked_profile_pid(profile_dir: str) -> int | None:
    """The PID Chrome itself recorded as the owner of our profile.

    Read straight off `SingletonLock`, with no subprocess involved, so it still
    works when the `ps` listing that normally finds Chrome has timed out — and
    it names the very process holding the profile lock we must not leak.
    """
    try:
        target = os.readlink(os.path.join(profile_dir, CHROME_PROFILE_LOCK))
    except OSError:
        return None  # no lock: Chrome exited cleanly, or never got that far
    try:
        pid = int(target.rpartition("-")[2])
    except ValueError:
        log.warning("unrecognised Chrome profile lock target %r", target)
        return None
    return pid if pid > 0 else None


def _is_our_chrome(pid: int, profile_dir: str, budget: Budget | None) -> bool:
    """Whether `pid` really is a Chrome running on our profile.

    A stale lock file can name a PID the OS has since recycled, and signalling
    that would hit an unrelated process, so an unverifiable candidate is never
    signalled. This asks about one PID rather than the whole process table, so
    it can still answer when the full listing could not.
    """
    marker = f"--user-data-dir={os.path.abspath(profile_dir)}"
    try:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=_step_timeout(budget, PROCESS_QUERY_TIMEOUT_SECONDS),
            check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    try:
        return marker in shlex.split(out)
    except ValueError:
        return False


def _pids_from_profile_lock(
    profile_dir: str, budget: Budget | None
) -> set[int] | None:
    """Last-resort discovery: the profile's own lock, verified before use.

    `None` still means unknown — an unverifiable candidate is not signalled.
    """
    pid = _locked_profile_pid(profile_dir)
    if pid is None:
        return None
    if not _is_our_chrome(pid, profile_dir, budget):
        log.warning(
            "Chrome profile lock for %s names PID %d, which could not be"
            " confirmed as ours — not signalling it",
            profile_dir,
            pid,
        )
        return None
    log.warning(
        "process listing unavailable; falling back to the profile lock to"
        " terminate Chrome PID %d on %s",
        pid,
        profile_dir,
    )
    return {pid}


def _terminate_by_profile(profile_dir: str, budget: Budget | None = None) -> bool:
    """Terminate only our Chrome profile and confirm it goes away.

    `open` detaches, so there is no child PID to wait on. The profile path is
    unique to this watcher, so the user's own Chrome can never match. A short
    bounded wait catches a Chrome that ignored SIGTERM without holding the
    watcher open indefinitely.

    `budget` bounds the `ps` calls too, not only the confirmation wait — two
    15 s process listings already outlast the whole cleanup reserve on their
    own. SIGTERM is never skipped for want of time once a process is known;
    only the *confirmation* is cut short.

    Returns whether Chrome on our profile was signalled or was already gone.
    False means nothing could be discovered — by `ps` or by the profile lock —
    so nothing was signalled and a leftover instance may still hold the lock.
    The caller must not report a clean mint on that, or the leak repeats every
    run in silence (round-4 review).
    """
    pids = _discover_profile_pids(profile_dir, budget)
    if pids is None:
        pids = _pids_from_profile_lock(profile_dir, budget)
    if pids is None:
        log.error(
            "could not determine whether Chrome is still running on watcher"
            " profile %s, so nothing could be signalled — a leftover instance"
            " may hold the profile lock for the next mint",
            profile_dir,
        )
        return False
    if not pids:
        return True

    deadline = time.monotonic() + CLEANUP_WAIT_SECONDS
    while pids:
        for pid in pids:
            try:
                os.kill(pid, 15)
            except (ProcessLookupError, PermissionError):
                continue

        if budget is not None and budget.expired():
            break
        current = _discover_profile_pids(profile_dir, budget)
        if current is None:
            # SIGTERM went out; only the confirmation is missing. Say so rather
            # than returning as though Chrome had exited.
            log.warning(
                "signalled Chrome on watcher profile %s but could not confirm"
                " it exited: process lookup unavailable",
                profile_dir,
            )
            return True
        if not current:
            return True
        pids = current
        remaining = deadline - time.monotonic()
        if budget is not None:
            remaining = min(remaining, budget.remaining())
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
    return True  # signalled; a slow shutdown is not a leak


def _cleanup_chrome(profile: str, previous_app: str | None) -> bool:
    """Tear our Chrome down on its own allowance, and hand focus back.

    Returns whether Chrome was signalled (or was already gone). Never raises:
    the caller may be unwinding a failure whose cause must survive.
    """
    cleanup = Budget(CLEANUP_BUDGET_SECONDS, label="Chrome cleanup")
    try:
        return _terminate_by_profile(profile, cleanup)
    except Exception:
        log.exception("unexpected error while cleaning up watcher Chrome")
        return False
    finally:
        _restore_focus(previous_app, cleanup)


def _page_title(ws: _WebSocket, timeout: float = CDP_CALL_TIMEOUT_SECONDS) -> str:
    """Read the title without turning a transient CDP error into a failure."""
    try:
        value = (
            ws.call(
                "Runtime.evaluate",
                {"expression": "document.title", "returnByValue": True},
                timeout=timeout,
            )
            .get("result", {})
            .get("result", {})
            .get("value", "")
        )
    except (AttributeError, CDPError, OSError, TypeError, ValueError):
        return ""
    return value if isinstance(value, str) else ""


def _devtools(url: str, method: str = "GET", timeout: float = DEVTOOLS_TIMEOUT_SECONDS) -> Any:
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
    startup_seconds: float = 30.0,
    budget: Budget | None = None,
) -> Any:
    """Load `url` in a real headed Chrome and poll `expression` until truthy.

    The window is placed far offscreen so a scheduled run does not steal focus
    or flash on screen. Chrome runs on a throwaway profile directory, never the
    user's own, and is always terminated before returning.

    `startup_seconds` (DevTools coming up) and `wait_seconds` (the page
    producing the value) are the two phases' own ceilings. `budget` is the hard
    bound on the whole operation: no step *starts* after it is exhausted, and
    every blocking call — the `open` that launches Chrome, each DevTools HTTP
    poll, the websocket connect and handshake, every CDP round trip, and the
    `ps` calls during teardown — is clamped to what is left of it. Shortening
    the phases alone was not enough: any one of those calls carries its own
    fixed timeout and can outlast the phase containing it.

    Teardown is deliberately outside that bound, on its own small allowance, so
    the throwaway profile is still killed when the mint runs out of time. If it
    cannot establish that our Chrome was signalled, the mint fails even though
    the page produced a token: a leftover Chrome holding the profile lock breaks
    the *next* mint, and a run that reported success would hide it until someone
    noticed the watch had gone quiet.

    None of this touches how the challenge is cleared. It is the same real,
    headed browser on its own merits — it simply gets less patience.
    """
    if not os.path.exists(chrome_path):
        raise CDPError(f"Chrome not found at {chrome_path} — set [cinesa] chrome_path")

    os.makedirs(profile_dir, exist_ok=True)
    profile = os.path.abspath(profile_dir)
    port = _free_port()
    previous_app = _frontmost_app(budget)
    result: Any = _MISSING
    try:
        _require_time(budget, "launching Chrome")
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
            budget=budget,
        )
        deadline = time.monotonic() + startup_seconds
        while time.monotonic() < deadline and not (budget and budget.expired()):
            try:
                _devtools(
                    f"http://127.0.0.1:{port}/json/version",
                    timeout=_step_timeout(budget, DEVTOOLS_TIMEOUT_SECONDS),
                )
                break
            except (urllib.error.URLError, OSError):
                _nap(budget, STARTUP_POLL_SECONDS)
        else:
            raise CDPError(
                f"Chrome DevTools endpoint never came up within {startup_seconds:.0f}s"
                + ("" if budget is None else f" ({budget.exhausted_message()})")
            )

        _require_time(budget, "opening the token tab")
        tab = _devtools(
            f"http://127.0.0.1:{port}/json/new?{urllib.parse.quote(url, safe=':/?=&%')}",
            method="PUT",
            timeout=_step_timeout(budget, DEVTOOLS_TIMEOUT_SECONDS),
        )
        # Opening the tab is Chrome's last activation point, so hand focus back
        # now — doing it earlier just lets Chrome take it again.
        _restore_focus(previous_app, budget)
        ws = _WebSocket(
            tab["webSocketDebuggerUrl"],
            timeout=_step_timeout(budget, WEBSOCKET_TIMEOUT_SECONDS),
            budget=budget,
        )
        try:
            deadline = time.monotonic() + wait_seconds
            title = ""
            while time.monotonic() < deadline and not (budget and budget.expired()):
                reply = ws.call(
                    "Runtime.evaluate",
                    {"expression": expression, "returnByValue": True},
                    timeout=_step_timeout(budget, CDP_CALL_TIMEOUT_SECONDS),
                )
                value = reply.get("result", {}).get("result", {}).get("value")
                if value:
                    result = value
                    break
                title = _page_title(ws, _step_timeout(budget, CDP_CALL_TIMEOUT_SECONDS))
                if title.strip().startswith(HARD_BLOCK_TITLE):
                    raise CDPError(
                        f"Cloudflare hard block: page title is {title.strip()!r}"
                    )
                _nap(budget, poll_seconds)
            if result is _MISSING:
                if not title and not (budget and budget.expired()):
                    title = _page_title(
                        ws, _step_timeout(budget, CDP_CALL_TIMEOUT_SECONDS)
                    )
                raise CDPError(
                    f"page never produced the value within {wait_seconds:.0f}s"
                    f" (last page title: {title!r})"
                    + ("" if budget is None else f"; {budget.exhausted_message()}")
                )
        finally:
            ws.close()
    finally:
        signalled = _cleanup_chrome(profile, previous_app)

    # Only reached when the page produced a value: any failure above propagates
    # through the `finally` instead, keeping its own cause.
    if not signalled:
        raise ChromeLeakError(
            f"read the value, but Chrome on watcher profile {profile} could not be"
            " confirmed terminated — refusing to report a clean mint while a"
            " leftover instance may hold the profile lock"
        )
    return result
