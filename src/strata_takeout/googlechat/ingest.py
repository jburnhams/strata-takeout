"""Load Google chat exports into schema `googlechat`, idempotently: each (account, source) keeps a fingerprint of its input in `sources`; an unchanged input is
skipped, a changed or new one replaces every row of that (account, source) in one transaction, so a re-run never duplicates."""

import hashlib
import mimetypes
import os
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.orm import Session

from . import parse
from .models import Account, Attachment, Conversation, Member, Message, Source

BATCH = 1000


def hash_file(path: str) -> tuple[int | None, str | None]:
    if not os.path.isfile(path):
        return None, None
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return os.path.getsize(path), h.hexdigest()


def stat_of(paths: Iterable[str]) -> tuple[int, int, int]:
    """(files, total bytes, newest mtime ns) of an input."""
    n = size = mt = 0
    for p in paths:
        st = os.stat(p)
        n, size, mt = n + 1, size + st.st_size, max(mt, st.st_mtime_ns)
    return n, size, mt


def media_paths(root: str) -> Iterator[str]:
    g = os.path.join(root, parse.GROUPS)
    for d in sorted(os.listdir(g)):
        if os.path.isdir(os.path.join(g, d)):
            for f in sorted(os.listdir(os.path.join(g, d))):
                yield os.path.join(g, d, f)


def gvault_files(tree: str) -> Iterator[str]:
    for d, _, fs in os.walk(tree):
        for f in fs:
            if f.endswith(".meta"):
                yield os.path.join(d, f)


def wipe(session: Session, account: str, source: str) -> None:
    ids = select(Conversation.id).where(Conversation.account == account, Conversation.source == source)
    for t in (Attachment, Message):
        session.execute(delete(t).where(t.conversation_id.in_(ids)))
    session.execute(delete(Member).where(Member.conversation_id.in_(ids)))
    session.execute(delete(Conversation).where(Conversation.account == account, Conversation.source == source))


def put_messages(session: Session, account: str, source: str, root: str | None, conv_id: int, msgs: list[dict]) -> int:
    rows = [
        {k: v for k, v in x.items() if k != "attachments"}
        | dict(account=account, source=source, conversation_id=conv_id, has_attachment=bool(x["attachments"]))
        for x in msgs
    ]
    session.execute(insert(Message), rows)
    ids = dict(
        session.execute(
            select(Message.ext_id, Message.id).where(
                Message.account == account, Message.source == source, Message.ext_id.in_([x["ext_id"] for x in msgs])
            )
        ).all()
    )
    atts = []
    for x in msgs:
        for a in x["attachments"]:
            size, sha = hash_file(os.path.join(root, a["path"])) if root and a.get("path") else (None, None)
            atts.append(
                dict(
                    account=account,
                    source=source,
                    conversation_id=conv_id,
                    message_id=ids[x["ext_id"]],
                    seq=a["seq"],
                    status=a["status"],
                    match=a["match"],
                    original_name=a.get("original_name"),
                    export_name=a.get("export_name"),
                    path=a.get("path"),
                    mime=mimetypes.guess_type(a.get("path") or a.get("export_name") or "")[0],
                    size=size,
                    sha256=sha,
                )
            )
    if atts:
        session.execute(insert(Attachment), atts)
    return len(msgs)


def finish(session: Session, conv_id: int) -> None:
    n, lo, hi = session.execute(
        select(func.count(), func.min(Message.at), func.max(Message.at)).where(Message.conversation_id == conv_id)
    ).one()
    session.execute(
        update(Conversation).where(Conversation.id == conv_id).values(n_messages=n, first_at=lo, last_at=hi)
    )


