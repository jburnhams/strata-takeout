"""Instagram loader: a synthetic export tree (no real data) into the `instagram` schema."""

import json

import pytest
from omarchy_db.testing import test_migrations, test_schema_matches_models  # noqa: F401
from sqlalchemy import func, select

from strata_takeout.facebook import ingest
from strata_takeout.instagram import cli, models, parse
from strata_takeout.instagram.models import Attachment, Follow, Message, Reaction

pytestmark = pytest.mark.db_app("instagram")


def moji(s: str) -> str:
    return s.encode("utf-8").decode("latin-1")


def put(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=True))


def sync(session, tree, **kw):
    return ingest.sync_tree(session, "main", str(tree), m=models, reader=parse, **kw)


@pytest.fixture
def tree(tmp_path):
    t = tmp_path / "export"
    a = t / "your_instagram_activity"
    put(
        a / "messages" / "inbox" / "pal_1" / "message_1.json",
        {
            "participants": [{"name": "alex"}, {"name": "sam"}],
            "title": "sam",
            "messages": [
                {
                    "sender_name": "sam",
                    "timestamp_ms": 1_600_000_001_000,
                    "content": moji("café ☕"),
                    "share": {"link": "https://example.org/p"},
                },
                {
                    "sender_name": "alex",
                    "timestamp_ms": 1_600_000_002_000,
                    "content": "hi",
                    "reactions": [{"reaction": "❤", "actor": "sam"}],
                },
            ],
        },
    )
    (a / "media" / "posts").mkdir(parents=True)
    (a / "media" / "posts" / "p1.jpg").write_bytes(b"jpg")
    put(
        a / "media" / "posts_1.json",
        [
            {
                "media": [
                    {
                        "uri": "your_instagram_activity/media/posts/p1.jpg",
                        "creation_timestamp": 1_500_000_000,
                        "title": moji("Pretty café"),
                    }
                ],
                "creation_timestamp": 1_500_000_000,
            },
            {
                "media": [{"uri": "x/v.mp4", "creation_timestamp": 1_500_000_100}],
                "title": "clip",
                "creation_timestamp": 1_500_000_100,
            },
        ],
    )
    put(
        a / "media" / "archived_posts.json",
        {
            "ig_archived_post_media": [
                {
                    "media": [{"uri": "x/old.jpg", "creation_timestamp": 1_000_000_000, "title": "old"}],
                    "creation_timestamp": 1_000_000_000,
                }
            ]
        },
    )
    put(
        a / "comments" / "post_comments_1.json",
        [
            {
                "media_list_data": [],
                "string_map_data": {
                    "Comment": {"value": "nice"},
                    "Media Owner": {"value": "sam"},
                    "Time": {"timestamp": 1_400_000_000},
                },
            }
        ],
    )
    put(
        a / "likes" / "liked_posts.json",
        [
            {
                "timestamp": 1_300_000_000,
                "media": [],
                "label_values": [
                    {"label": "URL", "value": "https://example.org/q"},
                    {"label": "Title", "value": "sam"},
                ],
            }
        ],
    )
    f = t / "connections" / "followers_and_following"
    put(
        f / "followers_1.json",
        [
            {
                "title": "",
                "string_list_data": [{"href": "https://example.org/u/a", "value": "a", "timestamp": 1_200_000_000}],
            }
        ],
    )
    put(
        f / "following.json",
        {
            "relationships_following": [
                {"title": "b", "string_list_data": [{"href": "https://example.org/u/b", "timestamp": 1_200_000_001}]}
            ]
        },
    )
    return t


def test_direct_messages_use_the_shared_reader(tree):
    ((t, names, msgs),) = list(parse.threads(str(tree), parse.Ids()))
    assert (t["thread_id"], t["folder"], names) == ("pal_1", "inbox", ["alex", "sam"])
    assert msgs[0]["body"] == "café ☕" and msgs[0]["attachments"][0]["url"] == "https://example.org/p"
    assert msgs[1]["reactions"][0]["reaction"] == "❤"


def test_posts_comments_likes_follows(tree):
    rows = list(parse.activity(str(tree), parse.Ids()))
    posts = {r["body"]: r for r in rows if r["kind"] == "post"}
    assert posts["Pretty café"]["audience"] == "timeline" and posts["Pretty café"]["attachments"][0]["kind"] == "photo"
    assert posts["clip"]["attachments"][0]["kind"] == "video"
    assert posts["old"]["audience"] == "archive"
    (c,) = [r for r in rows if r["kind"] == "comment"]
    assert (c["audience"], c["audience_name"], c["body"]) == ("post", "sam", "nice")
    (lk,) = list(parse.likes(str(tree)))
    assert lk["target_title"] == "sam https://example.org/q"
    assert {(f["direction"], f["name"]) for f in parse.follows(str(tree))} == {("follower", "a"), ("following", "b")}


def test_load_is_idempotent_and_skips_unchanged(db_session, tree):
    n = sync(db_session, tree, label="Main")
    assert n == 2 + 3 + 1 + 1 + 2  # messages, posts, comment, like, follows
    db_session.flush()
    assert sync(db_session, tree) is None
    assert sync(db_session, tree, force=True) == n
    db_session.flush()
    count = lambda t: db_session.scalar(select(func.count()).select_from(t))
    assert (count(Message), count(Reaction), count(Follow)) == (6, 2, 2)
    photo = db_session.scalar(select(Attachment).where(Attachment.kind == "photo", Attachment.size.is_not(None)))
    assert photo.size == 3 and photo.mime == "image/jpeg" and len(photo.sha256) == 64


def test_settings_parsing():
    assert cli.accounts("main:Label=/a/b") == [("main", "Label", "/a/b")]
    with pytest.raises(SystemExit):
        cli.accounts("")


def test_older_wrapped_likes_are_read(tmp_path):
    put(
        tmp_path / "your_instagram_activity" / "likes" / "liked_posts.json",
        {
            "likes_media_likes": [
                {"title": "sam", "string_list_data": [{"href": "https://example.org/q", "timestamp": 1_300_000_000}]}
            ]
        },
    )
    put(
        tmp_path / "your_instagram_activity" / "likes" / "liked_comments.json",
        {
            "likes_comment_likes": [
                {"title": "pal", "string_list_data": [{"href": "https://example.org/c", "timestamp": 1_300_000_001}]}
            ]
        },
    )
    got = list(parse.likes(str(tmp_path)))
    assert [(r["target_title"], r["at"].year) for r in got] == [
        ("sam https://example.org/q", 2011),
        ("pal https://example.org/c", 2011),
    ]
