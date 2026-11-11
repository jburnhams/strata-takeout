import os

from sqlalchemy import text

from strata_takeout.youtube.datasets import duration, timestamp
from strata_takeout.youtube.ingest import sync_tree

R = 'YouTube and YouTube Music'


def put(tree, rel, body):
    p = os.path.join(tree, R, rel); os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, 'w', encoding='utf-8') as f: f.write(body)


def make(tmp_path):
    t = str(tmp_path)
    put(t, 'subscriptions/subscriptions.csv', 'Channel ID,Channel URL,Channel title\nUC1,http://x/UC1,One\nUC2,http://x/UC2,Two\n')
    put(t, 'playlists/playlists.csv', "Playlist ID,Add new videos to top,Playlist title (original),Playlist title (original) language,Playlist create timestamp,Playlist update timestamp,Playlist video order,Playlist visibility\nPL1,False,We've Got A Mix,,2013-04-24T14:30:45+00:00,2014-05-29T12:44:38+00:00,Manual,Private\n")
    put(t, 'playlists/We_ve Got A Mix videos.csv', 'Video ID,Playlist video creation timestamp\nv1,2012-10-31T14:25:57+00:00\nv2,2012-10-31T14:25:58+00:00\n')
    put(t, 'Video metadata/videos.csv', 'Video ID,Approx duration (ms),Video category,Channel ID,Video title (original),Privacy,Video state,Video create timestamp,Tag 1,Tag 2\nv1,49000,People,UC1,Old title,Private,Processed,2014-02-11T19:03:37+00:00,a,b\n')
    put(t, 'Video metadata/videos(1).csv', 'Video ID,Video title (original)\nv1,New title\n')                  # a later version wins
    put(t, 'Video metadata/video recordings.csv', 'Video ID,Video recording altitude,Video recording latitude,Video recording longitude\nv1,0,0,0\n')
    put(t, 'comments/comments.csv', 'Comment ID,Channel ID,Comment create timestamp,Price,Parent comment ID,Video ID,Comment text,Top-level comment ID\nc1,UC1,2026-03-31T17:05:43.493202123+00:00,0,,vx,"{""text"":""@A""},{""text"":"" hi""}",c1\n')
    put(t, 'music (library and uploads)/music uploads metadata.csv', 'Song Title,Album Title,Artist Name 1,Artist Name 2,Duration\nS,Al,Ar,,3m26.5s\n')
    return t


def test_datasets_load_idempotently(tmp_path, db_session):
    t = make(tmp_path)
    first = sync_tree(db_session, 'main', t); db_session.commit()
    assert first['subscriptions'] == 2 and first['playlists'] == 3 and first['videos'] == 1 and first['comments'] == 1 and first['music'] == 1 and 'live-chats' not in first
    q = lambda sql: db_session.execute(text(sql)).all()
    assert q("select title, tags, latitude from videos") == [('New title', '["a", "b"]', None)] or q("select title, latitude from videos") == [('New title', None)]
    assert q("select playlist_id, video_id from playlist_items order by position") == [('PL1', 'v1'), ('PL1', 'v2')]
    assert q("select text from comments") == [('@A hi',)] and q("select duration_s from music_tracks") == [(206.5,)]
    again = sync_tree(db_session, 'main', t); assert again['subscriptions'] is None and again['videos'] is None       # unchanged
    sync_tree(db_session, 'main', t, force=True); db_session.commit()
    assert q("select count(*) from subscriptions") == [(2,)] and q("select count(*) from playlist_items") == [(2,)]
    put(t, 'subscriptions/subscriptions.csv', 'Channel ID,Channel URL,Channel title\nUC1,http://x/UC1,One\n')       # a newer export dropped a subscription
    sync_tree(db_session, 'main', t); db_session.commit()
    assert q("select count(*) from subscriptions") == [(1,)]
    os.remove(os.path.join(t, R, 'subscriptions/subscriptions.csv')); sync_tree(db_session, 'main', t)              # a tree lacking the file never wipes earlier rows
    assert q("select count(*) from subscriptions") == [(1,)]


def test_helpers():
    assert timestamp('2026-03-31T17:05:43.493202123+00:00').microsecond == 493202 and timestamp('') is None
    assert duration('1h2m3s') == 3723 and duration('3m26.471s') == 206.471 and duration('') is None
