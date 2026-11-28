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
if "-D" not in sys.argv:  # without -D the real tool leaves Deleted Items out
    tree = {{k: v for k, v in {tree!r}.items() if not k.startswith("Deleted")}}
else:
    tree = {tree!r}
for path, files in tree.items():
    d = os.path.join(out, "Store", path)  # the real tool nests everything under a directory named for the store
    os.makedirs(d, exist_ok=True)
    for name, data in files.items():
        open(os.path.join(d, name), "wb").write(data)
sys.exit({rc})
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
    "Deleted Items": {"1.eml": eml("d@example.org", "gone", "deleted text")},
    "Contacts": {"1.vcf": b"BEGIN:VCARD\nEND:VCARD\n"},  # not mail: ignored
}


@pytest.fixture
def fake(tmp_path):
    def make(tree=TREE, rc=0):
        exe = tmp_path / "readpst"
        exe.write_text(FAKE.format(py=sys.executable, tree=tree, rc=rc))
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
        (1 * pst.STEP + 1, "deleted items"),
        (2 * pst.STEP + 2, "inbox"),
        (2 * pst.STEP + 10, "inbox"),
        (3 * pst.STEP + 1, "inbox/family"),
        (4 * pst.STEP + 1, "junk e-mail"),
        (5 * pst.STEP + 1, "sent items"),
    ]  # numeric file order, then folder path order; the contact is not mail
    assert not list(tmp_path.glob("strata-pst-*"))  # the scratch export is removed


def test_load_labels_dates_and_is_idempotent(db_session, pstfile, fake, tmp_path):
    exe = fake()
    n = ingest.sync_tree(db_session, "old", str(pstfile), None, "Old", workdir=str(tmp_path), readpst=exe)
    assert n == 6
    db_session.flush()
    rows = {m.subject: m for m in db_session.scalars(select(Message))}
    assert all(m.source == "pst" and m.account == "old" for m in rows.values())
    assert (rows["out"].tier, rows["out"].tier_reason, rows["out"].body_text) == ("keep", "sent", "sent text")
    assert rows["gone"].tier_reason == "spam-trash" and "trash" in rows["gone"].labels  # Deleted Items came with -D
    assert rows["out"].labels == "sent items,sent"  # the store directory is not part of the folder path
    assert rows["junk"].tier_reason == "spam-trash" and rows["fam"].labels == "inbox/family,inbox"
    assert rows["ten"].at is None  # no Date and no Received: stays undated
    assert ingest.sync_tree(db_session, "old", str(pstfile), None, workdir=str(tmp_path), readpst=exe) is None
    assert ingest.sync_tree(db_session, "old", str(pstfile), None, force=True, workdir=str(tmp_path), readpst=exe) == 6
    db_session.flush()
    assert db_session.scalar(select(func.count()).select_from(Message)) == 6
    assert db_session.get(Source, ("old", "pst")).rows == 6


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


def test_a_failed_export_loads_nothing_even_if_files_were_written(db_session, pstfile, fake, tmp_path):
    with pytest.raises(RuntimeError, match="readpst failed"):
        ingest.sync_tree(db_session, "old", str(pstfile), None, workdir=str(tmp_path), readpst=fake(rc=2))
    assert db_session.get(Source, ("old", "pst")) is None


SHELL = b"From: <MAILER-DAEMON>\nSubject: Lost jacket\nTo: me@example.org\nMIME-Version: 1.0\nContent-Type: text/plain\n\n\n"
HEADERS = b"Received: from a.example.org by b.example.org; Wed, 05 May 2004 10:00:00 +0000\r\nFrom: Ann <ann@example.org>\r\nSubject: Lost jacket\r\nTo: me@example.org\r\nMessage-ID: <j1@example.org>\r\n\r\n"


def test_header_only_shells_are_replaced_by_the_headers_libpff_read(db_session, pstfile, fake, tmp_path, monkeypatch):
    exe = fake(
        {
            "Inbox": {
                "1.eml": eml("a@example.org", "real", "a body"),
                "2.eml": SHELL,
                "3.eml": SHELL.replace(b"jacket", b"keys"),
            }
        }
    )
    monkeypatch.setattr(
        pst,
        "stubs",
        lambda p: {"Inbox": [("Lost jacket", HEADERS, 1083751200.0)], "Other": [("Only here", HEADERS, None)]},
    )
    got = list(pst.items(str(pstfile), str(tmp_path), exe))
    by = {g["offset"]: g for g in got}
    # the real message and the unmatched shell ("keys": libpff did not see it) stay; the matched shell is gone; both recovered items are added
    assert sorted(by) == [
        1 * pst.STEP + 1,
        1 * pst.STEP + 3,
        1 * pst.STEP + pst.STUB_BASE,
        2 * pst.STEP + pst.STUB_BASE,
    ]
    assert b"ann@example.org" in by[1 * pst.STEP + pst.STUB_BASE]["raw"] and by[2 * pst.STEP + pst.STUB_BASE][
        "labels"
    ] == ["other"]
    n = ingest.sync_tree(db_session, "uni", str(pstfile), None, keep_all=True, workdir=str(tmp_path), readpst=exe)
    assert n == 4
    db_session.flush()
    row = db_session.scalar(select(Message).where(Message.offset == pst.STEP + pst.STUB_BASE))
    assert (row.from_addr, row.subject, row.message_id) == ("ann@example.org", "Lost jacket", "<j1@example.org>")
    assert row.at.replace(tzinfo=None) == datetime(2004, 5, 5, 10, 0)  # from the Received header


def test_a_shell_is_recognised_by_its_shape():
    assert pst.is_stub(SHELL) and not pst.is_stub(eml("a@example.org", "s", "b"))
    assert not pst.is_stub(SHELL + b"x" * 2000)  # a real (long) message from a daemon is not a shell
    assert pst._subject(SHELL) == "Lost jacket"
