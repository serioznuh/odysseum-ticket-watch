"""Unit tests for the pure detection logic."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

from watcher import detect
from watcher.detect import FMT_IMAX, FMT_IMAX70, FMT_OTHER, Snapshot
from watcher.state import DEFAULT_STATE

PARIS = detect.TZ_PARIS
NOW = datetime(2026, 7, 6, 9, 0, tzinfo=PARIS)
TODAY = NOW.date()

PATTERNS = [
    'dune.{0,16}troisieme',
    'dune.{0,16}part(ie)?[ .:-]*(three|iii|3)\\b',
    'dune[ .:-]{0,4}3\\b',
]

PRIMARY = "dune-troisieme-partie-50828"


class Cfg:
    primary_slug = PRIMARY
    film_title = "Dune : Troisième partie"
    film_page_url = f"https://www.pathe.fr/films/{PRIMARY}"
    release_date = "2026-12-16"
    match_patterns = PATTERNS
    cinema_slug = "cinema-pathe-odysseum"
    cinema_name = "Pathé Odysseum"
    cinema_city = "Montpellier"
    cinema_page_url = "https://www.pathe.fr/cinemas/cinema-pathe-odysseum"
    news_min_confidence = "low"
    news_max_age_days = 10
    news_max_alerts_per_run = 3


def fresh_state() -> dict:
    return json.loads(json.dumps(DEFAULT_STATE))


def primary_show(**over) -> dict:
    show = {
        "slug": PRIMARY,
        "title": "Dune : Troisième partie",
        "isMovie": True,
        "releaseAt": {"FR_FR": "2026-12-16"},
        "salesOpeningDatetime": None,
        "showtimesDisplayDatetime": None,
    }
    show.update(over)
    return show


def event_show(**over) -> dict:
    show = {
        "slug": "dune-troisieme-partie-projection-imax-70mm-56789",
        "title": "Dune : Troisième partie : Projection IMAX 70mm",
        "isMovie": False,
        "releaseAt": {"FR_FR": "2026-12-16"},
        "salesOpeningDatetime": None,
        "showtimesDisplayDatetime": None,
    }
    show.update(over)
    return show


# ------------------------------------------------------------------ matching

def test_norm_strips_accents_and_case():
    assert detect.norm("Réservations  Ouvertes ! Décembre") == "reservations ouvertes ! decembre"


def test_show_matches_primary_and_event():
    assert detect.show_matches(primary_show(), PATTERNS, PRIMARY)
    assert detect.show_matches(event_show(), PATTERNS, PRIMARY)
    assert detect.show_matches({"slug": "dune-part-three-imax-1234", "title": "Dune: Part Three"}, PATTERNS, PRIMARY)


def test_show_matches_rejects_other_films():
    assert not detect.show_matches(
        {"slug": "dune-deuxieme-partie-40000", "title": "Dune : Deuxième partie"}, PATTERNS, PRIMARY
    )
    assert not detect.show_matches(
        {"slug": "des-minions-et-des-monstres-40746", "title": "Des Minions et des monstres"},
        PATTERNS,
        PRIMARY,
    )
    assert not detect.show_matches({"slug": "la-dune-3000", "title": "La Dune"}, PATTERNS, PRIMARY)


# ------------------------------------------------------------------ formats

def test_classify_format():
    assert detect.classify_format("Dune : Projection IMAX 70mm") == FMT_IMAX70
    assert detect.classify_format("some film", "tags: imax pmr") == FMT_IMAX
    assert detect.classify_format("IMAX 1.43:1 experience") == FMT_IMAX70
    assert detect.classify_format("Des Minions", "3d 4dx") == FMT_OTHER


# ------------------------------------------------------------------ Pathé analysis

def test_baseline_no_findings():
    snap = Snapshot(matched_shows=[primary_show()])
    assert detect.analyze_pathe(snap, fresh_state(), Cfg, NOW) == []


def test_new_event_listing_detected_once():
    snap = Snapshot(matched_shows=[primary_show(), event_show()])
    findings = detect.analyze_pathe(snap, fresh_state(), Cfg, NOW)
    assert [f.kind for f in findings] == ["NEW_LISTING"]
    assert "IMAX 70 mm" in " ".join(findings[0].lines)
    assert findings[0].url.startswith("https://www.pathe.fr/evenements/")

    seen = fresh_state()
    seen["shows_seen"] = [PRIMARY, event_show()["slug"]]
    assert detect.analyze_pathe(snap, seen, Cfg, NOW) == []


def test_sale_date_announced():
    iso = "2026-11-05T08:00:00+01:00"
    snap = Snapshot(matched_shows=[primary_show(salesOpeningDatetime=iso)])
    findings = detect.analyze_pathe(snap, fresh_state(), Cfg, NOW)
    assert len(findings) == 1
    f = findings[0]
    assert f.kind == "SALE_DATE"
    assert f.confidence == "high"
    assert f.sale_datetime == iso
    assert f.key == f"sale:{PRIMARY}:{iso}"
    assert any("05 Nov 2026" in line for line in f.lines)


def test_sale_date_changed_and_unchanged():
    old = "2026-11-05T08:00:00+01:00"
    new = "2026-11-12T08:00:00+01:00"
    st = fresh_state()
    st["shows_seen"] = [PRIMARY]
    st["sales"] = {PRIMARY: old}

    snap_same = Snapshot(matched_shows=[primary_show(salesOpeningDatetime=old)])
    assert detect.analyze_pathe(snap_same, st, Cfg, NOW) == []

    snap_new = Snapshot(matched_shows=[primary_show(salesOpeningDatetime=new)])
    findings = detect.analyze_pathe(snap_new, st, Cfg, NOW)
    assert [f.kind for f in findings] == ["SALE_DATE_CHANGED"]
    assert findings[0].sale_datetime == new


def test_tickets_available_with_imax70_format():
    show = event_show()
    st = fresh_state()
    st["shows_seen"] = [PRIMARY, show["slug"]]
    days = {
        "2026-12-16": [
            {
                "tags": ["DEFAULT", "imax", "pmr"],
                "auditoriumName": "IMAX",
                "refCmd": "https://s.pathe.fr/fr/BOOKME/booking",
                "status": "available",
            }
        ]
    }
    snap = Snapshot(
        matched_shows=[primary_show(), show],
        cinema_entries={show["slug"]: {"isBookable": True, "bookable": True}},
        showtimes={show["slug"]: days},
    )
    findings = detect.analyze_pathe(snap, st, Cfg, NOW)
    assert [f.kind for f in findings] == ["TICKETS_AVAILABLE"]
    f = findings[0]
    joined = " ".join(f.lines)
    assert "IMAX 70 mm (1.43:1)" in joined  # from the listing title
    assert "https://s.pathe.fr/fr/BOOKME/booking" in joined
    assert "Pathé Odysseum" in joined
    assert f.key == f"tickets:{show['slug']}:imax70"


def test_tickets_available_not_repeated_for_known_format():
    show = event_show()
    st = fresh_state()
    st["shows_seen"] = [PRIMARY, show["slug"]]
    st["formats_seen"] = {show["slug"]: ["imax70"]}
    days = {"2026-12-16": [{"tags": ["imax"], "auditoriumName": "IMAX", "refCmd": "x"}]}
    snap = Snapshot(matched_shows=[show], showtimes={show["slug"]: days})
    assert detect.analyze_pathe(snap, st, Cfg, NOW) == []


def test_cinema_listed_but_not_bookable():
    st = fresh_state()
    st["shows_seen"] = [PRIMARY]
    snap = Snapshot(
        matched_shows=[primary_show()],
        cinema_entries={PRIMARY: {"isBookable": False, "bookable": False}},
    )
    findings = detect.analyze_pathe(snap, st, Cfg, NOW)
    assert [f.kind for f in findings] == ["CINEMA_LISTED"]


# ------------------------------------------------------------------ phrases

def test_generic_reserve_button_is_not_a_signal():
    assert detect.phrase_hits("Réserver maintenant") == []


def test_release_date_sentence_is_not_a_signal():
    assert detect.phrase_hits("Au cinéma à partir du 16 décembre 2026") == []


def test_sale_phrases_hit():
    assert "reservations ouvertes" in detect.phrase_hits("Les réservations ouvertes dès maintenant !")
    hits = detect.phrase_hits("Réservations ouvertes à partir du 5 novembre")
    assert "a partir du" in hits
    assert "imax 70 mm" in detect.phrase_hits("la projection IMAX 70 mm de Dune")
    assert "on sale" in detect.phrase_hits("Tickets go on sale Monday")


# ------------------------------------------------------------------ dates

def test_extract_dates_french():
    assert detect.extract_dates("ouverture le 5 novembre 2026", TODAY) == [date(2026, 11, 5)]
    assert detect.extract_dates("dès le 1er décembre", TODAY) == [date(2026, 12, 1)]
    assert detect.extract_dates("le 05/11/2026 à 9h", TODAY) == [date(2026, 11, 5)]


def test_extract_dates_english_and_year_inference():
    assert detect.extract_dates("on sale November 5, 2026", TODAY) == [date(2026, 11, 5)]
    # month has already passed this year -> next year
    assert detect.extract_dates("opens 5 January", TODAY) == [date(2027, 1, 5)]


def test_extract_dates_ignores_bare_month_year():
    assert detect.extract_dates("en décembre 2026", TODAY) == []


# ------------------------------------------------------------------ news

def news_item(title: str, url: str = "https://example.com/a", days_old: int = 1) -> dict:
    return {
        "title": title,
        "url": url,
        "summary": "",
        "source": "Example",
        "published": NOW - timedelta(days=days_old),
    }


def test_news_lead_with_future_date_is_medium():
    items = [news_item("Dune 3 : les réservations IMAX 70mm ouvriront le 5 novembre")]
    findings = detect.analyze_news(items, Cfg, fresh_state(), NOW)
    assert len(findings) == 1
    assert findings[0].kind == "NEWS_LEAD"
    assert findings[0].confidence == "medium"
    assert any("05 Nov 2026" in line for line in findings[0].lines)


def test_news_release_date_only_stays_low():
    items = [news_item("Dune : Troisième partie en IMAX 70 mm au Pathé Odysseum le 16 décembre 2026")]
    findings = detect.analyze_news(items, Cfg, fresh_state(), NOW)
    assert len(findings) == 1
    assert findings[0].confidence == "low"


def test_news_format_only_trailer_spam_is_dropped():
    # Real-world false positive: SEO spam trailer pages match the film and
    # format keywords but say nothing about sales and mention no venue.
    items = [news_item("DUNE: PART THREE | Official IMAX 70MM Trailer (2026) 4K David Attenborough Wife")]
    assert detect.analyze_news(items, Cfg, fresh_state(), NOW) == []


def test_news_format_with_venue_is_kept():
    items = [news_item("Le Pathé Odysseum projettera Dune 3 en IMAX 70mm")]
    findings = detect.analyze_news(items, Cfg, fresh_state(), NOW)
    assert len(findings) == 1
    assert findings[0].confidence == "low"


def test_news_min_confidence_medium():
    class MediumCfg(Cfg):
        news_min_confidence = "medium"

    low_item = news_item("Le Pathé Odysseum projettera Dune 3 en IMAX 70mm", url="https://example.com/lo")
    medium_item = news_item(
        "Dune 3 : réservations ouvertes le 5 novembre", url="https://example.com/med"
    )
    findings = detect.analyze_news([low_item, medium_item], MediumCfg, fresh_state(), NOW)
    assert [f.confidence for f in findings] == ["medium"]


def test_news_requires_sale_phrase():
    items = [news_item("Dune : Troisième partie — nouvelle bande-annonce épique")]
    assert detect.analyze_news(items, Cfg, fresh_state(), NOW) == []


def test_news_requires_film_match():
    items = [news_item("Dune : Deuxième partie — réservations ouvertes pour la ressortie")]
    assert detect.analyze_news(items, Cfg, fresh_state(), NOW) == []


def test_news_old_items_skipped():
    items = [news_item("Dune 3 : réservations ouvertes !", days_old=30)]
    assert detect.analyze_news(items, Cfg, fresh_state(), NOW) == []


def test_news_url_dedup_and_cap():
    st = fresh_state()
    items = [
        news_item("Dune 3 : réservations ouvertes !", url=f"https://example.com/{i}")
        for i in range(5)
    ]
    findings = detect.analyze_news(items, Cfg, st, NOW)
    assert len(findings) == Cfg.news_max_alerts_per_run

    st["alerts"][findings[0].key] = NOW.isoformat()
    again = detect.analyze_news(items[:1], Cfg, st, NOW)
    assert again == []
