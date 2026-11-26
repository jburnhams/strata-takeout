"""Read the old-format Android `msgstore.db` (table `messages`, user_version <= 1; what a decrypted crypt12/14 backup
holds) and its companion `wa.db` (contacts). Read-only, standard library only, tolerant of versions: a column that is
not there reads as NULL, so a database from an older or newer app version loads with fewer fields instead of failing.
Never prints or logs message text."""

import base64
import hashlib
import os
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime

# media_wa_type -> kind. Codes are WhatsApp's own; unknown ones load as `other` with the raw code kept.
KINDS = {
    0: "text",
    1: "image",
    2: "audio",
    3: "video",
    4: "contact",
    5: "location",
    8: "call",
    9: "document",
    10: "call",
    11: "other",
    13: "gif",
    14: "contact",
    15: "deleted",
    16: "location",
    20: "sticker",
}
SYSTEM_STATUS = 6  # status 6 on a type-0 row is a system notice ("X changed the subject")
MAX_SIZE = 1 << 40  # bigger than any file: old databases hold junk sizes on some rows
MIN_TS, MAX_TS = 1_000_000_000_000, 4_102_444_800_000  # ms epoch for 2001..2100: anything else is not a real time


def open_ro(path: str) -> sqlite3.Connection:
    """The database read-only (never creates the -wal/-shm side files next to a backup)."""
    con = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    con.row_factory = sqlite3.Row
    con.text_factory = lambda b: b.decode("utf-8", "replace")  # old rows hold invalid UTF-8 now and then
    return con


def columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in con.execute(f'PRAGMA table_info("{table}")')}


def count(con: sqlite3.Connection, table: str) -> int:
    if not columns(con, table):
        return 0
    return con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]


def to_time(ms) -> datetime | None:
    ms = to_int(ms)
    if ms is None or not MIN_TS <= ms <= MAX_TS:
        return None
    return datetime.fromtimestamp(ms / 1000, UTC)


def kind_of(type_code, status) -> str:
    if type_code in (0, None) and status == SYSTEM_STATUS:
        return "system"
    return KINDS.get(type_code if type_code is not None else 0, "other")


def to_int(v) -> int | None:
    """An integer from a column that old versions declared as TEXT (media_wa_type is "0", "1", ...) or left NULL/junk."""
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, int | float):
        return int(v)
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _coord(v, kind: str) -> float | None:
    if not isinstance(v, int | float):
        return None
    return float(v) if v != 0 or kind == "location" else None


def clean_size(v) -> int | None:
    n = to_int(v)
    return n if n is not None and 0 <= n < MAX_SIZE else None


class MediaIndex(dict[str, str]):
    """basename -> path relative to its root (first root wins on a clash), for every file under the media roots, plus
    the files by size, so a message whose name matches nothing can still be found by WhatsApp's own content hash."""

    def __init__(self) -> None:
        super().__init__()
        self.by_size: dict[int, list[tuple[str, str]]] = {}  # size -> [(relative path, absolute path)]
        self._hashes: dict[str, str] = {}

    def by_hash(self, size: int | None, b64sha256: str | None) -> str | None:
        """Relative path of a file of this size whose SHA-256 (base64, as the backup stores it) is `b64sha256`."""
        if not size or not b64sha256:
            return None
        for rel, path in self.by_size.get(size, ()):
            if path not in self._hashes:
                with open(path, "rb") as f:
                    self._hashes[path] = base64.b64encode(hashlib.file_digest(f, "sha256").digest()).decode()
            if self._hashes[path] == b64sha256:
                return rel
        return None

    def signature(self) -> str:
        """Changes when a file appears, goes, moves, or changes size: what a reload of the media links depends on."""
        h = hashlib.sha256()
        for name, rel in sorted(self.items()):
            h.update(f"{name}\0{rel}\0".encode())
        for size, files in sorted(self.by_size.items()):
            h.update(f"{size}:{len(files)}\0".encode())
        return h.hexdigest()


def index_media(roots: list[str]) -> MediaIndex:
    """Old backups keep media loose in one folder, so the basename is the key WhatsApp's own paths end in."""
    out = MediaIndex()
    for root in roots:
        for dirpath, _dirs, files in os.walk(root):
            for f in files:
                path = os.path.join(dirpath, f)
                rel = os.path.relpath(path, root)
                out.setdefault(f, rel)
                try:
                    out.by_size.setdefault(os.stat(path).st_size, []).append((rel, path))
                except OSError:
                    continue  # vanished while walking
    return out


def media_files(con: sqlite3.Connection) -> dict[int, str]:
    """message row id -> the media path the backup recorded (table message_media, where the version has it)."""
    cols = columns(con, "message_media")
    if not {"message_row_id", "file_path"} <= cols:
        return {}
    return {
        r[0]: r[1]
        for r in con.execute("SELECT message_row_id, file_path FROM message_media WHERE file_path IS NOT NULL")
    }


