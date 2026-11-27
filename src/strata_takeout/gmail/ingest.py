"""Load Gmail into the `gmail` schema, from an mbox (Google Takeout) or a gmvault backup (an IMAP-era export, see gmvault.py), idempotently: the source's fingerprint (size, mtime) and
the rule set's version are kept in `sources`; an unchanged one is skipped, a changed or new one (or new rules) replaces every row of its (account, source) in one
transaction, so a re-run never duplicates. `source` is `mbox` or `gmvault`; a message's `offset` is its byte offset in the mbox, or its Gmail id in a gmvault backup."""

import hashlib
import os
from datetime import UTC, datetime

from sqlalchemy import delete, insert, select
from sqlalchemy.orm import Session

from . import gmvault, parse, rules
from .models import Account, Attachment, Message, Source

SOURCE = "mbox"
GMVAULT = "gmvault"
BATCH = 2000


def find_mbox(tree: str) -> str | None:
    """`tree` itself when it is a file, else the largest `*.mbox` under it (Takeout names it `All mail Including Spam and Trash.mbox`)."""
    if os.path.isfile(tree):
        return tree
    hits = [os.path.join(d, f) for d, _, fs in os.walk(tree) for f in fs if f.lower().endswith(".mbox")]
    return max(hits, key=os.path.getsize) if hits else None


def _flush(session: Session, account: str, source: str, batch: list[tuple[dict, list[dict]]]) -> None:
    rows = [dict(account=account, source=source, **m) for m, _ in batch]
    session.execute(insert(Message), rows)
    if not any(a for _, a in batch):
        return
    ids = {
        o: i
        for o, i in session.execute(
            select(Message.offset, Message.id).where(
                Message.account == account,
                Message.source == source,
                Message.offset.in_([m["offset"] for m, _ in batch]),
            )
        )
    }
    session.execute(insert(Attachment), [dict(message_id=ids[m["offset"]], **a) for m, atts in batch for a in atts])


def load(session: Session, account: str, source: str, items) -> int:
    """Replace the rows of (account, source) with `items` (an iterator of (message row, attachment rows)); returns how many."""
    session.execute(
        delete(Attachment).where(
            Attachment.message_id.in_(select(Message.id).where(Message.account == account, Message.source == source))
        )
    )
    session.execute(delete(Message).where(Message.account == account, Message.source == source))
    n, batch = 0, []
    for item in items:
        batch.append(item)
        n += 1
        if len(batch) >= BATCH:
            _flush(session, account, source, batch)
            batch = []
    if batch:
        _flush(session, account, source, batch)
    return n


def gmvault_items(root: str, folder: str | None, verdicts: dict[int, dict] | None = None):
    """(row, attachments) of every message of a gmvault backup, oldest first. A message whose Message-ID was already loaded is skipped (an import that
    left copies); the ones with none are all kept."""
    from datetime import UTC
    from datetime import datetime as dt

    seen: set[str] = set()
    for it in gmvault.items(root):
        at = dt.fromtimestamp(it["internal_date"], UTC) if it["internal_date"] is not None else None
        row, atts = parse.message(
            it["gm_id"],
            it["raw"],
            folder,
            (verdicts or {}).get(it["gm_id"]),
            source_labels=it["labels"],
            fallback_at=at,
        )
        mid = row.get("message_id")
        if mid:
            if mid in seen:
                continue
            seen.add(mid)
        yield row, atts


def _ensure_account(session: Session, account: str, label: str | None) -> None:
    if session.get(Account, account) is None:
        session.add(Account(id=account, label=label))
        session.flush()
    elif label:
        a = session.get(Account, account)
        a.label = label


def _version(verdicts: str | None) -> str:
    vs = os.stat(verdicts) if verdicts else None
    # a new verdicts file reloads like new rules do (a short hash of its size and mtime keeps the column small)
    return rules.RULES + (f"+{hashlib.sha1(f'{vs.st_size}.{vs.st_mtime_ns}'.encode()).hexdigest()[:12]}" if vs else "")


def _record(
    session: Session, row: Source | None, account: str, source: str, fp: tuple[str, int, int], version: str, n: int
) -> None:
    path, size, mtime = fp
    now = datetime.now(UTC)
    if row is None:
        session.add(
            Source(
                account=account,
                source=source,
                path=path,
                size=size,
                mtime_ns=mtime,
                rules=version,
                rows=n,
                loaded_at=now,
            )
        )
    else:
        row.path, row.size, row.mtime_ns, row.rules, row.rows, row.loaded_at = path, size, mtime, version, n, now


def sync_gmvault(
    session: Session,
    account: str,
    tree: str,
    folder: str | None,
    label: str | None = None,
    force: bool = False,
    verdicts: str | None = None,
) -> int | None:
    """Rows loaded, or None when unchanged; 0 when `tree` holds no gmvault backup. The caller commits."""
    if not gmvault.detect(tree):
        return 0
    _ensure_account(session, account, label)
    fp, version = gmvault.fingerprint(tree), _version(verdicts)
    row = session.get(Source, (account, GMVAULT))
    if not force and row and (row.path, row.size, row.mtime_ns, row.rules) == (*fp, version):
        return None
    n = load(
        session, account, GMVAULT, gmvault_items(tree, folder, parse.read_verdicts(verdicts) if verdicts else None)
    )
    _record(session, row, account, GMVAULT, fp, version, n)
    return n


def sync_tree(
    session: Session,
    account: str,
    tree: str,
    folder: str | None,
    label: str | None = None,
    force: bool = False,
    verdicts: str | None = None,
) -> int | None:
    """`sync_gmvault` for a gmvault backup, else `sync_mbox`."""
    if gmvault.detect(tree):
        return sync_gmvault(session, account, tree, folder, label, force, verdicts)
    return sync_mbox(session, account, tree, folder, label, force, verdicts)


def sync_mbox(
    session: Session,
    account: str,
    tree: str,
    folder: str | None,
    label: str | None = None,
    force: bool = False,
    verdicts: str | None = None,
    keep_all: bool = False,
) -> int | None:
    """Rows loaded, or None when unchanged; 0 when `tree` holds no mbox. The caller commits."""
    path = find_mbox(tree)
    if path is None:
        return 0
    _ensure_account(session, account, label)
    st = os.stat(path)
    fp, version = (path, st.st_size, st.st_mtime_ns), _version(verdicts)
    row = session.get(Source, (account, SOURCE))
    if not force and row and (row.path, row.size, row.mtime_ns, row.rules) == (*fp, version):
        return None
    n = load(
        session, account, SOURCE, parse.messages(path, folder, parse.read_verdicts(verdicts) if verdicts else None)
    )
    _record(session, row, account, SOURCE, fp, version, n)
    return n
