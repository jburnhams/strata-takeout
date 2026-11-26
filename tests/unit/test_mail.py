"""Mail loader: a synthetic Gmail-style mbox (made up here, no real data) into the `mail` schema."""

import hashlib

import pytest
from omarchy_db.testing import test_migrations, test_schema_matches_models  # noqa: F401
from sqlalchemy import func, select

from strata_takeout.mail import cli, ingest, parse, rules
from strata_takeout.mail.models import Attachment, Message, Source

pytestmark = pytest.mark.db_app("mail")

PDF = b"%PDF-1.4 synthetic"


def msg(
    frm, to, subject, labels, extra="", body="hello world", date="Mon, 03 Jul 2017 10:00:00 +0000", att=False, mid=None
):
    head = (
        f"From {frm} Mon Jul  3 10:00:00 2017\n"
        f"From: <{frm}>\nTo: {to}\nSubject: {subject}\nDate: {date}\nMessage-ID: <{mid or subject}@x>\n"
        f"X-Gmail-Labels: {labels}\n{extra}"
    )
    if not att:
        return head + f"Content-Type: text/plain; charset=utf-8\n\n{body}\n\n"
    import base64

    b64 = base64.encodebytes(PDF).decode()
    return (
        head + 'MIME-Version: 1.0\nContent-Type: multipart/mixed; boundary="B"\n\n'
        f"--B\nContent-Type: text/plain\n\n{body}\n--B\nContent-Type: application/pdf; name=a.pdf\n"
        f"Content-Disposition: attachment; filename=a.pdf\nContent-Transfer-Encoding: base64\n\n{b64}--B--\n\n"
    )


@pytest.fixture
def mbox(tmp_path):
    text = "".join(
        [
            msg("me@example.org", "friend@example.org", "Hi", "Sent", att=True),  # keep: sent, with an attachment
            msg("friend@example.org", "me@example.org", "Re: Hi", "Inbox"),  # keep: the sender was written to
            msg(
                "shop@example.org", "me@example.org", "Sale", "Category Promotions", "List-Unsubscribe: <mailto:u@x>\n"
            ),  # drop
            msg("shop@example.org", "me@example.org", "Receipt", "Category Purchases,Inbox"),  # keep by label
            msg("stranger@example.org", "me@example.org", "Hello?", "Inbox"),  # undecided
            msg(
                "bank@example.org", "me@example.org", "Both", "Category Purchases", "Precedence: bulk\n"
            ),  # conflict: keep wins
        ]
    )
    p = tmp_path / "all.mbox"
    p.write_text(text)
    return p


def test_scan_splits_messages_and_read_at_drops_separator(mbox):
    with open(mbox, "rb") as f:
        spans = list(parse.scan(f))  # collected first: read_at seeks the same handle
        assert len(spans) == 6 and spans[0][0] == 0
        first = parse.read_at(f, *spans[0])
    assert first.startswith(b"From: <me@example.org>")


def test_headers_labels_and_flags():
    h = parse.headers(
        msg("A@Example.org", "b@x.org, C@x.org", "Sub", 'Sent,"Odd, label"', "Precedence: bulk\n")
        .split("\n", 1)[1]
        .encode()
    )
    assert h["from_addr"] == "a@example.org" and h["to_addrs"] == ["b@x.org", "c@x.org"]
    assert h["labels"] == ["Sent", "Odd, label"] and h["is_sent"] and h["is_bulk"]
    assert h["sent_at"].year == 2017
    assert parse.headers(b"Subject: x\nDate: not a date\n\nbody")["sent_at"] is None


def test_rules_keep_wins_conflicts_and_undecided():
    base = dict(is_sent=False, labels=[], from_addr="s@x", has_unsubscribe=False, is_bulk=False)
    r = rules.Rules(replied_to=frozenset({"friend@x"}))
    assert rules.decide({**base, "is_sent": True}, r)[0] == "keep"
    assert rules.decide({**base, "from_addr": "friend@x"}, r)[0] == "keep"
    assert rules.decide({**base, "labels": ["Category Purchases"], "is_bulk": True}, r) == (
        "keep",
        "keep-label:Category Purchases",
    )
    assert rules.decide({**base, "has_unsubscribe": True}, r)[0] == "drop"
    assert rules.decide({**base, "labels": ["Spam"]}, r)[0] == "drop"
    assert rules.decide(base, r)[0] == "undecided"


def test_body_text_and_attachment(mbox):
    with open(mbox, "rb") as f:
        spans = list(parse.scan(f))
        text, files = parse.body(parse.read_at(f, *spans[0]))
    assert text == "hello world" and files[0]["filename"] == "a.pdf" and files[0]["data"] == PDF


def test_sync_loads_decides_fills_and_is_idempotent(db_session, mbox, tmp_path):
    root = str(tmp_path / "att")
    res = ingest.sync_account(db_session, "main", str(mbox), root, "Main")
    assert res == dict(messages=6, keep=4, drop=1, undecided=1, filled=4)
    db_session.flush()
    rows = {m.subject: m for m in db_session.scalars(select(Message))}
    assert rows["Hi"].decision == "keep" and rows["Hi"].body_text == "hello world" and rows["Hi"].is_sent
    assert rows["Re: Hi"].decided_by == "replied-to-sender"
    assert rows["Sale"].decision == "drop" and rows["Sale"].body_text is None  # no body for dropped mail
    assert rows["Hello?"].decision == "undecided" and rows["Hello?"].body_text is None
    att = db_session.scalar(select(Attachment))
    assert att.sha256 == hashlib.sha256(PDF).hexdigest() and (tmp_path / "att" / att.path).read_bytes() == PDF
    assert ingest.sync_account(db_session, "main", str(mbox), root) is None  # unchanged: skipped
    assert ingest.sync_account(db_session, "main", str(mbox), root, force=True) == res
    db_session.flush()
    assert db_session.scalar(select(func.count()).select_from(Message)) == 6  # replaced, never doubled
    assert db_session.scalar(select(func.count()).select_from(Attachment)) == 1
    assert db_session.get(Source, ("main", ingest.SOURCE)).rows == 6


def test_fill_serves_rows_marked_keep_later(db_session, mbox):
    ingest.sync_account(db_session, "main", str(mbox), None)
    db_session.flush()
    m = db_session.scalar(select(Message).where(Message.subject == "Hello?"))
    m.decision, m.decided_by = "keep", "classifier"
    db_session.flush()
    assert ingest.fill(db_session, "main", ingest.SOURCE, str(mbox), None) == 1
    db_session.flush()
    assert db_session.scalar(select(Message.body_text).where(Message.subject == "Hello?")) == "hello world"


def test_account_specs(tmp_path):
    assert cli.accounts("a:Main=/m/a.mbox,b=/m/b.mbox") == [("a", "Main", "/m/a.mbox"), ("b", None, "/m/b.mbox")]
    assert cli.attachments("a=/x") == {"a": "/x"}
    with pytest.raises(SystemExit):
        cli.accounts("")
    with pytest.raises(ValueError):
        cli.accounts("nonsense")
