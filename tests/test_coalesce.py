"""Same-run alert merging — the fix for duplicate Telegram messages.

Two real bursts motivate this module, both reproduced below: four Pathé
messages on 2026-09-03 21:46 (two of them announcing the same 09:00 opening)
and two identical Cinesa messages on 2026-08-06 15:08 (one per watched date).
"""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar

from watcher import coalesce, detect, notify
from watcher.detect import TZ_PARIS, CinesaSnapshot, Finding, Snapshot

NOW = datetime(2026, 9, 3, 21, 46, tzinfo=TZ_PARIS)
SALE = "2026-09-09T09:00:00+02:00"
IMAX = "0000000086"


class Cfg:
    primary_slug = "dune-troisieme-partie-50828"
    film_title = "Dune : Troisième partie"
    cinema_name = "Pathé Odysseum"
    cinema_city = "Montpellier"
    reminder_offsets_minutes: ClassVar = [1440, 120, 15]
    silent_kinds: ClassVar = list(notify.DEFAULT_SILENT_KINDS)
    cinesa_film_title = "La odisea"
    cinesa_page_url = "https://www.cinesa.es/peliculas/la-odisea/HO00003228/"
    cinesa_site_id = "032"
    cinesa_site_name = "Cinesa Diagonal Mar"
    cinesa_site_city = "Barcelona"
    cinesa_film_id = "HO00003228"
    cinesa_imax_attribute_id = IMAX
    cinesa_target_dates: ClassVar = ["2026-08-26", "2026-08-27"]


def show(slug, title, sale=None, movie=False) -> dict:
    out = {"slug": slug, "title": title, "isMovie": movie,
           "releaseAt": {"FR_FR": "2026-12-16"}}
    if sale:
        out["salesOpeningDatetime"] = sale
    return out


def finding(kind, key, *, title="t", lines=None, url="u", item=None, sale=None) -> Finding:
    return Finding(
        kind=kind, key=key, confidence="high", title=title,
        lines=lines if lines is not None else ["id"], url=url,
        sale_datetime=sale, merge_item=item,
    )


def texts(alerts) -> list[str]:
    return [notify.render_finding(a.finding) for a in alerts]


# --------------------------------------------------------------- the reported bursts

def pathe_burst() -> list[Finding]:
    """The 2026-09-03 21:46 run: the primary film plus two dedicated listings."""
    snap = Snapshot(
        matched_shows=[
            show("dune-troisieme-partie-50828", "Dune : Troisième partie", SALE, movie=True),
            show(
                "dune-troisieme-partie-projection-imax-70mm-55289",
                "Dune - Troisième partie : Projection IMAX 70mm",
                SALE,
            ),
            show(
                "la-seance-70mm-dune-troisieme-partie-55319",
                "La Séance 70mm : Dune - Troisième partie",
            ),
        ]
    )
    state = {"shows_seen": ["dune-troisieme-partie-50828"], "sales": {},
             "formats_seen": {}, "tickets_available": False}
    return detect.analyze_pathe(snap, state, Cfg, NOW)


def test_the_two_same_minute_sale_alerts_become_one_message():
    findings = pathe_burst()
    assert len(findings) == 4  # what the user actually received

    alerts = coalesce.merge(findings, Cfg)

    assert len(alerts) == 2
    sale = next(a for a in alerts if a.finding.kind == "SALE_DATE")
    assert sale.finding.title == "Sale opens Wed 9 Sep, 09:00"
    # Both formats named once, best first — not one message per listing.
    assert sale.finding.lines[0] == (
        "Dune : Troisième partie · IMAX 70 mm (1.43:1), Standard / other · Pathé Odysseum"
    )
    assert sale.keys == [
        "sale:dune-troisieme-partie-50828:2026-09-09T09:00:00+02:00",
        "sale:dune-troisieme-partie-projection-imax-70mm-55289:2026-09-09T09:00:00+02:00",
    ]
    # The link goes to the IMAX 70 mm listing — the one the user is here for.
    assert sale.finding.url.endswith("projection-imax-70mm-55289")


def test_merged_sale_says_the_shared_body_once():
    sale = next(a for a in coalesce.merge(pathe_burst(), Cfg)
                if a.finding.kind == "SALE_DATE")

    assert sale.finding.lines[1:] == [
        "Reminders set: 24 h, 2 h and 15 min before.",
        "Opening time is national — seats can go in minutes.",
    ]


