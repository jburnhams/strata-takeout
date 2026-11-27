"""Google chat loader: synthetic exports (no real data) into the `googlechat` schema."""

import gzip
import json

import pytest
from omarchy_db.testing import test_migrations, test_schema_matches_models  # noqa: F401
from sqlalchemy import func, select

from strata_takeout.googlechat import cli, ingest, parse
from strata_takeout.googlechat.models import Attachment, Conversation, Member, Message, Source

pytestmark = pytest.mark.db_app("googlechat")


def put(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def user(name, email):
    return dict(name=name, email=email, user_type="Human")


def line(i, who, text, date, **kw):
    return dict(creator=who, created_date=date, text=text, topic_id=f"T{i}", message_id=f"G/T{i}/T{i}", **kw)


A, B = user("Ann Example", "ann@example.org"), user("Bob Example", "Bob@Example.org")


@pytest.fixture
def takeout(tmp_path):
    g = tmp_path / "Google Chat" / "Groups"
    dm = g / "DM abc"
    put(dm / "group_info.json", dict(members=[A, B]))
    put(
        dm / "messages.json",
        dict(
            messages=[
                line(1, A, "hello", "Sunday, 26 May 2013 at 13:12:51 UTC"),
                line(
                    2,
                    B,
                    "pic",
                    "Monday, 27 May 2013 at 08:00:00 UTC",
                    attached_files=[dict(original_name="x.jpg", export_name="File-2013-05-27.jpg")],
                ),
                line(
                    3,
                    A,
                    None,
                    "Monday, 27 May 2013 at 09:00:00 UTC",
                    attached_files=[dict(original_name="x.jpg", export_name="File-2013-05-27.jpg")],
                    annotations=[dict(start_index=0, length=3, url_metadata=dict(url="http://example.org"))],
                ),
                line(
                    4,
                    B,
                    "q",
                    "Tuesday, 28 May 2013 at 09:00:00 UTC",
                    attached_files=[dict(original_name="1?account_id=0.png", export_name="File-1?account_id=0.png")],
                ),
                line(
                    5,
                    A,
                    "gone",
                    "Tuesday, 28 May 2013 at 10:00:00 UTC",
                    attached_files=[dict(original_name="z.png", export_name="File-nothere.png")],
                ),
            ]
        ),
    )
    (dm / "File-2013-05-27.jpg").write_bytes(b"one")
    (dm / "File-2013-05-27(1).jpg").write_bytes(b"two")
    (dm / "File-1_account_id=0.png").write_bytes(b"png")
    (dm / "File-stray.jpg").write_bytes(b"stray")
    sp = g / "Space xyz"
    put(sp / "group_info.json", dict(name="Club", members=[A]))
    put(sp / "messages.json", dict(messages=[line(9, A, "x", "Friday, 2 February 2016 at 05:51:30 UTC")]))
    return tmp_path


def chat_mail(n, frm, to, lines, extra="", date=True, labels="Chat", sep="Fri Dec 09 17:36:24 +0000 2011"):
    body = "".join(
        f'<cli:message to="{to}" from="{who}/Res" int:sequence-no="{i}" int:time-stamp="{ms}" xmlns:cli="jabber:client" xmlns:int="google:internal">'
        f"<cli:body>{text}</cli:body></cli:message>"
        for i, (who, text, ms) in enumerate(lines)
    )
    return (
        f"From {n}@xxx {sep}\nX-Gmail-Labels: {labels}\nFrom: {frm}\nTo: {to}\nMessage-ID: <{n}@chat.example.org>\n"
        + ("Date: Fri, 9 Dec 2011 17:00:00 +0000\n" if date else "")
        + f'{extra}Subject: Chat\nContent-Type: multipart/alternative; boundary="B"\n\n--B\nContent-Type: text/xml; charset=utf-8\n\n'
        f'<con:conversation xmlns:con="google:archive:conversation">{body}</con:conversation>\n'
        "--B\nContent-Type: text/html\n\n<div>x</div>\n--B--\n\n"
    )


@pytest.fixture
def mbox(tmp_path):
    p = tmp_path / "Mail" / "All mail.mbox"
    p.parent.mkdir()
    ordinary = (
        "From 7@xxx Mon Jan  1 00:00:00 2018\nX-Gmail-Labels: Inbox\nFrom: s@example.org\nTo: me@example.org\n"
        "Message-ID: <7@example.org>\nSubject: hi\n\nbody\n\n"
    )
    p.write_text(
        chat_mail(
            1,
            "a@example.org",
            "me@example.org",
            [("a@example.org", "hi", 1323451862533), ("me@example.org", "yo", 1323451900000)],
        )
        + ordinary
        + chat_mail(2, "a@example.org", "me@example.org", [("a@example.org", "again", 0)], date=False)
        + chat_mail(
            3, "z@example.org", "me@example.org", [("z@example.org", "other", 1323451862533)], labels="Chat,Sent"
        )
    )
    return p.parent


def test_when_parses_the_export_wording():
    assert parse.when("Sunday, 26 May 2013 at 13:12:51 UTC").isoformat() == "2013-05-26T13:12:51+00:00"
    assert parse.when("garbage") is None and parse.when(None) is None
    assert parse.when("Sunday, 31 Foo 2013 at 13:12:51 UTC") is None


def test_media_matching_orders_duplicates_and_unquotes():
    files = {"a.jpg", "a(1).jpg", "q_x=0.png", "left.jpg"}
    refs = [
        [dict(export_name="a.jpg")],
        [dict(export_name="a.jpg")],
        [dict(export_name="a.jpg")],
        [dict(export_name="q?x=0.png")],
        [dict(export_name="nope.jpg")],
    ]
    out, orphans = parse.match_media(files, refs)
    assert [(r[0]["path"], r[0]["match"]) for r in out] == [
        ("a.jpg", "exact"),
        ("a(1).jpg", "sequence"),
        ("a.jpg", "shared"),  # a third reference, no third file: the export reused it
        ("q_x=0.png", "unquoted"),
        (None, None),
    ]
    assert out[4][0]["status"] == "missing" and orphans == ["left.jpg"]


def test_takeout_parse(takeout):
    convs = list(parse.takeout(str(takeout)))
    assert [c["ext_id"] for c in convs] == ["DM abc", "Space xyz"]
    dm = convs[0]
    assert (dm["kind"], len(dm["members"])) == ("dm", 2)
    assert dm["members"][1]["email"] == "bob@example.org"  # lowercased
    assert dm["messages"][0]["at"].isoformat() == "2013-05-26T13:12:51+00:00"
    assert dm["messages"][2]["text"] is None and dm["messages"][2]["raw"]["annotations"][0]["url_metadata"]
    assert dm["messages"][1]["attachments"][0]["path"].endswith("File-2013-05-27.jpg")
    assert dm["messages"][2]["attachments"][0]["match"] == "sequence"
    assert [o["export_name"] for o in dm["orphans"]] == ["File-stray.jpg"]
    assert convs[1]["kind"] == "space" and convs[1]["name"] == "Club"
    assert list(parse.takeout(str(takeout / "nowhere"))) == []


def test_repeated_message_ids_stay_unique(tmp_path):
    g = tmp_path / "Groups" / "DM r"
    put(g / "messages.json", dict(messages=[line(1, A, "a", "Monday, 1 January 2018 at 10:00:00 UTC")] * 2))
    ids = [m["ext_id"] for c in parse.takeout(str(tmp_path)) for m in c["messages"]]
    assert ids == ["G/T1/T1", "G/T1/T1~2"]


def test_takeout_load_is_idempotent_and_keeps_everything(db_session, takeout):
    assert ingest.sync(db_session, "main", [str(takeout)], "Main") == {"takeout": 5 + 1}
    db_session.flush()
    assert ingest.sync(db_session, "main", [str(takeout)]) == {"takeout": None}  # unchanged
    assert ingest.sync(db_session, "main", [str(takeout)], force=True) == {"takeout": 6}
    db_session.flush()
    count = lambda t: db_session.scalar(select(func.count()).select_from(t))
    assert (count(Conversation), count(Member), count(Message)) == (2, 3, 6)
    dm = db_session.scalar(select(Conversation).where(Conversation.ext_id == "DM abc"))
    assert (dm.n_messages, dm.participants, dm.first_at.day, dm.last_at.day) == (5, 2, 26, 28)
    assert dm.info["members"][0]["email"] == "ann@example.org"
    by = {(a.export_name, a.status): a for a in db_session.scalars(select(Attachment))}
    assert by["File-stray.jpg", "orphan"].message_id is None and by["File-stray.jpg", "orphan"].size == 5
    assert by["File-nothere.png", "missing"].path is None
    got = db_session.scalar(
        select(Attachment).where(Attachment.match == "exact", Attachment.export_name == "File-2013-05-27.jpg")
    )
    assert (got.size, got.match, got.mime, len(got.sha256)) == (3, "exact", "image/jpeg", 64)
    m = db_session.scalar(select(Message).where(Message.ext_id == "G/T3/T3"))
    assert m.text is None and m.has_attachment and m.raw["annotations"][0]["length"] == 3
    assert db_session.get(Source, ("main", "takeout")).rows == 6


def test_gmail_chat_mails(db_session, mbox):
    assert ingest.sync(db_session, "main", [str(mbox)]) == {"gmail": 4}
    db_session.flush()
    convs = db_session.scalars(select(Conversation).order_by(Conversation.first_at)).all()
    assert [c.kind for c in convs] == ["chat", "chat"] and [c.n_messages for c in convs] == [
        3,
        1,
    ]  # two mails, same people, one conversation
    assert convs[0].participants == 2
    ms = db_session.scalars(select(Message).where(Message.conversation_id == convs[0].id).order_by(Message.at)).all()
    assert [(m.sender_email, m.text, m.at_precision) for m in ms] == [
        ("a@example.org", "hi", "ms"),
        ("me@example.org", "yo", "ms"),
        ("a@example.org", "again", "mail"),  # a zero stamp is unknown: the separator date stands in
    ]
    assert ms[0].raw["mail"]["labels"] == ["chat"] and ms[0].raw["mail"]["message_id"] == "<1@chat.example.org>"
    assert ms[0].at.replace(tzinfo=None).isoformat() == "2011-12-09T17:31:02.533000"


def test_undated_mail_without_stamps_uses_separator_date():
    raw = (
        b"From 5@xxx Fri Dec 09 17:36:24 +0000 2011\nFrom: a@example.org\nTo: b@example.org\nMessage-ID: <5@example.org>\n"
        b"Content-Type: text/plain\n\njust text\n"
    )
    c = parse.mail_chat(raw)
    assert c is not None and c["people"] == ["a@example.org", "b@example.org"]
    (m,) = c["messages"]
    assert (m["text"], m["at"].isoformat(), m["at_precision"]) == ("just text", "2011-12-09T17:36:24+00:00", "mail")


def test_gmvault_tree(db_session, tmp_path):
    t = tmp_path / "vault" / "chats"
    t.mkdir(parents=True)
    raw = chat_mail(1, "a@example.org", "me@example.org", [("a@example.org", "hi", 1323451862533)], date=False).encode()
    for n, comp in (("100", False), ("101", True), ("102", False)):
        (t / f"{n}.eml{'.gz' if comp else ''}").write_bytes(gzip.compress(raw) if comp else raw)
        labels = ["gmvault-chats"] if n != "102" else ["Inbox"]
        (t / f"{n}.meta").write_text(
            json.dumps(
                dict(msg_id="<m1>" if n != "101" else "<m2>", gm_id=int(n), labels=labels, internal_date=1323451862)
            )
        )
    (t / "103.meta").write_text("{broken")
    (t / "104.meta").write_text(json.dumps(dict(msg_id="<m4>", labels=["gmvault-chats"])))  # no eml beside it
    assert ingest.sync(db_session, "work", [str(tmp_path / "vault")]) == {"gmvault": 2}
    db_session.flush()
    assert db_session.scalar(select(func.count()).select_from(Message)) == 2


def test_gmvault_chat_zip(db_session, tmp_path):
    import zipfile

    raw = chat_mail(1, "a@example.org", "me@example.org", [("a@example.org", "hi", 1323451862533)], date=False).encode()
    meta = json.dumps(dict(msg_id="<m1>", gm_id=1, labels=["gmvault-chats"], internal_date=1323451862))
    with zipfile.ZipFile(tmp_path / "chats (2).zip", "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("chats/subchats-1/1.eml", raw)
        z.writestr("chats/subchats-1/1.meta", meta)
        z.writestr("chats/subchats-2/2.eml.gz", gzip.compress(raw))
        z.writestr("chats/subchats-2/2.meta", meta)  # the same chat stored twice: one message
    (tmp_path / "2012-01.zip").write_bytes(b"not a chat archive")  # mail archives are the mail loader's
    assert ingest.sync(db_session, "work", [str(tmp_path)]) == {"gmvault": 1}


def test_nothing_to_load(db_session, tmp_path):
    assert ingest.sync(db_session, "main", [str(tmp_path)]) == {}


def test_settings_parsing():
    assert cli.accounts("a:Alpha=/x;/y,b=/z") == [("a", "Alpha", ["/x", "/y"]), ("b", None, ["/z"])]
    with pytest.raises(SystemExit):
        cli.accounts("")
    with pytest.raises(ValueError):
        cli.accounts("nopath")


def test_a_groups_folder_without_chat_is_not_chat(tmp_path):
    (tmp_path / "Groups" / "example.org").mkdir(parents=True)  # Google Groups, not Google Chat
    assert parse.takeout_root(str(tmp_path)) is None
    (tmp_path / "Google Chat" / "Groups" / "DM x").mkdir(parents=True)
    (tmp_path / "Google Chat" / "Groups" / "DM x" / "messages.json").write_text("{}")
    assert parse.takeout_root(str(tmp_path)) == str(tmp_path / "Google Chat")
