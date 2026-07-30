"""Client for Cinesa's Vista OCAPI (verified 2026-07).

Two hosts, and the split is the whole design:

  www.cinesa.es          Cloudflare managed challenge — 403 for every plain
                         HTTP client, on every path except robots.txt. Its only
                         role here is minting the API token (see cdp.py).
  vwc.cinesa.es          The real data API. NOT bot-protected: plain httpx gets
                         clean JSON, it just wants the bearer token.

So exactly one step needs a browser, roughly twice a day (tokens live 12 h);
every actual check is a single small httpx call and can run as often as we like.

Endpoint used:
  GET /ocapi/v1/film-screening-dates?siteIds={site}&filmIds={film}
    -> {"filmScreeningDates": [{"businessDate": "YYYY-MM-DD",
          "filmScreenings": [{"filmId", "sites": [{"siteId",
             "showtimeAttributeIds": [...]}]}]}]}

`showtimeAttributeIds` is what carries the format: IMAX at Diagonal Mar is
attribute 0000000086.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx

from . import cdp, detect

log = logging.getLogger(__name__)

# The token is published into the page as window.initialData.api.authToken.
TOKEN_EXPRESSION = (
    "(window.initialData&&window.initialData.api&&window.initialData.api.authToken)||''"
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        " (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
    "Origin": "https://www.cinesa.es",
    "Referer": "https://www.cinesa.es/",
}

# Refresh when the cached token has less than this much life left.
TOKEN_SKEW_SECONDS = 600


class TokenRejected(RuntimeError):
    """The API refused the token (401/403) — it needs re-minting."""


def make_client() -> httpx.Client:
    return httpx.Client(headers=HEADERS, timeout=20.0, follow_redirects=True)


# --------------------------------------------------------------------------- token

def token_expiry(token: str) -> int | None:
    """`exp` claim (epoch seconds) read without verifying — we only need timing."""
    try:
        segment = token.split(".")[1]
        padded = segment + "=" * (-len(segment) % 4)
        return int(json.loads(base64.urlsafe_b64decode(padded)).get("exp"))
    except (IndexError, ValueError, TypeError, binascii.Error):
        return None


def load_cached_token(path: str | Path, now_epoch: float) -> str | None:
    """Cached token if it is still comfortably valid, else None."""
    p = Path(path)
    if not p.exists():
        return None
    try:
        token = json.loads(p.read_text(encoding="utf-8")).get("token") or ""
    except (OSError, json.JSONDecodeError):
        return None
    if not token:
        return None
    exp = token_expiry(token)
    if exp is None or exp - now_epoch <= TOKEN_SKEW_SECONDS:
        return None
    return token


def save_token(path: str | Path, token: str) -> None:
    """Cache the token 0600 — it is a credential, and must never be committed."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"token": token}), encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(p)


def get_token(cfg: Any, *, force: bool = False) -> str:
    """Cached token, or a freshly minted one via a real headed Chrome."""
    if not force:
        cached = load_cached_token(cfg.cinesa_token_cache, time.time())
        if cached:
            log.debug("using cached Cinesa token")
            return cached
    log.info("minting a new Cinesa token via headed Chrome")
    token = cdp.evaluate_on_page(
        cfg.cinesa_token_url,
        TOKEN_EXPRESSION,
        chrome_path=cfg.cinesa_chrome_path,
        profile_dir=cfg.cinesa_chrome_profile,
    )
    if not isinstance(token, str) or not token:
        raise cdp.CDPError("Chrome returned an empty Cinesa token")
    save_token(cfg.cinesa_token_cache, token)
    exp = token_expiry(token)
    log.info(
        "new Cinesa token cached (valid ~%.1fh)",
        (exp - time.time()) / 3600 if exp else float("nan"),
    )
    return token


# --------------------------------------------------------------------------- api

def _get_json(client: httpx.Client, url: str, token: str) -> Any:
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            r = client.get(url, headers={"Authorization": f"Bearer {token}"})
            if r.status_code in (401, 403):
                raise TokenRejected(f"HTTP {r.status_code} from {url}")
            r.raise_for_status()
            return r.json()
        except TokenRejected:
            raise
        except (httpx.HTTPError, ValueError) as e:
            last_error = e
            log.warning("GET %s failed (attempt %d/3): %s", url, attempt + 1, e)
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Cinesa API request failed for {url}: {last_error}")


def parse_screening_dates(payload: Any, cfg: Any) -> list[dict]:
    """Flatten the OCAPI response to [{"date", "attributes"}], sorted by date."""
    days: list[dict] = []
    for entry in (payload or {}).get("filmScreeningDates", []):
        date = entry.get("businessDate")
        if not date:
            continue
        # The film AND the site must match together: the same response carries
        # other Cinesa venues, and counting one of those would inflate the
        # horizon and could fire a target-date alert for the wrong cinema.
        # A matching day with no attributes still counts as bookable.
        present = False
        attributes: set[str] = set()
        for screening in entry.get("filmScreenings", []):
            if screening.get("filmId") != cfg.cinesa_film_id:
                continue
            for site in screening.get("sites", []):
                if site.get("siteId") == cfg.cinesa_site_id:
                    present = True
                    attributes.update(site.get("showtimeAttributeIds") or [])
        if present:
            days.append({"date": date, "attributes": sorted(attributes)})
    return sorted(days, key=lambda d: d["date"])


def fetch_snapshot(cfg: Any) -> detect.CinesaSnapshot:
    """One check: at most one browser launch, exactly one small API call."""
    url = (
        f"{cfg.cinesa_api_base}/ocapi/v1/film-screening-dates"
        f"?siteIds={cfg.cinesa_site_id}&filmIds={cfg.cinesa_film_id}"
    )
    token = get_token(cfg)
    client = make_client()
    try:
        try:
            payload = _get_json(client, url, token)
        except TokenRejected as e:
            # Expected roughly twice a day, or if Cinesa rotates signing keys.
            log.info("Cinesa token rejected (%s) — re-minting once", e)
            payload = _get_json(client, url, get_token(cfg, force=True))
    finally:
        client.close()

    days = parse_screening_dates(payload, cfg)
    log.info(
        "cinesa snapshot: %d bookable day(s) %s→%s, IMAX on %d",
        len(days),
        days[0]["date"] if days else "-",
        days[-1]["date"] if days else "-",
        sum(1 for d in days if cfg.cinesa_imax_attribute_id in d["attributes"]),
    )
    return detect.CinesaSnapshot(days=days)
