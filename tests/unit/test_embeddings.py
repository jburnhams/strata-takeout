"""Embeddings of what the loaders keep: chunking, the Gmail chunks table and the WhatsApp conversation windows, against a fake strata-models client
(vectors hashed from the words, so a search finds what shares words)."""

import hashlib
import re
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from strata_takeout import embedding
from strata_takeout.gmail import embed as gembed
from strata_takeout.gmail.models import Account as GAccount
from strata_takeout.gmail.models import Chunk, Message
from strata_takeout.whatsapp import windows
from strata_takeout.whatsapp.models import Account as WAccount
from strata_takeout.whatsapp.models import Contact, Window
from strata_takeout.whatsapp.models import Message as WMessage

pytestmark = pytest.mark.db_app("gmail")
DIM = 1024


def vec(text):
    v = [0.0] * DIM
    for w in re.findall(r"[a-z]{3,}", text.lower()):
        v[int(hashlib.md5(w.encode()).hexdigest(), 16) % DIM] += 1.0
    n = sum(x * x for x in v) ** 0.5 or 1.0
    return [x / n for x in v]


class FakeClient:
    """strata_models_client's run_many, answering from `vec`; `model` is what the service says it serves."""

    def __init__(self, model="fake-embed"):
        self.model, self.calls = model, []

    def run_many(self, role, items, app, priority="normal", model=None, options=None, batch=None, timeout=None):
        assert role == "text-embed"
        self.calls.append([len(i["texts"]) for i in items])
        return [dict(vectors=[vec(t) for t in i["texts"]], model=self.model, dim=DIM) for i in items]


def test_chunking_packs_paragraphs_and_cuts_long_ones():
    assert embedding.chunk("") == [] and embedding.chunk("   \n\n ") == []
    t = "Hello there.\n\n" + "word " * 400 + "\n\nShort tail."
    spans = embedding.chunk(t)
    assert spans[0][0] == 0 and spans[-1][1] == len(t) and all(b - a <= 1500 for a, b in spans) and len(spans) == 2
    many = "\n\n".join(f"paragraph {i} " * 20 for i in range(30))
    pieces = embedding.chunk(many)
    assert len(pieces) > 3 and all(b - a <= embedding.MAX_CHARS for a, b in pieces)
    assert all(pieces[i][1] < pieces[i + 1][0] for i in range(len(pieces) - 1))  # in order, no overlap
    assert embedding.chunk("a\n\nb\n\nc") == [(0, 7)]  # tiny paragraphs share a piece


def test_prepare_drops_quotes_and_shortens_links():
    assert embedding.prepare("> old\nkeep https://x.example/a?b=1 here\n\n\n\nend") == "keep [link] here\n\nend"


def test_embed_batches_and_reports_the_model():
    fc = FakeClient()
    vs, model = embedding.embed([f"t{i}" for i in range(50)], batch=24, client=fc)
    assert len(vs) == 50 and model == "fake-embed" and fc.calls == [[24, 24, 2]]
    assert embedding.embed([], client=fc) == ([], "")


def keep_message(session, i, body, subject="Subject", tier="keep"):
    m = Message(
        account="main", source="mbox", offset=i, size=10, subject=subject, from_addr=f"a{i}@example.org", tier=tier, body_text=body,
        at=datetime(2020, 1, 1, tzinfo=UTC),
    )  # fmt: skip
    session.add(m)
    return m


