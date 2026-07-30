"""Configuration loading: config.toml for the watch target, environment for secrets."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib  # type: ignore[no-redef]


@dataclass
class Config:
    # [film]
    primary_slug: str
    film_title: str
    film_page_url: str
    release_date: str
    match_patterns: list[str]
    # [cinema]
    cinema_slug: str
    cinema_name: str
    cinema_city: str
    cinema_page_url: str
    # [reminders]
    reminder_offsets_minutes: list[int]
    # [news]
    news_enabled: bool
    news_min_confidence: str
    news_max_age_days: int
    news_max_alerts_per_run: int
    google_news_queries: list[str]
    extra_pages: list[str]
    # [alerts]
    heartbeat_days: int
    failure_streak_threshold: int
    stale_check_hours: int
    silent_kinds: list[str]
    # [cadence]
    cadence_baseline_hours: float
    cadence_within_week_hours: float
    cadence_final_48h_hours: float
    cadence_opening_window_minutes: float
    cadence_after_tickets_hours: float
    # [cinesa]
    cinesa_enabled: bool
    cinesa_film_id: str
    cinesa_film_title: str
    cinesa_page_url: str
    cinesa_site_id: str
    cinesa_site_name: str
    cinesa_site_city: str
    cinesa_imax_attribute_id: str
    cinesa_target_dates: list[str]
    cinesa_api_base: str
    cinesa_token_url: str
    cinesa_token_cache: str
    cinesa_token_refresh_before_hours: float
    cinesa_chrome_path: str
    cinesa_chrome_profile: str
    # [general]
    state_file: str
    # environment
    telegram_token: str | None
    telegram_chat_id: str | None


def load_config(path: str | Path) -> Config:
    raw = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    film = raw.get("film", {})
    cinema = raw.get("cinema", {})
    reminders = raw.get("reminders", {})
    news = raw.get("news", {})
    alerts = raw.get("alerts", {})
    cadence = raw.get("cadence", {})
    general = raw.get("general", {})
    cinesa = raw.get("cinesa", {})

    if "primary_slug" not in film:
        raise ValueError("config: [film] primary_slug is required")
    if "slug" not in cinema:
        raise ValueError("config: [cinema] slug is required")

    cinesa_enabled = bool(cinesa.get("enabled", False))
    if cinesa_enabled:
        for required in ("film_id", "site_id"):
            if not cinesa.get(required):
                raise ValueError(f"config: [cinesa] {required} is required when enabled")
    cinesa_page_url = str(cinesa.get("page_url") or "https://www.cinesa.es/")
    target_dates = [str(d) for d in cinesa.get("target_dates", [])]
    for value in target_dates:
        try:
            date.fromisoformat(value)
        except ValueError:
            raise ValueError(
                f"config: [cinesa] target_dates entry {value!r} is not YYYY-MM-DD"
            ) from None

    return Config(
        primary_slug=film["primary_slug"],
        film_title=film.get("title", film["primary_slug"]),
        film_page_url=film.get(
            "page_url", f"https://www.pathe.fr/films/{film['primary_slug']}"
        ),
        release_date=str(film.get("release_date", "")),
        match_patterns=list(film.get("match_patterns", [])),
        cinema_slug=cinema["slug"],
        cinema_name=cinema.get("name", cinema["slug"]),
        cinema_city=cinema.get("city", ""),
        cinema_page_url=cinema.get(
            "page_url", f"https://www.pathe.fr/cinemas/{cinema['slug']}"
        ),
        reminder_offsets_minutes=sorted(
            (int(x) for x in reminders.get("offsets_minutes", [1440, 120, 15])),
            reverse=True,
        ),
        news_enabled=bool(news.get("enabled", True)),
        news_min_confidence=str(news.get("min_confidence", "low")),
        news_max_age_days=int(news.get("max_age_days", 10)),
        news_max_alerts_per_run=int(news.get("max_alerts_per_run", 3)),
        google_news_queries=list(news.get("google_news_queries", [])),
        extra_pages=list(news.get("extra_pages", [])),
        heartbeat_days=int(alerts.get("heartbeat_days", 7)),
        failure_streak_threshold=int(alerts.get("failure_streak_threshold", 3)),
        stale_check_hours=int(alerts.get("stale_check_hours", 72)),
        silent_kinds=[
            str(k).upper()
            for k in alerts.get("silent_kinds", ["HEARTBEAT", "NEWS_LEAD", "RECOVERED"])
        ],
        cadence_baseline_hours=float(cadence.get("baseline_hours", 4.0)),
        cadence_within_week_hours=float(cadence.get("within_week_hours", 2.0)),
        cadence_final_48h_hours=float(cadence.get("final_48h_hours", 0.5)),
        cadence_opening_window_minutes=float(cadence.get("opening_window_minutes", 15)),
        cadence_after_tickets_hours=float(cadence.get("after_tickets_hours", 6.0)),
        cinesa_enabled=cinesa_enabled,
        cinesa_film_id=str(cinesa.get("film_id", "")),
        cinesa_film_title=str(cinesa.get("film_title", cinesa.get("film_id", ""))),
        cinesa_page_url=cinesa_page_url,
        cinesa_site_id=str(cinesa.get("site_id", "")),
        cinesa_site_name=str(cinesa.get("site_name", cinesa.get("site_id", ""))),
        cinesa_site_city=str(cinesa.get("site_city", "")),
        cinesa_imax_attribute_id=str(cinesa.get("imax_attribute_id", "0000000086")),
        cinesa_target_dates=target_dates,
        cinesa_api_base=str(
            cinesa.get("api_base", "https://vwc.cinesa.es/WSVistaWebClient")
        ),
        # Documented fallback: the token is minted on the film page itself.
        cinesa_token_url=str(cinesa.get("token_url") or cinesa_page_url),
        cinesa_token_cache=str(cinesa.get("token_cache", ".cache/cinesa-token.json")),
        cinesa_token_refresh_before_hours=float(
            cinesa.get("token_refresh_before_hours", 3.0)
        ),
        cinesa_chrome_path=str(
            cinesa.get(
                "chrome_path", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
            )
        ),
        cinesa_chrome_profile=str(
            cinesa.get("chrome_profile", ".cache/chrome-profile")
        ),
        state_file=general.get("state_file", "state/state.json"),
        telegram_token=os.environ.get("TELEGRAM_BOT_TOKEN") or None,
        telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID") or None,
    )
