"""Read an Outlook data file (`.pst`) as input for the Gmail loader, through libpst's `readpst` (which also copes with damaged folders that other readers give up on).

`readpst -r -e -8 -D` writes one `.eml` per message under a folder tree that mirrors the PST's folders (inside one directory named for the store, which is dropped); contacts, calendar and notes come out as other file types and are
ignored here. The export goes to a scratch folder (`workdir`, else the system temp folder; a PST can expand to more than its own size, so the job points it at a disk with room) that
is removed afterwards.

What the loader gets per message: the raw `.eml`, an `offset`, the labels and a fallback date.
- offset: `(folder number) * 10_000_000 + (file number)` where folders are numbered in sorted path order. Stable while the PST is unchanged (a changed PST reloads everything anyway),
  unique per (account, source), and what a verdicts file keys on, like an mbox offset.
- labels: the folder's lower-case path (`inbox/family`) plus the system label the rules know for the top-level folder: `inbox`, `sent` (Sent Items, Sent), `draft`, `spam` (Junk E-mail,
  Junk, Spam, Bulk Mail), `trash` (Deleted Items, Trash). Outlook has no Gmail categories, so everything but sent mail waits for a classifier, as for a gmvault backup.
- date: the `Date` header when there is one; else the oldest-hop-last `Received:` header's time (when the mailbox's own server first saw the message), the closest thing a PST message has to an
  mbox separator date."""

import collections
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

STEP = 10_000_000
STUB_BASE = (
    5_000_000  # offsets of header-only messages recovered through libpff start here, above any readpst file number
)
SYSTEM = {
    "inbox": "inbox",
    "sent": "sent",
    "sent items": "sent",
    "sent mail": "sent",
    "drafts": "draft",
    "draft": "draft",
    "junk e-mail": "spam",
    "junk email": "spam",
    "junk": "spam",
    "spam": "spam",
    "bulk mail": "spam",
    "deleted items": "trash",
    "trash": "trash",
}
RECEIVED = re.compile(rb"^Received:.*?;\s*([^\r\n]+(?:\r?\n[ \t]+[^\r\n]+)*)\s*$", re.I | re.M | re.S)


def detect(tree: str) -> bool:
    return os.path.isfile(tree) and tree.lower().endswith(".pst")


def fingerprint(path: str) -> tuple[str, int, int]:
    st = os.stat(path)
    return path, st.st_size, st.st_mtime_ns


def labels(folder: str) -> list[str]:
    """Lower-case path of an Outlook folder (`Inbox/Family ` -> `inbox/family`) plus the system label of its top-level folder."""
    parts = [p.strip().lower().replace(",", " ") for p in folder.split("/") if p.strip()]
    out = ["/".join(parts)] if parts else []
    if parts and parts[0] in SYSTEM and SYSTEM[parts[0]] not in out:
        out.append(SYSTEM[parts[0]])
    return out


def received_when(raw: bytes) -> datetime | None:
    """Time of the topmost `Received:` header (the last hop: delivery into this mailbox), else None. Looks only at the header block."""
    head = re.split(rb"\r?\n\r?\n", raw[:65536], maxsplit=1)[0]
    for block in re.split(rb"\r?\n(?=\S)", head):
        if not block[:9].lower().startswith(b"received:"):
            continue
        m = RECEIVED.match(block)
        if not m:
            continue
        try:
            d = parsedate_to_datetime(re.sub(r"\s+", " ", m.group(1).decode("latin-1")).strip())
        except Exception:
            continue
        return d.astimezone(UTC) if d.tzinfo else d.replace(tzinfo=UTC)
    return None


def export(path: str, out: str, readpst: str = "readpst") -> None:
    """Run readpst on `path` into the (empty) folder `out`. A message it cannot read is skipped by readpst itself; a failure to start or open the file raises."""
    if shutil.which(readpst) is None:
        raise SystemExit(
            f"{readpst} not found: install libpst (the job may name the binary with STRATA_TAKEOUT_GMAIL_READPST)"
        )
    # -D: Deleted Items too (readpst skips them by default; they get the `trash` label). A nonzero exit is a failed export even when some files were written
    # (a full scratch disk, a killed process): loading the prefix would replace a complete load and the unchanged fingerprint would hide it. Items readpst skips on its own are not an error (exit 0).
    r = subprocess.run([readpst, "-q", "-r", "-e", "-8", "-D", "-o", out, path], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"readpst failed ({r.returncode}): {r.stderr.strip()[:300]}")


def _folders(root: str) -> tuple[str, dict[str, list[str]]]:
    """(the store's folder, {folder path relative to it: sorted .eml names}), only folders that hold messages. readpst puts everything under one directory named for the store;
    it is dropped (a lone top-level directory with no messages of its own) so `Sent Items` is a top-level folder and gets its system label."""
    top = os.listdir(root)
    if len(top) == 1 and os.path.isdir(os.path.join(root, top[0])):
        inner = os.path.join(root, top[0])
        if not any(f.endswith(".eml") for f in os.listdir(inner)):
            root = inner
    out: dict[str, list[str]] = {}
    for d, dirs, fs in os.walk(root):
        dirs.sort()
        mails = sorted((f for f in fs if f.endswith(".eml")), key=lambda f: int(f[:-4]) if f[:-4].isdigit() else -1)
        if mails:
            out[os.path.relpath(d, root).replace(os.sep, "/")] = mails
    return root, out