def load_takeout(session: Session, account: str, root: str) -> int:
    wipe(session, account, "takeout")
    total = 0
    for c in parse.takeout(root):
        conv = Conversation(
            account=account,
            source="takeout",
            ext_id=c["ext_id"],
            kind=c["kind"],
            name=c["name"],
            participants=len(c["members"]),
            info=c["info"],
        )
        session.add(conv)
        session.flush()
        if c["members"]:
            session.execute(insert(Member), [dict(conversation_id=conv.id, **m) for m in c["members"]])
        for i in range(0, len(c["messages"]), BATCH):
            total += put_messages(session, account, "takeout", root, conv.id, c["messages"][i : i + BATCH])
        orph = []
        for o in c["orphans"]:
            size, sha = hash_file(os.path.join(root, o["path"]))
            orph.append(
                dict(
                    account=account,
                    source="takeout",
                    conversation_id=conv.id,
                    message_id=None,
                    seq=0,
                    status="orphan",
                    match=None,
                    original_name=None,
                    export_name=o["export_name"],
                    path=o["path"],
                    mime=mimetypes.guess_type(o["path"])[0],
                    size=size,
                    sha256=sha,
                )
            )
        if orph:
            session.execute(insert(Attachment), orph)
        finish(session, conv.id)
    return total


def load_mail_chats(session: Session, account: str, source: str, chats: Iterable[dict]) -> int:
    """Chat-log mails: the same set of people always lands in the same conversation; lines keep unique ids even if a mail repeats."""
    wipe(session, account, source)
    convs: dict[str, int] = {}
    people: dict[int, set[str]] = {}
    seen: dict[str, int] = {}
    total = 0
    for c in chats:
        gid = parse.group_id(c["people"])
        if gid not in convs:
            conv = Conversation(account=account, source=source, ext_id=gid, kind="chat")
            session.add(conv)
            session.flush()
            convs[gid] = conv.id
            people[conv.id] = set()
        cid = convs[gid]
        people[cid].update(c["people"])
        for x in c["messages"]:
            e = x["ext_id"]
            seen[e] = seen.get(e, 0) + 1
            if seen[e] > 1:
                x["ext_id"] = f"{e}~{seen[e]}"
        total += put_messages(session, account, source, None, cid, c["messages"])
    for cid, ps in people.items():
        if ps:
            session.execute(
                insert(Member), [dict(conversation_id=cid, name=None, email=p, user_type=None) for p in sorted(ps)]
            )
        session.execute(update(Conversation).where(Conversation.id == cid).values(participants=len(ps)))
        finish(session, cid)
    return total


def sync_input(
    session: Session, account: str, source: str, path: str, fp: tuple[int, int, int], loader: Any, force: bool
) -> int | None:
    row = session.get(Source, (account, source))
    size, mtime = fp[1], fp[2]
    sig = (path, size, mtime)
    if not force and row and (row.path, row.size, row.mtime_ns) == sig:
        return None
    n = loader()
    now = datetime.now(UTC)
    if row is None:
        session.add(Source(account=account, source=source, path=path, size=size, mtime_ns=mtime, rows=n, loaded_at=now))
    else:
        row.path, row.size, row.mtime_ns, row.rows, row.loaded_at = path, size, mtime, n, now
    return n


def sync(
    session: Session, account: str, paths: list[str], label: str | None = None, force: bool = False
) -> dict[str, int | None]:
    """Load whatever each path holds: a Takeout `Google Chat/` tree, a Gmail mbox (or a tree holding one), a gmvault tree. {source: rows or None when unchanged}."""
    from strata_takeout.gmail.ingest import find_mbox

    acct = session.get(Account, account)
    if acct is None:
        session.add(Account(id=account, label=label))
        session.flush()
    elif label:
        acct.label = label
    out: dict[str, int | None] = {}
    for p in paths:
        p = os.path.expanduser(p)
        root = parse.takeout_root(p) if os.path.isdir(p) else None
        if root:
            out["takeout"] = sync_input(
                session,
                account,
                "takeout",
                root,
                stat_of(parse.json_files(root) + list(media_paths(root))),
                lambda: load_takeout(session, account, root),
                force,
            )
            continue
        mbox = find_mbox(p)
        if mbox:
            out["gmail"] = sync_input(
                session,
                account,
                "gmail",
                mbox,
                stat_of([mbox]),
                lambda: load_mail_chats(session, account, "gmail", parse.gmail_mbox(mbox)),
                force,
            )
            continue
        metas = list(gvault_files(p)) if os.path.isdir(p) else []
        if metas:
            out["gmvault"] = sync_input(
                session,
                account,
                "gmvault",
                p,
                stat_of(metas),
                lambda: load_mail_chats(session, account, "gmvault", parse.gmvault(p)),
                force,
            )
    return out
