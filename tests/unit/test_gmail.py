"""Gmail loader: a synthetic mbox (no real data) into the `gmail` schema."""

import os

import pytest
from omarchy_db.testing import test_migrations, test_schema_matches_models  # noqa: F401
from sqlalchemy import func, select

from strata_takeout.gmail import cli, ingest, parse, rules
from strata_takeout.gmail.models import Attachment, Message, Source

pytestmark = pytest.mark.db_app("gmail")


def msg(n, frm, labels, extra="", body="hello there", to="me@example.org", subject="Subject"):
    return (
        f"From {n}@xxx Mon Jan  1 00:00:00 2018\n"
        f"From: Someone <{frm}>\nTo: {to}\nSubject: {subject} {n}\nDate: Mon, 01 Jan 2018 10:00:00 +0000\n"
        f"Message-ID: <{n}@example.org>\nX-Gmail-Labels: {labels}\n{extra}"
        f"MIME-Version: 1.0\nContent-Type: text/plain; charset=utf-8\n\n{body}\n\n"
    )


ATTACH = (
    "MIME-Version: 1.0\nContent-Type: multipart/mixed; boundary=B\n\n--B\nContent-Type: text/plain\n\nsee attached\n"
    "--B\nContent-Type: application/pdf; name=a.pdf\nContent-Disposition: attachment; filename=a.pdf\n"
    "Content-Transfer-Encoding: base64\n\nJVBERi0xLjQK\n--B--\n"
)


@pytest.fixture
def mbox(tmp_path):
    tree = tmp_path / "Mail"
    tree.mkdir()
    sent = msg(1, "me@example.org", "Sent,Inbox", to="friend@example.org").replace(
        "MIME-Version: 1.0\nContent-Type: text/plain; charset=utf-8\n\nhello there\n", ATTACH
    )
    sent2 = sent.replace("From 1@", "From 6@").replace("<1@", "<6@")  # the same file sent again
    text = (
        sent
        + sent2
        + msg(2, "pal@example.org", "Inbox,Category personal")
        + msg(3, "shop@example.com", "Inbox,Category promotions", "List-Unsubscribe: <mailto:x@example.com>\n")
        + msg(4, "noreply@example.com", "Inbox,Category updates")
        + msg(5, "x@example.com", "Spam")
    )
    (tree / "All mail.mbox").write_text(text)
    return tree


def test_rules():
    assert rules.tier(["sent"], "me@example.org", False, False) == ("keep", "sent")
    assert rules.tier(["inbox", "category personal"], "a@b.c", False, False) == ("keep", "personal")
    assert rules.tier(["inbox"], "a@b.c", False, False) == ("keep", "personal")  # uncategorised
    assert rules.tier(["category promotions"], "a@b.c", False, False)[0] == "other"
    assert rules.tier(["inbox", "category personal"], "a@b.c", True, False) == ("other", "bulk")
    assert rules.tier(["inbox"], "no-reply@b.c", False, False) == ("other", "automated-sender")
    assert rules.tier(["spam"], "a@b.c", False, False) == ("other", "spam-trash")
    assert rules.tier(["sent", "spam"], "a@b.c", False, False)[0] == "keep"


def test_split_ignores_escaped_from_lines(tmp_path):
    p = tmp_path / "a.mbox"
    p.write_text(msg(1, "a@b.c", "Inbox", body=">From here on it is body text") + msg(2, "a@b.c", "Inbox"))
    (batch,) = list(parse.split(str(p)))
    assert [o for o, _ in batch][0] == 0
    assert len(batch) == 2


def test_parse_message_and_attachment(mbox, tmp_path):
    folder = tmp_path / "att"
    rows = list(parse.messages(str(mbox / "All mail.mbox"), str(folder)))
    assert len(rows) == 6
    m, atts = rows[0]
    assert (m["tier"], m["tier_reason"], m["from_addr"], m["n_attachments"]) == ("keep", "sent", "me@example.org", 1)
    assert m["body_text"] == "see attached"
    assert atts[0]["filename"] == "a.pdf" and atts[0]["size"] == 9
    assert os.path.isfile(folder / atts[0]["stored_path"])
    assert rows[1][1][0]["stored_path"] == atts[0]["stored_path"]  # same bytes: stored once
    junk, jatts = rows[3]
    assert (junk["tier"], junk["body_text"], jatts) == ("other", None, [])
    assert junk["subject"] == "Subject 3" and junk["has_unsubscribe"] is True


def test_load_is_idempotent_and_skips_unchanged(db_session, mbox, tmp_path):
    folder = str(tmp_path / "att")
    assert ingest.sync_mbox(db_session, "main", str(mbox), folder, "Main") == 6
    db_session.flush()
    assert ingest.sync_mbox(db_session, "main", str(mbox), folder) is None  # unchanged: skipped
    assert ingest.sync_mbox(db_session, "main", str(mbox), folder, force=True) == 6
    db_session.flush()
    assert db_session.scalar(select(func.count()).select_from(Message)) == 6  # replaced, never doubled
    assert db_session.scalar(select(func.count()).select_from(Attachment)) == 2
    assert db_session.scalar(select(func.count()).select_from(Message).where(Message.tier == "keep")) == 3
    assert db_session.scalar(select(func.count()).select_from(Message).where(Message.body_text.is_not(None))) == 3
    assert db_session.get(Source, ("main", ingest.SOURCE)).rows == 6


def test_new_rules_reload_unchanged_mbox(db_session, mbox, monkeypatch):
    ingest.sync_mbox(db_session, "main", str(mbox), None)
    db_session.flush()
    monkeypatch.setattr(rules, "RULES", "r-next")
    assert ingest.sync_mbox(db_session, "main", str(mbox), None) == 6


def test_no_mbox_under_tree(db_session, tmp_path):
    assert ingest.sync_mbox(db_session, "main", str(tmp_path), None) == 0


def test_settings_parsing():
    assert cli.accounts("main:Label=/a/b,other=/c") == [("main", "Label", "/a/b"), ("other", None, "/c")]
    with pytest.raises(SystemExit):
        cli.accounts("")
