"""External sources: Google News RSS feeds and optional extra pages."""

from __future__ import annotations

import html
import logging
import re
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from .budget import Budget, out_of_time, request_timeout

log = logging.getLogger(__name__)

REQUEST_TIMEOUT = 20.0

TAG_RE = re.compile(r"<[^>]+>")


def make_client() -> httpx.Client:
    """Return the cloud-safe client used when news runs without Pathé.

    Redirects stay disabled so an allowed feed or explicitly opted-in page
    cannot bounce a cloud request onto a Pathé host after the URL guard.
    """
    return httpx.Client(timeout=REQUEST_TIMEOUT, follow_redirects=False)


def _hostname(url: str) -> str:
    return (urlsplit(url).hostname or "").rstrip(".").lower()


def _is_pathe_url(url: str) -> bool:
    host = _hostname(url)
    return host == "pathe.fr" or host.endswith(".pathe.fr")


def _cloud_feed_allowed(url: str) -> bool:
    host = _hostname(url)
    return host == "news.google.com" or host.endswith(".news.google.com")


def _cloud_urls(cfg: Any) -> tuple[list[str], list[str]]:
    feeds: list[str] = []
    pages: list[str] = []
    for url in cfg.google_news_queries:
        if _is_pathe_url(url):
            log.error("cloud news refused Pathé URL: %s", url)
        elif not _cloud_feed_allowed(url):
            log.warning("cloud news skipped non-Google feed: %s", url)
        else:
            feeds.append(url)
    for url in getattr(cfg, "cloud_extra_pages", []):
        if _is_pathe_url(url):
            log.error("cloud news refused Pathé URL: %s", url)
        else:
            pages.append(url)
    return feeds, pages


def strip_tags(markup: str) -> str:
    return html.unescape(TAG_RE.sub(" ", markup or ""))


def parse_rss(xml_text: str) -> list[dict]:
    items: list[dict] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        log.warning("RSS parse error: %s", e)
        return items
    for node in root.iter("item"):
        published = None
        pub_text = node.findtext("pubDate")
        if pub_text:
            try:
                published = parsedate_to_datetime(pub_text)
            except (TypeError, ValueError):
                pass
        items.append(
            {
                "title": strip_tags(node.findtext("title") or "").strip(),
                "url": (node.findtext("link") or "").strip(),
                "summary": strip_tags(node.findtext("description") or "").strip(),
                "source": (node.findtext("source") or "").strip() or None,
                "published": published,
            }
        )
    return items


def fetch_news_items(
    client: httpx.Client,
    cfg: Any,
    budget: Budget | None = None,
    *,
    cloud: bool = False,
) -> list[dict]:
    """Fetch all configured feeds and extra pages. Failures are logged, not fatal.

    The budget covers the whole loop, not each feed: news is the least
    time-critical signal here, and a feed that hangs must not push the reminder
    ladder out of its window. Whatever was fetched before the budget ran out is
    still analysed.
    """
    items: list[dict] = []
    feed_urls, page_urls = (
        _cloud_urls(cfg) if cloud else (cfg.google_news_queries, cfg.extra_pages)
    )
    for feed_url in feed_urls:
        if out_of_time(budget):
            log.warning("news feeds cut short: %s", budget.exhausted_message())
            return items
        try:
            r = client.get(feed_url, timeout=request_timeout(budget, REQUEST_TIMEOUT))
            r.raise_for_status()
            fetched = parse_rss(r.text)
            log.info("news feed ok (%d items): %s", len(fetched), feed_url)
            items.extend(fetched)
        except httpx.HTTPError as e:
            log.warning("news feed failed %s: %s", feed_url, e)

    for page_url in page_urls:
        if out_of_time(budget):
            log.warning("watched pages cut short: %s", budget.exhausted_message())
            return items
        try:
            r = client.get(page_url, timeout=request_timeout(budget, REQUEST_TIMEOUT))
            r.raise_for_status()
            text = strip_tags(r.text)
            items.append(
                {
                    "title": f"Watched page: {page_url}",
                    "url": page_url,
                    "summary": text[:20000],
                    "source": page_url,
                    "published": None,
                    "is_page": True,
                }
            )
            log.info("extra page ok: %s", page_url)
        except httpx.HTTPError as e:
            log.warning("extra page failed %s: %s", page_url, e)

    return items
