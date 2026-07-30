"""Unit tests for watcher.config's load_config defaults."""

from __future__ import annotations

from watcher import notify
from watcher.config import load_config

MINIMAL_TOML = """
[film]
primary_slug = "dune-troisieme-partie"

[cinema]
slug = "montpellier-multiplexe-odysseum"
"""

NO_ALERTS_SECTION_TOML = MINIMAL_TOML

EMPTY_ALERTS_SECTION_TOML = MINIMAL_TOML + """
[alerts]
"""


def test_missing_silent_kinds_key_falls_back_to_notify_defaults(tmp_path):
    """A config.toml with no [alerts] section at all must still silence every
    kind notify.py considers non-urgent by default (CINESA_TARGET_NO_IMAX
    included) rather than only a hardcoded subset."""
    config = tmp_path / "config.toml"
    config.write_text(NO_ALERTS_SECTION_TOML, encoding="utf-8")

    cfg = load_config(config)

    assert cfg.silent_kinds == notify.DEFAULT_SILENT_KINDS
    assert "CINESA_TARGET_NO_IMAX" in cfg.silent_kinds


def test_empty_alerts_section_falls_back_to_notify_defaults(tmp_path):
    """An [alerts] section present but omitting silent_kinds must behave the
    same as no [alerts] section at all."""
    config = tmp_path / "config.toml"
    config.write_text(EMPTY_ALERTS_SECTION_TOML, encoding="utf-8")

    cfg = load_config(config)

    assert cfg.silent_kinds == notify.DEFAULT_SILENT_KINDS
    assert "CINESA_TARGET_NO_IMAX" in cfg.silent_kinds
