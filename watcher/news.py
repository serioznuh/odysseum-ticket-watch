"""External sources: Google News RSS feeds and optional extra pages."""

from __future__ import annotations

import html
import logging
import re
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

log = logging.getLogger(__name__)

TAG_RE = re.compile(r"<[^>]+>")


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


def fetch_news_items(client: httpx.Client, cfg: Any) -> list[dict]:
    """Fetch all configured feeds and extra pages. Failures are logged, not fatal."""
    items: list[dict] = []
    for feed_url in cfg.google_news_queries:
        try:
            r = client.get(feed_url)
            r.raise_for_status()
            fetched = parse_rss(r.text)
            log.info("news feed ok (%d items): %s", len(fetched), feed_url)
            items.extend(fetched)
        except httpx.HTTPError as e:
            log.warning("news feed failed %s: %s", feed_url, e)

    for page_url in cfg.extra_pages:
        try:
            r = client.get(page_url)
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
