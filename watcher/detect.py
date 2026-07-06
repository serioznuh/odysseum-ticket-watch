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
            findings.append(
                Finding(
                    kind="NEW_LISTING",
                    key=f"new_show:{slug}",
                    confidence="high",
                    title="New Pathé listing detected",
                    lines=[
                        f"Listing: {title}",
                        f"Detected format: {FORMAT_LABELS[listing_fmt]}",
                        f"Release date: {fmt_release(show)}",
                        "Dedicated event listings get their own sale opening — watch incoming.",
                        "Confidence: HIGH — new entry in the official Pathé catalogue matching your film patterns.",
                    ],
                    url=url,
                )
            )

        # 2. Sale-opening datetime published or changed (THE advance signal).
        sale_iso = show.get("salesOpeningDatetime")
        if sale_iso and known_sales.get(slug) != sale_iso:
            changed = slug in known_sales
            display_iso = show.get("showtimesDisplayDatetime")
            lines = [
                f"Listing: {title}",
                f"Sales open: {fmt_dt(parse_iso(sale_iso))}",
                f"Watched cinema: {cfg.cinema_name}, {cfg.cinema_city}",
                f"Format of this listing: {FORMAT_LABELS[listing_fmt]}",
            ]
            if changed:
                lines.insert(
                    1, f"Previous sale opening: {fmt_dt(parse_iso(known_sales[slug]))} → CHANGED"
                )
            if display_iso and display_iso != sale_iso:
                lines.append(f"Showtimes visible from: {fmt_dt(parse_iso(display_iso))}")
            lines += [
                "Confidence: HIGH — official Pathé API field salesOpeningDatetime.",
                "Note: opening time is national; popular IMAX 70 mm seats can sell out in minutes.",
            ]
            findings.append(
                Finding(
                    kind="SALE_DATE_CHANGED" if changed else "SALE_DATE",
                    key=f"sale:{slug}:{sale_iso}",
                    confidence="high",
                    title="Ticket sale opening CHANGED" if changed else "Ticket sale opening announced",
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
                fmt_bits = [
                    f"{FORMAT_LABELS[f]}: {summary['counts'].get(f, '?')} session(s)"
                    for f in sorted(present)
                ]
                best = (
                    FMT_IMAX70
                    if FMT_IMAX70 in present
                    else (FMT_IMAX if FMT_IMAX in present else FMT_OTHER)
                )
                book = summary["booking_by_fmt"].get(best) or url
                lines = [
                    f"Listing: {title}",
                    f"Cinema: {cfg.cinema_name}, {cfg.cinema_city}",
                    "Formats bookable: " + "; ".join(fmt_bits),
                ]
                if summary["first_day"]:
                    lines.append(
                        f"Session dates: {summary['first_day']} → {summary['last_day']}"
                        f" ({summary['total']} sessions)"
                    )
                lines += [
                    f"Book ({FORMAT_LABELS[best]}): {book}",
                    "Confidence: HIGH — sessions returned by the Pathé booking API for this cinema.",
                ]
                findings.append(
                    Finding(
                        kind="TICKETS_AVAILABLE",
                        key="tickets:%s:%s" % (slug, ",".join(sorted(new_fmts))),
                        confidence="high",
                        title="Tickets are bookable NOW",
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
                    title="Film now listed at the cinema (not bookable yet)",
                    lines=[
                        f"Listing: {title}",
                        f"Cinema: {cfg.cinema_name}, {cfg.cinema_city}",
                        "The film appears on the cinema's programme feed without bookable sessions — sales usually open soon after.",
                        "Confidence: HIGH — Pathé cinema programme API.",
                    ],
                    url=url,
                )
            )

    return findings


# --------------------------------------------------------------------------- phrases & dates (news layer)

STRONG_PHRASES = [
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
    "tickets available",
    "tickets on sale",
    "on sale",
    "advance tickets",
    "booking open",
    "book tickets",
    "imax 70mm",
    "imax 70 mm",
    "70mm",
    "70 mm",
    "1.43",
]

# Weak phrases only count next to a sale-context word, so a release-date line
# like "au cinéma à partir du 16 décembre 2026" is NOT a signal.
WEAK_PHRASES = ["a partir du", "des le "]
CONTEXT_WORDS = ["reservation", "billet", "vente", "ticket", "sale", "booking", "prevente", "imax"]


def phrase_hits(text: str) -> list[str]:
    t = norm(text)
    hits = {p for p in STRONG_PHRASES if re.search(rf"\b{re.escape(p)}\b", t)}
    for w in WEAK_PHRASES:
        for m in re.finditer(re.escape(w), t):
            window = t[max(0, m.start() - 60): m.end() + 60]
            if any(c in window for c in CONTEXT_WORDS):
                hits.add(w.strip())
                break
    return sorted(hits)


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

    An item must match a film pattern AND at least one sale/format phrase.
    Dates equal to the film's release date are not treated as sale dates.
    """
    findings: list[Finding] = []
    sent = state.get("alerts", {})
    try:
        release = date.fromisoformat(cfg.release_date) if cfg.release_date else None
    except ValueError:
        release = None

    for item in items:
        url = (item.get("url") or "").strip()
        if not url:
            continue
        text = f"{item.get('title', '')} {item.get('summary', '')}"
        hay = norm(text)
        if not any(re.search(p, hay) for p in cfg.match_patterns):
            continue
        hits = phrase_hits(text)
        if not hits:
            continue
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

        confidence = "medium" if sale_dates else "low"
        lines = [
            f"Headline: {(item.get('title') or '').strip()}",
            f"Source: {item.get('source') or 'web'}"
            + (f", published {pub:%Y-%m-%d}" if pub else ""),
            f"Matched phrases: {', '.join(hits)}",
        ]
        if sale_dates:
            lines.append(
                "Date(s) mentioned (possible sale date): "
                + ", ".join(d.strftime("%d %b %Y") for d in sale_dates)
            )
        lines.append(
            f"Confidence: {confidence.upper()} — external lead, verify on Pathé."
            " No reminders are scheduled from news alone."
        )
        findings.append(
            Finding(
                kind="NEWS_LEAD",
                key=key,
                confidence=confidence,
                title="News lead: possible ticket-sale info",
                lines=lines,
                url=url,
            )
        )
        if len(findings) >= cfg.news_max_alerts_per_run:
            break

    return findings
