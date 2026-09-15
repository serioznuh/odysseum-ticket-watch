"""JSON state: alert dedup, known facts baseline, reminder ladder."""

from __future__ import annotations

import json
import logging
import os
import re
from copy import deepcopy
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from . import detect

log = logging.getLogger(__name__)

# Consecutive IMAX-free checks needed before `imax_present` flips to False.
# Doubles as the ceiling for the streak counter: past the confirmation point a
# bigger number would mean nothing, but it would keep the state file changing
# on every firing (see update_from_cinesa).
IMAX_ABSENT_CONFIRM = 2
CURRENT_STATE_VERSION = 2

DEFAULT_STATE: dict = {
    "version": CURRENT_STATE_VERSION,
    "alerts": {},          # dedup key -> ISO timestamp of when the alert was sent
    "sales": {},           # show slug -> salesOpeningDatetime ISO (as last seen)
    "formats_seen": {},    # show slug -> [format classes with sessions already alerted]
    "shows_seen": [],      # matched show slugs already known
    "reminders_sent": {},  # sale target ISO -> ["1440", "120", "15", "open"]
    "sale_target": None,   # earliest upcoming salesOpeningDatetime (ISO)
    "tickets_available": False,
    "failure_streak": 0,
    "error_alerted": False,
    "last_check_ok": None,
    "last_heartbeat": None,
    # Cinesa (Diagonal Mar) half — namespaced so it never collides with Pathé.
    # No per-run timestamp lives here on purpose: the Cinesa check runs on
    # every 5-min firing, and a field that changed each time would make
    # local-check.sh commit and push ~288 times a day. Only real changes land.
    "cinesa": {
        "imax_present": None,       # None until the first successful check
        "imax_absent_streak": 0,    # consecutive non-empty checks without IMAX
        "horizon": None,            # last bookable business date (YYYY-MM-DD)
        "day_count": 0,
        "last_change": None,        # when horizon/IMAX last actually moved
        "failure_streak": 0,
        "error_alerted": False,
    },
}


class StateError(RuntimeError):
    """State cannot be trusted enough to run notification logic."""


class StateMissingError(StateError):
    """State is absent; only an explicit bootstrap may create it."""


_CORE_FIELDS = {
    "alerts",
    "sales",
    "formats_seen",
    "shows_seen",
    "reminders_sent",
    "sale_target",
    "tickets_available",
    "failure_streak",
    "error_alerted",
    "last_check_ok",
    "last_heartbeat",
}
_TOP_LEVEL_FIELDS = _CORE_FIELDS | {"version", "last_error", "cinesa"}
_CINESA_FIELDS = {
    "imax_present",
    "imax_absent_streak",
    "horizon",
    "day_count",
    "last_change",
    "failure_streak",
    "error_alerted",
    "blind_since",
}


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    return type(value).__name__


def _fail(field: str, expected: str, value: Any) -> None:
    raise StateError(f"{field}: expected {expected}, got {_type_name(value)}")


def _require_mapping(value: Any, field: str) -> dict:
    if not isinstance(value, dict):
        _fail(field, "object", value)
    return value


def _require_string(value: Any, field: str) -> str:
    if not isinstance(value, str):
        _fail(field, "string", value)
    return value


def _require_bool(value: Any, field: str) -> None:
    if not isinstance(value, bool):
        _fail(field, "boolean", value)


