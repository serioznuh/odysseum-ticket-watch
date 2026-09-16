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


REFUSAL_MESSAGE = "no movie allowed"


def origin_refusal(r: httpx.Response) -> str | None:
    """Pathé's own "not a movie" message for a 403, else None.

    The showtimes endpoint serves only `isMovie: true` listings. Every *event*
    listing (`isMovie: false`) — the dedicated "Projection IMAX 70mm" and
    "La Séance 70mm" entries — is refused with a short JSON string,
    `"No movie allowed !"`, which is the API saying "not a movie", not
    "not yet". Measured 2026-09-03: `l-odyssee-projection-imax-70mm-54413` is
    bookable at Odysseum *right now* and still 403s, so this never clears and
    bookability for an event listing has to come from the cinema programme
    entry instead.

    Matched on the message, not merely on "403 with a JSON string body": the
    Akamai block this project has actually observed is also JSON
    (`{"error":"Error from IP …"}` — an object, 2026-09-02), and a bot 403 that
    happened to carry a bare string must stay a hard failure rather than be
    read as "no sessions".
    """
    if r.status_code != 403:
        return None
    if not r.headers.get("content-type", "").startswith("application/json"):
        return None
    try:
        body = r.json()
    except ValueError:
        return None
    if not isinstance(body, str):
        return None
    message = " ".join(body.split())[:80]
    return message if REFUSAL_MESSAGE in message.lower() else None


def _get_json_result(
    client: httpx.Client,
    path: str,
    *,
    allow_404: bool = False,
    allow_refusal: bool = False,
) -> detect.FetchResult:
    url = path if path.startswith("http") else BASE + path
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            r = client.get(url)
            if r.status_code == 404 and allow_404:
                return detect.FetchResult.authoritative(None)
            refusal = origin_refusal(r)
            if refusal is not None:
                if allow_refusal:
                    # Expected and permanent for event listings, not an outage.
                    log.info("Pathé serves no showtimes for event listing %s (403 %r)", url, refusal)
                    return detect.FetchResult.refused(refusal)
                # Deterministic, so there is nothing to retry. The marker keeps
                # the outage alert from blaming the IP for the origin's call.
                return detect.FetchResult.failed(
                    f"HTTP 403 refused by origin from {url} — {refusal!r}"
                )
            r.raise_for_status()
            return detect.FetchResult.authoritative(r.json())
        except (httpx.HTTPError, ValueError) as e:
            last_error = e
            log.warning("GET %s failed (attempt %d/3): %s", url, attempt + 1, e)
            time.sleep(1.5 * (attempt + 1))
    return detect.FetchResult.failed(f"Pathé API request failed for {url}: {last_error}")


def get_json(
    client: httpx.Client,
    path: str,
    *,
    allow_404: bool = False,
    allow_refusal: bool = False,
) -> Any:
    """Compatibility wrapper for authoritative calls that fail by exception."""
    result = _get_json_result(
        client, path, allow_404=allow_404, allow_refusal=allow_refusal
    )
    if not result.healthy:
        raise RuntimeError(result.diagnostic)
    return result.data


def show_detail(client: httpx.Client, slug: str) -> detect.FetchResult:
    """One listing's detail, best-effort.

    Per-show calls must never fail the whole snapshot — that is precisely the
    2026-09-02 bug. A failure is carried as degraded health while the caller
    keeps whatever authoritative catalogue data is still available.
    """
    result = _get_json_result(
        client, f"/show/{slug}", allow_404=True, allow_refusal=True
    )
    if not result.healthy:
        log.warning("detail for %s unavailable, continuing: %s", slug, result.diagnostic)
    return result


def fetch_snapshot(client: httpx.Client, cfg: Any) -> detect.Snapshot:
    """Fetch every Pathé signal we watch, in ~4-8 small requests."""
    listing_results: dict[str, dict[str, detect.FetchResult]] = {}
    payload = get_json(client, "/shows")
    all_shows = payload.get("shows", payload) if isinstance(payload, dict) else payload
    matched = [
        s for s in all_shows if detect.show_matches(s, cfg.match_patterns, cfg.primary_slug)
    ]
    if not any(s.get("slug") == cfg.primary_slug for s in matched):
        detail_result = show_detail(client, cfg.primary_slug)
        listing_results.setdefault(cfg.primary_slug, {})["detail"] = detail_result
        if detail_result.data:
            matched.append(detail_result.data)
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
            detail_result = show_detail(client, slug)
            listing_results.setdefault(slug, {})["detail"] = detail_result
            matched.append(detail_result.data or {"slug": slug, "title": slug})
            matched_slugs.add(slug)

    entries: dict[str, dict] = {}
    showtimes: dict[str, dict] = {}
    for show in matched:
        slug = show.get("slug", "")
        if slug in cinema_shows:
            entries[slug] = cinema_shows[slug]
        showtimes_result = _get_json_result(
            client,
            f"/show/{slug}/showtimes/{cfg.cinema_slug}",
            allow_404=True,
            allow_refusal=True,
        )
        listing_results.setdefault(slug, {})["showtimes"] = showtimes_result
        if not showtimes_result.healthy:
            # One listing must never blind the whole watch. The catalogue calls
            # above must not be discarded, but partial health must survive into
            # supervision and must not manufacture format evidence.
            log.warning(
                "showtimes for %s unavailable, continuing: %s",
                slug,
                showtimes_result.diagnostic,
            )
        st = showtimes_result.data
        if isinstance(st, dict) and st:
            showtimes[slug] = st
        time.sleep(0.3)  # be polite

    for show in matched:
        if detect.selected_listing(show, cfg):
            slug = show.get("slug", "")
            entry = entries.get(slug) or {}
            bookable_days = sorted(
                day for day, info in (entry.get("days") or {}).items()
                if info.get("bookable") is True or info.get("isBookable") is True
            )
            log.info(
                "programme %s: bookable=%s, bookable dates=%s",
                slug, bool(entry.get("isBookable") or entry.get("bookable")), bookable_days,
            )
    snap = detect.Snapshot(
        matched_shows=matched,
        cinema_entries=entries,
        showtimes=showtimes,
        listing_results=listing_results,
    )
    log.info(
        "snapshot: %d matched listing(s) %s | at %s: %d listed, %d with sessions%s",
        len(matched),
        sorted(matched_slugs),
        cfg.cinema_slug,
        len(entries),
        len(showtimes),
        f" | {snap.degradation_summary()}" if not snap.healthy else "",
    )
    return snap
