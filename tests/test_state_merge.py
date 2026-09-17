"""Domain-aware three-way state reconciliation from OTW-14."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime

import pytest

from watcher.state import DEFAULT_STATE, due_reminders, load_state
from watcher.state_merge import StateMergeError, merge_states, run

MERGE_NOW = datetime.fromisoformat("2026-09-17T12:00:00+02:00")


def fresh_state() -> dict:
    return deepcopy(DEFAULT_STATE)


def test_merge_preserves_both_delivery_histories_and_acknowledged_baselines():
    base = fresh_state()
    upstream = fresh_state()
    local = fresh_state()
    target = "2026-12-01T09:00:00+01:00"

    upstream["alerts"] = {
        f"sale:cloud-listing:{target}": "2026-10-01T10:00:00+02:00",
        "cloud-only": "2026-10-01T10:01:00+02:00",
    }
    upstream["reminders_sent"] = {target: ["1440", "120"]}
    upstream["shows_seen"] = ["cloud-listing"]
    upstream["formats_seen"] = {"cloud-listing": ["imax70"]}
    upstream["sales"] = {"cloud-listing": target}
    upstream["sale_target"] = target

    local["alerts"] = {
        "tickets:local-listing:imax70": "2026-10-01T10:02:00+02:00",
        "local-only": "2026-10-01T10:03:00+02:00",
    }
    local["reminders_sent"] = {target: ["15"]}
    local["shows_seen"] = ["local-listing"]
    local["formats_seen"] = {"local-listing": ["imax70"]}
    local["tickets_available"] = True
    local["last_check_ok"] = "2026-10-01T10:03:00+02:00"

    merged = merge_states(base, upstream, local, now=MERGE_NOW)

    assert set(merged["alerts"]) == {
        f"sale:cloud-listing:{target}",
        "cloud-only",
        "tickets:local-listing:imax70",
        "local-only",
    }
    assert merged["reminders_sent"] == {target: ["120", "1440", "15"]}
    assert merged["shows_seen"] == ["cloud-listing", "local-listing"]
    assert merged["formats_seen"] == {
        "cloud-listing": ["imax70"],
        "local-listing": ["imax70"],
    }
    assert merged["sales"] == {"cloud-listing": target}
    assert merged["sale_target"] == target
    assert merged["tickets_available"] is True
    assert merged["last_check_ok"] == local["last_check_ok"]


def test_merge_unions_same_receipt_keys_and_keeps_earliest_delivery_proof():
    base = fresh_state()
    upstream = fresh_state()
    local = fresh_state()
    target = "2026-12-01T09:00:00+01:00"
    upstream["alerts"]["same"] = "2026-10-01T08:05:00Z"
    local["alerts"]["same"] = "2026-10-01T10:04:00+02:00"
    upstream["reminders_sent"][target] = ["1440", "120"]
    local["reminders_sent"][target] = ["120", "15"]
    upstream["formats_seen"]["dune"] = ["other", "imax70"]
    local["formats_seen"]["dune"] = ["imax", "imax70"]

    merged = merge_states(base, upstream, local)

    assert merged["alerts"]["same"] == "2026-10-01T10:04:00+02:00"
    assert merged["reminders_sent"][target] == ["120", "1440", "15"]
    assert merged["formats_seen"]["dune"] == ["imax", "imax70", "other"]


def test_sales_baseline_is_not_lost_when_only_one_side_still_has_it():
    target = "2026-12-01T09:00:00+01:00"
    base = fresh_state()
    base["alerts"][f"sale:dune:{target}"] = "2026-10-01T10:00:00+02:00"
    base["sales"]["dune"] = target
    base["sale_target"] = target
    upstream = deepcopy(base)
    upstream["alerts"].clear()
    upstream["sales"].clear()
    upstream["sale_target"] = None
    local = deepcopy(base)

    merged = merge_states(base, upstream, local, now=MERGE_NOW)

    assert merged["alerts"] == base["alerts"]
    assert merged["sales"] == base["sales"]
    assert merged["sale_target"] == target


def test_concurrent_sale_changes_keep_the_most_recently_acknowledged_baseline():
    old = "2026-11-01T09:00:00+01:00"
    upstream_sale = "2026-11-02T09:00:00+01:00"
    local_sale = "2026-11-03T09:00:00+01:00"
    base = fresh_state()
    base["sales"] = {"dune": old}
    base["sale_target"] = old
    upstream = deepcopy(base)
    upstream["sales"]["dune"] = upstream_sale
    upstream["sale_target"] = upstream_sale
    upstream["alerts"][f"sale:dune:{upstream_sale}"] = "2026-10-01T10:00:00+02:00"
    local = deepcopy(base)
    local["sales"]["dune"] = local_sale
    local["sale_target"] = local_sale
    local["alerts"][f"sale:dune:{local_sale}"] = "2026-10-01T10:05:00+02:00"

    merged = merge_states(base, upstream, local, now=MERGE_NOW)

    assert merged["sales"] == {"dune": local_sale}
    assert merged["sale_target"] == local_sale
    assert set(merged["alerts"]) == {
        f"sale:dune:{upstream_sale}",
        f"sale:dune:{local_sale}",
    }


def test_concurrent_targets_choose_earliest_merged_upcoming_opening():
    october = "2026-10-10T09:00:00+02:00"
    november = "2026-11-10T09:00:00+01:00"
    base = fresh_state()
    upstream = fresh_state()
    upstream["sales"] = {"october-listing": october}
    upstream["sale_target"] = october
    upstream["alerts"][f"sale:october-listing:{october}"] = (
        "2026-09-17T10:00:00+02:00"
    )
    local = fresh_state()
    local["sales"] = {"november-listing": november}
    local["sale_target"] = november
    # The later delivery receipt must not make the later opening win.
    local["alerts"][f"sale:november-listing:{november}"] = (
        "2026-09-17T10:05:00+02:00"
    )

    merged = merge_states(base, upstream, local, now=MERGE_NOW)

    assert merged["sales"] == {
        "november-listing": november,
        "october-listing": october,
    }
    assert merged["sale_target"] == october


def test_agreed_elapsed_target_survives_for_open_now_reminder():
    target = "2026-09-17T10:00:00+02:00"
    base = fresh_state()
    base["sales"] = {"dune": target}
    base["sale_target"] = target
    upstream = deepcopy(base)
    upstream["reminders_sent"] = {target: ["1440"]}
    local = deepcopy(base)
    local["reminders_sent"] = {target: ["120", "15"]}

    merged = merge_states(base, upstream, local, now=MERGE_NOW)

    assert merged["sale_target"] == target
    assert merged["reminders_sent"] == {target: ["120", "1440", "15"]}
    assert due_reminders(merged, [1440, 120, 15], MERGE_NOW) == [
        {"offset": "open", "target": target}
    ]


def test_divergent_elapsed_targets_fail_closed():
    earlier = "2026-09-17T09:00:00+02:00"
    later = "2026-09-17T10:00:00+02:00"
    base = fresh_state()
    upstream = fresh_state()
    upstream["sales"] = {"earlier": earlier}
    upstream["sale_target"] = earlier
    local = fresh_state()
    local["sales"] = {"later": later}
    local["sale_target"] = later

    with pytest.raises(StateMergeError, match="multiple elapsed candidates"):
        merge_states(base, upstream, local, now=MERGE_NOW)


def test_same_optional_key_removed_on_both_sides_stays_absent():
    base = fresh_state()
    base["last_error"] = "old failure"
    upstream = deepcopy(base)
    upstream.pop("last_error")
    upstream["failure_streak"] = 1
    local = deepcopy(base)
    local.pop("last_error")
    local["last_check_ok"] = "2026-09-17T11:00:00+02:00"

    merged = merge_states(base, upstream, local, now=MERGE_NOW)

    assert "last_error" not in merged
    assert merged["failure_streak"] == 1
    assert merged["last_check_ok"] == local["last_check_ok"]


def test_unsafe_concurrent_scalar_change_fails_instead_of_guessing():
    base = fresh_state()
    upstream = fresh_state()
    local = fresh_state()
    upstream["last_error"] = "upstream failure"
    local["last_error"] = "local failure"

    with pytest.raises(StateMergeError, match="last_error changed differently"):
        merge_states(base, upstream, local)


def test_cli_failure_names_recovery_and_does_not_replace_output(tmp_path, capsys):
    base = fresh_state()
    upstream = fresh_state()
    local = fresh_state()
    upstream["last_error"] = "upstream failure"
    local["last_error"] = "local failure"
    paths = {}
    for name, state in (("base", base), ("upstream", upstream), ("local", local)):
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(state), encoding="utf-8")
        paths[name] = path
    output = tmp_path / "state.json"
    output.write_text(json.dumps(base), encoding="utf-8")
    before = output.read_bytes()

    status = run(
        [
            "--base",
            str(paths["base"]),
            "--upstream",
            str(paths["upstream"]),
            "--local",
            str(paths["local"]),
            "--output",
            str(output),
        ]
    )

    assert status == 2
    assert "state reconciliation failed" in capsys.readouterr().err
    assert output.read_bytes() == before
    assert load_state(output) == base