def test_the_two_new_listings_become_one_message_naming_both():
    new = next(a for a in coalesce.merge(pathe_burst(), Cfg)
               if a.finding.kind == "NEW_LISTING")

    assert new.finding.title == "New listings: IMAX 70 mm (1.43:1)"
    assert new.finding.lines == [
        "Dune : Troisième partie · Pathé Odysseum, Montpellier",
        "“Dune - Troisième partie : Projection IMAX 70mm”",
        "Release 2026-12-16 · sale opens Wed 9 Sep, 09:00.",
        "“La Séance 70mm : Dune - Troisième partie”",
        "Release 2026-12-16 · no sale date published yet.",
        # Said once, after both listings, not repeated per listing.
        "Dedicated listings get their own opening — now on the watch list.",
    ]
    assert len(new.keys) == 2


def test_the_two_cinesa_date_alerts_become_one_message():
    """The 2026-08-06 15:08 burst: two dates opened, two identical messages."""
    snap = CinesaSnapshot(days=[
        {"date": "2026-08-26", "attributes": [IMAX]},
        {"date": "2026-08-27", "attributes": [IMAX]},
    ])
    findings = detect.analyze_cinesa(snap, {"cinesa": {"imax_present": True}}, Cfg, NOW)
    assert len(findings) == 2

    alerts = coalesce.merge(findings, Cfg)

    assert len(alerts) == 1
    assert alerts[0].finding.title == "Your dates are open — 26 Aug, 27 Aug, IMAX"
    assert alerts[0].finding.lines == [
        "La odisea · Cinesa Diagonal Mar, Barcelona",
        "Bookable with IMAX: 2026-08-26, 2026-08-27",
    ]
    assert alerts[0].keys == [
        "cinesa_target:032:HO00003228:2026-08-26",
        "cinesa_target:032:HO00003228:2026-08-27",
    ]


# --------------------------------------------------------------- what must NOT merge

def test_a_lone_finding_is_passed_through_untouched():
    """The common case must render exactly as it did before merging existed."""
    f = finding("SALE_DATE", "sale:x:y", title="Sale opens Wed 9 Sep, 09:00",
                lines=["a", "b"], item=detect.FMT_IMAX70, sale=SALE)

    alerts = coalesce.merge([f], Cfg)

    assert len(alerts) == 1
    assert alerts[0].finding is f
    assert alerts[0].keys == ["sale:x:y"]
    assert alerts[0].merged is False


def test_different_opening_times_stay_different_messages():
    other = "2026-09-10T09:00:00+02:00"
    findings = [
        finding("SALE_DATE", "sale:a", item=detect.FMT_IMAX70, sale=SALE),
        finding("SALE_DATE", "sale:b", item=detect.FMT_OTHER, sale=other),
    ]

    assert len(coalesce.merge(findings, Cfg)) == 2


def test_unrelated_kinds_never_share_a_message():
    findings = [
        finding("WATCHER_ERROR", "err:1"),
        finding("RECOVERED", "rec:1"),
        finding("NEWS_LEAD", "news:1"),
        finding("CINEMA_LISTED", "cinema_listed:x"),
        finding("HEARTBEAT", "hb:1"),
    ]

    alerts = coalesce.merge(findings, Cfg)

    assert [a.finding.key for a in alerts] == [f.key for f in findings]
    assert all(not a.merged for a in alerts)


def test_a_finding_with_nothing_to_name_it_stands_alone():
    """merge_item is what the merged title lists; without it an item would
    vanish from the message while its key was still marked sent."""
    findings = [
        finding("NEW_LISTING", "new_show:a", item=None),
        finding("NEW_LISTING", "new_show:b", item=None),
    ]

    assert len(coalesce.merge(findings, Cfg)) == 2


def test_merging_preserves_the_order_findings_arrived_in():
    findings = [
        finding("CINEMA_LISTED", "cinema_listed:x"),
        finding("NEW_LISTING", "new_show:a", item=detect.FMT_OTHER),
        finding("WATCHER_ERROR", "err:1"),
        finding("NEW_LISTING", "new_show:b", item=detect.FMT_IMAX),
    ]

    alerts = coalesce.merge(findings, Cfg)

    assert [a.kinds for a in alerts] == [
        ["CINEMA_LISTED"], ["NEW_LISTING", "NEW_LISTING"], ["WATCHER_ERROR"]
    ]


# --------------------------------------------------------------- merged message shape

def test_a_moved_opening_wins_the_title_and_the_icon():
    """One listing moving is the louder news; the icon must agree with it."""
    findings = [
        finding("SALE_DATE", "sale:a", item=detect.FMT_OTHER, sale=SALE, lines=["id"]),
        finding("SALE_DATE_CHANGED", "sale:b", item=detect.FMT_IMAX70, sale=SALE,
                lines=["id", "Was: Wed 2 Sep, 09:00 — moved 7 days later."]),
    ]

    alert = coalesce.merge(findings, Cfg)[0]

    assert alert.finding.kind == "SALE_DATE_CHANGED"
    assert alert.finding.title == "Sale time CHANGED → Wed 9 Sep, 09:00"
    # The line only one member carried is kept, not silently dropped.
    assert "Was: Wed 2 Sep, 09:00 — moved 7 days later." in alert.finding.lines


