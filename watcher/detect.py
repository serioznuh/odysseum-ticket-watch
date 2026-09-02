"""Pure detection logic — no I/O, fully unit-testable.

Confidence model:
  high   — structured facts from the official Pathé API (salesOpeningDatetime,
           bookable sessions at the watched cinema).
  medium — external news item matching the film AND a sale phrase AND carrying
           a plausible future date (other than the release date).
  low    — external news item matching the film and a sale phrase, no date.

A bare "Réserver maintenant" button is never used as evidence: Pathé signals
are read from structured fields, and text phrases are only applied to news.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

TZ_PARIS = ZoneInfo("Europe/Paris")

# --------------------------------------------------------------------------- formats

FMT_IMAX70 = "imax70"
FMT_IMAX = "imax"
FMT_OTHER = "other"

FORMAT_LABELS = {
    FMT_IMAX70: "IMAX 70 mm (1.43:1)",
    FMT_IMAX: "IMAX",
    FMT_OTHER: "Standard / other",
}

IMAX70_RE = re.compile(r"70\s*mm|1\.43|imax\s*70")
IMAX_RE = re.compile(r"imax")


def norm(text: str | None) -> str:
    """Lowercase, strip accents, collapse whitespace."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", text.lower()).strip()


def classify_format(*texts: str | None) -> str:
    hay = norm(" ".join(t for t in texts if t))
    if IMAX70_RE.search(hay):
        return FMT_IMAX70
    if IMAX_RE.search(hay):
        return FMT_IMAX
    return FMT_OTHER


# --------------------------------------------------------------------------- shows

def show_matches(show: dict, patterns: list[str], primary_slug: str) -> bool:
    if show.get("slug") == primary_slug:
        return True
    hay = norm(f"{show.get('slug', '')} {show.get('title', '')}")
    return any(re.search(p, hay) for p in patterns)


def show_url(show: dict) -> str:
    kind = "films" if show.get("isMovie", True) else "evenements"
    return f"https://www.pathe.fr/{kind}/{show.get('slug', '')}"


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def as_aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=TZ_PARIS)


def fmt_dt(dt: datetime | None) -> str:
    if dt is None:
        return "unknown"
    dt = as_aware(dt).astimezone(TZ_PARIS)
    return dt.strftime("%a %d %b %Y, %H:%M") + " (Paris time)"


def fmt_dt_short(dt: datetime | None) -> str:
    """'Wed 15 Oct, 10:00' — for titles, where the year and the timezone note
    cost more room than they earn. Still Paris time, like every other time."""
    if dt is None:
        return "unknown"
    dt = as_aware(dt).astimezone(TZ_PARIS)
    return f"{dt:%a} {dt.day} {dt:%b}, {dt:%H:%M}"


def fmt_day(iso_day: str | None) -> str:
    """'2026-12-16' -> '16 Dec'."""
    if not iso_day:
        return "unknown"
    try:
        d = date.fromisoformat(iso_day)
    except ValueError:
        return iso_day
    return f"{d.day} {d:%b}"


def fmt_offsets(minutes: list[int]) -> str:
    """[1440, 120, 15] -> '24 h, 2 h and 15 min'."""
    def one(m: int) -> str:
        if m >= 1440 and m % 1440 == 0:
            return f"{m // 1440} d" if m > 1440 else "24 h"
        if m >= 60 and m % 60 == 0:
            return f"{m // 60} h"
        return f"{m} min"

    parts = [one(m) for m in sorted(minutes, reverse=True)]
    if not parts:
        return "none"
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + f" and {parts[-1]}"


def plural(n: int, word: str) -> str:
    """'1 session' / '2 sessions'. Alerts are read at a glance; '(s)' is a form
    field, not a sentence."""
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def watch_line(cfg: Any) -> str:
    """Film and cinema, on every alert. With more than one watch configured an
    unlabelled alert cannot be told apart from another target's."""
    return f"{cfg.film_title} · {cfg.cinema_name}, {cfg.cinema_city}"


def cinesa_line(cfg: Any) -> str:
    return f"{cfg.cinesa_film_title} · {cfg.cinesa_site_name}, {cfg.cinesa_site_city}"


