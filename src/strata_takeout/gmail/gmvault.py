"""Read a gmvault backup (an IMAP-era Gmail export: one `.eml` per message beside a `.meta` JSON, grouped per month) as input for the Gmail loader.

Layout: `<root>/db/YYYY-MM.zip`, each holding `YYYY-MM/<gm_id>.eml` and `YYYY-MM/<gm_id>.meta`; the same folders unzipped (`<root>/db/YYYY-MM/...`) or the
zips directly under `<root>` work too. The meta is `{gm_id, msg_id, internal_date, labels, flags, subject, ...}`.

What the loader gets per message: the raw `.eml`, the Gmail id (`gm_id`, stored as the message's `offset`: it is unique and stable, and a verdicts file keys on it
like it does on an mbox offset), the labels (below), and `internal_date` as the date when the message has no usable `Date` header.

Labels become the lower-case names the rules know: IMAP's `\\Inbox`, `\\Sent`, `\\Important`, `\\Starred`, `\\Draft`, `\\Spam`, `\\Trash` lose the backslash, flag `\\Seen` adds
`opened` (else `unread`) and `\\Flagged` adds `starred`; any other label (a folder name) is kept as written, lower-cased. This era has no Gmail categories, so the rules
leave everything but sent mail to a classifier.

Chat logs that gmvault stored with the mail (label `gmvault-chats`) are not mail and are skipped here (`chat_items` yields exactly those, for the Google chat loader); so are items whose `.eml` is missing."""

import glob
import gzip
import json
import os
import zipfile
from collections.abc import Iterator

SYSTEM = {"inbox", "sent", "important", "starred", "draft", "drafts", "spam", "trash", "chats"}
SKIP_LABELS = {"gmvault-chats"}


def archives(root: str) -> list[str]:
    """The month zips of a backup, oldest first; a folder of unzipped months is returned as the folder itself."""
    base = os.path.join(root, "db") if os.path.isdir(os.path.join(root, "db")) else root
    zips = sorted(glob.glob(os.path.join(glob.escape(base), "*.zip")))
    if zips:
        return [z for z in zips if os.path.basename(z)[:4].isdigit()]
    return [base] if glob.glob(os.path.join(glob.escape(base), "*", "*.meta")) else []


def detect(tree: str) -> bool:
    return os.path.isdir(tree) and bool(archives(tree))


def fingerprint(root: str) -> tuple[str, int, int]:
    """(path, total size, newest mtime) of everything a load reads: a changed, added or removed archive changes it."""
    size, mtime = 0, 0
    for a in archives(root):
        paths = [a] if os.path.isfile(a) else glob.glob(os.path.join(glob.escape(a), "*", "*"))
        for p in paths:
            st = os.stat(p)
            size += st.st_size
            mtime = max(mtime, st.st_mtime_ns)
    return root, size, mtime


def normalise(labels: list[str] | None, flags: list[str] | None) -> list[str]:
    out: list[str] = []
    for lab in labels or []:
        name = str(lab).strip().lstrip("\\").lower()
        if name and name not in out:
            out.append(name)
    fl = {str(f) for f in flags or []}
    out.append("opened" if "\\Seen" in fl else "unread")
    if "\\Flagged" in fl:
        out.append("starred")
    return out


def _pairs(archive: str) -> Iterator[tuple[dict, bytes]]:
    if os.path.isfile(archive):
        with zipfile.ZipFile(archive) as z:
            names = set(z.namelist())
            for n in sorted(names):
                if n.endswith(".meta") and n[:-5] + ".eml" in names:
                    yield json.loads(z.read(n)), z.read(n[:-5] + ".eml")
    else:
        for meta in sorted(glob.glob(os.path.join(glob.escape(archive), "*", "*.meta"))):
            eml = meta[:-5] + ".eml"
            if os.path.exists(eml):
                with open(meta, encoding="utf-8") as f, open(eml, "rb") as g:
                    yield json.load(f), g.read()


def items(root: str) -> Iterator[dict]:
    """One dict per message, oldest archive first: raw, gm_id, labels, internal_date (UTC timestamp or None)."""
    for archive in archives(root):
        for meta, raw in _pairs(archive):
            if SKIP_LABELS & {str(x).lower() for x in meta.get("labels") or []}:
                continue
            try:
                gm_id = int(meta["gm_id"])
            except (KeyError, TypeError, ValueError):
                continue
            ts = meta.get("internal_date")
            yield dict(
                raw=raw,
                gm_id=gm_id,
                labels=normalise(meta.get("labels"), meta.get("flags")),
                internal_date=int(ts) if isinstance(ts, int | float) else None,
            )


def _chat_pairs(root: str) -> Iterator[tuple[dict, bytes]]:
    """(meta, raw .eml) of every item under `root`, whatever the layout: `chats*.zip` archives (`chats/subchats-N/<id>.eml|.eml.gz` + `.meta`) or the same unzipped, at any depth.
    A zip stored with a method Python cannot read (PPMd) is skipped: a deflate copy of the same data works."""
    for d, dirs, fs in os.walk(root):
        dirs.sort()
        for f in sorted(fs):
            p = os.path.join(d, f)
            if f.lower().startswith("chats") and f.lower().endswith(".zip"):
                try:
                    with zipfile.ZipFile(p) as z:
                        names = set(z.namelist())
                        for n in sorted(names):
                            e = next((x for x in (n[:-5] + ".eml", n[:-5] + ".eml.gz") if x in names), None)
                            if n.endswith(".meta") and e:
                                data = z.read(e)
                                yield json.loads(z.read(n)), gzip.decompress(data) if e.endswith(".gz") else data
                except NotImplementedError:
                    continue
            elif f.endswith(".meta"):
                e = next((x for x in (p[:-5] + ".eml", p[:-5] + ".eml.gz") if os.path.exists(x)), None)
                if e:
                    try:
                        with open(p, encoding="utf-8") as fh, open(e, "rb") as g:
                            data = g.read()
                            yield json.load(fh), gzip.decompress(data) if e.endswith(".gz") else data
                    except ValueError:
                        continue


def chat_items(root: str) -> Iterator[dict]:
    """The chat logs of a backup (label `gmvault-chats`): raw, msg_id, gm_id, labels (lower-case, as stored), internal_date. Repeats are the caller's to drop (a backup holds the same chat twice)."""
    for meta, raw in _chat_pairs(root):
        labels = [str(x).lower() for x in meta.get("labels") or []]
        if not SKIP_LABELS & set(labels):
            continue
        ts = meta.get("internal_date")
        yield dict(
            raw=raw,
            msg_id=str(meta.get("msg_id") or "").strip("<>") or None,
            gm_id=meta.get("gm_id"),
            labels=labels,
            internal_date=int(ts) if isinstance(ts, int | float) else None,
        )


def chat_files(root: str) -> list[str]:
    """What a chat load reads (for its fingerprint): the `chats*.zip` files and loose `.meta` files."""
    out = []
    for d, _, fs in os.walk(root):
        out += [
            os.path.join(d, f)
            for f in fs
            if f.endswith(".meta") or (f.lower().startswith("chats") and f.lower().endswith(".zip"))
        ]
    return sorted(out)
