"""Alert construction: every Finding the watcher can raise, and the shared
Pathé failure bookkeeping that decides when an outage is worth a message.

Split out of the CLI so the jobs in `watcher/jobs.py` can build alerts without
importing `__main__` (OTW-19). `watcher/__main__` re-exports these names, which
is how they have always been addressed from tests and from the REPL.

Nothing here reads the clock: every function takes the run's `now`, so one
run's bookkeeping, dedup keys and staleness arithmetic all agree with
themselves.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta

from . import cdp, detect, notify, state_sync
from . import state as state_mod
from .detect import TZ_PARIS, Finding

log = logging.getLogger("watcher.alerts")


def summarize_pathe_error(error: str) -> tuple[str, int | None]:
    """Turn httpx's multiline status message into a concise alert line."""
    status = re.search(
        r"(?:Client|Server) error '(\d{3}) ([^']+)' for url '([^']+)'", error
    )
    if status:
        return f"HTTP {status.group(1)} {status.group(2)} from {status.group(3)}", int(
            status.group(1)
        )
    # Idempotent on its own output: the summary is what gets stored in state for
    # the cloud pass to read back, so re-summarising it must not lose the code.
    already = re.match(r"HTTP (\d{3})\b", error)
    if already:
        return " ".join(error.split())[:300], int(already.group(1))
    return " ".join(error.split())[:300], None


def watch_label(cfg) -> str:
    """Which watch a message is about. Every alert carries this: with more than
    one film or cinema in play, an unlabelled alert is ambiguous."""
    return f"{cfg.film_title} · {cfg.cinema_name}"


def cinesa_label(cfg) -> str:
    return f"{cfg.cinesa_film_title} · {cfg.cinesa_site_name}"