def fmt_release(show: dict | None) -> str:
    rel = (show or {}).get("releaseAt") or {}
    if isinstance(rel, dict):
        return rel.get("FR_FR") or next(iter(rel.values()), "unknown")
    return str(rel) if rel else "unknown"


# --------------------------------------------------------------------------- data

@dataclass
class Snapshot:
    """One fetch of everything we watch on the Pathé side."""

    matched_shows: list[dict] = field(default_factory=list)
    cinema_entries: dict[str, dict] = field(default_factory=dict)  # slug -> programme entry
    showtimes: dict[str, dict[str, list[dict]]] = field(default_factory=dict)  # slug -> day -> sessions


@dataclass
class CinesaSnapshot:
    """One fetch of the Cinesa booking calendar for the watched film + site.

    `days` is sorted by date: [{"date": "YYYY-MM-DD", "attributes": [ids]}].
    The last entry is the current booking horizon.
    """

    days: list[dict] = field(default_factory=list)


@dataclass
class Finding:
    kind: str  # SALE_DATE, SALE_DATE_CHANGED, TICKETS_AVAILABLE, NEW_LISTING, CINEMA_LISTED, NEWS_LEAD, ...
    key: str  # dedup key: one alert per key, ever
    confidence: str  # high / medium / low
    title: str
    lines: list[str]
    url: str
    sale_datetime: str | None = None  # ISO; set only for Pathé API dates (drives reminders)


def summarize_sessions(show: dict, days: dict[str, list[dict]]) -> dict:
    counts: dict[str, int] = {}
    booking_by_fmt: dict[str, str] = {}
    total = 0
    for sessions in days.values():
        for s in sessions:
            fmt = classify_format(
                show.get("title"),
                show.get("slug"),
                " ".join(s.get("tags") or []),
                s.get("auditoriumName"),
                s.get("specialShowtimeDetails"),
            )
            counts[fmt] = counts.get(fmt, 0) + 1
            total += 1
            if fmt not in booking_by_fmt and s.get("refCmd"):
                booking_by_fmt[fmt] = s["refCmd"]
    all_days = sorted(days)
    return {
        "counts": counts,
        "total": total,
        "first_day": all_days[0] if all_days else None,
        "last_day": all_days[-1] if all_days else None,
        "booking_by_fmt": booking_by_fmt,
    }


def reminders_cover(sale_iso: str, snap: Snapshot, state: dict, now: datetime) -> bool:
    """Whether the reminder ladder will actually fire for this opening.

    `due_reminders` tracks a single `sale_target` — the earliest *future*
    opening across all matched listings — and stops entirely once tickets are
    known to be bookable. Announcing "reminders set" for anything else was a
    promise the watcher does not keep.
    """
    if state.get("tickets_available"):
        return False
    target = parse_iso(sale_iso)
    if target is None or as_aware(target) <= now:
        return False
    future = [
        as_aware(dt)
        for dt in (
            parse_iso(sh.get("salesOpeningDatetime")) for sh in snap.matched_shows
        )
        if dt is not None and as_aware(dt) > now
    ]
    return bool(future) and as_aware(target) == min(future)


# --------------------------------------------------------------------------- Pathé analysis

