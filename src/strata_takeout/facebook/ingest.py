"""Load a Facebook export tree into the `facebook` schema, idempotently: the JSON files' fingerprint (count, total size, newest mtime) is kept in
`sources`; an unchanged tree is skipped, a changed or new one replaces every row of its (account, source) in one transaction, so a re-run never
duplicates. Attachment files are looked up under the tree root by the path the JSON gives: size and SHA-256 are filled in when the file exists."""

import hashlib
import mimetypes
import os
from datetime import UTC, datetime

from sqlalchemy import delete, insert, select
from sqlalchemy.orm import Session

from . import parse
from .models import Account, Attachment, Member, Message, Reaction, Source, Thread

SOURCE = "tree"
BATCH = 2000


def fingerprint(tree: str) -> tuple[int, int, int]:
    files = parse.json_files(tree)
    sts = [os.stat(f) for f in files]
    return len(files), sum(s.st_size for s in sts), max((s.st_mtime_ns for s in sts), default=0)


class Files:
    """size/mime/sha256 of attachment files, each path once, never outside the tree."""

    def __init__(self, tree: str) -> None:
        self.root = os.path.realpath(tree)
        self.cache: dict[str, tuple[int | None, str | None, str | None]] = {}

    def info(self, rel: str | None) -> tuple[int | None, str | None, str | None]:
        if not rel:
            return None, None, None
        if rel in self.cache:
            return self.cache[rel]
        p = os.path.realpath(os.path.join(self.root, rel))
        mime = mimetypes.guess_type(rel)[0]
        if not p.startswith(self.root + os.sep) or not os.path.isfile(p):
            r = (None, mime, None)
        else:
            h = hashlib.sha256()
            with open(p, "rb") as f:
                for b in iter(lambda: f.read(1 << 20), b""):
                    h.update(b)
            r = (os.path.getsize(p), mime, h.hexdigest())
        self.cache[rel] = r
        return r


def _flush(session: Session, account: str, batch: list[dict], files: Files) -> None:
    rows = [
        {k: v for k, v in m.items() if k not in ("reactions", "attachments")} | dict(account=account, source=SOURCE)
        for m in batch
    ]
    session.execute(insert(Message), rows)
    if not any(m["reactions"] or m["attachments"] for m in batch):
        return
    ids = {
        e: i
        for e, i in session.execute(
            select(Message.ext_id, Message.id).where(
                Message.account == account, Message.source == SOURCE, Message.ext_id.in_([m["ext_id"] for m in batch])
            )
        )
    }
    reacts = [
        dict(account=account, source=SOURCE, message_id=ids[m["ext_id"]], target_title=None, **r)
        for m in batch
        for r in m["reactions"]
    ]
    if reacts:
        session.execute(insert(Reaction), reacts)
    atts = []
    for m in batch:
        for i, a in enumerate(m["attachments"]):
            size, mime, sha = files.info(a["path"])
            atts.append(dict(message_id=ids[m["ext_id"]], seq=i, size=size, mime=mime, sha256=sha, **a))
    if atts:
        session.execute(insert(Attachment), atts)


def load(session: Session, account: str, tree: str) -> int:
    session.execute(delete(Reaction).where(Reaction.account == account, Reaction.source == SOURCE))
    session.execute(
        delete(Attachment).where(
            Attachment.message_id.in_(select(Message.id).where(Message.account == account, Message.source == SOURCE))
        )
    )
    for t in (Message, Member, Thread):
        session.execute(delete(t).where(t.account == account, t.source == SOURCE))
    files, ids, batch, n = Files(tree), parse.Ids(), [], 0

    def push(m: dict) -> None:
        nonlocal batch, n
        batch.append(m)
        n += 1
        if len(batch) >= BATCH:
            _flush(session, account, batch, files)
            batch = []

    for t, names, msgs in parse.threads(tree, ids):
        session.execute(insert(Thread), [dict(account=account, source=SOURCE, **t)])
        if names:
            session.execute(
                insert(Member),
                [dict(account=account, source=SOURCE, thread_id=t["thread_id"], name=x) for x in sorted(set(names))],
            )
        for m in msgs:
            push(m)
    for m in parse.activity(tree, ids):
        push(m)
    if batch:
        _flush(session, account, batch, files)
    lk = [dict(account=account, source=SOURCE, message_id=None, **r) for r in parse.likes(tree)]
    for i in range(0, len(lk), BATCH):
        session.execute(insert(Reaction), lk[i : i + BATCH])
    return n + len(lk)


def sync_tree(session: Session, account: str, tree: str, label: str | None = None, force: bool = False) -> int | None:
    """Rows loaded, or None when unchanged; 0 when `tree` holds none of the export. The caller commits."""
    count, size, mtime = fingerprint(tree)
    if count == 0:
        return 0
    acc = session.get(Account, account)
    if acc is None:
        session.add(Account(id=account, label=label))
        session.flush()
    elif label:
        acc.label = label
    row = session.get(Source, (account, SOURCE))
    if not force and row and (row.path, row.size, row.mtime_ns) == (tree, size, mtime):
        return None
    n = load(session, account, tree)
    now = datetime.now(UTC)
    if row is None:
        session.add(Source(account=account, source=SOURCE, path=tree, size=size, mtime_ns=mtime, rows=n, loaded_at=now))
    else:
        row.path, row.size, row.mtime_ns, row.rows, row.loaded_at = tree, size, mtime, n, now
    return n