def fmt_duration(delta: timedelta) -> str:
    """Human span: '45 min', '6 h 20 m', '3 days'."""
    total = max(0, int(delta.total_seconds()))
    if total < 3600:
        return f"{total // 60} min"
    if total < 48 * 3600:
        hours, minutes = divmod(total // 60, 60)
        return f"{hours} h {minutes} m" if minutes else f"{hours} h"
    return f"{total // 86400} days"


def short_dt(dt: datetime | None) -> str:
    """'Wed 2 Sep, 07:11' — Paris time, no year, no timezone suffix."""
    if dt is None:
        return "unknown"
    dt = detect.as_aware(dt).astimezone(detect.TZ_PARIS)
    return f"{dt:%a} {dt.day} {dt:%b}, {dt:%H:%M}"


def blind_since(st: dict, now: datetime) -> tuple[str, str | None]:
    """(when the watcher last saw Pathé, how long it has been blind)."""
    last = detect.parse_iso(st.get("last_check_ok"))
    if last is None:
        return "the watcher started", None
    last = detect.as_aware(last)
    return short_dt(last), fmt_duration(now - last)


def running_in_ci() -> bool:
    return bool(os.environ.get("GITHUB_ACTIONS"))


def pathe_cause(error: str, *, ci: bool = False) -> tuple[str, str]:
    """(cause line, what-happens-next line) for a Pathé failure.

    `ci` describes the machine that hit the error, which is only knowable for
    an error the current process just caught. A cause read back out of state
    was recorded by the *other* half, so callers doing that must leave it
    False — the cloud pass always runs in Actions, and would otherwise report
    every one of the Mac's 403s as the expected datacenter block.
    """
    if error.startswith(detect.PARTIAL_PATHE_FAILURE):
        affected = error.removeprefix(detect.PARTIAL_PATHE_FAILURE).strip()
        return (
            f"Cause: Pathé listing data is unavailable ({affected}).",
            "Catalogue signals still work; retrying the missing data every 5 min.",
        )
    summary, status = summarize_pathe_error(error)
    if status == 403 and "refused by origin" in error:
        # Pathé's own nginx declining a listing, which no amount of waiting or
        # re-running fixes and which has nothing to do with the IP. Kept ahead
        # of the block branches so CI cannot relabel it a datacenter block.
        return (
            "Cause: Pathé is refusing a listing (403), not your IP.",
            "Expected on event listings; check the logs for which call failed.",
        )
    if status == 403:
        if ci:
            # Pathé blocks GitHub datacenter IPs outright, so this one will not
            # clear on its own and no local retry is scheduled (OTW-03).
            return (
                "Cause: Pathé blocks GitHub datacenter IPs (403).",
                "Expected in CI — run the check locally instead.",
            )
        return (
            "Cause: Pathé is blocking your IP (403).",
            "Retrying every 5 min — usually clears by itself.",
        )
    if status is not None:
        return (
            f"Cause: Pathé returned HTTP {status}.",
            "Retrying every 5 min; check the logs if it persists.",
        )
    return (
        f"Cause: {summary[:160]}",
        "Retrying every 5 min; check the logs if it persists.",
    )


def build_error_finding(cfg, st: dict, error: str, now: datetime) -> Finding:
    """Fired once the local half is confidently blind or persistently degraded."""
    cause, tail = pathe_cause(error, ci=running_in_ci())
    when, blind_for = blind_since(st, now)
    degraded = error.startswith(detect.PARTIAL_PATHE_FAILURE)
    since = (
        f"Full listing coverage unavailable since {when}"
        if degraded
        else f"No sale detection since {when}"
    )
    since += f" ({blind_for})." if blind_for else "."
    return Finding(
        kind="WATCHER_ERROR",
        key=f"error:{now:%Y-%m-%d}",
        confidence="high",
        title="Pathé watch is DEGRADED" if degraded else "Pathé watch is BLIND",
        lines=[watch_label(cfg), since, cause, tail],
        url=cfg.film_page_url,
    )


def build_recovered_finding(cfg, st: dict, now: datetime) -> Finding:
    """Sent on the first successful check after an outage. Reads `st` before the
    caller refreshes `last_check_ok`, so the blind span is still recoverable."""
    _, blind_for = blind_since(st, now)
    label = (
        "Degraded"
        if str(st.get("last_error", "")).startswith(detect.PARTIAL_PATHE_FAILURE)
        else "Blind"
    )
    if blind_for:
        line = f"{label} for {blind_for}. "
    elif label == "Degraded":
        line = "Degraded state cleared. "
    else:
        line = ""
    return Finding(
        kind="RECOVERED",
        key=f"recovered:{now:%Y-%m-%dT%H%M}",
        confidence="high",
        title="Pathé watch is back",
        lines=[watch_label(cfg), f"{line}Checks are running normally."],
        url=cfg.film_page_url,
    )


def build_state_sync_failure_finding(cfg, marker: dict[str, str]) -> Finding:
    """A local state commit could not be reconciled with shared state."""
    return Finding(
        kind="WATCHER_ERROR",
        key=state_sync.failure_key(marker),
        confidence="high",
        title="State sync needs you",
        lines=[
            watch_label(cfg),
            "Local state rebase recovery failed.",
            "Unpushed alert and reminder receipts were preserved.",
            f"Cause: {marker['detail']}",
            "Needs you: inspect the production clone and reconcile state before retrying.",
        ],
        url=cfg.film_page_url,
    )


def record_pathe_failure(cfg, st: dict, error: str, now: datetime, *, dry_run: bool) -> bool:
    """Advance the shared Pathé supervision streak and alert at its threshold."""
    previous_partial = str(st.get("last_error", "")).startswith(
        detect.PARTIAL_PATHE_FAILURE
    )
    current_partial = error.startswith(detect.PARTIAL_PATHE_FAILURE)
    st["failure_streak"] = min(
        st.get("failure_streak", 0) + 1, cfg.failure_streak_threshold
    )
    summary, status = summarize_pathe_error(error)
    # Store no endpoint URL for ordinary outages, and only stable endpoint/slug
    # names for partial failures, so an unchanged outage settles in state.
    if current_partial:
        recorded = error[:300]
    elif status and "refused by origin" in error:
        recorded = f"HTTP {status} refused by origin"
    elif status:
        recorded = f"HTTP {status}"
    else:
        recorded = summary[:120]
    if st.get("last_error") != recorded:
        st["last_error"] = recorded

    # One bit is sufficient as long as a strict severity escalation re-arms
    # it. Unchanged degradation/blindness stays quiet; degraded -> blind gets
    # its own loud alert even if the lesser condition was acknowledged.
    if previous_partial and not current_partial and st.get("error_alerted"):
        st["error_alerted"] = False

    # With adaptive cadence, retries come every 5 min — require both a failure
    # streak AND 6h without a fully healthy snapshot before crying wolf.
    if (
        st["failure_streak"] >= cfg.failure_streak_threshold
        and not state_mod.is_check_fresh(st, 6.0, now)
        and not st.get("error_alerted")
    ):
        finding = build_error_finding(cfg, st, error, now)
        if notify.send_telegram(
            cfg,
            notify.render_finding(finding),
            dry_run=dry_run,
            silent=notify.is_silent(cfg, finding.kind),
        ):
            st["error_alerted"] = True
            return True
    return False


def stale_period(blind: timedelta, stale_hours: int) -> int:
    """Which 24 h slot of an outage we are in: 0 at the alert threshold, then
    one per day. Measured from the threshold rather than from the last good
    check, so repeats land 24h apart — the same wall-clock time, except across
    a Europe/Paris DST change, where the hour shifts by one.
    """
    return (blind - timedelta(hours=stale_hours)).days


def build_stale_finding(cfg, st: dict, blind: timedelta, key: str, day: int) -> Finding:
    """Cloud-side supervision. `day` 1 is the first alert at the threshold;
    every later one is a silent 24 h repeat, so a long outage cannot go quiet.

    The cloud pass never calls Pathé. This function is called only after the
    separate catalogue-liveness pulse is stale, so a partial-failure marker is
    historical context, never evidence that the Mac is still alive.
    """
    repeat = day > 1
    when = short_dt(detect.parse_iso(state_mod.catalogue_check_iso(st)))
    partial = str(st.get("last_error", "")).startswith(detect.PARTIAL_PATHE_FAILURE)
    if partial:
        cause = "Cause: the Mac stopped checking after reporting degraded listing data."
    elif st.get("error_alerted") and st.get("last_error"):
        cause, _ = pathe_cause(str(st["last_error"]))  # ci=False: recorded by the Mac
        if repeat:
            cause = cause.replace("Cause: Pathé is", "Cause: Pathé is still")
    else:
        cause = "Cause: the Mac hasn't completed a check — off, asleep, or can't push."
    # Catalogue liveness goes stale when the *local half* stops, and that half
    # runs the news feeds and Cinesa too, so naming only Pathé understates the
    # outage (OTW-07).
    dark = "Pathé and news checks"
    if getattr(cfg, "cinesa_enabled", False):
        dark = "Pathé, news and Cinesa checks"
    title = (
        f"Still blind — day {day}"
        if repeat
        else f"Local checks have stopped — {fmt_duration(blind)}"
    )
    return Finding(
        kind="WATCHER_STILL_BLIND" if repeat else "WATCHER_ERROR",
        key=key,
        confidence="high",
        title=title,
        lines=[
            watch_label(cfg),
            f"Last catalogue check: {when}.",
            cause,
            f"{dark} are dark — cloud reminders still run.",
        ],
        url=cfg.film_page_url,
    )


def _cinesa_error_status(error: str | Exception) -> int | None:
    status = getattr(error, "status_code", None)
    if status is None:
        response = getattr(error, "response", None)
        status = getattr(response, "status_code", None)
    if status is not None:
        try:
            return int(status)
        except (TypeError, ValueError):
            pass

    text = str(error)
    match = re.search(r'''(?:\bHTTP\s+|[\'"])(\d{3})\b''', text)
    return int(match.group(1)) if match else None


def build_cinesa_error_finding(
    cfg, error: str | Exception, key: str, *, day: int = 1, since: str | None = None
) -> Finding:
    """Cinesa half is blind. Kept separate from the Pathé error: the two halves
    fail for unrelated reasons and one must never mask the other.

    Cinesa state still carries no per-run success timestamp (at 5-min cadence
    it would rewrite and push state.json ~288x a day); `since` comes from
    `blind_since`, stamped once when an outage is first confirmed.
    """
    status = _cinesa_error_status(error)
    text = str(error)
    if isinstance(error, cdp.ChromeLeakError):
        # Needs the owner's hands, and says so: "check Chrome is installed"
        # would send them after the wrong thing entirely.
        cause = "Cause: a leftover Chrome may still hold the watcher profile."
        tail = "Needs you: quit Chrome — every token refresh fails until then."
    elif status == 403:
        cause = "Cause: Cinesa is blocking your IP (403)."
        tail = "Retrying every 5 min — the cached token is kept."
    elif status == 401:
        cause = "Cause: Cinesa rejected the token."
        tail = "A fresh token is minted on the next retry."
    elif "Chrome" in text or "CDP" in text or "challenge" in text.lower():
        cause = "Cause: the token step couldn't drive Chrome."
        tail = "Needs you: check Chrome is installed and the Mac is logged in and awake."
    else:
        cause = f"Cause: {' '.join(text.split())[:160]}"
        tail = "Retrying every 5 min; check the logs if it persists."
    repeat = day > 1
    return Finding(
        kind="WATCHER_STILL_BLIND" if repeat else "WATCHER_ERROR",
        key=key,
        confidence="high",
        title=f"Cinesa watch still blind — day {day}" if repeat else "Cinesa watch is BLIND",
        lines=[
            cinesa_label(cfg),
            (
                f"Not watched since {since} — the Pathé half is unaffected."
                if since
                else "Not being watched right now — the Pathé half is unaffected."
            ),
            cause,
            tail,
        ],
        url=cfg.cinesa_page_url,
    )


def build_cinesa_leak_finding(cfg, since: datetime, key: str, *, day: int = 1) -> Finding:
    """A Chrome left holding the watcher profile, which needs the owner.

    Separate from the outage alert on purpose: the cached token usually keeps
    working for hours, so the watch is *not* dark and saying so would be wrong.
    What is broken is every future token refresh, and only quitting Chrome fixes
    it — so this fires on its own schedule rather than waiting for the outage
    that eventually follows.
    """
    repeat = day > 1
    return Finding(
        kind="WATCHER_STILL_BLIND" if repeat else "WATCHER_ERROR",
        key=key,
        confidence="high",
        title=(
            f"Cinesa token step still stuck — day {day}"
            if repeat
            else "Cinesa token step needs you"
        ),
        lines=[
            cinesa_label(cfg),
            f"A leftover Chrome has held the watcher profile since {short_dt(since)}.",
            "Cause: the token step could not confirm Chrome exited.",
            "Needs you: quit Chrome — token refreshes keep failing until then.",
        ],
        url=cfg.cinesa_page_url,
    )


def build_cinesa_recovered_finding(cfg, now: datetime) -> Finding:
    return Finding(
        kind="RECOVERED",
        key=f"cinesa_recovered:{now:%Y-%m-%dT%H%M}",
        confidence="high",
        title="Cinesa watch is back",
        lines=[cinesa_label(cfg), "Checks are running normally."],
        url=cfg.cinesa_page_url,
    )


def build_heartbeat(cfg, snap: detect.Snapshot, st: dict, now: datetime) -> Finding:
    now = detect.as_aware(now).astimezone(TZ_PARIS)
    primary = next(
        (s for s in snap.matched_shows if s.get("slug") == cfg.primary_slug), None
    )
    wanted = cfg.pathe_target_format
    # `sales` in state is a delivery/dedup baseline, not current evidence. It
    # deliberately retains withdrawn/old dates. Only a future timestamp on a
    # live, selected listing belongs in the status, and it is national rather
    # than a promise that the user's dates will open then.
    sales = sorted({
        detect.as_aware(dt)
        for show in snap.matched_shows if detect.selected_listing(show, cfg)
        for dt in [detect.parse_iso(show.get("salesOpeningDatetime"))]
        if dt is not None and detect.as_aware(dt) > now
    })
    if sales:
        label = "opening" if len(sales) == 1 else "openings"
        sale_line = f"Upcoming national sale {label}: " + "; ".join(
            detect.fmt_dt_short(dt) for dt in sales
        ) + "."
    else:
        scope = " for this format" if wanted else ""
        sale_line = f"No upcoming national sale opening published{scope}."
    lines = [
        watch_label(cfg),
        f"Release {detect.fmt_release(primary) if primary else cfg.release_date}.",
    ]
    if wanted:
        lines.append(f"Watching: {detect.FORMAT_LABELS[wanted]}.")
    if cfg.pathe_target_dates:
        # Reuse the alert detector's exact day + format evidence. Sent keys and
        # the historical formats_seen baseline must not imply current booking.
        confirmed = {f.key for f in detect.target_date_findings(snap, cfg, now)}
        for day in cfg.pathe_target_dates:
            if day < now.date().isoformat():
                status = "date has passed"
            elif detect.pathe_date_key(cfg, day) in confirmed:
                status = "booking confirmed"
            else:
                status = "booking not confirmed"
            lines.append(f"{detect.fmt_day(day)}: {status}.")
    else:
        bookable = False
        for show in snap.matched_shows:
            slug = show.get("slug", "")
            entry = snap.cinema_entries.get(slug) or {}
            if detect.selected_listing(show, cfg) and (
                entry.get("isBookable") is True or entry.get("bookable") is True
            ) and (not wanted or snap.endpoint_healthy(slug, "showtimes")):
                bookable = True
            for day, sessions in (snap.showtimes.get(slug) or {}).items():
                if day < now.date().isoformat():
                    continue
                for session in sessions:
                    fmt = detect.classify_format(
                        show.get("title"), slug, " ".join(session.get("tags") or []),
                        session.get("auditoriumName"), session.get("specialShowtimeDetails"),
                    )
                    if session.get("status") == "available" and (not wanted or fmt == wanted):
                        bookable = True
        status = "booking confirmed" if bookable else "booking not confirmed"
        listed = "Listed at the cinema" if snap.cinema_entries else "Not yet listed"
        lines.append(f"{listed} · {status}.")
    lines += [sale_line, f"{detect.plural(len(snap.matched_shows), 'listing')} watched."]
    if cfg.cinesa_enabled:
        cin = st.get("cinesa", {})
        imax = {True: "IMAX scheduled", False: "no IMAX scheduled"}.get(
            cin.get("imax_present"), "IMAX unknown"
        )
        lines += [
            "",
            cinesa_label(cfg),
            (
                f"Bookable to {cin.get('horizon') or 'unknown'}"
                f" ({cin.get('day_count') or 0} days) · {imax}."
            ),
            "Watching: " + (", ".join(cfg.cinesa_target_dates) or "no target dates"),
        ]
    degraded = snap.degraded_results
    if degraded:
        lines += [
            "Pathé check degraded — catalogue signals are available, but listing data is incomplete:",
            "; ".join(f"{endpoint}: {slug}" for slug, endpoint, _result in degraded) + ".",
        ]
        title = "All quiet — Pathé partly degraded"
    else:
        lines.append("All checks healthy.")
        title = "All quiet — nothing new"
    return Finding(
        kind="HEARTBEAT",
        key=f"heartbeat:{now:%Y-%m-%d}",
        confidence="high",
        title=title,
        lines=lines,
        url=(cfg.pathe_page_url or cfg.film_page_url) if wanted else cfg.film_page_url,
    )


def heartbeat_due(st: dict, now: datetime, days: int) -> bool:
    if days <= 0:
        return False
    last = detect.parse_iso(st.get("last_heartbeat"))
    return last is None or (now - detect.as_aware(last)) >= timedelta(days=days)
