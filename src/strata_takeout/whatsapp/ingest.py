"""Load a decrypted Android `msgstore.db` (and `wa.db`) into the `whatsapp` schema, idempotently: the files' fingerprint
(size, mtime, the media folders and how many files they hold) is kept in `sources`; an unchanged one is skipped, a
changed or new one replaces every row of its (account, source) in one transaction, so a re-run never duplicates."""

import glob
import hashlib
import os
from datetime import UTC, datetime

from sqlalchemy import delete, insert
from sqlalchemy.orm import Session

from . import parse
from .models import Account, Chat, Contact, Message, Participant, Source

SOURCE = "android-msgstore"
BATCH = 5000


def find_db(tree: str, name: str = "msgstore.db") -> str | None:
    """`tree` itself when it is the file, else the shallowest `name` under it (a backup is often unpacked into a subfolder)."""
    if os.path.isfile(tree):
        return tree if os.path.basename(tree) == name else None
    hits = sorted(
        glob.glob(os.path.join(glob.escape(tree), "**", name), recursive=True), key=lambda p: (p.count(os.sep), p)
    )
    return hits[0] if hits else None


def load(
    session: Session, account: str, source: str, db: str, wa_db: str | None, found: dict[str, str]
) -> dict[str, int]:
    for t in (Message, Chat, Contact, Participant):
        session.execute(delete(t).where(t.account == account, t.source == source))
    con = parse.open_ro(db)
    try:
        n, batch = 0, []
        chats: dict[str, dict] = {}
        for m in parse.messages(con, found):
            batch.append(dict(account=account, source=source, **m))
            c = chats.setdefault(m["chat_jid"], dict(first_at=None, last_at=None, message_count=0))
            c["message_count"] += 1
            if m["at"]:
                c["first_at"] = min(filter(None, [c["first_at"], m["at"]]))
                c["last_at"] = max(filter(None, [c["last_at"], m["at"]]))
            n += 1
            if len(batch) >= BATCH:
                session.execute(insert(Message), batch)
                batch = []
        if batch:
            session.execute(insert(Message), batch)
        subj = parse.subjects(con)
        session.execute(
            insert(Chat),
            [
                dict(account=account, source=source, jid=j, subject=subj.get(j), is_group=j.endswith("@g.us"), **c)
                for j, c in chats.items()
            ],
        ) if chats else None
        people = {p["jid"] + "\0" + p["group_jid"]: p for p in parse.participants(con)}
        if people:
            session.execute(insert(Participant), [dict(account=account, source=source, **p) for p in people.values()])
    finally:
        con.close()
    contacts = {c["jid"]: c for c in (parse.contacts(wa_db) if wa_db else [])}
    if contacts:
        session.execute(insert(Contact), [dict(account=account, source=source, **c) for c in contacts.values()])
    return dict(messages=n, chats=len(chats), contacts=len(contacts), participants=len(people))


def sync_account(
    session: Session, account: str, tree: str, media_roots: list[str], label: str | None = None, force: bool = False
) -> dict[str, int] | None:
    """Counts of what was loaded, None when unchanged, {} when `tree` holds no msgstore.db. The caller commits."""
    db = find_db(tree)
    if db is None:
        return {}
    wa_db = find_db(os.path.dirname(db), "wa.db")
    roots = [r for r in media_roots if os.path.isdir(r)]
    st, wa_st = os.stat(db), os.stat(wa_db) if wa_db else None
    found = parse.index_media(roots)
    # everything the rows depend on: each database's own size and mtime, and which file name sits at which path (a
    # moved or replaced media file changes `media_file`), so any of them reloads
    mapping = hashlib.sha256("\0".join(f"{k}\0{v}" for k, v in sorted(found.items())).encode()).hexdigest()
    wa_key = f"{wa_st.st_size}:{wa_st.st_mtime_ns}" if wa_st else ""
    key = "|".join([db, wa_db or "", wa_key, *roots, f"media={len(found)}:{mapping}"])
    size, mtime = st.st_size, st.st_mtime_ns
    acct = session.get(Account, account)
    if acct is None:
        session.add(Account(id=account, label=label))
        session.flush()
    elif label:
        acct.label = label
    prev = session.get(Source, (account, SOURCE))
    if not force and prev and (prev.size, prev.mtime_ns, prev.path) == (size, mtime, key):
        return None
    counts = load(session, account, SOURCE, db, wa_db, found)
    now = datetime.now(UTC)
    if prev is None:
        session.add(
            Source(
                account=account,
                source=SOURCE,
                path=key,
                size=size,
                mtime_ns=mtime,
                rows=counts["messages"],
                loaded_at=now,
            )
        )
    else:
        prev.path, prev.size, prev.mtime_ns, prev.rows, prev.loaded_at = key, size, mtime, counts["messages"], now
    return counts