def analyze_pathe(snap: Snapshot, state: dict, cfg: Any, now: datetime) -> list[Finding]:
    findings: list[Finding] = []
    shows_seen = set(state.get("shows_seen", []))
    known_sales: dict = state.get("sales", {})
    formats_seen = {k: set(v) for k, v in state.get("formats_seen", {}).items()}

    for show in snap.matched_shows:
        slug = show.get("slug", "")
        title = show.get("title", slug)
        url = show_url(show)
        listing_fmt = classify_format(title, slug)

        # 1. Brand-new listing matching the film (e.g. a dedicated
        #    "Projection IMAX 70mm" event page, as Pathé did for L'Odyssée).
        if slug not in shows_seen and slug != cfg.primary_slug:
            # A dedicated event page often appears with its opening already
            # set, and the SALE_DATE finding below fires in the same pass.
            new_sale = show.get("salesOpeningDatetime")
            findings.append(
                Finding(
                    kind="NEW_LISTING",
                    key=f"new_show:{slug}",
                    confidence="high",
                    title=f"New listing: {FORMAT_LABELS[listing_fmt]}",
                    lines=[
                        watch_line(cfg),
                        f"“{title}”",
                        (
                            f"Release {fmt_release(show)} · "
                            + (
                                f"sale opens {fmt_dt_short(parse_iso(new_sale))}."
                                if new_sale
                                else "no sale date published yet."
                            )
                        ),
                        "Dedicated listings get their own opening — now watching it.",
                    ],
                    url=url,
                )
            )

        # 2. Sale-opening datetime published or changed (THE advance signal).
        sale_iso = show.get("salesOpeningDatetime")
        if sale_iso and known_sales.get(slug) != sale_iso:
            changed = slug in known_sales
            display_iso = show.get("showtimesDisplayDatetime")
            sale_dt = parse_iso(sale_iso)
            lines = [f"{cfg.film_title} · {FORMAT_LABELS[listing_fmt]} · {cfg.cinema_name}"]
            if changed:
                prev = parse_iso(known_sales[slug])
                moved = ""
                if prev and sale_dt:
                    shift = as_aware(sale_dt) - as_aware(prev)
                    days = round(shift.total_seconds() / 86400)
                    if days:
                        moved = (
                            f" — moved {plural(abs(days), 'day')}"
                            f" {'later' if days > 0 else 'earlier'}"
                        )
                    else:
                        hours = round(shift.total_seconds() / 3600)
                        if hours:
                            moved = (
                                f" — moved {abs(hours)} h"
                                f" {'later' if hours > 0 else 'earlier'}"
                            )
                lines.append(f"Was: {fmt_dt_short(prev)}{moved}.")
            if display_iso and display_iso != sale_iso:
                lines.append(f"Showtimes visible from {fmt_dt_short(parse_iso(display_iso))}.")
            if reminders_cover(sale_iso, snap, state, now):
                lines.append(
                    "Reminders rescheduled automatically."
                    if changed
                    else f"Reminders set: {fmt_offsets(cfg.reminder_offsets_minutes)} before."
                )
            lines.append("Opening time is national — seats can go in minutes.")
            findings.append(
                Finding(
                    kind="SALE_DATE_CHANGED" if changed else "SALE_DATE",
                    key=f"sale:{slug}:{sale_iso}",
                    confidence="high",
                    title=(
                        f"Sale time CHANGED → {fmt_dt_short(sale_dt)}"
                        if changed
                        else f"Sale opens {fmt_dt_short(sale_dt)}"
                    ),
                    lines=lines,
                    url=url,
                    sale_datetime=sale_iso,
                )
            )

        # 3. Bookable sessions at the watched cinema (fallback + confirmation signal).
        entry = snap.cinema_entries.get(slug) or {}
        days = snap.showtimes.get(slug) or {}
        entry_bookable = bool(entry.get("isBookable") or entry.get("bookable"))
        if days or entry_bookable:
            summary = summarize_sessions(show, days)
            present = set(summary["counts"]) if summary["counts"] else {listing_fmt}
            new_fmts = present - formats_seen.get(slug, set())
            if new_fmts:
                # The alert exists because `new_fmts` appeared; naming the
                # best format merely *present* would re-announce IMAX 70 mm
                # when what actually changed was standard sessions.
                best = (
                    FMT_IMAX70
                    if FMT_IMAX70 in new_fmts
                    else (FMT_IMAX if FMT_IMAX in new_fmts else FMT_OTHER)
                )
                book = summary["booking_by_fmt"].get(best) or url
                lines = [watch_line(cfg)]
                if summary["first_day"]:
                    lines.append(
                        f"{plural(summary['total'], 'session')},"
                        f" {fmt_day(summary['first_day'])} – {fmt_day(summary['last_day'])}."
                    )
                others = [
                    f"{FORMAT_LABELS[f]}: {summary['counts'].get(f, '?')}"
                    for f in sorted(present)
                    if f != best
                ]
                if others:
                    lines.append("Also bookable: " + "; ".join(others))
                lines.append(f"👉 {book}")
                findings.append(
                    Finding(
                        kind="TICKETS_AVAILABLE",
                        key="tickets:{}:{}".format(slug, ",".join(sorted(new_fmts))),
                        confidence="high",
                        title=f"BOOK NOW — {FORMAT_LABELS[best]} is live",
                        lines=lines,
                        url=url,
                    )
                )
        elif entry:
            # 4. Listed on the cinema's programme but nothing bookable yet.
            findings.append(
                Finding(
                    kind="CINEMA_LISTED",
                    key=f"cinema_listed:{slug}",
                    confidence="high",
                    title=f"Now on the programme at {cfg.cinema_name}",
                    lines=[
                        watch_line(cfg),
                        "On the cinema's feed, with no bookable sessions yet.",
                        "Sales usually open within days of this.",
                    ],
                    url=url,
                )
            )

    return findings


