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
import os
import time
from pathlib import Path
from typing import Any

import httpx

from . import cdp, detect
from .budget import (
    TOKEN_MINT_BUDGET_SECONDS,
    TOKEN_MINT_MINIMUM_SECONDS,
    Budget,
    out_of_time,
    request_timeout,
)

log = logging.getLogger(__name__)

REQUEST_TIMEOUT = 20.0

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

# Below this much remaining life a token is treated as unusable: a run must
# never start with one that could die mid-flight.
TOKEN_SKEW_SECONDS = 600

# While a still-valid token is being refreshed ahead of time, don't re-attempt
# minting more often than this. Without it, a Mac that is locked for three
# hours would launch Chrome on all twelve firings in the proactive window.
PROACTIVE_RETRY_SECONDS = 1800

# A 403 from the data host is more likely to be a network/IP rejection than a
# bad bearer token. Do not launch headed Chrome again on every 5-min firing
# while that condition persists.
NETWORK_REJECTION_COOLDOWN_SECONDS = 60 * 60


class TokenRejected(RuntimeError):
    """The API refused the token, retaining the response status."""

    def __init__(self, status_code: int, url: str):
        self.status_code = status_code
        self.url = url
        super().__init__(f"HTTP {status_code} from {url}")


class TokenMintCooldown(RuntimeError):
    """A previous 403 blocked token minting, so Chrome must stay closed."""

    status_code = 403

    def __init__(self):
        super().__init__(
            "Cinesa token mint is on cooldown after an HTTP 403 network/IP rejection"
        )


def make_client() -> httpx.Client:
    return httpx.Client(headers=HEADERS, timeout=REQUEST_TIMEOUT, follow_redirects=True)


def _pause(budget: Budget | None, seconds: float) -> None:
    """Backoff pause, clipped to the job's remaining budget. Goes through this
    module's own `time` so the existing test seams keep working."""
    if budget is None:
        time.sleep(seconds)
    else:
        budget.sleep(seconds)


# --------------------------------------------------------------------------- token

def token_expiry(token: str) -> int | None:
    """`exp` claim (epoch seconds) read without verifying — we only need timing."""
    try:
        segment = token.split(".")[1]
        padded = segment + "=" * (-len(segment) % 4)
        return int(json.loads(base64.urlsafe_b64decode(padded)).get("exp"))
    except (IndexError, ValueError, TypeError, binascii.Error):
        return None


