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


def test_shipped_config_silences_every_kind_the_code_treats_as_quiet():
    """config.toml *overrides* DEFAULT_SILENT_KINDS rather than extending it,
    so a new quiet kind added only in code still buzzes in production. This
    caught WATCHER_STILL_BLIND buzzing once a day during an outage."""
    from pathlib import Path

    from watcher import notify
    from watcher.config import load_config

    cfg = load_config(Path(__file__).resolve().parent.parent / "config.toml")

    missing = [k for k in notify.DEFAULT_SILENT_KINDS if k not in cfg.silent_kinds]
    assert missing == [], (
        f"config.toml alerts.silent_kinds is missing {missing} — these kinds "
        "will notify loudly in production"
    )
