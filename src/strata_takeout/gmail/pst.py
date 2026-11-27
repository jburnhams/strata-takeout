"""Read an Outlook data file (`.pst`) as input for the Gmail loader, through libpst's `readpst` (which also copes with damaged folders that other readers give up on).

`readpst -r -e -8` writes one `.eml` per message under a folder tree that mirrors the PST's folders; contacts, calendar and notes come out as other file types and are
ignored here. The export goes to a scratch folder (`workdir`, else the system temp folder; a PST can expand to more than its own size, so the job points it at a disk with room) that
is removed afterwards.

What the loader gets per message: the raw `.eml`, an `offset`, the labels and a fallback date.
- offset: `(folder number) * 10_000_000 + (file number)` where folders are numbered in sorted path order. Stable while the PST is unchanged (a changed PST reloads everything anyway),
  unique per (account, source), and what a verdicts file keys on, like an mbox offset.
- labels: the folder's lower-case path (`inbox/family`) plus the system label the rules know for the top-level folder: `inbox`, `sent` (Sent Items, Sent), `draft`, `spam` (Junk E-mail,
  Junk, Spam, Bulk Mail), `trash` (Deleted Items, Trash). Outlook has no Gmail categories, so everything but sent mail waits for a classifier, as for a gmvault backup.
- date: the `Date` header when there is one; else the oldest-hop-last `Received:` header's time (when the mailbox's own server first saw the message), the closest thing a PST message has to an
  mbox separator date."""

import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

STEP = 10_000_000
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
    r = subprocess.run([readpst, "-q", "-r", "-e", "-8", "-o", out, path], capture_output=True, text=True)
    if r.returncode != 0 and not any(f.endswith(".eml") for _, _, fs in os.walk(out) for f in fs):
        raise RuntimeError(f"readpst failed ({r.returncode}): {r.stderr.strip()[:300]}")


def _folders(root: str) -> dict[str, list[str]]:
    """{folder path relative to root: sorted .eml names}, only folders that hold messages."""
    out: dict[str, list[str]] = {}
    for d, dirs, fs in os.walk(root):
        dirs.sort()
        mails = sorted((f for f in fs if f.endswith(".eml")), key=lambda f: int(f[:-4]) if f[:-4].isdigit() else -1)
        if mails:
            out[os.path.relpath(d, root).replace(os.sep, "/")] = mails
    return out


def items(path: str, workdir: str | None = None, readpst: str = "readpst") -> Iterator[dict]:
    """One dict per message in folder-path order: raw, offset, labels, internal_date (a UTC timestamp from the Received header, or None)."""
    tmp = tempfile.mkdtemp(prefix="strata-pst-", dir=workdir or None)
    try:
        export(path, tmp, readpst)
        folders = _folders(tmp)
        for n, folder in enumerate(sorted(folders), 1):
            lab = labels(folder)
            for f in folders[folder]:
                seq = int(f[:-4]) if f[:-4].isdigit() else 0
                with open(os.path.join(tmp, folder, f), "rb") as fh:
                    raw = fh.read()
                d = received_when(raw)
                yield dict(raw=raw, offset=n * STEP + seq, labels=lab, internal_date=d.timestamp() if d else None)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
