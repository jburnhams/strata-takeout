"""The gmvault reader and its path through the Gmail loader: synthetic month zips (no real data)."""

import json
import zipfile
from datetime import datetime

import pytest
from sqlalchemy import func, select

from strata_takeout.gmail import gmvault, ingest, rules
from strata_takeout.gmail.models import Message, Source

pytestmark = pytest.mark.db_app("gmail")


def eml(frm, subject, body, mid=None, date="Mon, 01 Jan 2012 10:00:00 +0000"):
    d = f"Date: {date}\n" if date else ""
    m = f"Message-ID: <{mid}>\n" if mid else ""
    return f"From: {frm}\nTo: me@example.org\nSubject: {subject}\n{d}{m}Content-Type: text/plain; charset=utf-8\n\n{body}\n".encode()


def meta(gm_id, labels, flags=("\\Seen",), msg_id=None, ts=1325412000):
    return json.dumps(
        dict(gm_id=gm_id, msg_id=msg_id, labels=list(labels), flags=list(flags), internal_date=ts, subject="x")
    ).encode()


def month(path, name, items):
    with zipfile.ZipFile(path, "w") as z:
        for gm_id, m, e in items:
            z.writestr(f"{name}/{gm_id}.meta", m)
            z.writestr(f"{name}/{gm_id}.eml", e)


@pytest.fixture
def backup(tmp_path):
    db = tmp_path / "bk" / "db"
    db.mkdir(parents=True)
    month(
        db / "2012-01.zip",
        "2012-01",
        [
            (11, meta(11, ["\\Sent"]), eml("me@example.org", "Plans", "see you at nine", mid="a1")),
            (
                12,
                meta(12, ["\\Inbox", "\\Important", "Projects/Alpha"], msg_id="b2"),
                eml("boss@example.org", "Review", "please review the draft", mid="b2"),
            ),
            (
                13,
                meta(13, ["\\Inbox"], flags=[]),
                eml("build@example.org", "Build failed", "job 12 failed", mid="c3", date=""),
            ),
        ],
    )
    month(
        db / "2012-02.zip",
        "2012-02",
        [
            (
                21,
                meta(21, ["\\Inbox"], flags=["\\Seen", "\\Flagged"]),
                eml("boss@example.org", "Review again", "same message id as before", mid="b2"),
            ),  # a copy
            (22, meta(22, ["gmvault-chats"]), eml("chat@example.org", "chat", "hi")),
            (23, meta(23, ["\\Inbox"]), eml("pal@example.org", "Lunch", "lunch on friday?", mid="d4")),
        ],
    )
    (db / "notes.txt").write_text("not an archive")
    return tmp_path / "bk"


def test_the_reader_pairs_files_normalises_labels_and_skips_chats(backup):
    assert gmvault.detect(str(backup)) and not gmvault.detect(str(backup / "nope"))
    got = list(gmvault.items(str(backup)))
    assert [g["gm_id"] for g in got] == [11, 12, 13, 21, 23]  # chats left out, oldest month first
    by = {g["gm_id"]: g for g in got}
    assert by[11]["labels"] == ["sent", "opened"]
    assert by[12]["labels"] == ["inbox", "important", "projects/alpha", "opened"]
    assert by[13]["labels"] == ["inbox", "unread"] and by[21]["labels"] == ["inbox", "opened", "starred"]
    assert by[11]["internal_date"] == 1325412000


def test_unzipped_months_work_too(backup, tmp_path):
    out = tmp_path / "plain" / "db"
    for z in (backup / "db").glob("*.zip"):
        zipfile.ZipFile(z).extractall(out)
    assert [g["gm_id"] for g in gmvault.items(str(tmp_path / "plain"))] == [11, 12, 13, 21, 23]


def test_load_keeps_sent_dedupes_dates_and_is_idempotent(db_session, backup):
    assert ingest.sync_tree(db_session, "work", str(backup), None, "Work") == 4  # the copy of b2 is dropped
    db_session.flush()
    rows = {m.offset: m for m in db_session.scalars(select(Message))}
    assert set(rows) == {11, 12, 13, 23} and all(m.source == "gmvault" for m in rows.values())
    assert (rows[11].tier, rows[11].tier_reason, rows[11].body_text) == ("keep", "sent", "see you at nine")
    assert rows[12].tier == "other" and rows[12].tier_reason == "uncategorised" and "important" in rows[12].labels
    assert rows[13].at.replace(tzinfo=None) == datetime(2012, 1, 1, 10, 0)  # no Date header: gmvault's internal date
    assert ingest.sync_tree(db_session, "work", str(backup), None) is None  # unchanged
    assert ingest.sync_tree(db_session, "work", str(backup), None, force=True) == 4
    db_session.flush()
    assert db_session.scalar(select(func.count()).select_from(Message)) == 4
    assert db_session.get(Source, ("work", "gmvault")).rows == 4


def test_a_new_month_reloads_and_verdicts_key_on_the_gmail_id(db_session, backup, tmp_path):
    ingest.sync_tree(db_session, "work", str(backup), None)
    db_session.flush()
    month(
        backup / "db" / "2012-03.zip",
        "2012-03",
        [(31, meta(31, ["\\Inbox"]), eml("x@example.org", "New", "a new one", mid="e5"))],
    )
    v = tmp_path / "v.jsonl"
    v.write_text(json.dumps(dict(offset=12, keep=True, label="personal", confidence=0.9, source="t")) + "\n")
    assert ingest.sync_tree(db_session, "work", str(backup), None, verdicts=str(v)) == 5
    db_session.flush()
    promoted = db_session.scalar(select(Message).where(Message.offset == 12))
    assert (promoted.tier, promoted.tier_reason, promoted.body_text) == (
        "keep",
        "classified",
        "please review the draft",
    )


def test_nothing_to_load_under_a_plain_folder(db_session, tmp_path):
    assert ingest.sync_tree(db_session, "work", str(tmp_path), None) == 0
    assert rules.tier(["inbox", "important", "opened"], "boss@example.org", False, False) == ("other", "uncategorised")
