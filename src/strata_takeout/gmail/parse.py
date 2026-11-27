"""Read a Gmail mbox: split it into messages (streaming, never the whole file in memory) and turn each into a metadata row, a body and attachments."""

import hashlib
import html
import os
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from email import policy
from email.header import decode_header, make_header
from email.message import Message
from email.parser import BytesParser
from email.utils import getaddresses, parseaddr, parsedate_to_datetime

from . import rules

FROM_LINE = re.compile(
    rb"^From \S+ .*\d{4}\r?\n?$"
)  # mbox separator as Takeout writes it; body lines starting "From " are escaped (">From ")
TAG = re.compile(r"<(style|script|head)[^>]*>.*?</\1>|<[^>]+>", re.S | re.I)
BLOCK = re.compile(r"</?(p|div|br|tr|li|h\d|table)[^>]*>", re.I)
BATCH = 200


def split(path: str, batch: int = BATCH) -> Iterator[list[tuple[int, bytes]]]:
    """Batches of (offset, raw message) in file order. A separator counts only after a blank line (or at the start)."""
    out: list[tuple[int, bytes]] = []
    cur: list[bytes] = []
    off = start = 0
    prev_blank = True
    with open(path, "rb") as f:
        for line in f:
            if line.startswith(b"From ") and prev_blank and FROM_LINE.match(line):
                if cur:
                    out.append((start, b"".join(cur)))
                    cur = []
                    if len(out) >= batch:
                        yield out
                        out = []
                start = off
            cur.append(line)
            off += len(line)
            prev_blank = line in (b"\n", b"\r\n")
    if cur:
        out.append((start, b"".join(cur)))
    if out:
        yield out


def clean(s: str) -> str:
    """Postgres text cannot hold NUL, and mail has them (in subjects, bodies, file names): drop them."""
    return s.replace("\x00", "")


def header(v: object) -> str:
    try:
        return clean(re.sub(r"\s+", " ", str(make_header(decode_header(str(v or ""))))).strip())
    except Exception:  # a malformed encoded word must not lose the message
        return clean(re.sub(r"\s+", " ", str(v or "")).strip())


def when(m: Message) -> datetime | None:
    try:
        d = parsedate_to_datetime(str(m.get("Date")))
    except Exception:
        return None
    return d.astimezone(UTC) if d.tzinfo else d.replace(tzinfo=UTC)


def labels(m: Message) -> list[str]:
    return [
        x.strip().strip('"').lower()
        for x in re.sub(r"\s+", " ", str(m.get("X-Gmail-Labels") or "")).split(",")
        if x.strip()
    ]


def addrs(m: Message, name: str) -> str:
    return " ".join(sorted({a.lower() for _, a in getaddresses([header(v) for v in m.get_all(name, [])]) if a}))


def text_of(part: Message) -> str:
    raw = part.get_payload(decode=True)
    if not isinstance(raw, bytes):
        return ""
    try:
        t = raw.decode(part.get_content_charset() or "utf-8", "replace")
    except LookupError:
        t = raw.decode("utf-8", "replace")
    if part.get_content_type() == "text/html":
        t = html.unescape(TAG.sub(" ", BLOCK.sub("\n", t)))
    return clean(re.sub(r"[ \t\r\f\v]+", " ", re.sub(r"\n\s*\n+", "\n\n", t)).strip())


def store(data: bytes, folder: str, name: str | None) -> tuple[str, str]:
    """Write `data` content-addressed under `folder`; returns (sha256, path relative to folder). An existing file is left alone."""
    sha = hashlib.sha256(data).hexdigest()
    ext = os.path.splitext(name or "")[1].lower()
    ext = ext if re.fullmatch(r"\.[a-z0-9]{1,8}", ext) else ""
    rel = os.path.join(sha[:2], sha + ext)
    full = os.path.join(folder, rel)
    if not os.path.exists(full):
        os.makedirs(os.path.dirname(full), exist_ok=True)
        tmp = f"{full}.part{os.getpid()}"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, full)
    return sha, rel


def leaves(m: Message) -> Iterator[Message]:
    """Every non-multipart part, except that an attached message (message/rfc822) is one leaf: its inner text is the
    attachment's content, not the parent's body (`walk()` would enter it and merge the two)."""
    if m.get_content_type() == "message/rfc822" or not m.is_multipart():
        yield m
        return
    for sub in m.get_payload():  # type: ignore[union-attr]
        yield from leaves(sub)  # type: ignore[arg-type]


def part_bytes(p: Message) -> bytes:
    if p.get_content_type() == "message/rfc822":
        inner = p.get_payload()
        return inner[0].as_bytes() if isinstance(inner, list) and inner else b""  # type: ignore[union-attr]
    data = p.get_payload(decode=True)
    return data if isinstance(data, bytes) else b""


def message(offset: int, raw: bytes, folder: str | None) -> tuple[dict, list[dict]]:
    """(message row, attachment rows) of one raw message. A kept message gets `body_text` and, when `folder` is given, its parts written there."""
    m = BytesParser(policy=policy.compat32).parsebytes(raw)
    name, addr = parseaddr(header(m.get("From")))
    addr = addr.lower()
    ls = labels(m)
    unsub = m.get("List-Unsubscribe") is not None
    bulk = str(m.get("Precedence") or "").strip().lower() in ("bulk", "list", "junk")
    tier, reason = rules.tier(ls, addr, unsub, bulk)
    row = dict(
        offset=offset,
        size=len(raw),
        at=when(m),
        message_id=header(m.get("Message-ID")) or None,
        thread_id=str(m.get("X-GM-THRID") or "").strip() or None,
        in_reply_to=header(m.get("In-Reply-To")) or None,
        from_addr=addr or None,
        from_name=name or None,
        to_addrs=addrs(m, "To") or None,
        cc_addrs=addrs(m, "Cc") or None,
        subject=header(m.get("Subject")) or None,
        labels=",".join(ls) or None,
        has_unsubscribe=unsub,
        is_bulk=bulk,
        tier=tier,
        tier_reason=reason,
        body_text=None,
    )
    text = atts = 0
    bodies: dict[str, list[str]] = {"text/plain": [], "text/html": []}
    parts = []
    for p in leaves(m):
        ct = p.get_content_type()
        disp = str(p.get("Content-Disposition") or "").lower()
        if ct in bodies and "attachment" not in disp:
            t = text_of(p)
            text += len(t)
            bodies[ct].append(t)
        else:
            data = part_bytes(p)
            atts += len(data)
            parts.append((p, ct, data, "inline" in disp or bool(p.get("Content-ID"))))
    row.update(text_bytes=text, attach_bytes=atts, n_attachments=len(parts))
    out: list[dict] = []
    if tier == "keep":
        row["body_text"] = ("\n\n".join(bodies["text/plain"]) or "\n\n".join(bodies["text/html"])) or None
        for seq, (p, ct, data, inline) in enumerate(parts):
            fn = header(p.get_filename()) or None
            sha = rel = None
            if data and folder:
                sha, rel = store(data, folder, fn)
            out.append(
                dict(seq=seq, filename=fn, content_type=ct, size=len(data), sha256=sha, stored_path=rel, inline=inline)
            )
    return row, out


def messages(path: str, folder: str | None) -> Iterator[tuple[dict, list[dict]]]:
    for batch in split(path):
        for off, raw in batch:
            yield message(off, raw, folder)
