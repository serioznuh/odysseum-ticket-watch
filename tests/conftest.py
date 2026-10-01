"""Shared test fixtures."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _neutral_ci_environment(monkeypatch):
    """Run every test as if off CI, whatever the host actually is.

    `build_error_finding` swaps its 403 wording when `GITHUB_ACTIONS` is set
    (OTW-03). Tests inheriting that from the environment assert one thing on a
    laptop and another on Actions — which is exactly how this fixture came to
    exist: the suite passed locally and failed in CI. Tests that care about the
    CI branch set the variable themselves with `monkeypatch.setenv`.
    """
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)


@pytest.fixture(autouse=True)
def _no_real_healthcheck_ping(monkeypatch):
    """Never let a sourced `.env` point a test firing at the real check.

    `local-check.sh` pings `HEALTHCHECK_PING_URL` at the end of every completed
    firing (OTW-39), and the wrapper tests hand it a copy of this environment.
    Tests that exercise the ping set the variable themselves, with a fake `curl`.
    """
    monkeypatch.delenv("HEALTHCHECK_PING_URL", raising=False)


@pytest.fixture(autouse=True)
def _snapshot_tap_in_tmp(monkeypatch, tmp_path_factory):
    """Keep the Pathé snapshot tap (OTW-38) out of the working copy's `.cache/`.

    Check-mode tests run real (non-dry) passes from the repository root, and
    the tap's directory is relative to it, like every other `.cache/` path.
    A directory of its own, so tests listing `tmp_path` see nothing new.
    """
    from watcher import tap

    monkeypatch.setattr(tap, "SNAPSHOT_DIR", tmp_path_factory.mktemp("pathe-snapshots"))
