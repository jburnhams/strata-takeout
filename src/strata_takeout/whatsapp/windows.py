"""Conversation windows and their embeddings. A window is a run of text messages in one chat, split where two are more than GAP_MIN minutes apart
and kept under MAX_CHARS (a long run becomes several windows). It is embedded as the conversation reads: `[2019-04-02 18:30] Name: text` lines, `Me` for
the account's owner, the contact's name where the backup has one and `Person N` (numbered per chat) where it does not, so no phone number is embedded.

`rebuild` recomputes the windows of an account's source from its messages after a load, keeping the vectors of windows whose text is unchanged;
`embed_pending` then embeds the rest. Quiet when the service is down: the next run catches up."""

import hashlib
from collections import defaultdict
from datetime import timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from strata_takeout import embedding
from strata_takeout.vectors import nearest

from .models import Contact, Message, Window

GAP_MIN = 30
MAX_CHARS = 1600
MIN_CHARS = 60  # shorter than this says too little to find
GROUP = 64


def names(session: Session, account: str, source: str) -> dict[str, str]:
    return {
        r.jid: (r.display_name or r.wa_name or "")
        for r in session.scalars(select(Contact).where(Contact.account == account, Contact.source == source))
    }


def lines(rows: list[Message], contacts: dict[str, str]) -> list[str]:
    anon: dict[str, str] = {}
    out = []
    for m in rows:
        if m.from_me:
            who = "Me"
        else:
            jid = m.sender_jid or m.chat_jid
            who = contacts.get(jid) or anon.setdefault(jid, f"Person {len(anon) + 1}")
        out.append(f"[{m.at:%Y-%m-%d %H:%M}] {who}: {m.text}")
    return out


def spans(rows: list[Message], contacts: dict[str, str]):
    """(first message index, last index, text) of each window of one chat's text messages, in time order."""
    cur: list[int] = []
    size = 0
    last = None
    all_lines = lines(rows, contacts)
    for i, m in enumerate(rows):
        gap = last is not None and (m.at - last) > timedelta(minutes=GAP_MIN)
        if cur and (gap or size + len(all_lines[i]) > MAX_CHARS):
            yield cur[0], cur[-1], "\n".join(all_lines[cur[0] : cur[-1] + 1])
            cur, size = [], 0
        cur.append(i)
        size += len(all_lines[i]) + 1
        last = m.at
    if cur:
        yield cur[0], cur[-1], "\n".join(all_lines[cur[0] : cur[-1] + 1])


def chat_rows(session: Session, account: str, source: str, chat_jid: str) -> list[Message]:
    return list(
        session.scalars(
            select(Message)
            .where(
                Message.account == account,
                Message.source == source,
                Message.chat_jid == chat_jid,
                Message.kind == "text",
                Message.text.is_not(None),
                Message.text != "",
                Message.at.is_not(None),
            )
            .order_by(Message.at, Message.row_id)
        )
    )


def rebuild(session: Session, account: str, source: str) -> int:
    """Recompute the windows of (account, source); returns how many. A window with the same chat, span and text keeps its vector."""
    keep = {
        (w.chat_jid, w.first_row_id, w.last_row_id, w.text_sha): (w.embedded, w.embedding)
        for w in session.scalars(select(Window).where(Window.account == account, Window.source == source))
        if w.embedded and w.embedding is not None
    }
    session.execute(delete(Window).where(Window.account == account, Window.source == source))
    contacts = names(session, account, source)
    chats = session.scalars(
        select(Message.chat_jid)
        .where(Message.account == account, Message.source == source, Message.kind == "text")
        .distinct()
    ).all()
    n = 0
    for chat in chats:
        rows = chat_rows(session, account, source, chat)
        for a, b, text in spans(rows, contacts):
            if len(text) < MIN_CHARS:
                continue
            sha = hashlib.sha1(text.encode()).hexdigest()
            emb = keep.get((chat, rows[a].row_id, rows[b].row_id, sha))
            at, at2 = rows[a].at, rows[b].at  # chat_rows only returns messages with a time
            session.add(
                Window(
                    account=account, source=source, chat_jid=chat, first_row_id=rows[a].row_id, last_row_id=rows[b].row_id,
                    start_at=at, end_at=at2, n_messages=b - a + 1, chars=len(text), text_sha=sha,
                    embedded=emb[0] if emb else None, embedding=emb[1] if emb else None,
                )
            )  # fmt: skip
            n += 1
    session.flush()
    return n


def window_text(session: Session, w: Window, contacts: dict[str, str], cache: dict) -> str:
    key = (w.account, w.source, w.chat_jid)
    if key not in cache:
        cache.clear()  # one chat at a time
        cache[key] = chat_rows(session, w.account, w.source, w.chat_jid)
    rows = [m for m in cache[key] if w.first_row_id <= m.row_id <= w.last_row_id]
    return "\n".join(lines(rows, contacts))


def current_tag(client=None) -> str:
    _, model = embedding.embed(["."], client=client)
    return f"{model}:{embedding.CHUNKER}"


def embed_pending(
    session: Session, account: str | None = None, limit: int | None = None, client=None, commit=None
) -> dict[str, int]:
    tag = current_tag(client)
    done = 0
    cache: dict = {}
    contacts: dict[tuple[str, str], dict[str, str]] = {}
    while True:
        q = select(Window).where(Window.embedded.is_(None) | (Window.embedded != tag))
        if account:
            q = q.where(Window.account == account)
        rows = session.scalars(
            q.order_by(Window.account, Window.source, Window.chat_jid, Window.start_at).limit(
                min(GROUP, limit - done) if limit else GROUP
            )
        ).all()
        if not rows:
            break
        texts = []
        for w in rows:
            c = contacts.setdefault((w.account, w.source), names(session, w.account, w.source))
            texts.append(window_text(session, w, c, cache))
        vectors, model = embedding.embed([embedding.prepare(t) for t in texts], client=client)
        if f"{model}:{embedding.CHUNKER}" != tag:
            raise RuntimeError(f"the embedding model changed from {tag} to {model} during a run: run again")
        for w, v in zip(rows, vectors, strict=True):
            w.embedding, w.embedded = v, tag
        session.flush()
        done += len(rows)
        if commit:
            commit()
        if limit and done >= limit:
            break
    return dict(windows=done)


def counts(session: Session, account: str | None = None) -> dict[str, int]:
    q = select(func.count()).select_from(Window)
    if account:
        q = q.where(Window.account == account)
    return dict(windows=session.scalar(q) or 0, embedded=session.scalar(q.where(Window.embedded.is_not(None))) or 0)


def search(session: Session, query: str, k: int = 10, client=None, account: str | None = None):
    """The k closest windows to `query` as (window, text, distance), closest first."""
    vec, _ = embedding.embed([query], client=client)
    where = [Window.account == account] if account else []
    contacts: dict[tuple[str, str], dict[str, str]] = defaultdict(dict)
    out = []
    cache: dict = {}
    for w, dist in nearest(session, Window.embedding, vec[0], k=k, where=[Window.embedding.is_not(None), *where]):
        c = contacts[(w.account, w.source)] or names(session, w.account, w.source)
        contacts[(w.account, w.source)] = c
        out.append((w, window_text(session, w, c, cache), dist))
    return out
