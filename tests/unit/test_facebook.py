"""Facebook loader: a synthetic export tree (no real data) into the `facebook` schema."""

import hashlib
import json

import pytest
from omarchy_db.testing import test_migrations, test_schema_matches_models  # noqa: F401
from sqlalchemy import func, select

from strata_takeout.facebook import cli, ingest, parse
from strata_takeout.facebook.models import Attachment, Message, Reaction, Source, Thread

pytestmark = pytest.mark.db_app("facebook")


def moji(s: str) -> str:
    """What Facebook writes: UTF-8 bytes read as latin-1."""
    return s.encode("utf-8").decode("latin-1")


def put(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=True))


@pytest.fixture
def tree(tmp_path):
    t = tmp_path / "export"
    a = t / "your_facebook_activity"
    thread = a / "messages" / "inbox" / "friends_123"
    (thread / "photos").mkdir(parents=True)
    (thread / "photos" / "1.jpg").write_bytes(b"jpegbytes")
    put(
        thread / "message_1.json",
        {
            "participants": [{"name": "Alex Example"}, {"name": "Sam Sample"}],
            "title": moji("Café club"),
            "is_still_participant": True,
            "messages": [
                {
                    "sender_name": "Sam Sample",
                    "timestamp_ms": 1_600_000_001_000,
                    "content": moji("Hello café ☕"),
                    "reactions": [{"reaction": moji("👍"), "actor": "Alex Example"}],
                    "photos": [
                        {
                            "uri": "your_facebook_activity/messages/inbox/friends_123/photos/1.jpg",
                            "creation_timestamp": 1_600_000_000,
                        }
                    ],
                },
                {
                    "sender_name": "Alex Example",
                    "timestamp_ms": 1_600_000_000_000,
                    "content": "same",
                    "share": {"link": "https://example.org/x"},
                },
                {
                    "sender_name": "Alex Example",
                    "timestamp_ms": 1_600_000_000_000,
                    "content": "same",
                },  # an exact repeat
                {"sender_name": "Sam Sample", "timestamp_ms": 1_600_000_002_000, "call_duration": 61},
            ],
        },
    )
    put(
        a / "posts" / "your_posts__check_ins__photos_and_videos_1.json",
        [
            {
                "timestamp": 1_500_000_000,
                "title": "Alex Example updated his status.",
                "data": [{"post": moji("Naïve post")}],
            },
            {
                "timestamp": 1_500_000_100,
                "attachments": [{"data": [{"external_context": {"url": "https://example.org/y"}}]}],
            },
        ],
    )
    put(
        a / "posts" / "posts_on_other_pages_and_profiles.json",
        [
            {
                "timestamp": 1_400_000_000,
                "label_values": [
                    {"label": "Message", "value": "Happy birthday"},
                    {"label": "Target", "value": "Sam Sample"},
                ],
            }
        ],
    )
    put(
        a / "groups" / "group_posts_and_comments.json",
        {
            "group_posts_v2": [
                {
                    "timestamp": 1_300_000_000,
                    "title": "Alex Example posted in Walking Club.",
                    "data": [{"post": "route?"}],
                }
            ]
        },
    )
    put(
        a / "groups" / "your_comments_in_groups.json",
        {
            "group_comments_v2": [
                {
                    "timestamp": 1_300_000_050,
                    "data": [
                        {
                            "comment": {
                                "timestamp": 1_300_000_050,
                                "comment": "nice",
                                "author": "Alex Example",
                                "group": "Walking Club",
                            }
                        }
                    ],
                }
            ]
        },
    )
    put(
        a / "comments_and_reactions" / "comments.json",
        {
            "comments_v2": [
                {
                    "timestamp": 1_200_000_000,
                    "title": "Alex Example commented on Sam Sample's post.",
                    "data": [{"comment": {"timestamp": 1_200_000_000, "comment": "ha", "author": "Alex Example"}}],
                },
                {
                    "timestamp": 1_200_000_100,
                    "title": "Alex Example replied to his own comment.",
                    "data": [{"comment": {"timestamp": 1_200_000_100, "comment": "again", "author": "Alex Example"}}],
                },
            ]
        },
    )
    put(
        a / "comments_and_reactions" / "likes_and_reactions_1.json",
        [
            {
                "timestamp": 1_100_000_000,
                "title": "Alex Example liked Sam Sample's photo.",
                "data": [{"reaction": {"reaction": "LIKE", "actor": "Alex Example"}}],
            }
        ],
    )
    return t


def test_fix_undoes_latin1_and_leaves_other_text():
    assert parse.fix(moji("café ☕ 👍")) == "café ☕ 👍"
    assert parse.fix("café") == "café"  # already right: not UTF-8 bytes, left alone
    assert parse.fix("plain") == "plain"


def test_chat_messages(tree):
    (t, names, msgs), *rest = list(parse.threads(str(tree), parse.Ids()))
    assert not rest
    assert (t["thread_id"], t["folder"], t["title"], t["participants"]) == ("friends_123", "inbox", "Café club", 2)
    assert names == ["Alex Example", "Sam Sample"]
    first = msgs[0]
    assert (first["kind"], first["audience"], first["body"]) == ("chat", "thread", "Hello café ☕")
    assert first["reactions"] == [{"reaction": "👍", "actor": "Alex Example", "at": None}]
    assert first["attachments"][0]["kind"] == "photo" and msgs[1]["attachments"][0]["url"] == "https://example.org/x"
    assert msgs[3]["call_seconds"] == 61
    assert len({m["ext_id"] for m in msgs}) == 4  # an exact repeat gets its own id