# --------------------------------------------------------------------------- Cinesa analysis

def imax_days(days: list[dict], imax_attribute_id: str) -> list[str]:
    return [d["date"] for d in days if imax_attribute_id in (d.get("attributes") or [])]


def analyze_cinesa(snap: CinesaSnapshot, state: dict, cfg: Any, now: datetime) -> list[Finding]:
    """Target dates becoming bookable in IMAX, plus IMAX leaving/returning.

    Deliberately narrow: the watched dates are the only thing that buzzes. The
    booking horizon itself is recorded in state (and surfaces in the heartbeat)
    but never alerts on its own — it may simply roll forward one day per day,
    which would be pure noise.
    """
    findings: list[Finding] = []
    cin = state.get("cinesa", {})
    days = snap.days
    known = {d["date"] for d in days}
    imax = set(imax_days(days, cfg.cinesa_imax_attribute_id))

    # 1. A watched date became bookable.
    for target in cfg.cinesa_target_dates:
        if target not in known:
            continue
        if target in imax:
            findings.append(
                Finding(
                    kind="CINESA_TARGET_DATE",
                    key=f"cinesa_target:{cfg.cinesa_site_id}:{cfg.cinesa_film_id}:{target}",
                    confidence="high",
                    title=f"Your date is open — {fmt_day(target)}, IMAX",
                    lines=[
                        cinesa_line(cfg),
                        f"{target} is bookable with IMAX in that day's schedule.",
                        f"👉 {cfg.cinesa_page_url}",
                    ],
                    url=cfg.cinesa_page_url,
                )
            )
        else:
            # Silent by default: the day is live but IMAX is not on it (yet).
            findings.append(
                Finding(
                    kind="CINESA_TARGET_NO_IMAX",
                    key=f"cinesa_target_noimax:{cfg.cinesa_site_id}:{cfg.cinesa_film_id}:{target}",
                    confidence="high",
                    title=f"{fmt_day(target)} opened — but no IMAX",
                    lines=[
                        cinesa_line(cfg),
                        f"{target} is bookable, with no IMAX session on it.",
                        "You'll get a loud alert if IMAX appears for it.",
                    ],
                    url=cfg.cinesa_page_url,
                )
            )

    # 2. IMAX disappeared / came back. An empty snapshot is treated as a blip,
    #    never as evidence: a transient API hiccup must not fake a drop alert.
    was_present = cin.get("imax_present")
    if days:
        if imax and was_present is False:
            findings.append(
                Finding(
                    kind="CINESA_IMAX_BACK",
                    key=f"cinesa_imax_back:{now:%Y-%m-%d}",
                    confidence="high",
                    title="IMAX is back",
                    lines=[
                        cinesa_line(cfg),
                        (
                            f"Scheduled again on {plural(len(imax), 'day')},"
                            f" {fmt_day(min(imax))} → {fmt_day(max(imax))}."
                        ),
                    ],
                    url=cfg.cinesa_page_url,
                )
            )
        # One confirmation required before crying wolf (see streak below).
        elif not imax and was_present and cin.get("imax_absent_streak", 0) >= 1:
            findings.append(
                Finding(
                    kind="CINESA_IMAX_GONE",
                    key=f"cinesa_imax_gone:{now:%Y-%m-%d}",
                    confidence="high",
                    title="IMAX dropped from the schedule",
                    lines=[
                        cinesa_line(cfg),
                        (
                            f"{plural(len(days), 'day')} still bookable"
                            f" ({fmt_day(days[0]['date'])} → {fmt_day(days[-1]['date'])}),"
                            " none in IMAX — likely moved off the IMAX screen."
                        ),
                        "Confirmed over two consecutive checks.",
                    ],
                    url=cfg.cinesa_page_url,
                )
            )

    return findings


