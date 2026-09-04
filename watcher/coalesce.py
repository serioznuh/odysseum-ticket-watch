"""Merge same-run findings that carry the same news into one message.

Analysis is per listing and per date, and that is right for dedup memory:
every finding keeps its own `Finding.key`, so an item announced yesterday
stays quiet today. But one pass can raise several findings a human reads as a
single piece of news — two Pathé listings whose sale opens at the same minute,
three watched Cinesa dates that became bookable together. Delivered one by one
they arrive as a burst of near-identical messages, which is the noise this
watcher exists to avoid.

Merging runs *after* the already-sent filter, so a group only ever holds
findings actually about to go out: a date announced last week never re-joins
one. The message discharges every member key at once — all of them on a
successful send, none on a failure, so a failed send retries the whole group
on the next run rather than splitting it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from . import detect
from .detect import Finding

# Kinds whose findings are one-per-item and can share a message with their
# siblings. Everything else — errors, heartbeats, news leads, CINEMA_LISTED —
# is one-per-run already and always stands alone.
SALE_KINDS = ("SALE_DATE", "SALE_DATE_CHANGED")
ITEM_GROUP_KINDS = (
    "NEW_LISTING",
    "TICKETS_AVAILABLE",
    "CINESA_TARGET_DATE",
    "CINESA_TARGET_NO_IMAX",
)

_FMT_ORDER = {detect.FMT_IMAX70: 0, detect.FMT_IMAX: 1, detect.FMT_OTHER: 2}
_CONF_ORDER = {"low": 0, "medium": 1, "high": 2}

# Beyond this many items the title lists a count instead of every value: a
# title long enough to wrap is worse than one that says "4 dates".
MAX_TITLE_ITEMS = 3


@dataclass
class Alert:
    """One outgoing message and every dedup key it discharges."""

    finding: Finding
    keys: list[str]
    kinds: list[str] = field(default_factory=list)

    @property
    def merged(self) -> bool:
        return len(self.keys) > 1


# --------------------------------------------------------------------------- grouping

def group_of(f: Finding) -> tuple | None:
    """The bucket `f` merges into, or None when it must stand alone.

    A finding with no `merge_item` never merges: the title builders below name
    the items, and one that cannot be named would silently vanish from a
    merged message while its key was still marked sent. A kind with no
    `_GROUPS` entry never merges either — one message per finding is the old
    behaviour and costs nothing, where raising here would abort the send loop
    and take every other alert in the run down with it.
    """
    if f.merge_item is None:
        return None
    if f.kind in SALE_KINDS:
        # Same film, same cinema, same minute: one opening, however many
        # listings carry it. A different minute is different news.
        if not (f.sale_datetime and "sale" in _GROUPS):
            return None
        return ("sale", f.sale_datetime)
    if f.kind in ITEM_GROUP_KINDS and f.kind in _GROUPS:
        return (f.kind,)
    return None


def merge(findings: list[Finding], cfg: Any) -> list[Alert]:
    """Group `findings` into the messages to actually send, in order.

    A single-member group is passed through untouched, so the common case
    renders exactly as it did before any merging existed.
    """
    buckets: dict[Any, list[Finding]] = {}
    for i, f in enumerate(findings):
        g = group_of(f)
        buckets.setdefault(g if g is not None else ("solo", i), []).append(f)

    alerts: list[Alert] = []
    for members in buckets.values():
        if len(members) == 1:
            f = members[0]
            alerts.append(Alert(finding=f, keys=[f.key], kinds=[f.kind]))
        else:
            alerts.append(
                Alert(
                    finding=_merge_findings(members, cfg),
                    keys=[m.key for m in members],
                    kinds=[m.kind for m in members],
                )
            )
    return alerts


# --------------------------------------------------------------------------- merging

def _merge_findings(members: list[Finding], cfg: Any) -> Finding:
    spec = _GROUPS[group_of(members[0])[0]]
    lead = _lead(members)
    head = spec.head(members, cfg) if spec.head else members[0].lines[0]
    return Finding(
        kind=_merged_kind(members),
        # Only used for logging; `run` marks every key in `Alert.keys`.
        key=lead.key,
        confidence=min(members, key=lambda m: _CONF_ORDER.get(m.confidence, 0)).confidence,
        title=spec.title(members, cfg),
        lines=[head] + _merge_bodies(members, spec),
        url=lead.url,
        sale_datetime=lead.sale_datetime,
        merge_item=lead.merge_item,
    )


def _merged_kind(members: list[Finding]) -> str:
    """The kind the merged message speaks with — it picks the icon.

    A moved opening outranks a first announcement: the title says CHANGED, so
    the icon has to agree.
    """
    kinds = [m.kind for m in members]
    return "SALE_DATE_CHANGED" if "SALE_DATE_CHANGED" in kinds else kinds[0]


def _lead(members: list[Finding]) -> Finding:
    """The member whose link the merged message carries: the best format on
    offer, since that is the one the user is here for. Dates rank equal, and
    Cinesa members all share one URL anyway, so the first wins."""
    return min(
        members, key=lambda m: _FMT_ORDER.get(m.merge_item or "", len(_FMT_ORDER))
    )


def _merge_bodies(members: list[Finding], spec: _GroupSpec) -> list[str]:
    """Every member's body, minus the identity line the caller already merged.

    Lines unique to a member stay with it, in member order, so nothing is
    lost — unless the group folds them, which is for groups whose per-item
    line is one sentence with the item swapped in: repeating it verbatim per
    date is the same noise, one message down. Lines all members carry are said
    once, at the end, where they read as the message's conclusion ("Opening
    time is national — seats can go in minutes.") rather than an interruption
    between items.
    """
    bodies = [list(m.lines[1:]) for m in members]
    shared = [line for line in bodies[0] if all(line in b for b in bodies[1:])]
    if spec.fold is not None:
        out = list(spec.fold(members))
    else:
        out = []
        for body in bodies:
            for line in body:
                if line not in shared and line not in out:
                    out.append(line)
    for line in shared:
        if line not in out:
            out.append(line)
    return out


# --------------------------------------------------------------------------- per-group text

def _fmt_classes(members: list[Finding]) -> list[str]:
    seen = [m.merge_item for m in members if m.merge_item]
    return sorted(dict.fromkeys(seen), key=lambda c: _FMT_ORDER.get(c, len(_FMT_ORDER)))


def _fmt_labels(members: list[Finding]) -> str:
    return ", ".join(
        detect.FORMAT_LABELS.get(c, c) for c in _fmt_classes(members)
    )


def _days(members: list[Finding]) -> str:
    dates = sorted(dict.fromkeys(m.merge_item for m in members if m.merge_item))
    if len(dates) > MAX_TITLE_ITEMS:
        return detect.plural(len(dates), "date")
    return ", ".join(detect.fmt_day(d) for d in dates)


def _sale_title(members: list[Finding], cfg: Any) -> str:
    when = detect.fmt_dt_short(detect.parse_iso(members[0].sale_datetime))
    if any(m.kind == "SALE_DATE_CHANGED" for m in members):
        return f"Sale time CHANGED → {when}"
    return f"Sale opens {when}"


def _sale_head(members: list[Finding], cfg: Any) -> str:
    """The Pathé sale line names the format, so it is the one identity line
    that differs between members and has to be rebuilt."""
    return f"{cfg.film_title} · {_fmt_labels(members)} · {cfg.cinema_name}"


def _new_listing_title(members: list[Finding], cfg: Any) -> str:
    return f"New listings: {_fmt_labels(members)}"


def _tickets_title(members: list[Finding], cfg: Any) -> str:
    verb = "is live" if len(_fmt_classes(members)) == 1 else "are live"
    return f"BOOK NOW — {_fmt_labels(members)} {verb}"


def _cinesa_dates(members: list[Finding]) -> str:
    return ", ".join(sorted(dict.fromkeys(m.merge_item for m in members if m.merge_item)))


def _cinesa_open_fold(members: list[Finding]) -> list[str]:
    return [f"Bookable with IMAX: {_cinesa_dates(members)}"]


def _cinesa_no_imax_fold(members: list[Finding]) -> list[str]:
    return [f"Bookable, no IMAX session on them: {_cinesa_dates(members)}"]


def _cinesa_open_title(members: list[Finding], cfg: Any) -> str:
    return f"Your dates are open — {_days(members)}, IMAX"


def _cinesa_no_imax_title(members: list[Finding], cfg: Any) -> str:
    return f"{_days(members)} opened — but no IMAX"


@dataclass(frozen=True)
class _GroupSpec:
    """How one group speaks when it has more than one member.

    `head` is None wherever every member's identity line is already identical
    (film + cinema), which is all of them except the Pathé sale line.
    """

    title: Callable[[list[Finding], Any], str]
    head: Callable[[list[Finding], Any], str] | None = None
    # Replaces *every* per-member line with the lines it returns, so a group
    # that folds must render each member's whole contribution itself. Adding a
    # second varying line to a folded finding without extending its fold drops
    # that line from merged messages. Shared lines are still appended after.
    fold: Callable[[list[Finding]], list[str]] | None = None


_GROUPS = {
    "sale": _GroupSpec(title=_sale_title, head=_sale_head),
    "NEW_LISTING": _GroupSpec(title=_new_listing_title),
    "TICKETS_AVAILABLE": _GroupSpec(title=_tickets_title),
    "CINESA_TARGET_DATE": _GroupSpec(title=_cinesa_open_title, fold=_cinesa_open_fold),
    "CINESA_TARGET_NO_IMAX": _GroupSpec(
        title=_cinesa_no_imax_title, fold=_cinesa_no_imax_fold
    ),
}