def messages(con: sqlite3.Connection, found: MediaIndex | dict[str, str]) -> Iterator[dict]:
    """One dict per row of `messages`, ordered by row id. `found` is index_media()'s result."""
    cols = columns(con, "messages")
    if not {"_id", "key_remote_jid"} <= cols:
        raise ValueError("no old-format `messages` table: not a msgstore.db this loader understands")
    paths = media_files(con)

    def col(name, alias=None):
        return f'"{name}"' if name in cols else f"NULL AS {alias or name}"

    sql = (
        "SELECT _id, key_remote_jid, "
        + ", ".join(
            col(c)
            for c in (
                "key_from_me",
                "key_id",
                "status",
                "data",
                "timestamp",
                "media_mime_type",
                "media_wa_type",
                "media_size",
                "media_name",
                "media_caption",
                "media_duration",
                "media_hash",
                "latitude",
                "longitude",
                "remote_resource",
                "starred",
            )
        )
        + " FROM messages ORDER BY _id"
    )
    for r in con.execute(sql):
        type_code = to_int(r["media_wa_type"])
        status = to_int(r["status"])
        path = paths.get(r["_id"])
        name = r["media_name"]
        # the recorded path first, then the file name WhatsApp stored: both resolve by basename in the media index
        file = next(
            (found[b] for b in (os.path.basename(path or ""), os.path.basename(name or "")) if b in found), None
        )
        size = clean_size(r["media_size"])
        if file is None and isinstance(
            found, MediaIndex
        ):  # nothing by name: the content hash finds moved or renamed files
            file = found.by_hash(size, r["media_hash"])
        sender = r["remote_resource"] or None
        kind = kind_of(type_code, status)
        yield dict(
            row_id=r["_id"],
            chat_jid=r["key_remote_jid"],
            key_id=r["key_id"],
            from_me=bool(r["key_from_me"]),
            sender_jid=sender if sender != r["key_remote_jid"] else None,
            at=to_time(r["timestamp"]),
            kind=kind,
            type_code=type_code,
            status=status,
            text=r["data"] if r["data"] != "" else None,
            caption=r["media_caption"] or None,
            media_mime=r["media_mime_type"] or None,
            media_name=name or None,
            media_size=size,
            media_seconds=to_int(r["media_duration"]),
            media_hash=r["media_hash"] or None,
            media_path=path,
            media_file=file,
            # old rows hold 0.0 for "no position"; a real 0.0 only means something on a location message
            latitude=_coord(r["latitude"], kind),
            longitude=_coord(r["longitude"], kind),
            starred=bool(r["starred"]),
        )


def subjects(con: sqlite3.Connection) -> dict[str, str]:
    """group jid -> subject, from whichever chat table this version has (chat_list, or chat joined to jid)."""
    out: dict[str, str] = {}
    cl = columns(con, "chat_list")
    if {"key_remote_jid", "subject"} <= cl:
        out.update(
            {r[0]: r[1] for r in con.execute("SELECT key_remote_jid, subject FROM chat_list WHERE subject IS NOT NULL")}
        )
    ch, jd = columns(con, "chat"), columns(con, "jid")
    if {"jid_row_id", "subject"} <= ch and {"_id", "raw_string"} <= jd:
        out.update(
            {
                r[0]: r[1]
                for r in con.execute(
                    "SELECT j.raw_string, c.subject FROM chat c JOIN jid j ON j._id = c.jid_row_id WHERE c.subject IS NOT NULL"
                )
            }
        )
    return out


def participants(con: sqlite3.Connection) -> list[dict]:
    cols = columns(con, "group_participants")
    if not {"gjid", "jid"} <= cols:
        return []
    admin = "admin" if "admin" in cols else "NULL"
    return [
        dict(group_jid=r[0], jid=r[1] or r[0], is_admin=None if r[2] is None else bool(r[2]))
        for r in con.execute(f"SELECT gjid, jid, {admin} FROM group_participants")
    ]


def contacts(path: str) -> list[dict]:
    """wa.db's wa_contacts: jid, number, display_name, wa_name, is_whatsapp_user."""
    con = open_ro(path)
    try:
        cols = columns(con, "wa_contacts")
        if "jid" not in cols:
            return []

        def c(n):
            return n if n in cols else "NULL"

        rows = con.execute(
            f"SELECT jid, {c('number')}, {c('display_name')}, {c('wa_name')}, {c('is_whatsapp_user')} FROM wa_contacts WHERE jid IS NOT NULL"
        )
        return [
            dict(
                jid=r[0],
                number=r[1],
                display_name=r[2],
                wa_name=r[3],
                is_whatsapp_user=None if r[4] is None else bool(r[4]),
            )
            for r in rows
        ]
    finally:
        con.close()