# --------------------------------------------------------------------------- phrases & dates (news layer)

# Phrases that talk about the ticket sale itself. Bare ticket nouns are
# included: in film news they are reliably sale-related, and SEO trailer spam
# never uses them (it says "trailer", "4K", "IMAX 70mm" — format words only).
SALE_PHRASES = [
    "reservez vos places",
    "reservations ouvertes",
    "les reservations sont ouvertes",
    "reservations disponibles",
    "ouverture des reservations",
    "ouverture des ventes",
    "mise en vente",
    "billets disponibles",
    "billetterie ouverte",
    "prevente",
    "preventes",
    "reservation",
    "reservations",
    "billet",
    "billets",
    "billetterie",
    "tickets available",
    "tickets on sale",
    "on sale",
    "advance tickets",
    "booking open",
    "book tickets",
    "ticket",
    "tickets",
]

# Format keywords are relevant but far too generic alone — every trailer
# re-upload says "IMAX 70mm". For news they only count next to a venue word.
FORMAT_PHRASES = ["imax 70mm", "imax 70 mm", "70mm", "70 mm", "1.43"]

# Weak phrases only count next to a sale-context word, so a release-date line
# like "au cinéma à partir du 16 décembre 2026" is NOT a signal.
WEAK_PHRASES = ["a partir du", "des le "]
CONTEXT_WORDS = ["reservation", "billet", "vente", "ticket", "sale", "booking", "prevente"]


def _match_phrases(t: str, phrases: list[str]) -> set[str]:
    return {p for p in phrases if re.search(rf"\b{re.escape(p)}\b", t)}


def sale_phrase_hits(text: str) -> list[str]:
    """Sale wording, incl. weak date-intro phrases near a sale-context word."""
    t = norm(text)
    hits = _match_phrases(t, SALE_PHRASES)
    for w in WEAK_PHRASES:
        for m in re.finditer(re.escape(w), t):
            window = t[max(0, m.start() - 60): m.end() + 60]
            if any(c in window for c in CONTEXT_WORDS):
                hits.add(w.strip())
                break
    return sorted(hits)


def format_phrase_hits(text: str) -> list[str]:
    return sorted(_match_phrases(norm(text), FORMAT_PHRASES))


def phrase_hits(text: str) -> list[str]:
    """All sale + format phrase hits (for display and page scanning)."""
    return sorted(set(sale_phrase_hits(text)) | set(format_phrase_hits(text)))


MONTHS = {
    "janvier": 1, "janv": 1, "january": 1, "jan": 1,
    "fevrier": 2, "fevr": 2, "fev": 2, "february": 2, "feb": 2,
    "mars": 3, "march": 3, "mar": 3,
    "avril": 4, "avr": 4, "april": 4, "apr": 4,
    "mai": 5, "may": 5,
    "juin": 6, "june": 6, "jun": 6,
    "juillet": 7, "juil": 7, "july": 7, "jul": 7,
    "aout": 8, "august": 8, "aug": 8,
    "septembre": 9, "september": 9, "sept": 9, "sep": 9,
    "octobre": 10, "october": 10, "oct": 10,
    "novembre": 11, "november": 11, "nov": 11,
    "decembre": 12, "december": 12, "dec": 12,
}

DATE_DAY_FIRST = re.compile(r"\b(\d{1,2})(?:er)?\s+([a-z]{3,10})\.?\b(?:\s+(\d{4})\b)?")
DATE_MONTH_FIRST = re.compile(r"\b([a-z]{3,10})\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b(?:,?\s*(\d{4})\b)?")
DATE_NUMERIC = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")


def _infer_year(month: int, day: int, explicit: int | None, today: date) -> int | None:
    if explicit:
        return explicit
    try:
        candidate = date(today.year, month, day)
    except ValueError:
        return None
    return today.year if candidate >= today else today.year + 1


