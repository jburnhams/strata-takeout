"""Load a Gmail Takeout mbox into the `mail` schema, idempotently, in three streaming steps:

  1. scan: every message's header block becomes a metadata row (all of them, 20 GB of mbox stay out of memory);
  2. decide: `rules` marks each row keep / drop / undecided (the addresses the owner wrote to need the whole scan first);
  3. fill: for every kept row without a body, the message is read back by its byte offset, its text stored and its
     attachments written under the attachments root by hash.

An unchanged mbox (size, mtime) is skipped; a changed one replaces its (account, source) rows in one transaction. `fill`
alone (`refill`) serves rows a classifier has since moved to keep, without rescanning. The original mbox is never touched."""

import hashlib
import os
from datetime import UTC, datetime

from sqlalchemy import delete, insert, select, update
from sqlalchemy.orm import Session

from . import parse, rules
from .models import Account, Attachment, Message, Source

SOURCE = "gmail-mbox"
BATCH = 2000
MAX_ATTACHMENT = 100 << 20  # bigger files are recorded (name, size, hash) but not written


def store_file(root: str, data: bytes) -> tuple[str, str]:
    """(sha256, path relative to root); content-addressed, so a repeated file is written once and a rerun rewrites nothing."""
    sha = hashlib.sha256(data).hexdigest()
    rel = os.path.join(sha[:2], sha[2:4], sha)
    dest = os.path.join(root, rel)
    if not os.path.exists(dest):
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        tmp = f"{dest}.tmp{os.getpid()}"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, dest)
    return sha, rel


def fill(session: Session, account: str, source: str, mbox: str, root: str | None, limit: int | None = None) -> int:
    """Body and attachments for kept messages that have none yet. Returns how many were filled."""
    q = (
        select(Message.id, Message.mbox_offset, Message.mbox_size)
        .where(
            Message.account == account,
            Message.source == source,
            Message.decision == "keep",
            Message.body_text.is_(None),
        )
        .order_by(Message.mbox_offset)
    )
    if limit:
        q = q.limit(limit)
    rows = session.execute(q).all()
    n = 0
    with open(mbox, "rb") as f:
        for pk, off, size in rows:
            text, files = parse.body(parse.read_at(f, off, size))
            session.execute(delete(Attachment).where(Attachment.message_pk == pk))
            atts = []
            for a in files:
                data = a.pop("data")
                if root is None or len(data) > MAX_ATTACHMENT:
                    atts.append(
                        dict(
                            message_pk=pk,
                            filename=a["filename"],
                            mime=a["mime"][:128],
                            size=len(data),
                            sha256=hashlib.sha256(data).hexdigest(),
                            path=None,
                        )
                    )
                else:
                    sha, rel = store_file(root, data)
                    atts.append(
                        dict(
                            message_pk=pk,
                            filename=a["filename"],
                            mime=a["mime"][:128],
                            size=len(data),
                            sha256=sha,
                            path=rel,
                        )
                    )
            if atts:
                session.execute(insert(Attachment), atts)
            session.execute(
                update(Message).where(Message.id == pk).values(body_text=text)
            )  # '' when empty, so it is not refilled
            n += 1
    return n


def load(session: Session, account: str, source: str, mbox: str, rule: rules.Rules | None = None) -> dict[str, int]:
    session.execute(
        delete(Attachment).where(
            Attachment.message_pk.in_(select(Message.id).where(Message.account == account, Message.source == source))
        )
    )
    session.execute(delete(Message).where(Message.account == account, Message.source == source))
    rows: list[dict] = []
    with open(mbox, "rb") as scanner, open(mbox, "rb") as f:  # read_at seeks, which would derail the scan: two handles
        for off, size in parse.scan(scanner):
            raw = parse.read_at(f, off, size)
            h = parse.headers(raw)
            text_b, att_b = parse.sizes(raw)
            rows.append(
                dict(
                    account=account,
                    source=source,
                    mbox_offset=off,
                    mbox_size=size,
                    text_bytes=text_b,
                    attachment_bytes=att_b,
                    **h,
                )
            )
    rule = rule or rules.Rules(replied_to=rules.replied_to(rows))
    counts = {"keep": 0, "drop": 0, "undecided": 0}
    for r in rows:
        r["decision"], r["decided_by"] = rules.decide(r, rule)
        counts[r["decision"]] += 1
    for i in range(0, len(rows), BATCH):
        session.execute(insert(Message), rows[i : i + BATCH])
    return dict(messages=len(rows), **counts)


def sync_account(
    session: Session, account: str, mbox: str, root: str | None, label: str | None = None, force: bool = False
) -> dict[str, int] | None:
    """Counts of what was loaded, None when unchanged, {} when `mbox` is missing. The caller commits."""
    if not os.path.isfile(mbox):
        return {}
    st = os.stat(mbox)
    acct = session.get(Account, account)
    if acct is None:
        session.add(Account(id=account, label=label))
        session.flush()
    elif label:
        acct.label = label
    prev = session.get(Source, (account, SOURCE))
    if not force and prev and (prev.size, prev.mtime_ns, prev.path) == (st.st_size, st.st_mtime_ns, mbox):
        return None
    counts = load(session, account, SOURCE, mbox)
    session.flush()
    counts["filled"] = fill(session, account, SOURCE, mbox, root)
    now = datetime.now(UTC)
    if prev is None:
        session.add(
            Source(
                account=account,
                source=SOURCE,
                path=mbox,
                size=st.st_size,
                mtime_ns=st.st_mtime_ns,
                rows=counts["messages"],
                loaded_at=now,
            )
        )
    else:
        prev.path, prev.size, prev.mtime_ns, prev.rows, prev.loaded_at = (
            mbox,
            st.st_size,
            st.st_mtime_ns,
            counts["messages"],
            now,
        )
    return counts
