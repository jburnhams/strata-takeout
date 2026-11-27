"""The Outlook .pst reader and its path through the Gmail loader. A fake `readpst` (a script that writes a folder tree of .eml files) stands in for libpst: synthetic data only."""

import stat
import sys
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from strata_takeout.gmail import ingest, pst
from strata_takeout.gmail.models import Message, Source

pytestmark = pytest.mark.db_app("gmail")

FAKE = """#!{py}
import os, sys
out = sys.argv[sys.argv.index("-o") + 1]
tree = {tree!r}
for path, files in tree.items():
    d = os.path.join(out, path)
    os.makedirs(d, exist_ok=True)
    for name, data in files.items():
        open(os.path.join(d, name), "wb").write(data)
"""


def eml(frm, subject, body, date="Mon, 01 Jan 2005 10:00:00 +0000", received=None):
    h = f"Received: from mx.example.org by mail.example.org; {received}\n" if received else ""
    d = f"Date: {date}\n" if date else ""
    return f"{h}From: {frm}\nTo: me@example.org\nSubject: {subject}\n{d}Message-ID: <{subject}@example.org>\nContent-Type: text/plain\n\n{body}\n".encode()


TREE = {
    "Inbox": {"2.eml": eml("a@example.org", "two", "second"), "10.eml": eml("b@example.org", "ten", "tenth", date="")},
    "Inbox/Family ": {"1.eml": eml("c@example.org", "fam", "hello family")},
    "Sent Items": {"1.eml": eml("me@example.org", "out", "sent text")},
    "Junk E-mail": {"1.eml": eml("x@example.org", "junk", "buy now")},
    "Contacts": {"1.vcf": b"BEGIN:VCARD\nEND:VCARD\n"},  # not mail: ignored
}


@pytest.fixture
def fake(tmp_path):
    def make(tree=TREE):
        exe = tmp_path / "readpst"
        exe.write_text(FAKE.format(py=sys.executable, tree=tree))
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
        return str(exe)

    return make


@pytest.fixture
def pstfile(tmp_path):
    p = tmp_path / "mail.pst"
    p.write_bytes(b"!BDN fake")
    return p


def test_labels_name_the_folder_and_the_system_label():
    assert pst.labels("Inbox/Family ") == ["inbox/family", "inbox"]
    assert pst.labels("Sent Items") == ["sent items", "sent"]
    assert pst.labels("Junk E-mail") == ["junk e-mail", "spam"]
    assert pst.labels("Projects/A,B") == ["projects/a b"]
    assert pst.labels("") == []


def test_the_last_received_hop_dates_a_message_without_a_date():
    raw = eml("a@example.org", "s", "b", date="", received="Tue, 02 Feb 2010 12:30:00 +0100")
    assert pst.received_when(raw) == datetime(2010, 2, 2, 11, 30, tzinfo=UTC)
    assert pst.received_when(eml("a@example.org", "s", "b")) is None
    assert pst.received_when(b"Received: garbage; not a date\n\nbody") is None


def test_detect_and_items_walk_folders_in_order_with_stable_offsets(pstfile, fake, tmp_path):
    assert pst.detect(str(pstfile)) and not pst.detect(str(tmp_path)) and not pst.detect(str(tmp_path / "x.mbox"))
    got = list(pst.items(str(pstfile), str(tmp_path), fake()))
    assert [(g["offset"], g["labels"][0]) for g in got] == [
        (1 * pst.STEP + 2, "inbox"),
        (1 * pst.STEP + 10, "inbox"),
        (2 * pst.STEP + 1, "inbox/family"),
        (3 * pst.STEP + 1, "junk e-mail"),
        (4 * pst.STEP + 1, "sent items"),
    ]  # numeric file order, then folder path order; the contact is not mail
    assert not list(tmp_path.glob("strata-pst-*"))  # the scratch export is removed


def test_load_labels_dates_and_is_idempotent(db_session, pstfile, fake, tmp_path):
    exe = fake()
    n = ingest.sync_tree(db_session, "old", str(pstfile), None, "Old", workdir=str(tmp_path), readpst=exe)
    assert n == 5
    db_session.flush()
    rows = {m.subject: m for m in db_session.scalars(select(Message))}
    assert all(m.source == "pst" and m.account == "old" for m in rows.values())
    assert (rows["out"].tier, rows["out"].tier_reason, rows["out"].body_text) == ("keep", "sent", "sent text")
    assert rows["junk"].tier_reason == "spam-trash" and rows["fam"].labels == "inbox/family,inbox"
    assert rows["ten"].at is None  # no Date and no Received: stays undated
    assert ingest.sync_tree(db_session, "old", str(pstfile), None, workdir=str(tmp_path), readpst=exe) is None
    assert ingest.sync_tree(db_session, "old", str(pstfile), None, force=True, workdir=str(tmp_path), readpst=exe) == 5
    db_session.flush()
    assert db_session.scalar(select(func.count()).select_from(Message)) == 5
    assert db_session.get(Source, ("old", "pst")).rows == 5


def test_a_copy_in_two_folders_is_two_rows_and_keep_all_keeps_them(db_session, pstfile, fake, tmp_path):
    same = eml("a@example.org", "same", "in two places")
    exe = fake({"Inbox": {"1.eml": same}, "Archive": {"1.eml": same}})
    assert (
        ingest.sync_tree(db_session, "old", str(pstfile), None, keep_all=True, workdir=str(tmp_path), readpst=exe) == 2
    )
    db_session.flush()
    assert sorted(m.labels for m in db_session.scalars(select(Message))) == ["archive", "inbox"]


def test_a_missing_readpst_is_a_clear_error(db_session, pstfile, tmp_path):
    with pytest.raises(SystemExit, match="readpst"):
        ingest.sync_tree(db_session, "old", str(pstfile), None, workdir=str(tmp_path), readpst="no-such-readpst-binary")
