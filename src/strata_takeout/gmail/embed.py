"""Embeddings of the kept messages' bodies: chunk, ask the strata-models service for vectors, store them in `chunks`. Idempotent: a message is done when
`messages.embedded` equals "<model>:<chunker>" of the current run, so only what is new (or what a new model or chunker changed) is embedded, and a message
reloaded by the loader (its rows are replaced) is done again on the next run. Quiet when the service is down: the next run catches up."""

import logging

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from strata_takeout import embedding
from strata_takeout.vectors import nearest

from .models import Chunk, Message

log = logging.getLogger("strata_takeout.gmail.embed")
GROUP = 40  # messages embedded (and committed) together


def header(m: Message) -> str:
    """What is put before every piece of a message when it is embedded, so a piece knows who wrote it and what it was about."""
    return f"From: {m.from_name or m.from_addr}\nSubject: {m.subject}\nDate: {m.at.date() if m.at else ''}\n\n"


def current_tag(client=None) -> str:
    """ "<model>:<chunker>" of what the service would embed with now (one tiny request answers which model it serves)."""
    _, model = embedding.embed(["."], client=client)
    return f"{model}:{embedding.CHUNKER}"


def pending(session: Session, account: str | None, tag: str, limit: int | None):
    q = select(Message).where(Message.tier == "keep", Message.body_text.is_not(None), Message.body_text != "")
    if account:
        q = q.where(Message.account == account)
    q = q.where(Message.embedded.is_(None) | (Message.embedded != tag)).order_by(Message.id)
    return q.limit(limit) if limit else q


def embed_pending(
    session: Session, account: str | None = None, limit: int | None = None, client=None, commit=None
) -> dict[str, int]:
    """Embed the kept messages that have no (current) embedding. Returns {messages, chunks}. `commit` is called after each group (the caller's
    transaction control); without it the caller commits."""
    done = chunks = 0
    tag = current_tag(client)
    while True:
        rows = session.scalars(pending(session, account, tag, min(GROUP, limit - done) if limit else GROUP)).all()
        if not rows:
            break
        pieces: list[tuple[Message, int, int, int]] = []
        texts: list[str] = []
        for m in rows:
            body = m.body_text or ""
            for seq, (a, b) in enumerate(embedding.chunk(body)):
                text = embedding.prepare(body[a:b])
                if text:
                    pieces.append((m, seq, a, b))
                    texts.append(header(m) + text)
        vectors, model = embedding.embed(texts, client=client) if texts else ([], "")
        if model and f"{model}:{embedding.CHUNKER}" != tag:
            raise RuntimeError(f"the embedding model changed from {tag} to {model} during a run: run again")
        session.execute(delete(Chunk).where(Chunk.message_id.in_([m.id for m in rows])))
        session.add_all(
            Chunk(message_id=m.id, seq=seq, start=a, end=b, embedding=v)
            for (m, seq, a, b), v in zip(pieces, vectors, strict=True)
        )
        for m in rows:
            m.embedded = tag  # a body with nothing to embed is done too (no pieces)
        session.flush()
        done += len(rows)
        chunks += len(pieces)
        if commit:
            commit()
        if limit and done >= limit:
            break
    return dict(messages=done, chunks=chunks)


def counts(session: Session, account: str | None = None) -> dict[str, int]:
    q = (
        select(func.count())
        .select_from(Message)
        .where(Message.tier == "keep", Message.body_text.is_not(None), Message.body_text != "")
    )
    if account:
        q = q.where(Message.account == account)
    total = session.scalar(q) or 0
    emb = session.scalar(q.where(Message.embedded.is_not(None))) or 0
    return dict(kept_with_body=total, embedded=emb, chunks=session.scalar(select(func.count()).select_from(Chunk)) or 0)


def search(session: Session, query: str, k: int = 10, client=None, account: str | None = None):
    """The k closest pieces to `query` as (message, chunk, distance), closest first (cosine distance, 0 = same direction)."""
    vec, _ = embedding.embed([query], client=client)
    sel = select(Chunk).join(Message, Message.id == Chunk.message_id)
    where = [Message.account == account] if account else []
    out = []
    for chunk, dist in nearest(session, Chunk.embedding, vec[0], k=k, where=where, select=sel):
        out.append((session.get(Message, chunk.message_id), chunk, dist))
    return out
