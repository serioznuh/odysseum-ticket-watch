"""Regressions for the September 9 sale and the wanted December weekend."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from watcher import coalesce, detect, notify, state
from watcher.config import load_config

NOW = datetime(2026, 9, 9, 8, 45, tzinfo=detect.TZ_PARIS)
EVENT = 'dune-troisieme-partie-projection-imax-70mm-55289'


def cfg():
    return load_config('config.toml')


def snapshot(days=None, title='Dune : Projection IMAX 70mm', slug=EVENT):
    return detect.Snapshot(
        matched_shows=[{'slug': slug, 'title': title, 'isMovie': False,
                        'salesOpeningDatetime': (NOW + timedelta(minutes=15)).isoformat()}],
        cinema_entries={slug: {'isBookable': True, 'days': days or {}}},
    )


def targets(snap, st=None):
    return [f for f in detect.analyze_pathe(snap, st or deepcopy(state.DEFAULT_STATE), cfg(), NOW)
            if f.kind == 'PATHE_TARGET_DATE']


def test_standard_tickets_do_not_cancel_imax_reminders_in_existing_state():
    st = deepcopy(state.DEFAULT_STATE)
    st.update(tickets_available=True, sale_target=(NOW + timedelta(minutes=15)).isoformat(),
              formats_seen={cfg().primary_slug: ['other']})
    assert state.due_reminders(st, [1440, 120, 15], NOW, cfg=cfg())[0]['offset'] == 15
    assert detect.reminders_cover(st['sale_target'], snapshot(), st, NOW, cfg())
    st['formats_seen'][EVENT] = ['imax70']
    assert state.due_reminders(st, [1440, 120, 15], NOW, cfg=cfg()) == []


def test_only_imax_sale_controls_reminder_target():
    st = deepcopy(state.DEFAULT_STATE)
    snap = snapshot()
    snap.matched_shows.append({'slug': cfg().primary_slug, 'title': 'Dune',
                              'salesOpeningDatetime': (NOW + timedelta(minutes=5)).isoformat()})
    state.update_from_snapshot(st, snap, cfg(), NOW)
    assert st['sale_target'] == (NOW + timedelta(minutes=15)).isoformat()


def test_old_dec15_availability_does_not_hide_new_wanted_dates():
    st = deepcopy(state.DEFAULT_STATE)
    st['formats_seen'][EVENT] = ['imax70']
    assert targets(snapshot({'2026-12-15': {'bookable': True, 'tags': ['imax']}}), st) == []
    snap = snapshot({d: {'bookable': True, 'tags': ['imax']} for d in cfg().pathe_target_dates})
    findings = targets(snap, st)
    assert len(findings) == 2
    assert all('IMAX 70 mm' in f.title for f in findings)
    merged = coalesce.merge(findings, cfg())
    assert len(merged) == 1
    assert len(merged[0].keys) == 2
    assert '19 Dec' in merged[0].finding.title and '20 Dec' in merged[0].finding.title
    # Failed delivery: baseline can advance but wanted-date keys must remain eligible.
    state.update_from_snapshot(st, snap, cfg(), NOW)
    assert len(targets(snap, st)) == 2
    for f in findings:
        state.mark_sent(st, f.key, NOW)
    assert all(state.already_sent(st, f.key) for f in targets(snap, st))


@pytest.mark.parametrize('day', [{}, {'bookable': False}, {'tags': ['imax']}])
def test_listing_bookable_does_not_prove_date_bookable(day):
    assert targets(snapshot({'2026-12-19': day})) == []


@pytest.mark.parametrize('title,slug', [
    ('Dune', 'dune-troisieme-partie-50828'),
    ('Dune IMAX', 'dune-imax'),
    ('Dune La Séance 70mm', 'la-seance-70mm-dune'),
    ('Dune IMAX 1.43:1', 'dune-imax-laser'),
])
def test_other_formats_never_announce_imax70_dates(title, slug):
    snap = snapshot({'2026-12-19': {'bookable': True}}, title, slug)
    assert targets(snap) == []
    assert detect.analyze_pathe(snap, deepcopy(state.DEFAULT_STATE), cfg(), NOW) == []


def test_sessions_can_prove_target_format_on_regular_listing():
    snap = snapshot(slug=cfg().primary_slug, title='Dune')
    snap.showtimes = {cfg().primary_slug: {'2026-12-19': [
        {'tags': ['imax', '70mm'], 'status': 'available'}]}}
    assert len(targets(snap)) == 1
    snap.showtimes[cfg().primary_slug]['2026-12-19'][0]['status'] = 'soldOut'
    assert targets(snap) == []


def test_pending_dates_keep_cadence_fast_until_delivered_or_past():
    st = deepcopy(state.DEFAULT_STATE)
    st.update(tickets_available=True, sale_target=NOW.isoformat(),
              formats_seen={EVENT: ["imax70"]})
    later = NOW + timedelta(days=1)
    assert state.adaptive_staleness_hours(st, cfg(), later) <= .25
    for f in targets(snapshot({d: {'bookable': True} for d in cfg().pathe_target_dates})):
        state.mark_sent(st, f.key, NOW)
    assert state.adaptive_staleness_hours(st, cfg(), later) == cfg().cadence_after_tickets_hours
    assert state.adaptive_staleness_hours(deepcopy(state.DEFAULT_STATE), cfg(),
                                         NOW.replace(year=2027)) == cfg().cadence_baseline_hours


def test_open_reminder_does_not_claim_confirmed_availability():
    text = notify.render_reminder('open', NOW.isoformat(), cfg(), NOW)
    assert 'SALE IS OPEN' not in text
    assert 'IMAX 70 mm' in text
    assert 'not confirmed' in text
    assert cfg().pathe_page_url in text


def test_config_dates_require_explicit_format(tmp_path):
    p = tmp_path / 'config.toml'
    p.write_text('[film]\nprimary_slug="dune"\ntarget_dates=["2026-12-19"]\n[cinema]\nslug="odysseum"\n')
    with pytest.raises(ValueError, match='target_format'):
        load_config(p)


def test_no_target_dates_retains_normal_cadence():
    config = replace(cfg(), pathe_target_dates=[])
    st = deepcopy(state.DEFAULT_STATE)
    st['tickets_available'] = True
    st['formats_seen'][EVENT] = ['imax70']
    assert state.adaptive_staleness_hours(st, config, NOW) == config.cadence_after_tickets_hours


def test_aggregated_day_tags_cannot_combine_separate_formats():
    snap = snapshot({'2026-12-19': {'bookable': True, 'tags': ['imax', '70mm']}},
                    title='Dune', slug=cfg().primary_slug)
    assert targets(snap) == []


@pytest.mark.parametrize('value', ['2026-02-30', '19-12-2026', 'bad'])
def test_invalid_target_date_is_rejected(tmp_path, value):
    p = tmp_path / 'config.toml'
    p.write_text('[film]\nprimary_slug="dune"\ntarget_format="imax70"\n'
                 f'target_dates=["{value}"]\n[cinema]\nslug="odysseum"\n')
    with pytest.raises(ValueError, match='target_dates'):
        load_config(p)