def test_a_line_only_some_members_carry_survives_the_merge():
    findings = [
        finding("NEW_LISTING", "new_show:a", item=detect.FMT_OTHER,
                lines=["id", "only on a", "shared"]),
        finding("NEW_LISTING", "new_show:b", item=detect.FMT_IMAX70,
                lines=["id", "only on b", "shared"]),
    ]

    lines = coalesce.merge(findings, Cfg)[0].finding.lines

    assert lines == ["id", "only on a", "only on b", "shared"]


def test_many_cinesa_dates_are_counted_rather_than_listed_in_the_title():
    findings = [
        finding("CINESA_TARGET_DATE", f"cinesa_target:{d}", item=d,
                lines=["id", f"Bookable with IMAX: {d}"])
        for d in ("2026-08-26", "2026-08-27", "2026-08-28", "2026-08-29")
    ]

    alert = coalesce.merge(findings, Cfg)[0]

    assert alert.finding.title == "Your dates are open — 4 dates, IMAX"
    assert alert.finding.lines[1] == (
        "Bookable with IMAX: 2026-08-26, 2026-08-27, 2026-08-28, 2026-08-29"
    )


def test_book_now_names_every_format_that_went_live():
    findings = [
        finding("TICKETS_AVAILABLE", "tickets:a:other", item=detect.FMT_OTHER,
                lines=["id", "👉 https://book/other"]),
        finding("TICKETS_AVAILABLE", "tickets:b:imax70", item=detect.FMT_IMAX70,
                lines=["id", "👉 https://book/imax70"]),
    ]

    alert = coalesce.merge(findings, Cfg)[0]

    assert alert.finding.title == "BOOK NOW — IMAX 70 mm (1.43:1), Standard / other are live"
    # Every booking link survives: a merged message must stay as actionable.
    assert alert.finding.lines[1:] == ["👉 https://book/other", "👉 https://book/imax70"]


def test_one_format_going_live_keeps_the_singular():
    findings = [
        finding("TICKETS_AVAILABLE", "tickets:a:imax70", item=detect.FMT_IMAX70,
                lines=["id", "👉 https://book/a"]),
        finding("TICKETS_AVAILABLE", "tickets:b:imax70", item=detect.FMT_IMAX70,
                lines=["id", "👉 https://book/b"]),
    ]

    assert coalesce.merge(findings, Cfg)[0].finding.title == (
        "BOOK NOW — IMAX 70 mm (1.43:1) is live"
    )


# --------------------------------------------------------------- notification volume

def test_a_loud_member_makes_the_whole_group_buzz():
    """Merging must never silence an alert that would have arrived with sound."""
    findings = [
        finding("CINESA_TARGET_NO_IMAX", "noimax:a", item="2026-08-26"),
        finding("CINESA_TARGET_DATE", "open:b", item="2026-08-27"),
    ]

    # These two never share a group, but the rule the caller applies is what
    # matters: silence needs every member to be silent.
    for alert in coalesce.merge(findings, Cfg):
        silent = all(notify.is_silent(Cfg, k) for k in alert.kinds)
        assert silent == (alert.finding.kind == "CINESA_TARGET_NO_IMAX")

    mixed = coalesce.Alert(findings[0], keys=["a", "b"],
                           kinds=["CINESA_TARGET_NO_IMAX", "CINESA_TARGET_DATE"])
    assert not all(notify.is_silent(Cfg, k) for k in mixed.kinds)


def test_the_silent_cinesa_group_stays_silent_when_merged():
    findings = [
        finding("CINESA_TARGET_NO_IMAX", f"noimax:{d}", item=d,
                lines=["id", f"Bookable, no IMAX session on it: {d}", "trailer"])
        for d in ("2026-08-26", "2026-08-27")
    ]

    alert = coalesce.merge(findings, Cfg)[0]

    assert alert.finding.title == "26 Aug, 27 Aug opened — but no IMAX"
    assert alert.finding.lines == [
        "id", "Bookable, no IMAX session on them: 2026-08-26, 2026-08-27", "trailer",
    ]
    assert all(notify.is_silent(Cfg, k) for k in alert.kinds)


def test_every_merged_message_still_names_its_film_and_cinema():
    """AGENTS.md: a second watch target must never be mistaken for this one."""
    for alerts in (coalesce.merge(pathe_burst(), Cfg),):
        for alert in alerts:
            assert Cfg.film_title in alert.finding.lines[0]
            assert Cfg.cinema_name in alert.finding.lines[0]
    assert "Pathé Odysseum" in texts(coalesce.merge(pathe_burst(), Cfg))[0]