STUB_MAX = 1500  # a readpst .eml this small with no Date/Received and a MAILER-DAEMON sender is an item whose headers it could not find
CTRL = re.compile(r"[\x00-\x1f]")


def _subject(raw: bytes) -> str:
    from email.header import decode_header, make_header

    m = re.search(rb"^Subject:[ \t]*(.*(?:\r?\n[ \t].*)*)", raw[:4096], re.I | re.M)
    if not m:
        return ""
    v = re.sub(r"\s+", " ", m.group(1).decode("latin-1")).strip()
    try:
        v = str(make_header(decode_header(v)))
    except Exception:
        pass
    return CTRL.sub("", v).strip()


def is_stub(raw: bytes) -> bool:
    head = raw[:STUB_MAX]
    return (
        len(raw) < STUB_MAX
        and re.search(rb"^From:\s*<?MAILER-DAEMON>?\s*$", head, re.I | re.M) is not None
        and re.search(rb"^(Date|Received):", head, re.I | re.M) is None
    )


def stubs(path: str) -> dict[str, list[tuple[str, bytes, float | None]]]:
    """Header-only messages of a PST, {folder path: [(subject, raw headers, delivery timestamp)]}. A mail store kept by Outlook as an IMAP account caches only the headers of messages it never
    downloaded: no body, no attachments, and the header block sits in a named property (0x8015) that readpst does not read, so it exports a bodiless `MAILER-DAEMON` shell with no sender, date or
    Message-ID. This reads those properties with libpff (`pypff`, optional: without it the shells are loaded as readpst wrote them). Messages with a body are left to readpst."""
    try:
        import pypff  # type: ignore[import-not-found]
    except ImportError:
        return {}
    out: dict[str, list[tuple[str, bytes, float | None]]] = {}
    pf = pypff.file()
    pf.open(path)

    def walk(f, parts: list[str]) -> None:
        for i in range(f.number_of_sub_messages):
            try:
                m = f.get_sub_message(i)
                if m.transport_headers or m.plain_text_body or m.html_body or m.number_of_attachments:
                    continue
                hdr = next(
                    (e.data for rs in m.record_sets for e in rs.entries if e.entry_type == 0x8015 and e.data), None
                )
                if not hdr or b":" not in hdr:
                    continue
                t = getattr(m, "delivery_time", None)
                raw = hdr.replace(b"\x00", b"").rstrip(b"\r\n") + b"\r\n\r\n"
                out.setdefault("/".join(parts), []).append(
                    (CTRL.sub("", m.subject or "").strip(), raw, t.replace(tzinfo=UTC).timestamp() if t else None)
                )
            except Exception:
                continue
        for j in range(f.number_of_sub_folders):
            try:
                sub = f.get_sub_folder(j)
            except Exception:
                continue
            walk(sub, parts + [sub.name or "?"])

    root = pf.get_root_folder()
    for j in range(
        root.number_of_sub_folders
    ):  # the store's top folders; the first level is the store itself, not part of a folder path
        try:
            top = root.get_sub_folder(j)
        except Exception:
            continue
        for k in range(top.number_of_sub_folders):
            try:
                sub = top.get_sub_folder(k)
            except Exception:
                continue
            walk(sub, [sub.name or "?"])
    return out


def items(path: str, workdir: str | None = None, readpst: str = "readpst") -> Iterator[dict]:
    """One dict per message in folder-path order: raw, offset, labels, internal_date (a UTC timestamp from the Received header, or None).
    A header-only shell from readpst (see `stubs`) is replaced by the real headers libpff read; those get offsets `folder * STEP + STUB_BASE + n`."""
    st = stubs(path)
    tmp = tempfile.mkdtemp(prefix="strata-pst-", dir=workdir or None)
    try:
        export(path, tmp, readpst)
        store, folders = _folders(tmp)
        for n, folder in enumerate(sorted(set(folders) | set(st)), 1):
            lab = labels(folder)
            pending = collections.Counter(sub for sub, _, _ in st.get(folder, []))
            for f in folders.get(folder, []):
                seq = int(f[:-4]) if f[:-4].isdigit() else 0
                with open(os.path.join(store, folder, f), "rb") as fh:
                    raw = fh.read()
                if pending and is_stub(raw) and pending[_subject(raw)] > 0:
                    pending[_subject(raw)] -= 1  # recovered below with its real headers
                    continue
                d = received_when(raw)
                yield dict(raw=raw, offset=n * STEP + seq, labels=lab, internal_date=d.timestamp() if d else None)
            for k, (_, raw, ts) in enumerate(st.get(folder, [])):
                d = received_when(raw)
                yield dict(
                    raw=raw, offset=n * STEP + STUB_BASE + k, labels=lab, internal_date=d.timestamp() if d else ts
                )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