def extract_dates(text: str, today: date) -> list[date]:
    """Extract French/English/numeric dates; missing years resolve to the next occurrence."""
    t = norm(text)
    out: set[date] = set()

    for m in DATE_DAY_FIRST.finditer(t):
        month = MONTHS.get(m.group(2))
        if not month:
            continue
        day = int(m.group(1))
        year = _infer_year(month, day, int(m.group(3)) if m.group(3) else None, today)
        if year is None:
            continue
        try:
            out.add(date(year, month, day))
        except ValueError:
            pass

    for m in DATE_MONTH_FIRST.finditer(t):
        month = MONTHS.get(m.group(1))
        if not month:
            continue
        day = int(m.group(2))
        year = _infer_year(month, day, int(m.group(3)) if m.group(3) else None, today)
        if year is None:
            continue
        try:
            out.add(date(year, month, day))
        except ValueError:
            pass

    for m in DATE_NUMERIC.finditer(t):
        try:
            out.add(date(int(m.group(3)), int(m.group(2)), int(m.group(1))))
        except ValueError:
            pass

    return sorted(out)


def analyze_news(items: list[dict], cfg: Any, state: dict, now: datetime) -> list[Finding]:
    """Classify news/page items into low/medium-confidence leads.

    An item must match a film pattern AND carry sale wording; format keywords
    alone ("IMAX 70mm" — every trailer re-upload has them) only count when
    the item also mentions the venue/market (Pathé / cinema / city / France).
    Dates equal to the film's release date are not treated as sale dates.
    """
    findings: list[Finding] = []
    sent = state.get("alerts", {})
    try:
        release = date.fromisoformat(cfg.release_date) if cfg.release_date else None
    except ValueError:
        release = None
    venue_words = {"pathe", "france"}
    venue_words.update(norm(cfg.cinema_name).split())
    venue_words.update(norm(cfg.cinema_city).split())
    venue_words.discard("")
    only_medium = getattr(cfg, "news_min_confidence", "low") == "medium"

    for item in items:
        url = (item.get("url") or "").strip()
        if not url:
            continue
        text = f"{item.get('title', '')} {item.get('summary', '')}"
        hay = norm(text)
        if not any(re.search(p, hay) for p in cfg.match_patterns):
            continue
        sale_hits = sale_phrase_hits(text)
        fmt_hits = format_phrase_hits(text)
        if not sale_hits and not fmt_hits:
            continue
        if not sale_hits and not any(
            re.search(rf"\b{re.escape(v)}\b", hay) for v in venue_words
        ):
            continue  # format-keyword-only match without venue context = noise
        hits = sorted(set(sale_hits) | set(fmt_hits))
        dates = extract_dates(text, now.date())
        sale_dates = [d for d in dates if d != release and d >= now.date()]

        if item.get("is_page"):
            sig = ",".join(hits) + "|" + ",".join(d.isoformat() for d in sale_dates)
            key = "page:" + hashlib.sha1((url + "|" + sig).encode()).hexdigest()[:16]
        else:
            key = "news:" + hashlib.sha1(url.encode()).hexdigest()[:16]
        if key in sent:
            continue

        pub = item.get("published")
        if pub is not None:
            pub = as_aware(pub)
            if (now - pub).days > cfg.news_max_age_days:
                continue

        confidence = "medium" if (sale_dates and sale_hits) else "low"
        if only_medium and confidence == "low":
            continue
        dates = ", ".join(f"{d.day} {d:%b %Y}" for d in sale_dates) if sale_dates else ""
        # The film, not the cinema: a press article is about the release, and
        # naming a cinema it never mentions would overstate what this says.
        lines = [
            cfg.film_title,
            f"“{(item.get('title') or '').strip()}”",
            f"{item.get('source') or 'web'}"
            + (f", {pub.day} {pub:%b}" if pub else "")
            + f" · matched: {', '.join(hits)}",
            (
                f"Not confirmed on Pathé ({confidence} confidence)"
                " — no reminders are set from news alone."
            ),
        ]
        findings.append(
            Finding(
                kind="NEWS_LEAD",
                key=key,
                confidence=confidence,
                title=(
                    f"Press says {dates} — unconfirmed"
                    if dates
                    else "Press mention — unconfirmed"
                ),
                lines=lines,
                url=url,
            )
        )
        if len(findings) >= cfg.news_max_alerts_per_run:
            break

    return findings