def test_gmail_embeds_only_kept_bodies_once(db_session):
    db_session.add(GAccount(id="main"))
    keep_message(db_session, 1, "Flights to Lisbon are booked for the third of May.")
    keep_message(db_session, 2, "Quarterly invoice for the garden shed and roof repairs.")
    keep_message(db_session, 3, "advert advert advert", tier="other")
    keep_message(db_session, 4, "")
    db_session.flush()
    fc = FakeClient()
    res = gembed.embed_pending(db_session, client=fc)
    assert res == dict(messages=2, chunks=2)
    tagged = {m.id for m in db_session.scalars(select(Message).where(Message.embedded.is_not(None)))}
    assert len(tagged) == 2 and db_session.scalar(select(func.count()).select_from(Chunk)) == 2
    calls = len(fc.calls)
    assert gembed.embed_pending(db_session, client=fc) == dict(
        messages=0, chunks=0
    )  # nothing new: only the model probe is asked
    assert len(fc.calls) == calls + 1
    assert gembed.counts(db_session) == dict(kept_with_body=2, embedded=2, chunks=2)
    hits = gembed.search(db_session, "when are the flights to Lisbon", k=2, client=fc)
    assert hits[0][0].offset == 1 and hits[0][2] < hits[1][2]
    other = FakeClient(model="other-model")  # a different model redoes everything
    assert gembed.embed_pending(db_session, client=other)["messages"] == 2
    assert db_session.scalar(select(func.count()).select_from(Chunk)) == 2  # replaced, not doubled


def test_gmail_limit_and_reload(db_session):
    db_session.add(GAccount(id="main"))
    for i in range(5):
        keep_message(db_session, i, f"message number {i} about something")
    db_session.flush()
    fc = FakeClient()
    assert gembed.embed_pending(db_session, limit=2, client=fc)["messages"] == 2
    assert gembed.embed_pending(db_session, client=fc)["messages"] == 3


def wa_setup(session):
    session.add_all(
        [WAccount(id="main"), Contact(account="main", source="android", jid="p1@s.whatsapp.net", display_name="Alex")]
    )
    t0 = datetime(2019, 4, 2, 18, 0, tzinfo=UTC)
    rows = [
        ("c@g.us", 0, "p1@s.whatsapp.net", t0, "are we still on for the climbing wall on saturday"),
        ("c@g.us", 1, None, t0 + timedelta(minutes=2), "yes I booked the slot for ten o'clock"),
        ("c@g.us", 0, "p2@s.whatsapp.net", t0 + timedelta(minutes=3), "great see you both there then"),
        ("c@g.us", 0, "p1@s.whatsapp.net", t0 + timedelta(hours=5), "the plumber is coming on tuesday afternoon about the boiler"),
        ("c@g.us", 1, None, t0 + timedelta(hours=5, minutes=1), "thanks for letting me know about the boiler"),
        ("d@s.whatsapp.net", 0, None, t0, "ok"),
    ]  # fmt: skip
    for i, (chat, me, who, at, text) in enumerate(rows, 1):
        session.add(
            WMessage(
                account="main",
                source="android",
                row_id=i,
                chat_jid=chat,
                from_me=bool(me),
                sender_jid=who,
                at=at,
                kind="text",
                text=text,
            )
        )
    session.add(WMessage(account="main", source="android", row_id=9, chat_jid="c@g.us", at=t0, kind="image", text=None))
    session.flush()


@pytest.mark.db_app("whatsapp")
def test_whatsapp_windows_split_on_gaps_and_embed(db_session):
    wa_setup(db_session)
    assert (
        windows.rebuild(db_session, "main", "android") == 2
    )  # two conversations in c@g.us; the one-word chat is too short
    ws = db_session.scalars(select(Window).order_by(Window.start_at)).all()
    assert [w.n_messages for w in ws] == [3, 2]
    fc = FakeClient()
    assert windows.embed_pending(db_session, client=fc) == dict(windows=2)
    text = windows.window_text(db_session, ws[0], {"p1@s.whatsapp.net": "Alex"}, {})
    assert (
        "Alex: are we still on" in text
        and "Me: yes I booked" in text
        and "Person 1: great see you" in text
        and "@" not in text
    )
    hits = windows.search(db_session, "the plumber and the boiler", k=2, client=fc)
    assert hits[0][0].id == ws[1].id
    assert windows.rebuild(db_session, "main", "android") == 2  # a reload keeps the vectors of unchanged windows
    again = db_session.scalars(select(Window).order_by(Window.start_at)).all()
    assert all(w.embedding is not None and w.embedded for w in again)
    assert windows.embed_pending(db_session, client=fc) == dict(windows=0)
    assert windows.counts(db_session) == dict(windows=2, embedded=2)
