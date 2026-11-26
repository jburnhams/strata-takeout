"""Load a Gmail mbox into the `gmail` schema, idempotently: the file's fingerprint (size, mtime) and the rule set's version are kept in `sources`; an
unchanged one is skipped, a changed or new one (or new rules) replaces every row of its (account, source) in one transaction, so a re-run never duplicates."""

import hashlib
import os
from datetime import UTC, datetime

from sqlalchemy import delete, insert, select
from sqlalchemy.orm import Session

from . import parse, rules
from .models import Account, Attachment, Message, Source

SOURCE = "mbox"
BATCH = 2000


def find_mbox(tree: str) -> str | None:
    """`tree` itself when it is a file, else the largest `*.mbox` under it (Takeout names it `All mail Including Spam and Trash.mbox`)."""
    if os.path.isfile(tree):
        return tree
    hits = [os.path.join(d, f) for d, _, fs in os.walk(tree) for f in fs if f.lower().endswith(".mbox")]
    return max(hits, key=os.path.getsize) if hits else None


def _flush(session: Session, account: str, batch: list[tuple[dict, list[dict]]]) -> None:
    rows = [dict(account=account, source=SOURCE, **m) for m, _ in batch]
    session.execute(insert(Message), rows)
    if not any(a for _, a in batch):
        return
    ids = {
        o: i
        for o, i in session.execute(
            select(Message.offset, Message.id).where(
                Message.account == account,
                Message.source == SOURCE,
                Message.offset.in_([m["offset"] for m, _ in batch]),
            )
        )
    }
    session.execute(insert(Attachment), [dict(message_id=ids[m["offset"]], **a) for m, atts in batch for a in atts])


def load(session: Session, account: str, path: str, folder: str | None, verdicts: dict[int, dict] | None = None) -> int:
    session.execute(
        delete(Attachment).where(
            Attachment.message_id.in_(select(Message.id).where(Message.account == account, Message.source == SOURCE))
        )
    )
    session.execute(delete(Message).where(Message.account == account, Message.source == SOURCE))
    n, batch = 0, []
    for item in parse.messages(path, folder, verdicts):
        batch.append(item)
        n += 1
        if len(batch) >= BATCH:
            _flush(session, account, batch)
            batch = []
    if batch:
        _flush(session, account, batch)
    return n


def sync_mbox(
    session: Session,
    account: str,
    tree: str,
    folder: str | None,
    label: str | None = None,
    force: bool = False,
    verdicts: str | None = None,
) -> int | None:
    """Rows loaded, or None when unchanged; 0 when `tree` holds no mbox. The caller commits."""
    path = find_mbox(tree)
    if path is None:
        return 0
    if session.get(Account, account) is None:
        session.add(Account(id=account, label=label))
        session.flush()
    elif label:
        a = session.get(Account, account)
        a.label = label
    st = os.stat(path)
    vs = os.stat(verdicts) if verdicts else None
    # a new verdicts file reloads like new rules do (a short hash of its size and mtime keeps the column small)
    version = rules.RULES + (
        f"+{hashlib.sha1(f'{vs.st_size}.{vs.st_mtime_ns}'.encode()).hexdigest()[:12]}" if vs else ""
    )
    row = session.get(Source, (account, SOURCE))
    if (
        not force
        and row
        and (row.path, row.size, row.mtime_ns, row.rules) == (path, st.st_size, st.st_mtime_ns, version)
    ):
        return None
    n = load(session, account, path, folder, parse.read_verdicts(verdicts) if verdicts else None)
    now = datetime.now(UTC)
    if row is None:
        session.add(
            Source(
                account=account,
                source=SOURCE,
                path=path,
                size=st.st_size,
                mtime_ns=st.st_mtime_ns,
                rules=version,
                rows=n,
                loaded_at=now,
            )
        )
    else:
        row.path, row.size, row.mtime_ns, row.rules, row.rows, row.loaded_at = (
            path,
            st.st_size,
            st.st_mtime_ns,
            version,
            n,
            now,
        )
    return n
