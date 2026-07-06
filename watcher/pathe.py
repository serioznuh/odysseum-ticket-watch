"""Client for the public www.pathe.fr JSON API.

Endpoints (verified 2026-07):
  GET /api/shows                                -> {"shows": [ {slug, title, salesOpeningDatetime, ...} ]}
  GET /api/show/{slug}                          -> film/event detail (same fields)
  GET /api/cinema/{cinemaSlug}/shows            -> {"days": {...}, "shows": {slug: {bookable, isBookable, days: {...}}}}
  GET /api/show/{slug}/showtimes/{cinemaSlug}   -> {"YYYY-MM-DD": [ {time, tags, status, refCmd, auditoriumName, ...} ]}
                                                   ([] when the film has no sessions there)

The HTML pages are behind Akamai Bot Manager (403 for plain HTTP clients);
these JSON endpoints are not (as of writing) and must be used instead.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from . import detect

log = logging.getLogger(__name__)

BASE = "https://www.pathe.fr/api"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        " (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    "Referer": "https://www.pathe.fr/",
}


def make_client() -> httpx.Client:
    return httpx.Client(headers=HEADERS, timeout=20.0, follow_redirects=True)


def get_json(client: httpx.Client, path: str, *, allow_404: bool = False) -> Any:
    url = path if path.startswith("http") else BASE + path
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            r = client.get(url)
            if r.status_code == 404 and allow_404:
                return None
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, ValueError) as e:
            last_error = e
            log.warning("GET %s failed (attempt %d/3): %s", url, attempt + 1, e)
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Pathé API request failed for {url}: {last_error}")


def fetch_snapshot(client: httpx.Client, cfg: Any) -> detect.Snapshot:
    """Fetch every Pathé signal we watch, in ~4-8 small requests."""
    payload = get_json(client, "/shows")
    all_shows = payload.get("shows", payload) if isinstance(payload, dict) else payload
    matched = [
        s for s in all_shows if detect.show_matches(s, cfg.match_patterns, cfg.primary_slug)
    ]
    if not any(s.get("slug") == cfg.primary_slug for s in matched):
        detail = get_json(client, f"/show/{cfg.primary_slug}", allow_404=True)
        if detail:
            matched.append(detail)
        else:
            log.warning("primary slug %s not found in Pathé catalogue", cfg.primary_slug)

    cinema_payload = get_json(client, f"/cinema/{cfg.cinema_slug}/shows")
    cinema_shows = cinema_payload.get("shows", {}) if isinstance(cinema_payload, dict) else {}

    # Catch listings visible only on the cinema programme (defensive).
    matched_slugs = {s.get("slug") for s in matched}
    for slug in cinema_shows:
        if slug not in matched_slugs and detect.show_matches(
            {"slug": slug}, cfg.match_patterns, cfg.primary_slug
        ):
            detail = get_json(client, f"/show/{slug}", allow_404=True)
            matched.append(detail or {"slug": slug, "title": slug})
            matched_slugs.add(slug)

    entries: dict[str, dict] = {}
    showtimes: dict[str, dict] = {}
    for show in matched:
        slug = show.get("slug", "")
        if slug in cinema_shows:
            entries[slug] = cinema_shows[slug]
        st = get_json(client, f"/show/{slug}/showtimes/{cfg.cinema_slug}", allow_404=True)
        if isinstance(st, dict) and st:
            showtimes[slug] = st
        time.sleep(0.3)  # be polite

    log.info(
        "snapshot: %d matched listing(s) %s | at %s: %d listed, %d with sessions",
        len(matched),
        sorted(matched_slugs),
        cfg.cinema_slug,
        len(entries),
        len(showtimes),
    )
    return detect.Snapshot(matched_shows=matched, cinema_entries=entries, showtimes=showtimes)
