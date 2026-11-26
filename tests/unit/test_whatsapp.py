"""WhatsApp loader: a synthetic old-format msgstore.db/wa.db (no real data) into the `whatsapp` schema."""

import os
import sqlite3
from datetime import UTC, datetime

import pytest
from omarchy_db.testing import test_migrations, test_schema_matches_models  # noqa: F401
from sqlalchemy import func, select

from strata_takeout.whatsapp import cli, ingest, parse
from strata_takeout.whatsapp.models import Chat, Contact, Message, Participant, Source

pytestmark = pytest.mark.db_app("whatsapp")

T0 = 1_500_000_000_000  # 2017-07-14 in ms


def make_msgstore(path, rows, groups=()):
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE messages (_id INTEGER PRIMARY KEY, key_remote_jid TEXT, key_from_me INTEGER, key_id TEXT,
            status INTEGER, data TEXT, timestamp INTEGER, media_mime_type TEXT, media_wa_type TEXT, media_size INTEGER,
            media_name TEXT, media_caption TEXT, media_duration INTEGER, media_hash TEXT, latitude REAL, longitude REAL,
            remote_resource TEXT, starred INTEGER);
        CREATE TABLE message_media (message_row_id INTEGER, file_path TEXT);
        CREATE TABLE chat_list (key_remote_jid TEXT, subject TEXT);
        CREATE TABLE group_participants (gjid TEXT, jid TEXT, admin INTEGER);
        """
    )
    for r in rows:
        con.execute(
            "INSERT INTO messages (_id, key_remote_jid, key_from_me, key_id, status, data, timestamp, media_wa_type,"
            " media_size, media_name, remote_resource) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            r["m"],
        )
        if r.get("path"):
            con.execute("INSERT INTO message_media VALUES (?,?)", (r["m"][0], r["path"]))
    for g in groups:
        con.execute("INSERT INTO chat_list VALUES (?,?)", (g, "Group subject"))
        con.execute("INSERT INTO group_participants VALUES (?,?,1)", (g, "p1@s.whatsapp.net"))
        con.execute("INSERT INTO group_participants VALUES (?,?,1)", (g, "p1@s.whatsapp.net"))  # duplicate row
    con.commit()
    con.close()


def make_wa(path):
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE wa_contacts (jid TEXT, number TEXT, display_name TEXT, wa_name TEXT, is_whatsapp_user INTEGER)"
    )
    con.execute("INSERT INTO wa_contacts VALUES ('a@s.whatsapp.net', '+100', 'Alice', 'al', 1)")
    con.execute("INSERT INTO wa_contacts VALUES ('a@s.whatsapp.net', '+100', 'Alice', 'al', 1)")  # a repeated jid
    con.commit()
    con.close()


ROWS = [
    dict(m=(1, "a@s.whatsapp.net", 1, "K1", 0, "hello", T0, "0", None, None, None)),
    dict(
        m=(2, "a@s.whatsapp.net", 0, "K2", 0, None, T0 + 1000, "1", 123, "IMG-1.jpg", None),
        path="Media/WhatsApp Images/IMG-1.jpg",
    ),
    dict(m=(3, "g@g.us", 0, "K3", 0, "hi group", T0 + 2000, "0", None, None, "p1@s.whatsapp.net")),
    dict(m=(4, "g@g.us", 0, "K4", 6, "subject changed", T0 + 3000, "0", None, None, None)),
    dict(
        m=(5, "a@s.whatsapp.net", 1, "K5", 0, None, 0, "3", 10**18, "VID-9.mp4", None)
    ),  # junk size, no time, file not found
]


@pytest.fixture
def backup(tmp_path):
    tree = tmp_path / "backup" / "extracted"
    tree.mkdir(parents=True)
    make_msgstore(tree / "msgstore.db", ROWS, groups=["g@g.us"])
    make_wa(tree / "wa.db")
    media = tmp_path / "media"
    (media / "sub").mkdir(parents=True)
    (media / "sub" / "IMG-1.jpg").write_bytes(b"x")
    return tmp_path / "backup", media


def test_parse_kinds_sizes_times_and_media(backup):
    tree, media = backup
    con = parse.open_ro(ingest.find_db(str(tree)))
    rows = {m["row_id"]: m for m in parse.messages(con, parse.index_media([str(media)]))}
    assert rows[1]["kind"] == "text" and rows[1]["from_me"] and rows[1]["text"] == "hello"
    assert rows[1]["at"] == datetime.fromtimestamp(T0 / 1000, UTC)
    assert rows[2]["kind"] == "image" and rows[2]["media_file"] == os.path.join("sub", "IMG-1.jpg")
    assert rows[3]["sender_jid"] == "p1@s.whatsapp.net" and rows[1]["sender_jid"] is None
    assert rows[4]["kind"] == "system"
    assert rows[5]["media_size"] is None and rows[5]["at"] is None and rows[5]["media_file"] is None


def test_unknown_database_is_refused(tmp_path):
    con = sqlite3.connect(tmp_path / "x.db")
    con.execute("CREATE TABLE other (a)")
    con.commit()
    with pytest.raises(ValueError, match="no old-format"):
        list(parse.messages(parse.open_ro(str(tmp_path / "x.db")), {}))


def test_load_is_idempotent_and_skips_unchanged(db_session, backup):
    tree, media = backup
    res = ingest.sync_account(db_session, "main", str(tree), [str(media)], "Main")
    assert res == dict(messages=5, chats=2, contacts=1, participants=1)
    db_session.flush()
    assert ingest.sync_account(db_session, "main", str(tree), [str(media)]) is None  # unchanged: skipped
    again = ingest.sync_account(db_session, "main", str(tree), [str(media)], force=True)
    assert again == res
    db_session.flush()
    assert db_session.scalar(select(func.count()).select_from(Message)) == 5  # replaced, never doubled
    chat = db_session.get(Chat, ("main", ingest.SOURCE, "g@g.us"))
    assert chat.is_group and chat.subject == "Group subject" and chat.message_count == 2
    assert db_session.get(Contact, ("main", ingest.SOURCE, "a@s.whatsapp.net")).display_name == "Alice"
    assert db_session.scalar(select(func.count()).select_from(Participant)) == 1
    assert db_session.get(Source, ("main", ingest.SOURCE)).rows == 5


def test_new_media_reloads_unchanged_database(db_session, backup):
    tree, media = backup
    ingest.sync_account(db_session, "main", str(tree), [str(media)])
    db_session.flush()
    (media / "sub" / "VID-9.mp4").write_bytes(b"y")
    res = ingest.sync_account(db_session, "main", str(tree), [str(media)])
    assert res is not None
    db_session.flush()
    assert db_session.scalar(select(Message.media_file).where(Message.row_id == 5)) == os.path.join("sub", "VID-9.mp4")


def test_changed_database_replaces_rows(db_session, backup):
    tree, media = backup
    ingest.sync_account(db_session, "main", str(tree), [str(media)])
    db_session.flush()
    db = ingest.find_db(str(tree))
    con = sqlite3.connect(db)
    con.execute("DELETE FROM messages WHERE _id > 2")
    con.commit()
    con.close()
    os.utime(db, ns=(1, 2_000_000_000_000_000_000))  # a visibly newer mtime
    res = ingest.sync_account(db_session, "main", str(tree), [str(media)])
    assert res is not None and res["messages"] == 2
    db_session.flush()
    assert db_session.scalar(select(func.count()).select_from(Message)) == 2


def test_no_database_under_tree(db_session, tmp_path):
    assert ingest.sync_account(db_session, "main", str(tmp_path), []) == {}


def test_settings_parsing():
    assert cli.accounts("main:Label=/a/b,other=/c") == [("main", "Label", "/a/b"), ("other", None, "/c")]
    assert cli.media("main=/m1:/m2,other=/m3") == {"main": ["/m1", "/m2"], "other": ["/m3"]}
    with pytest.raises(SystemExit):
        cli.accounts("")
    with pytest.raises(ValueError):
        cli.accounts("nonsense")
    with pytest.raises(ValueError):
        cli.media("main")


def test_zero_coordinates_survive_on_location_messages_only(tmp_path):
    db = tmp_path / "msgstore.db"
    make_msgstore(
        db,
        [
            dict(m=(1, "a@s.whatsapp.net", 0, "K1", 0, None, T0, "5", None, None, None)),
            dict(m=(2, "a@s.whatsapp.net", 0, "K2", 0, "x", T0, "0", None, None, None)),
        ],
    )
    con = sqlite3.connect(db)
    con.execute("UPDATE messages SET latitude = 0.0, longitude = 20.5")
    con.commit()
    con.close()
    rows = {m["row_id"]: m for m in parse.messages(parse.open_ro(str(db)), {})}
    assert (rows[1]["latitude"], rows[1]["longitude"]) == (0.0, 20.5)  # equator is a position
    assert (rows[2]["latitude"], rows[2]["longitude"]) == (None, 20.5)  # 0.0 on a text message is "none"


def test_moved_media_or_replaced_contacts_reload_an_unchanged_database(db_session, backup):
    tree, media = backup
    ingest.sync_account(db_session, "main", str(tree), [str(media)])
    db_session.flush()
    os.rename(media / "sub" / "IMG-1.jpg", media / "IMG-1.jpg")  # same name count, new path
    assert ingest.sync_account(db_session, "main", str(tree), [str(media)]) is not None
    db_session.flush()
    assert db_session.scalar(select(Message.media_file).where(Message.row_id == 2)) == "IMG-1.jpg"
    assert ingest.sync_account(db_session, "main", str(tree), [str(media)]) is None
    wa = ingest.find_db(str(tree), "wa.db")
    st = os.stat(wa)
    make_wa(wa + ".new")
    os.replace(wa + ".new", wa)  # same size, older mtime than msgstore.db
    os.utime(wa, ns=(st.st_atime_ns, st.st_mtime_ns - 10**9))
    assert ingest.sync_account(db_session, "main", str(tree), [str(media)]) is not None
