from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

from strata_takeout.youtube.ingest import sync_tree
from strata_takeout.youtube.parse import parse_file, parse_time

CELL = '<div class="outer-cell mdl-cell"><div class="mdl-grid"><div class="header-cell"><p>YouTube<br></p></div><div class="content-cell mdl-cell mdl-typography--body-1">{body}</div><div class="content-cell mdl-cell mdl-typography--text-right"></div><div class="content-cell mdl-cell mdl-typography--caption"><b>Products:</b><br>&emsp;YouTube<br>{cap}<b>Why is this here?</b><br>&emsp;x</div></div></div>'
WATCH = 'Watched <a href="https://www.youtube.com/watch?v=abcdefghijk">A &amp; B</a><br><a href="https://www.youtube.com/channel/UC123">Chan &#39;x&#39;</a><br>9 Oct 2026, 21:57:37 BST<br>'
AD = 'Watched <a href="https://www.youtube.com/watch?v=zzzzzzzzzzz">Ad</a><br>Watched at 20:09<br>22 Sept 2026, 21:09:40 BST<br>'
SEARCH = 'Searched for&nbsp;<a href="https://www.youtube.com/results?search_query=x+y">x y</a><br>1 Jan 2020, 10:00:00 GMT<br>'
SURVEY = 'Answered survey question<br>Answer: Neutral<br>1 Jan 2020, 10:00:00 GMT<br>'


def page(*cells): return '<html><body><div class="mdl-grid">' + ''.join(cells) + '</div></body></html>'


def test_time_zones():
    assert parse_time('9 Oct 2026, 21:57:37 BST') == datetime(2026, 10, 9, 20, 57, 37, tzinfo=timezone.utc)
    assert parse_time('22 Sept 2026, 21:09:40 GMT') == datetime(2026, 9, 22, 21, 9, 40, tzinfo=timezone.utc)


def test_cells(tmp_path):
    f = tmp_path / 'h.html'; f.write_text(page(CELL.format(body=WATCH, cap=''), CELL.format(body=AD, cap='<b>Details:</b><br>&emsp;From Google Ads<br>'), CELL.format(body=SEARCH, cap=''), CELL.format(body=SURVEY, cap='')))
    w, ad, s, sv = parse_file(f)
    assert (w['action'], w['title'], w['video_id'], w['channel_id'], w['channel_name'], w['is_ad']) == ('Watched', 'A & B', 'abcdefghijk', 'UC123', "Chan 'x'", False)
    assert ad['is_ad'] and ad['details'] == 'Watched at 20:09; From Google Ads' and ad['channel_id'] is None
    assert (s['action'], s['title'], s['video_id']) == ('Searched for', 'x y', None)
    assert (sv['action'], sv['url'], sv['details']) == ('Answered survey question', None, 'Answer: Neutral')


def tree(tmp_path, acct, body):
    d = tmp_path / acct / 'YouTube and YouTube Music' / 'history'; d.mkdir(parents=True); f = d / 'watch-history.html'; f.write_text(page(CELL.format(body=body, cap=''))); return str(tmp_path / acct), f


def test_sync_is_idempotent_skips_unchanged_and_keeps_accounts(tmp_path, db_session):
    t1, f1 = tree(tmp_path, 'main', WATCH); t2, _ = tree(tmp_path, 'brand', AD)
    assert sync_tree(db_session, 'main', t1) == {'watch-history': 1} and sync_tree(db_session, 'brand', t2) == {'watch-history': 1}
    assert sync_tree(db_session, 'main', t1) == {'watch-history': None}                       # unchanged: skipped
    assert sync_tree(db_session, 'main', t1, force=True) == {'watch-history': 1}              # forced: replaced, not duplicated
    f1.write_text(page(CELL.format(body=WATCH, cap=''), CELL.format(body=WATCH, cap='')))     # file changed (a newer extract)
    assert sync_tree(db_session, 'main', t1) == {'watch-history': 2}
    db_session.commit()
    assert db_session.execute(text('select account, count(*) from activity group by account order by 1')).all() == [('brand', 1), ('main', 2)]
    assert db_session.execute(text('select count(*) from watches')).scalar() == 3
    assert sync_tree(db_session, 'gone', str(tmp_path / 'nope')) == {}


def test_accounts_setting():
    from strata_takeout.youtube.cli import accounts
    assert accounts('main:Main=/a,b=/c d') == [('main', 'Main', '/a'), ('b', None, '/c d')]
