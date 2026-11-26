"""Read an mbox (Gmail Takeout: one `All mail` file, labels in `X-Gmail-Labels`) without loading it: `scan` streams the
file and yields each message's byte span, `headers` reads the header block only, `body` extracts text and attachments of
one message read back by offset. Standard library only, tolerant: a bad header or date reads as None, never raises.
Never prints or logs message content."""

import email
import email.policy
import email.utils
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from email.message import EmailMessage, Message
from typing import BinaryIO

POLICY = email.policy.default.clone(refold_source="none")
HTML_TAG = re.compile(r"<[^>]+>")
SPACE = re.compile(r"[ \t\r\f\v]+")
BLANKS = re.compile(r"\n{3,}")
STYLE = re.compile(r"(?is)<(script|style)[^>]*>.*?</\1>")
BULK_PRECEDENCE = {"bulk", "list", "junk"}


def scan(f: BinaryIO) -> Iterator[tuple[int, int]]:
    """(offset, size) of every message. Iterates `f`: do not seek it meanwhile (use a second handle for `read_at`). A message starts at a line `From ` that follows a blank line (or the file start):
    an unescaped body line `From ...` after text is not a boundary, which is how mbox writers escape it (`>From`)."""
    start: int | None = None
    pos = 0
    prev_blank = True
    for line in f:
        if prev_blank and line.startswith(b"From "):
            if start is not None:
                yield start, pos - start
            start = pos
        prev_blank = line in (b"\n", b"\r\n")
        pos += len(line)
    if start is not None:
        yield start, pos - start


def read_at(f: BinaryIO, offset: int, size: int) -> bytes:
    f.seek(offset)
    raw = f.read(size)
    nl = raw.find(b"\n")
    return raw[nl + 1 :] if raw.startswith(b"From ") and nl >= 0 else raw  # drop the mbox separator line


def _addrs(value) -> list[str]:
    return sorted({a.lower() for _, a in email.utils.getaddresses([str(value)]) if a}) if value else []


def _date(value) -> datetime | None:
    try:
        d = email.utils.parsedate_to_datetime(str(value))
    except (TypeError, ValueError, IndexError):
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=UTC)
    d = d.astimezone(UTC)
    return d if 1990 <= d.year <= 2100 else None


def _labels(value) -> list[str]:
    if not value:
        return []
    # labels are comma separated; a label with a comma is quoted, which csv-style splitting would handle, but Gmail
    # writes plain names for system labels and the rest are the user's own: split on commas outside quotes
    return [
        p.strip().strip('"')
        for p in re.split(r',(?=(?:[^"]*"[^"]*")*[^"]*$)', str(value).replace("\r", "").replace("\n", ""))
        if p.strip()
    ]


def headers(raw: bytes) -> dict:
    """Metadata of one message from its header block only (everything up to the first blank line)."""
    head = raw.split(b"\n\n", 1)[0].split(b"\r\n\r\n", 1)[0]
    m = email.message_from_bytes(head, policy=POLICY)

    def get(name: str):
        try:
            return m.get(name)
        except Exception:  # a malformed header must not stop a 20 GB load
            return None

    name, addr = email.utils.parseaddr(str(get("From") or ""))
    labels = _labels(get("X-Gmail-Labels"))
    prec = str(get("Precedence") or "").strip().lower()
    return dict(
        message_id=(str(get("Message-ID") or "").strip() or None),
        thread_id=(str(get("X-GM-THRID") or "").strip() or None),
        sent_at=_date(get("Date")),
        from_addr=addr.lower() or None,
        from_name=name or None,
        to_addrs=sorted(set(_addrs(get("To")) + _addrs(get("Cc")))),
        subject=(str(get("Subject") or "").replace("\n", " ").strip() or None),
        labels=labels,
        is_sent="Sent" in labels,
        has_unsubscribe=get("List-Unsubscribe") is not None,
        is_bulk=prec in BULK_PRECEDENCE or str(get("Auto-Submitted") or "no").strip().lower() != "no",
    )


def _text_of(part: Message) -> str:
    try:
        payload = part.get_content()  # type: ignore[attr-defined]
    except (LookupError, UnicodeError, AssertionError, KeyError):
        return ""
    if not isinstance(payload, str):
        return ""
    if part.get_content_type() == "text/html":
        payload = HTML_TAG.sub(" ", STYLE.sub(" ", payload))
        payload = payload.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    return payload


def body(raw: bytes) -> tuple[str, list[dict]]:
    """(text, attachments) of one message: the text/plain parts (the HTML ones, tags stripped, only when there is no
    plain part), and each attachment as {filename, mime, data}."""
    m = email.message_from_bytes(raw, policy=POLICY)
    plain: list[str] = []
    html: list[str] = []
    files: list[dict] = []
    for part in m.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        disp = str(part.get("Content-Disposition") or "").split(";", 1)[0].strip().lower()
        name = part.get_filename()
        if disp == "attachment" or name or not ctype.startswith("text/"):
            try:
                data = part.get_payload(decode=True)
            except Exception:
                data = None
            if isinstance(data, bytes) and data:
                files.append(dict(filename=name, mime=ctype, data=data))
            continue
        text = _text_of(part)
        if ctype == "text/plain":
            plain.append(text)
        elif ctype == "text/html":
            html.append(text)
    text = "\n".join(plain) if any(p.strip() for p in plain) else "\n".join(html)
    text = BLANKS.sub(
        "\n\n", "\n".join(SPACE.sub(" ", ln).strip() for ln in text.replace("\x00", "").split("\n"))
    ).strip()
    return text, files


def sizes(raw: bytes) -> tuple[int, int]:
    """(text bytes, attachment bytes) estimated without decoding: the raw size of the text parts and of the others."""
    m: EmailMessage = email.message_from_bytes(raw, policy=POLICY)  # type: ignore[assignment]
    text = att = 0
    for part in m.walk():
        if part.is_multipart():
            continue
        n = len(part.get_payload() or "")
        if part.get_content_type().startswith("text/") and not part.get_filename():
            text += n
        else:
            att += n * 3 // 4  # base64 to bytes
    return text, att
