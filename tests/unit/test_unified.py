"""`unified.messages_all`: a view over the platform schemas, tested on a throwaway Postgres (it is Postgres only) with synthetic rows."""

import pytest
from omarchy_db import Project
from omarchy_db.scratch import scratch_database
from omarchy_db.testing import test_migrations, test_schema_matches_models  # noqa: F401
from sqlalchemy import create_engine, text

from strata_takeout.unified import view

pytestmark = pytest.mark.db_app("unified")

PLATFORMS = ("whatsapp", "facebook", "instagram", "googlechat")


def whatsapp_tables(eng):
    """WhatsApp's tables the view reads, straight from its models: its migrations need pgvector (the `windows` table), which a plain test server lacks."""
    from strata_takeout.whatsapp import models

    with eng.begin() as c:
        c.execute(text("CREATE SCHEMA IF NOT EXISTS whatsapp"))
    wa = create_engine(eng.url, connect_args={"options": "-csearch_path=whatsapp"})
    try:
        models.Base.metadata.create_all(
            wa, tables=[models.Base.metadata.tables[t] for t in ("accounts", "chats", "contacts", "messages")]
        )
    finally:
        wa.dispose()


def migrate(url, apps):
    eng = create_engine(url)
    for a in apps:
        if a == "whatsapp":
            whatsapp_tables(eng)
        else:
            Project.load(view.__file__, a).upgrade(eng)
    return eng


def fill(c):
    for acct in ("main",):
        for s in PLATFORMS:
            c.execute(text(f"INSERT INTO {s}.accounts (id, label) VALUES (:a, 'x')"), {"a": acct})
    c.execute(
        text(
            "INSERT INTO whatsapp.chats (account, source, jid, subject, is_group, message_count) VALUES ('main','db','g@g','Club',true,0)"
        )
    )
    c.execute(
        text("INSERT INTO whatsapp.contacts (account, source, jid, display_name) VALUES ('main','db','a@s','Ann')")
    )
    c.execute(
        text(
            "INSERT INTO whatsapp.messages (account, source, row_id, chat_jid, from_me, sender_jid, at, kind, text, media_mime, starred) VALUES "
            "('main','db',1,'g@g',false,'a@s','2020-01-01T10:00:00+00','text','hello',NULL,false),"
            "('main','db',2,'g@g',true,NULL,'2020-01-01T11:00:00+00','image',NULL,'image/jpeg',false)"
        )
    )
    for s in ("facebook", "instagram"):
        c.execute(
            text(
                f"INSERT INTO {s}.threads (account, source, thread_id, title, participants) VALUES ('main','m','t1','Group',2)"
            )
        )
        c.execute(
            text(
                f"INSERT INTO {s}.messages (account, source, ext_id, kind, audience, thread_id, at, author, body, unsent) VALUES "
                "('main','m','e1','chat','thread','t1','2021-01-01T10:00:00+00','Bob','hi',false),"
                "('main','m','e2','post','timeline',NULL,'2021-01-02T10:00:00+00','Bob','my post',false)"
            )
        )
        c.execute(
            text(
                f"INSERT INTO {s}.attachments (message_id, seq, kind) SELECT id, 0, 'photo' FROM {s}.messages WHERE ext_id = 'e1'"
            )
        )
    c.execute(
        text(
            "INSERT INTO googlechat.conversations (account, source, ext_id, kind, name, participants, n_messages) VALUES ('main','takeout','DM x','dm',NULL,2,1)"
        )
    )
    c.execute(
        text(
            "INSERT INTO googlechat.messages (account, source, ext_id, conversation_id, kind, sender_name, at, text, has_attachment) "
            "SELECT 'main','takeout','g1', id, 'message','Cy','2022-01-01T10:00:00+00','yo',true FROM googlechat.conversations"
        )
    )


def test_view_unions_every_platform():
    with scratch_database(True) as url:
        eng = migrate(url, (*PLATFORMS, "unified"))
        try:
            with eng.begin() as c:
                fill(c)
                assert view.present(c) == list(PLATFORMS)
            with eng.connect() as c:
                rows = (
                    c.execute(text(f"SELECT {view.COLUMNS} FROM unified.messages_all ORDER BY at, platform"))
                    .mappings()
                    .all()
                )
                assert [
                    (r["platform"], r["conversation_name"], r["sender"], r["text"], r["kind"], r["has_attachment"])
                    for r in rows
                ] == [
                    ("whatsapp", "Club", "Ann", "hello", "text", False),
                    ("whatsapp", "Club", "me", None, "image", True),
                    ("facebook", "Group", "Bob", "hi", "chat", True),
                    ("instagram", "Group", "Bob", "hi", "chat", True),
                    ("facebook", None, "Bob", "my post", "post", False),
                    ("instagram", None, "Bob", "my post", "post", False),
                    ("googlechat", "DM x", "Cy", "yo", "message", True),
                ]
                assert rows[4]["conversation_id"] == "timeline:"  # a post has no thread: grouped by its audience
                # source_id joins back to the platform's own row
                got = c.execute(
                    text(
                        "SELECT m.text FROM unified.messages_all v JOIN googlechat.messages m ON m.id::text = v.source_id WHERE v.platform = 'googlechat'"
                    )
                ).scalar()
                assert got == "yo"
        finally:
            eng.dispose()


def test_view_only_covers_loaded_platforms_and_is_rebuilt():
    with scratch_database(True) as url:
        eng = migrate(url, ("googlechat", "unified"))
        try:
            with eng.connect() as c:
                assert c.execute(text("SELECT count(*) FROM unified.messages_all")).scalar() == 0
            with eng.begin() as c:
                assert view.build(c) == ["googlechat"]
            migrate(url, ("facebook",))
            with eng.begin() as c:
                assert view.build(c) == ["facebook", "googlechat"]  # a later platform joins on rebuild
        finally:
            eng.dispose()


def test_no_platform_loaded_gives_an_empty_view():
    with scratch_database(True) as url:
        eng = migrate(url, ("unified",))
        try:
            with eng.connect() as c:
                assert c.execute(text("SELECT count(*) FROM unified.messages_all")).scalar() == 0
        finally:
            eng.dispose()
