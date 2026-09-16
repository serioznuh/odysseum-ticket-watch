"""Three-way recovery for state.json conflicts during the local state rebase.

This is deliberately narrower than a general state store.  It reconciles the
base, upstream and unpushed-local versions Git exposes for one conflicted state
commit, preserving delivery receipts and the baselines those receipts allow to
advance.  Fields without safe domain semantics still use a strict three-way
merge and stop recovery if both sides changed them differently.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

from .state import StateError, migrate_state, save_state


class StateMergeError(StateError):
    """Two valid states cannot be reconciled without guessing."""


_MISSING = object()


def _path_label(path: tuple[str, ...]) -> str:
    return ".".join(path) or "state"


def _three_way(base: Any, upstream: Any, local: Any, path: tuple[str, ...]) -> Any:
    if upstream == local:
        return deepcopy(upstream)
    if upstream == base:
        return deepcopy(local)
    if local == base:
        return deepcopy(upstream)

    if all(
        value is _MISSING or isinstance(value, dict)
        for value in (base, upstream, local)
    ):
        base_map = {} if base is _MISSING else base
        upstream_map = {} if upstream is _MISSING else upstream
        local_map = {} if local is _MISSING else local
        merged = {}
        for key in sorted(set(base_map) | set(upstream_map) | set(local_map)):
            value = _three_way(
                base_map.get(key, _MISSING),
                upstream_map.get(key, _MISSING),
                local_map.get(key, _MISSING),
                path + (key,),
            )
            if value is not _MISSING:
                merged[key] = value
        return merged

    if upstream is _MISSING and local is _MISSING:
        return _MISSING
    raise StateMergeError(
        f"{_path_label(path)} changed differently upstream and locally; "
        "refusing to guess"
    )


def _stable_union(*values: Iterable[str]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for items in values:
        for item in items:
            if item not in seen:
                merged.append(item)
                seen.add(item)
    return merged


def _parse_timestamp(value: str) -> datetime:
    # Match state.py's Python 3.9-compatible handling of the JSON `Z` suffix.
    candidate = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    return datetime.fromisoformat(candidate)


def _earlier_timestamp(first: str, second: str) -> str:
    """Keep the earliest proof that a duplicate-key delivery happened."""
    return first if _parse_timestamp(first) <= _parse_timestamp(second) else second


def _merge_alerts(upstream: dict, local: dict) -> dict:
    merged = deepcopy(upstream)
    for key, timestamp in local.items():
        if key in merged:
            merged[key] = _earlier_timestamp(merged[key], timestamp)
        else:
            merged[key] = timestamp
    return merged


def _merge_reminders(upstream: dict, local: dict) -> dict:
    merged: dict[str, list[str]] = {}
    for target in sorted(set(upstream) | set(local)):
        merged[target] = sorted(set(upstream.get(target, [])) | set(local.get(target, [])))
    return merged


def _merge_formats(upstream: dict, local: dict) -> dict:
    merged: dict[str, list[str]] = {}
    for slug in sorted(set(upstream) | set(local)):
        merged[slug] = sorted(set(upstream.get(slug, [])) | set(local.get(slug, [])))
    return merged


def _sale_receipt(state: dict, slug: str, sale_iso: str) -> str | None:
    return state["alerts"].get(f"sale:{slug}:{sale_iso}")


def _merge_sales(
    base: dict,
    upstream: dict,
    local: dict,
    upstream_state: dict,
    local_state: dict,
) -> dict:
    merged: dict[str, str] = {}
    for slug in sorted(set(base) | set(upstream) | set(local)):
        old = base.get(slug, _MISSING)
        theirs = upstream.get(slug, _MISSING)
        ours = local.get(slug, _MISSING)
        if theirs is _MISSING and ours is _MISSING:
            choice = _MISSING
        elif theirs is _MISSING:
            choice = ours
        elif ours is _MISSING or theirs == ours:
            choice = theirs
        elif theirs == old:
            choice = ours
        elif ours == old:
            choice = theirs
        else:
            upstream_receipt = _sale_receipt(upstream_state, slug, theirs)
            local_receipt = _sale_receipt(local_state, slug, ours)
            if upstream_receipt and local_receipt and upstream_receipt != local_receipt:
                choice = (
                    theirs
                    if _parse_timestamp(upstream_receipt)
                    > _parse_timestamp(local_receipt)
                    else ours
                )
            elif upstream_receipt and not local_receipt:
                choice = theirs
            elif local_receipt and not upstream_receipt:
                choice = ours
            else:
                raise StateMergeError(
                    f"sales.{slug} has two concurrently acknowledged values "
                    "with no unambiguous delivery order"
                )
        if choice is not _MISSING:
            merged[slug] = choice
    return merged


def _sale_target_receipt(state: dict, target: str | None) -> str | None:
    if target is None:
        return None
    receipts = [
        receipt
        for slug, sale_iso in state["sales"].items()
        if sale_iso == target
        for receipt in [_sale_receipt(state, slug, sale_iso)]
        if receipt is not None
    ]
    return max(receipts, key=_parse_timestamp, default=None)


def _merge_sale_target(base: dict, upstream: dict, local: dict) -> str | None:
    old = base["sale_target"]
    theirs = upstream["sale_target"]
    ours = local["sale_target"]
    if theirs == ours:
        return theirs
    if theirs == old:
        return ours
    if ours == old:
        return theirs

    upstream_receipt = _sale_target_receipt(upstream, theirs)
    local_receipt = _sale_target_receipt(local, ours)
    if upstream_receipt and local_receipt and upstream_receipt != local_receipt:
        return (
            theirs
            if _parse_timestamp(upstream_receipt) > _parse_timestamp(local_receipt)
            else ours
        )
    if upstream_receipt and not local_receipt:
        return theirs
    if local_receipt and not upstream_receipt:
        return ours
    raise StateMergeError(
        "sale_target changed differently upstream and locally with no "
        "unambiguous delivery order"
    )


def merge_states(base: dict, upstream: dict, local: dict) -> dict:
    """Merge validated state snapshots from a conflicted rebase.

    ``upstream`` is Git's stage 2 during a rebase; ``local`` is stage 3, the
    unpushed state commit being replayed.  Receipts and append-only baselines
    take a union.  Everything else follows ordinary three-way semantics.
    """
    base = migrate_state(base)
    upstream = migrate_state(upstream)
    local = migrate_state(local)

    domain_fields = {
        "alerts",
        "formats_seen",
        "reminders_sent",
        "sale_target",
        "sales",
        "shows_seen",
        "tickets_available",
    }
    ordinary_base = {key: value for key, value in base.items() if key not in domain_fields}
    ordinary_upstream = {
        key: value for key, value in upstream.items() if key not in domain_fields
    }
    ordinary_local = {key: value for key, value in local.items() if key not in domain_fields}
    merged = _three_way(ordinary_base, ordinary_upstream, ordinary_local, ())
    merged["alerts"] = _merge_alerts(upstream["alerts"], local["alerts"])
    merged["reminders_sent"] = _merge_reminders(
        upstream["reminders_sent"], local["reminders_sent"]
    )
    merged["shows_seen"] = _stable_union(
        base["shows_seen"], upstream["shows_seen"], local["shows_seen"]
    )
    merged["formats_seen"] = _merge_formats(
        upstream["formats_seen"], local["formats_seen"]
    )
    merged["sales"] = _merge_sales(
        base["sales"], upstream["sales"], local["sales"], upstream, local
    )
    merged["sale_target"] = _merge_sale_target(base, upstream, local)
    # This baseline only ever moves False -> True.  Once tickets were observed
    # and any gated alert was delivered, a concurrent stale False must not undo
    # that evidence.
    merged["tickets_available"] = (
        upstream["tickets_available"] or local["tickets_available"]
    )
    return migrate_state(merged)


def _read_state(path: str) -> dict:
    try:
        loaded = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StateMergeError(f"could not read {path}: {exc}") from exc
    return migrate_state(loaded)


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True)
    parser.add_argument("--upstream", required=True)
    parser.add_argument("--local", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        merged = merge_states(
            _read_state(args.base), _read_state(args.upstream), _read_state(args.local)
        )
        save_state(args.output, merged)
    except StateError as exc:
        print(f"state rebase recovery failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