def test_ids_are_stable_across_runs(tree):
    ids = lambda: [m["ext_id"] for _, _, ms in parse.threads(str(tree), parse.Ids()) for m in ms]
    assert ids() == ids()


def test_activity_audiences(tree):
    rows = list(parse.activity(str(tree), parse.Ids()))
    by = {(r["kind"], r["audience"]): r for r in rows if r["body"] != "ha" and r["body"] != "again"}
    assert {r["body"] for r in rows if r["audience"] == "timeline"} == {"Naïve post", None}  # the second is a link only
    assert (
        by[("post", "profile")]["audience_name"] == "Sam Sample" and by[("post", "profile")]["body"] == "Happy birthday"
    )
    assert by[("post", "group")]["audience_name"] == "Walking Club"
    assert by[("comment", "group")]["audience_name"] == "Walking Club"
    comments = {r["body"]: r for r in rows if r["kind"] == "comment" and r["audience"] == "post"}
    assert comments["ha"]["audience_name"] == "Sam Sample"
    assert comments["again"]["audience_name"] == "self"


def test_load_is_idempotent_and_skips_unchanged(db_session, tree):
    n = ingest.sync_tree(db_session, "main", str(tree), "Main")
    assert n == 12  # 4 chat + 2 own posts + 1 other profile + 1 group post + 1 group comment + 2 comments + 1 like
    db_session.flush()
    assert ingest.sync_tree(db_session, "main", str(tree)) is None  # unchanged
    assert ingest.sync_tree(db_session, "main", str(tree), force=True) == n
    db_session.flush()
    count = lambda t: db_session.scalar(select(func.count()).select_from(t))
    assert (count(Message), count(Thread), count(Reaction)) == (11, 1, 2)  # replaced, never doubled
    assert db_session.get(Source, ("main", ingest.SOURCE)).rows == n
    photo = db_session.scalar(select(Attachment).where(Attachment.kind == "photo"))
    assert (photo.size, photo.sha256, photo.mime) == (9, hashlib.sha256(b"jpegbytes").hexdigest(), "image/jpeg")
    assert (
        db_session.scalar(select(Attachment).where(Attachment.kind == "link", Attachment.path.is_(None)).limit(1))
        is not None
    )
    chat_react = db_session.scalar(select(Reaction).where(Reaction.message_id.is_not(None)))
    assert (chat_react.reaction, chat_react.actor) == ("👍", "Alex Example")
    kinds = {k: n for k, n in db_session.execute(select(Message.kind, func.count()).group_by(Message.kind))}
    assert kinds == {"chat": 4, "post": 4, "comment": 3}


def test_changed_tree_reloads(db_session, tree):
    ingest.sync_tree(db_session, "main", str(tree))
    db_session.flush()
    p = tree / "your_facebook_activity" / "comments_and_reactions" / "comments.json"
    d = json.loads(p.read_text())
    d["comments_v2"].append(
        {"timestamp": 1_200_000_200, "title": "x", "data": [{"comment": {"comment": "more", "author": "Alex Example"}}]}
    )
    p.write_text(json.dumps(d))
    assert ingest.sync_tree(db_session, "main", str(tree)) == 13


def test_attachment_paths_never_leave_the_tree(tmp_path):
    outside = tmp_path / "secret.txt"
    outside.write_text("x")
    t = tmp_path / "t"
    t.mkdir()
    assert ingest.Files(str(t)).info("../secret.txt")[0] is None


def test_no_export_under_tree(db_session, tmp_path):
    assert ingest.sync_tree(db_session, "main", str(tmp_path)) == 0


def test_settings_parsing():
    assert cli.accounts("main:Label=/a/b,other=/c") == [("main", "Label", "/a/b"), ("other", None, "/c")]
    with pytest.raises(SystemExit):
        cli.accounts("")


def test_unreadable_file_aborts_instead_of_dropping_rows(db_session, tree):
    ingest.sync_tree(db_session, "main", str(tree))
    db_session.flush()
    (tree / "your_facebook_activity" / "comments_and_reactions" / "comments.json").write_text("{not json")
    with (
        pytest.raises(ValueError, match="comments.json"),
        db_session.begin_nested(),
    ):  # the CLI's transaction rolls back the same way
        ingest.sync_tree(db_session, "main", str(tree))
    assert (
        db_session.scalar(select(func.count()).select_from(Message).where(Message.kind == "comment")) == 3
    )  # the earlier rows are still there


def test_threads_are_yielded_one_at_a_time_and_same_id_in_two_folders_is_kept(tree):
    other = tree / "your_facebook_activity" / "messages" / "archived_threads" / "friends_123"
    other.mkdir(parents=True)
    (other / "message_1.json").write_text(
        json.dumps(
            {"participants": [], "title": "Old", "messages": [{"sender_name": "A", "timestamp_ms": 5, "content": "x"}]}
        )
    )
    gen = parse.threads(str(tree), parse.Ids())
    first = next(gen)
    assert first[0]["folder"] == "archived_threads"  # a generator: the next conversation is not parsed yet
    second = next(gen)
    assert second[0]["thread_id"] == "inbox~friends_123" and second[0]["folder"] == "inbox"