def read_cache(path: str | Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_cache(path: str | Path, payload: dict) -> None:
    """Atomically write credential metadata without weakening its permissions."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    # Created 0600 rather than chmod'ed afterwards: the token must never be
    # world-readable, not even for the instant between write and chmod.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(payload))
    tmp.replace(p)


def load_cached_token(path: str | Path, now_epoch: float) -> str | None:
    """Cached token if it is still usable at all, else None."""
    token = read_cache(path).get("token") or ""
    if not token:
        return None
    exp = token_expiry(token)
    if exp is None or exp - now_epoch <= TOKEN_SKEW_SECONDS:
        return None
    return token


def save_token(path: str | Path, token: str, *, last_attempt: float | None = None) -> None:
    """Cache the token 0600 — it is a credential, and must never be committed."""
    # `or` would be wrong here: an explicit 0 ("never attempted") is falsy and
    # would silently become now, arming the backoff against a fresh cache.
    stamp = time.time() if last_attempt is None else last_attempt
    payload = {"token": token, "last_refresh_attempt": stamp}
    _write_cache(path, payload)


def mint_cooldown_active(path: str | Path, now_epoch: float | None = None) -> bool:
    """Whether a network-rejection cooldown still suppresses token minting."""
    now = time.time() if now_epoch is None else now_epoch
    try:
        return float(read_cache(path).get("mint_cooldown_until")) > now
    except (TypeError, ValueError):
        return False


def record_mint_cooldown(path: str | Path, now_epoch: float | None = None) -> None:
    """Record a fixed cooldown while preserving the cached credential."""
    now = time.time() if now_epoch is None else now_epoch
    cache = read_cache(path)
    try:
        existing = float(cache.get("mint_cooldown_until"))
    except (TypeError, ValueError):
        existing = 0.0
    # Repeated 403s during an active window must not extend it forever: the
    # watcher should get one fresh chance per window, not suppress minting for
    # the lifetime of a VPN outage.
    if existing > now:
        return
    cache["mint_cooldown_until"] = now + NETWORK_REJECTION_COOLDOWN_SECONDS
    _write_cache(path, cache)


def clear_mint_cooldown(path: str | Path) -> None:
    """Forget a network incident after the API accepts a token again."""
    cache = read_cache(path)
    if "mint_cooldown_until" not in cache:
        return
    del cache["mint_cooldown_until"]
    _write_cache(path, cache)


# Chrome's own worst case — DevTools startup, then the page poll, then the
# profile teardown — is longer than the whole Cinesa job budget, so a mint that
# ignored the budget could overrun it (and the aggregate polling budget behind
# it) on its own. Two things are reserved out of the job's remainder rather
# than shared with the mint: `cdp`'s teardown, which runs on its own allowance
# so the throwaway profile dies even when the mint does not finish, and the one
# small API call the token exists for.
CHROME_STARTUP_SECONDS = 30.0
CHROME_PAGE_WAIT_SECONDS = 60.0
CHROME_CLEANUP_RESERVE_SECONDS = cdp.CLEANUP_BUDGET_SECONDS
API_CALL_RESERVE_SECONDS = 5.0


def _mint_plan(budget: Budget | None) -> tuple[float, float, Budget | None]:
    """(DevTools startup ceiling, page-poll ceiling, hard budget) for one mint.

    The two ceilings split what is left in the same 1:2 proportion as the
    unbudgeted defaults, so startup cannot eat the page poll's share. The
    budget is the hard bound `cdp` clamps every blocking call to — the launch,
    each DevTools poll, the websocket connect and each CDP round trip — because
    ceilings on the phases alone leave those free to outlast them.

    Raises rather than launching Chrome at all when too little remains to see a
    mint through: the browser stays exactly the real headed one it has always
    been, it just gets less patience, and never a launch it cannot finish.
    """
    if budget is None:
        return CHROME_STARTUP_SECONDS, CHROME_PAGE_WAIT_SECONDS, None
    available = (
        budget.remaining() - CHROME_CLEANUP_RESERVE_SECONDS - API_CALL_RESERVE_SECONDS
    )
    if available < TOKEN_MINT_MINIMUM_SECONDS:
        raise cdp.CDPError(
            f"Chrome not launched: a Cinesa token mint needs at least"
            f" {TOKEN_MINT_MINIMUM_SECONDS:.0f}s and the {budget.label} budget"
            f" has {max(0.0, available):.0f}s left"
        )
    startup = min(CHROME_STARTUP_SECONDS, available / 3)
    return startup, available - startup, budget.child(available, "Cinesa token mint")


def mint_token(cfg: Any, budget: Budget | None = None) -> str:
    """Drive a real headed Chrome once and read the page's token."""
    startup_seconds, wait_seconds, mint_budget = _mint_plan(budget)
    log.info(
        "minting a new Cinesa token via headed Chrome (startup %.0fs, page %.0fs)",
        startup_seconds,
        wait_seconds,
    )
    token = cdp.evaluate_on_page(
        cfg.cinesa_token_url,
        TOKEN_EXPRESSION,
        chrome_path=cfg.cinesa_chrome_path,
        profile_dir=cfg.cinesa_chrome_profile,
        wait_seconds=wait_seconds,
        startup_seconds=startup_seconds,
        budget=mint_budget,
    )
    if not isinstance(token, str) or not token:
        raise cdp.CDPError("Chrome returned an empty Cinesa token")
    return token


def get_token(cfg: Any, *, force: bool = False, budget: Budget | None = None) -> str:
    """Cached token, refreshed *ahead* of expiry via a real headed Chrome.

    The token step is the one part that needs a GUI, so it is the one part a
    locked or sleeping Mac can block. Refreshing only at expiry meant a single
    blocked attempt took the whole Cinesa half down; refreshing while hours of
    life remain turns that into a long retry window, and a failed refresh falls
    back to the token still in hand instead of failing the run.

    `budget` covers the mint too (OTW-19): a Chrome that will not settle must
    not hold the run open past the ladder. A *proactive* refresh that no longer
    fits in the budget is simply deferred to a later firing, which is what the
    existing early-refresh window is for. A *required* mint — no usable token,
    or a forced renewal — still runs, with Chrome's waits shortened to what is
    left (`_mint_waits`), and fails loudly rather than going quiet if even that
    does not fit.
    """
    now = time.time()
    cache = read_cache(cfg.cinesa_token_cache)
    cached = load_cached_token(cfg.cinesa_token_cache, now)
    exp = token_expiry(cached) if cached else None

    if not force and mint_cooldown_active(cfg.cinesa_token_cache, now):
        if cached:
            log.debug(
                "using cached Cinesa token while the network-rejection cooldown is active"
            )
            return cached
        raise TokenMintCooldown()

    if cached and not force:
        remaining_h = (exp - now) / 3600 if exp else 0.0
        if remaining_h > cfg.cinesa_token_refresh_before_hours:
            log.debug("using cached Cinesa token (%.1fh left)", remaining_h)
            return cached
        since_attempt = now - float(cache.get("last_refresh_attempt") or 0)
        if since_attempt < PROACTIVE_RETRY_SECONDS:
            log.debug(
                "refresh due (%.1fh left) but last attempt was %.0f min ago — waiting",
                remaining_h,
                since_attempt / 60,
            )
            return cached
        if budget is not None and not budget.allows(TOKEN_MINT_BUDGET_SECONDS):
            log.info(
                "refresh due (%.1fh left) but only %.0fs of budget remain — deferring",
                remaining_h,
                budget.remaining(),
            )
            return cached
        log.info("refreshing Cinesa token early (%.1fh left)", remaining_h)

    try:
        token = mint_token(cfg, budget)
    except cdp.ChromeLeakError:
        # Never absorbed by the cached-token fallback below. That fallback exists
        # for a mint that simply did not work and left nothing behind; this one
        # left a Chrome holding the profile lock, so carrying on with the cached
        # token would pass the API check, clear the health state and exit 0 while
        # the next mint is already doomed.
        if cached:
            # Still record the attempt, so a persisting leak does not mean a
            # Chrome launch on every 5-min firing while it is being fixed.
            save_token(cfg.cinesa_token_cache, cached, last_attempt=now)
        log.error(
            "Cinesa token mint could not confirm Chrome was terminated —"
            " failing the check instead of continuing on the cached token;"
            " quit any leftover Chrome on the watcher profile"
        )
        raise
    except Exception as e:
        if cached and not force:
            # Still holding a usable token: stay up and try again later. Only a
            # token that is actually dead (or rejected) may fail the run.
            save_token(cfg.cinesa_token_cache, cached, last_attempt=now)
            log.warning(
                "Cinesa token refresh failed (%s) — keeping cached token,"
                " valid for another %.1fh; retrying in %d min",
                e,
                (exp - now) / 3600 if exp else 0.0,
                PROACTIVE_RETRY_SECONDS // 60,
            )
            return cached
        raise

    save_token(cfg.cinesa_token_cache, token, last_attempt=now)
    new_exp = token_expiry(token)
    log.info(
        "new Cinesa token cached (valid ~%.1fh)",
        (new_exp - now) / 3600 if new_exp else float("nan"),
    )
    return token


# --------------------------------------------------------------------------- api

def _get_json(
    client: httpx.Client, url: str, token: str, budget: Budget | None = None
) -> Any:
    last_error: Exception | None = None
    for attempt in range(3):
        # As in pathe: the budget covers the retries, not each attempt.
        if out_of_time(budget):
            raise RuntimeError(budget.exhausted_message())
        try:
            r = client.get(
                url,
                headers={"Authorization": f"Bearer {token}"},
                timeout=request_timeout(budget, REQUEST_TIMEOUT),
            )
            if r.status_code in (401, 403):
                raise TokenRejected(r.status_code, url)
            r.raise_for_status()
            return r.json()
        except TokenRejected:
            raise
        except (httpx.HTTPError, ValueError) as e:
            last_error = e
            log.warning("GET %s failed (attempt %d/3): %s", url, attempt + 1, e)
            _pause(budget, 1.5 * (attempt + 1))
    raise RuntimeError(f"Cinesa API request failed for {url}: {last_error}")


def _token_after_403(cfg: Any, token: str, budget: Budget | None = None) -> str:
    """Try one mint for a 403, or reuse the valid token during its cooldown."""
    now = time.time()
    if mint_cooldown_active(cfg.cinesa_token_cache, now):
        log.info("Cinesa API returned 403 — retrying the cached token without Chrome")
        return token

    try:
        return get_token(cfg, force=True, budget=budget)
    except cdp.ChromeLeakError:
        # The same exemption as in get_token, and needed here too: this fallback
        # keeps the old token for a *network* rejection, and must not quietly
        # absorb a Chrome that was left holding the profile lock. No cooldown
        # either — that mechanism is for IP rejections, and arranging quiet
        # retries is the opposite of what a leak needs.
        raise
    except Exception as e:
        # The 403 is usually an IP/network rejection. Keep the token that was
        # just used; it may work as soon as the VPN/proxy is removed.
        record_mint_cooldown(cfg.cinesa_token_cache, now)
        exp = token_expiry(token)
        if exp is None or exp > now:
            log.warning(
                "Cinesa token mint after HTTP 403 failed (%s) — keeping the cached"
                " token; Chrome minting is suppressed for the cooldown window",
                e,
            )
            return token
        raise


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


def fetch_snapshot(cfg: Any, budget: Budget | None = None) -> detect.CinesaSnapshot:
    """One check: at most one browser launch, exactly one small API call.

    `budget` is an aggregate allowance for the token step and the call
    together; running out of it fails the check like any other Cinesa outage,
    through the existing capped failure streak.
    """
    url = (
        f"{cfg.cinesa_api_base}/ocapi/v1/film-screening-dates"
        f"?siteIds={cfg.cinesa_site_id}&filmIds={cfg.cinesa_film_id}"
    )
    token = get_token(cfg, budget=budget)
    client = make_client()
    try:
        try:
            payload = _get_json(client, url, token, budget)
        except TokenRejected as e:
            if e.status_code == 401:
                # A 401 is an authentication failure: force an immediate mint,
                # even if a network-rejection cooldown is present.
                log.info("Cinesa token rejected (%s) — forcing one new token", e)
                payload = _get_json(
                    client, url, get_token(cfg, force=True, budget=budget), budget
                )
            elif e.status_code == 403:
                # A 403 is more likely an IP/network rejection. A failed mint
                # is contained by a cache-only cooldown, and the still-valid
                # token gets another API chance on later firings.
                log.info("Cinesa API rejected the network (%s)", e)
                retry_token = _token_after_403(cfg, token, budget)
                try:
                    payload = _get_json(client, url, retry_token, budget)
                except TokenRejected as retry_error:
                    if retry_error.status_code == 403:
                        # A freshly minted token that is also rejected points
                        # to the same network/IP block; start the cooldown.
                        record_mint_cooldown(cfg.cinesa_token_cache)
                    raise
            else:
                raise
    finally:
        client.close()

    clear_mint_cooldown(cfg.cinesa_token_cache)

    days = parse_screening_dates(payload, cfg)
    log.info(
        "cinesa snapshot: %d bookable day(s) %s→%s, IMAX on %d",
        len(days),
        days[0]["date"] if days else "-",
        days[-1]["date"] if days else "-",
        sum(1 for d in days if cfg.cinesa_imax_attribute_id in d["attributes"]),
    )
    return detect.CinesaSnapshot(days=days)
