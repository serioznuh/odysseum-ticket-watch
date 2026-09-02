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