def _require_nonnegative_int(value: Any, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        _fail(field, "non-negative integer", value)


def _parse_timestamp(value: Any, field: str) -> None:
    text = _require_string(value, field)
    # Python 3.9's fromisoformat does not accept the otherwise standard `Z`.
    candidate = f"{text[:-1]}+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise StateError(f"{field}: invalid ISO-8601 timestamp {text!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise StateError(f"{field}: timestamp must include a UTC offset")


def _parse_optional_timestamp(value: Any, field: str) -> None:
    if value is not None:
        _parse_timestamp(value, field)


def _validate_string_list(value: Any, field: str) -> None:
    if not isinstance(value, list):
        _fail(field, "array of strings", value)
    for index, item in enumerate(value):
        _require_string(item, f"{field}[{index}]")


def _validate_string_timestamp_map(value: Any, field: str) -> None:
    mapping = _require_mapping(value, field)
    for key, timestamp in mapping.items():
        _require_string(key, f"{field} key")
        _parse_timestamp(timestamp, f"{field}[{key!r}]")


def _validate_sales(value: Any) -> None:
    mapping = _require_mapping(value, "sales")
    for slug, timestamp in mapping.items():
        _require_string(slug, "sales key")
        _parse_timestamp(timestamp, f"sales[{slug!r}]")


def _validate_formats_seen(value: Any) -> None:
    mapping = _require_mapping(value, "formats_seen")
    for slug, formats in mapping.items():
        _require_string(slug, "formats_seen key")
        _validate_string_list(formats, f"formats_seen[{slug!r}]")


def _validate_reminders(value: Any) -> None:
    mapping = _require_mapping(value, "reminders_sent")
    for target, offsets in mapping.items():
        _parse_timestamp(target, "reminders_sent key")
        _validate_string_list(offsets, f"reminders_sent[{target!r}]")
        for offset in offsets:
            if offset != "open" and not (offset.isdigit() and int(offset) > 0):
                raise StateError(
                    f"reminders_sent[{target!r}]: invalid reminder receipt {offset!r}"
                )


def _validate_cinesa(value: Any, *, require_all: bool) -> None:
    cin = _require_mapping(value, "cinesa")
    unknown = set(cin) - _CINESA_FIELDS
    if unknown:
        raise StateError(f"cinesa: unknown field(s): {', '.join(sorted(unknown))}")
    required = _CINESA_FIELDS - {"blind_since"}
    missing = required - set(cin)
    if require_all and missing:
        raise StateError(f"cinesa: missing required field(s): {', '.join(sorted(missing))}")

    if "imax_present" in cin and cin["imax_present"] is not None:
        _require_bool(cin["imax_present"], "cinesa.imax_present")
    for field in ("imax_absent_streak", "day_count", "failure_streak"):
        if field in cin:
            _require_nonnegative_int(cin[field], f"cinesa.{field}")
    if "horizon" in cin and cin["horizon"] is not None:
        horizon = _require_string(cin["horizon"], "cinesa.horizon")
        try:
            date.fromisoformat(horizon)
        except ValueError as exc:
            raise StateError(f"cinesa.horizon: invalid ISO date {horizon!r}") from exc
    for field in ("last_change", "blind_since"):
        if field in cin:
            _parse_optional_timestamp(cin[field], f"cinesa.{field}")
    if "error_alerted" in cin:
        _require_bool(cin["error_alerted"], "cinesa.error_alerted")


def _validate_fields(state: dict, *, require_all: bool) -> None:
    unknown = set(state) - _TOP_LEVEL_FIELDS
    if unknown:
        raise StateError(f"state: unknown field(s): {', '.join(sorted(unknown))}")
    missing = _CORE_FIELDS - set(state)
    if missing:
        raise StateError(f"state: missing required field(s): {', '.join(sorted(missing))}")
    if require_all and "cinesa" not in state:
        raise StateError("state: missing required field(s): cinesa")

    _validate_string_timestamp_map(state["alerts"], "alerts")
    _validate_sales(state["sales"])
    _validate_formats_seen(state["formats_seen"])
    _validate_string_list(state["shows_seen"], "shows_seen")
    _validate_reminders(state["reminders_sent"])
    _parse_optional_timestamp(state["sale_target"], "sale_target")
    _require_bool(state["tickets_available"], "tickets_available")
    _require_nonnegative_int(state["failure_streak"], "failure_streak")
    _require_bool(state["error_alerted"], "error_alerted")
    _parse_optional_timestamp(state["last_check_ok"], "last_check_ok")
    _parse_optional_timestamp(state["last_heartbeat"], "last_heartbeat")
    if "last_error" in state:
        _require_string(state["last_error"], "last_error")
    if "cinesa" in state:
        _validate_cinesa(state["cinesa"], require_all=require_all)


def _migrate_v0_to_v1(state: dict) -> dict:
    migrated = deepcopy(state)
    migrated["version"] = 1
    return migrated


def _migrate_v1_to_v2(state: dict) -> dict:
    migrated = deepcopy(state)
    cinesa = deepcopy(DEFAULT_STATE["cinesa"])
    cinesa.update(migrated.get("cinesa", {}))
    migrated["cinesa"] = cinesa
    migrated["version"] = 2
    return migrated


_MIGRATIONS = {
    0: _migrate_v0_to_v1,
    1: _migrate_v1_to_v2,
}


def migrate_state(loaded: Any) -> dict:
    """Validate and migrate a decoded state object without mutating it."""
    state = _require_mapping(loaded, "state")
    raw_version = state.get("version", 0)
    if isinstance(raw_version, bool) or not isinstance(raw_version, int):
        _fail("version", "integer", raw_version)
    if raw_version < 0 or raw_version > CURRENT_STATE_VERSION:
        raise StateError(
            f"version: unsupported state schema {raw_version}; "
            f"this watcher supports versions 0 through {CURRENT_STATE_VERSION}"
        )

    # Every historical schema had the core delivery/baseline fields. Requiring
    # them prevents a truncated-but-valid `{}` from becoming empty dedup state.
    _validate_fields(state, require_all=raw_version == CURRENT_STATE_VERSION)
    migrated = deepcopy(state)
    version = raw_version
    while version < CURRENT_STATE_VERSION:
        migrated = _MIGRATIONS[version](migrated)
        version = migrated["version"]

    # Keep the established stale-key compatibility migration. It changes only
    # the obsolete stale key shape and preserves its delivery timestamp.
    migrate_stale_keys(migrated)
    _validate_fields(migrated, require_all=True)
    return migrated


def _state_error(path: Path, detail: str) -> StateError:
    return StateError(
        f"state file {path} is invalid: {detail}. The original was preserved unchanged; "
        "refusing to run with empty notification history. Stop schedulers and follow "
        "the README 'State recovery' procedure"
    )


def _reject_json_constant(value: str) -> None:
    raise StateError(f"non-standard JSON constant {value}")


def load_state(path: str | Path) -> dict:
    """Load trusted state, failing closed without changing the filesystem."""
    p = Path(path)
    if not p.exists():
        raise StateMissingError(
            f"state file {p} is missing; refusing to assume a new installation and lose "
            "notification history. For first use run with --bootstrap-state; for recovery "
            "follow the README 'State recovery' procedure"
        )
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise _state_error(p, str(exc)) from exc
    try:
        loaded = json.loads(text, parse_constant=_reject_json_constant)
        return migrate_state(loaded)
    except (json.JSONDecodeError, StateError) as exc:
        raise _state_error(p, str(exc)) from exc


def bootstrap_state(path: str | Path) -> dict:
    """Create initial empty state exclusively; never replace an existing file."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    initial = deepcopy(DEFAULT_STATE)
    try:
        with p.open("x", encoding="utf-8") as state_file:
            json.dump(initial, state_file, indent=2, sort_keys=True)
            state_file.write("\n")
    except FileExistsError as exc:
        raise StateError(
            f"state file {p} already exists; bootstrap never replaces notification history"
        ) from exc
    except OSError as exc:
        raise StateError(f"could not bootstrap state file {p}: {exc}") from exc
    return initial


LEGACY_STALE_KEY = re.compile(
    r"stale:\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?"
)


def migrate_stale_keys(state: dict) -> None:
    """Adopt the periodic stale key without re-alerting.

    The supervision alert used to be keyed `stale:{last_check_ok}` and fired
    once per outage. It now repeats every 24h, so the key carries the period:
    `stale:{last_check_ok}:{period}`. Without this migration the already-sent
    old key would no longer match, and a machine that is currently blind would
    get one duplicate alert on upgrade.
    """
    alerts = state.get("alerts")
    if not isinstance(alerts, dict):
        return
    for key in list(alerts):
        # Match the legacy shape exactly. "Does the remainder parse as a
        # timestamp?" is NOT a valid test: fromisoformat accepts sub-minute UTC
        # offsets, so "...+02:00:37" parses happily and every two-digit period
        # (outage days 11-100) would be mistaken for an un-migrated key and
        # renamed after each send — re-alerting on every cloud pass.
        if not LEGACY_STALE_KEY.fullmatch(key):
            continue
        alerts.setdefault(f"{key}:0", alerts[key])
        alerts.pop(key, None)


def save_state(path: str | Path, state: dict) -> None:
    # Validate before even creating the parent directory or temporary file: a
    # programming error must not replace the last trusted delivery history.
    validated = migrate_state(state)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(validated, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, p)


def already_sent(state: dict, key: str) -> bool:
    return key in state.get("alerts", {})


def mark_sent(state: dict, key: str, now: datetime) -> None:
    state.setdefault("alerts", {})[key] = now.isoformat()


def update_from_snapshot(
    state: dict,
    snap: detect.Snapshot,
    cfg: Any,
    now: datetime,
    advance_one_shot: bool = True,
    advance_sales: bool = True,
) -> None:
    """Record the Pathé snapshot as the new baseline (call after alerting).

    `advance_one_shot=False` freezes the one-shot alert baselines (`shows_seen`
    and `formats_seen`) while still recording sales and current ticket
    availability: the caller passes it when a NEW_LISTING or TICKETS_AVAILABLE
    finding was generated but not delivered, so the next run raises it again.

    `advance_sales=False` does the same for `sales`, which is the baseline
    behind SALE_DATE: recording an opening is what makes it "known", so doing
    that after a failed send retired the sale alert — the watcher's whole
    point — permanently. It is a separate flag because the two baselines fail
    independently, and holding `sales` back also holds back `sale_target` and
    the reminder ladder until the next run.
    """
    if not advance_one_shot:
        log.info(
            "Pathé: one-shot alert not delivered — keeping the previous listing/format baselines"
        )
    if not advance_sales:
        log.info("Pathé: sale alert not delivered — keeping the previous sale baseline")
    for show in snap.matched_shows:
        slug = show.get("slug", "")
        if not slug:
            continue
        if advance_one_shot and slug not in state["shows_seen"]:
            state["shows_seen"].append(slug)
        if advance_sales and show.get("salesOpeningDatetime"):
            state["sales"][slug] = show["salesOpeningDatetime"]

        days = snap.showtimes.get(slug) or {}
        entry = snap.cinema_entries.get(slug) or {}
        if days:
            summary = detect.summarize_sessions(show, days)
            if advance_one_shot:
                fmts = set(state["formats_seen"].get(slug, [])) | set(summary["counts"])
                state["formats_seen"][slug] = sorted(fmts)
            state["tickets_available"] = True
        elif entry.get("isBookable") or entry.get("bookable"):
            if advance_one_shot:
                fmt = detect.classify_format(show.get("title"), slug)
                fmts = set(state["formats_seen"].get(slug, [])) | {fmt}
                state["formats_seen"][slug] = sorted(fmts)
            state["tickets_available"] = True

    future = []
    shows = {s.get("slug"): s for s in snap.matched_shows}
    selected_sales = {
        slug: iso for slug, iso in state["sales"].items()
        if detect.selected_listing(shows.get(slug, {"slug": slug}), cfg)
    }
    if state.get("sale_target") not in selected_sales.values():
        state["sale_target"] = None
    for iso in selected_sales.values():
        dt = detect.parse_iso(iso)
        if dt and detect.as_aware(dt) > now:
            future.append((detect.as_aware(dt), iso))
    if future:
        state["sale_target"] = min(future)[1]


def update_from_cinesa(
    state: dict,
    snap: detect.CinesaSnapshot,
    cfg: Any,
    now: datetime,
    advance_imax: bool = True,
) -> None:
    """Record the Cinesa snapshot as the new baseline (call after alerting).

    An empty snapshot never flips `imax_present`: a transient API hiccup would
    otherwise manufacture an "IMAX disappeared" alert on the next check.
    `imax_present` only goes False once absence is confirmed twice, which is
    the same threshold analyze_cinesa uses before it alerts.

    `advance_imax=False` freezes the IMAX baseline (`imax_present` and its
    streak) while still recording the horizon: the caller passes it when an
    IMAX gone/back alert was generated but not delivered, so the next run sees
    the same transition again instead of losing the alert forever.
    """
    cin = state.setdefault("cinesa", {})
    before = dict(cin)
    cin["failure_streak"] = 0
    cin["error_alerted"] = False
    if not snap.days:
        log.warning("cinesa: snapshot has no bookable days — not updating IMAX baseline")
        return

    cin["horizon"] = snap.days[-1]["date"]
    cin["day_count"] = len(snap.days)
    if not advance_imax:
        log.info("cinesa: IMAX alert not delivered — keeping the previous IMAX baseline")
    elif detect.imax_days(snap.days, cfg.cinesa_imax_attribute_id):
        cin["imax_present"] = True
        cin["imax_absent_streak"] = 0
    else:
        # Capped: only the confirmation threshold is ever read, and an
        # ever-growing counter would diff the state file on every firing.
        cin["imax_absent_streak"] = min(
            cin.get("imax_absent_streak", 0) + 1, IMAX_ABSENT_CONFIRM
        )
        if cin["imax_absent_streak"] >= IMAX_ABSENT_CONFIRM:
            cin["imax_present"] = False

    # Timestamp only a genuine change, so an unchanged schedule leaves the
    # state file byte-identical and the 5-min job has nothing to commit.
    if {k: v for k, v in cin.items() if k != "last_change"} != {
        k: v for k, v in before.items() if k != "last_change"
    }:
        cin["last_change"] = now.isoformat()


# The local half's firing interval: launchd's StartInterval (300 s) in
# scripts/com.odysseum.ticket-watch.plist, which a test pins this constant to.
# The ladder's owner fires on that timer, so this is also the worst-case delay
# between a rung's window opening and the owner's first chance at that rung.
LOCAL_FIRING_INTERVAL_MINUTES = 5


def _failover_eligible_at(dt: datetime, offset: int, grace_minutes: float) -> datetime:
    """When a failover caller may send the `offset` reminder.

    The owner (grace 0) may send the moment a window opens. A failover must not
    be able to, or both halves send the same rung — the two-writer race the
    grace exists to prevent. The owner fires on a timer, so its *worst case*
    first chance at a rung is one full LOCAL_FIRING_INTERVAL_MINUTES after that
    rung's window opens; anything earlier is racing it, not failing over for it.

    The wait is therefore floored at that interval and capped at the rung's own
    width, which decides each rung by construction — a new offset in
    `config.toml` included:

    * a rung wider than the effective wait keeps the whole grace (2 h and 24 h here);
    * a rung no wider than the effective wait gets no failover turn of its own.
      With the shipped 25-min cloud grace, eligibility for the 15-min warning
      lands on the opening itself, so the 'open' ping covers it. The local
      owner now has three firing opportunities inside that window.
    """
    if grace_minutes <= 0:
        return dt - timedelta(minutes=offset)  # the owner: as soon as it opens
    wait = min(max(grace_minutes, LOCAL_FIRING_INTERVAL_MINUTES), offset)
    return dt - timedelta(minutes=offset - wait)


def due_reminders(
    state: dict,
    offsets_minutes: list[int],
    now: datetime,
    grace_minutes: float = 0.0,
    cfg: Any = None,
) -> list[dict]:
    """Return at most one due reminder: the most imminent unsent offset, or the
    'open' ping once the sale time has passed (within a 6h grace window).

    Reminders stop once the selected format is known to be available.
    Without a format filter, retain the original any-ticket behavior.

    `grace_minutes` makes the caller a *failover* instead of the ladder's owner:
    it only reports a reminder whose window opened at least that long ago. The
    local half (launchd, every 5 min) passes 0 and owns the ladder; the cloud
    pass passes a grace longer than that firing interval, so it only steps in
    for a reminder the local half demonstrably did not send in time. That is
    half of what removes the two-writer race on `reminders_sent` which used to
    make reminders cloud-only; the other half is `scripts/local-check.sh`
    pulling before it runs, so the Mac sees the failover's sends — see OTW-15.

    Grace delays *eligibility* only, and never to a point where it could beat
    the owner to a rung (see `_failover_eligible_at`). The 6h cutoff on the
    'open' ping stays anchored to the sale time itself, so a failover grace can
    never shorten how late that ping may still be sent.
    """
    if detect.target_format_available(state, cfg):
        return []
    iso = state.get("sale_target")
    dt = detect.parse_iso(iso) if iso else None
    if dt is None:
        return []
    dt = detect.as_aware(dt)
    sent = set(state.get("reminders_sent", {}).get(iso, []))
    grace_minutes = max(0.0, grace_minutes)

    if now >= dt:
        # The 'open' ping has no window to be squeezed out of — only the 6h
        # cutoff below — so the grace applies to it whole.
        opens_at = dt + timedelta(minutes=grace_minutes)
        if "open" not in sent and now >= opens_at and (now - dt) <= timedelta(hours=6):
            return [{"offset": "open", "target": iso}]
        return []

    if "open" in sent:
        return []
    active = [
        o
        for o in sorted(offsets_minutes)
        if now >= _failover_eligible_at(dt, o, grace_minutes) and str(o) not in sent
    ]
    if active:
        return [{"offset": min(active), "target": iso}]
    return []


def adaptive_staleness_hours(state: dict, cfg: Any, now: datetime) -> float:
    """Allowed staleness of the last successful check before checking again.

    War-room curve around an announced sale opening: tightens as the target
    approaches, stays tight from 4 h before until 6 h after (sessions appear
    right at opening), then relaxes once tickets are known to be bookable.
    The launchd firing interval (5 min) is the effective floor.
    """
    # A different date opening must not slow checks for the still-wanted dates.
    if any(
        day >= now.date().isoformat() and not already_sent(state, detect.pathe_date_key(cfg, day))
        for day in getattr(cfg, "pathe_target_dates", [])
    ):
        return 0.0  # every existing launchd firing, even if its interval drifts slightly
    target = detect.parse_iso(state.get("sale_target"))
    if target is not None:
        hours_to_target = (detect.as_aware(target) - now).total_seconds() / 3600
        if -6 <= hours_to_target <= 4:
            return cfg.cadence_opening_window_minutes / 60
        if 0 < hours_to_target <= 48:
            return cfg.cadence_final_48h_hours
        if 0 < hours_to_target <= 7 * 24:
            return cfg.cadence_within_week_hours
    if detect.target_format_available(state, cfg):
        return cfg.cadence_after_tickets_hours
    return cfg.cadence_baseline_hours


def is_check_fresh(state: dict, hours: float, now: datetime) -> bool:
    """True when the last successful Pathé check is newer than `hours`.

    Used by retry slots to exit instantly when the primary run already
    succeeded. False when there has never been a successful check.
    """
    if hours <= 0:
        return False
    last = detect.parse_iso(state.get("last_check_ok"))
    return last is not None and (now - detect.as_aware(last)) < timedelta(hours=hours)


def is_check_stale(state: dict, hours: int, now: datetime) -> bool:
    """True when the last successful Pathé check is older than `hours`.

    Never stale before the first successful check (setup phase).
    """
    if hours <= 0:
        return False
    last = detect.parse_iso(state.get("last_check_ok"))
    return last is not None and (now - detect.as_aware(last)) > timedelta(hours=hours)


def mark_reminder(state: dict, target_iso: str, offset: int | str, offsets_minutes: list[int]) -> None:
    """Mark `offset` sent; also skip any larger (earlier) offsets already in the past."""
    sent = set(state.setdefault("reminders_sent", {}).get(target_iso, []))
    if offset == "open":
        sent.add("open")
    else:
        sent.update(str(o) for o in offsets_minutes if o >= int(offset))
    state["reminders_sent"][target_iso] = sorted(sent)
