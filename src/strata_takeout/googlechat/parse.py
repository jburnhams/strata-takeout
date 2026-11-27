"""Readers for Google's chat exports, each yielding plain dicts for `ingest`:

  takeout(tree)      the Takeout `Google Chat/` folder (`Groups/<DM|Space id>/{group_info,messages}.json` plus media files beside them)
  gmail_mbox(path)   old Gmail/GTalk chat logs in a Gmail mbox (mails labelled Chat): one mail holds a run of chat lines as XML
  gmvault(tree)      the same mails from a gmvault backup (`<id>.eml[.gz]` + `<id>.meta` JSON)

A conversation dict: ext_id, kind, name, info, members[{name,email,user_type}], messages[...], orphans[...] (media no message refers to).
A message dict: ext_id, sender_*, at, at_precision, text, topic_id, raw, attachments[{original_name, export_name, path, status, match, seq}]."""

import hashlib
import json
import os
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from datetime import UTC, datetime
from email import policy
from email.message import Message
from email.parser import BytesParser
from typing import Any

from strata_takeout.gmail import gmvault as gmvault_items
from strata_takeout.gmail import parse as gm

GROUPS = "Groups"
CHAT_LABEL = "chat"
MEDIA = re.compile(r"^(?P<stem>.+?)(?:\((?P<n>\d+)\))?(?P<ext>\.[^.\\/()]*)?$")
DATE = re.compile(r"^\w+, (\d{1,2}) (\w+) (\d{4}) at (\d{1,2}):(\d\d):(\d\d)(?:\s*(UTC|GMT))?$")
MONTHS = {
    m: i
    for i, m in enumerate(
        [
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December",
        ],
        1,
    )
}


def takeout_root(tree: str) -> str | None:
    """The folder holding `Groups/`: `Google Chat` under `tree`, or `tree` itself. Only a `Groups/` with chat groups in it counts (a Takeout also has a `Groups/` of Google Groups)."""
    for p in (os.path.join(tree, "Google Chat"), tree):
        g = os.path.join(p, GROUPS)
        if os.path.isdir(g) and any(os.path.isfile(os.path.join(g, d, "messages.json")) for d in os.listdir(g)):
            return p
    return None


def json_files(root: str) -> list[str]:
    g = os.path.join(root, GROUPS)
    return sorted(
        os.path.join(g, d, f)
        for d in os.listdir(g)
        for f in ("group_info.json", "messages.json")
        if os.path.isfile(os.path.join(g, d, f))
    )


def when(s: Any) -> datetime | None:
    """`Sunday, 26 May 2013 at 13:12:51 UTC` (English, the export's own wording)."""
    m = DATE.match(str(s or "").strip())
    if not m or m[2] not in MONTHS:
        return None
    d, mon, y, h, mi, se = int(m[1]), MONTHS[m[2]], int(m[3]), int(m[4]), int(m[5]), int(m[6])
    try:
        return datetime(y, mon, d, h, mi, se, tzinfo=UTC)
    except ValueError:
        return None


def clean(s: Any) -> str | None:
    return None if s is None else str(s).replace("\x00", "")


def scrub(o: Any) -> Any:
    """Postgres text and JSON cannot hold NUL: drop them anywhere in a structure."""
    if isinstance(o, str):
        return o.replace("\x00", "")
    if isinstance(o, list):
        return [scrub(x) for x in o]
    if isinstance(o, dict):
        return {k: scrub(v) for k, v in o.items()}
    return o


def match_media(files: set[str], refs: list[list[dict]]) -> tuple[list[list[dict]], list[str]]:
    """Link each message's `attached_files` to a file beside it. `refs` is, per message in order, its attached_files entries; the result is the same shape with
    `path`, `status` (present | missing) and `match`, plus the files nothing claimed.
      exact      the export_name is a file
      unquoted   the export name carries a URL query (`1234?account_id=0.jpg`); the file was stored with `?` as `_`
      sequence   the same name appears again: the export stored the n-th copy as `name(n).ext`
      shared     the name is a file already claimed by an earlier message (the export reused it)"""
    used: set[str] = set()
    out: list[list[dict]] = []
    for msg in refs:
        row = []
        for a in msg:
            name = str(a.get("export_name") or "")
            found = how = None
            for cand, label in ((name, "exact"), (name.replace("?", "_"), "unquoted")):
                if cand in files and cand not in used:
                    found, how = cand, label
                    break
            if found is None:
                base = name.replace("?", "_")
                m = MEDIA.match(base)
                if m:
                    for n in range(1, 50):
                        cand = f"{m['stem']}({n}){m['ext'] or ''}"
                        if cand in files and cand not in used:
                            found, how = cand, "sequence"
                            break
            if found is None:
                for cand in (name, name.replace("?", "_")):
                    if cand in files:
                        found, how = cand, "shared"
                        break
            if found:
                used.add(found)
            row.append(dict(a) | dict(path=found, status="present" if found else "missing", match=how))
        out.append(row)
    return out, sorted(files - used)


def takeout(tree: str) -> Iterator[dict]:
    root = takeout_root(tree)
    if root is None:
        return
    groups = os.path.join(root, GROUPS)
    for d in sorted(os.listdir(groups)):
        gdir = os.path.join(groups, d)
        mp = os.path.join(gdir, "messages.json")
        if not os.path.isfile(mp):
            continue
        with open(mp, encoding="utf-8") as f:
            msgs = scrub(json.load(f)).get("messages", [])
        info: dict = {}
        ip = os.path.join(gdir, "group_info.json")
        if os.path.isfile(ip):
            with open(ip, encoding="utf-8") as f:
                info = scrub(json.load(f))
        files = {
            n
            for n in os.listdir(gdir)
            if n not in ("messages.json", "group_info.json") and os.path.isfile(os.path.join(gdir, n))
        }
        linked, orphans = match_media(files, [m.get("attached_files") or [] for m in msgs])
        seen: dict[str, int] = {}
        out = []
        for m, atts in zip(msgs, linked, strict=True):
            ext = str(m.get("message_id") or "")
            seen[ext] = seen.get(ext, 0) + 1
            if seen[ext] > 1:
                ext += f"~{seen[ext]}"  # an id the export repeated stays unique
            c = m.get("creator") or {}
            at = when(m.get("created_date"))
            out.append(
                dict(
                    ext_id=ext,
                    kind="message",
                    sender_name=c.get("name"),
                    sender_email=(c.get("email") or "").lower() or None,
                    sender_type=c.get("user_type"),
                    at=at,
                    at_precision="s" if at else None,
                    text=m.get("text"),
                    topic_id=m.get("topic_id"),
                    raw=m,
                    attachments=[
                        dict(
                            seq=i,
                            **{k: v for k, v in a.items() if k != "path"},
                            path=os.path.join(GROUPS, d, a["path"]) if a["path"] else None,
                        )
                        for i, a in enumerate(atts)
                    ],
                )
            )
        yield dict(
            ext_id=d,
            kind="space" if d.startswith("Space") else "dm" if d.startswith("DM") else "chat",
            name=info.get("name"),
            info=info,
            members=[
                dict(name=x.get("name"), email=(x.get("email") or "").lower() or None, user_type=x.get("user_type"))
                for x in info.get("members", [])
            ],
            messages=out,
            orphans=[dict(export_name=n, path=os.path.join(GROUPS, d, n)) for n in orphans],
        )


# ---- old Gmail chat logs -------------------------------------------------------------------------------------------------------------------

NS = {"cli": "jabber:client", "int": "google:internal", "ts": "google:timestamp", "dl": "jabber:x:delay"}
INT_TS = "{google:internal}time-stamp"


def bare(jid: Any) -> str | None:
    """`name@example.org/Resource` → `name@example.org` (lowercase)."""
    s = str(jid or "").split("/")[0].strip().lower()
    return s or None


def xml_time(el: ET.Element) -> tuple[datetime | None, str | None]:
    t = el.find("ts:time", NS)
    ms = t.get("ms") if t is not None else None
    if ms is None:
        ms = el.get(INT_TS)
    if ms and ms.isdigit() and int(ms) > 0:  # a zero stamp is "unknown", not 1970
        return datetime.fromtimestamp(int(ms) / 1000, UTC), "ms"
    x = el.find("dl:x", NS)
    stamp = x.get("stamp") if x is not None else None
    if stamp:
        try:
            return datetime.strptime(stamp, "%Y%m%dT%H:%M:%S").replace(tzinfo=UTC), "s"
        except ValueError:
            pass
    return None, None


def chat_part(m: Message) -> str | None:
    for p in m.walk():
        if p.get_content_type() == "text/xml":
            raw = p.get_payload(decode=True)
            if isinstance(raw, bytes) and b"google:archive:conversation" in raw:
                return raw.decode(p.get_content_charset() or "utf-8", "replace")
    return None


def mail_chat(
    raw: bytes, key: str | None = None, at: datetime | None = None, labels: list[str] | None = None
) -> dict | None:
    """One chat-log mail as a conversation dict with its lines. `at` is the fallback time (mail date or separator) for lines without a stamp of their own."""
    m = BytesParser(policy=policy.compat32).parsebytes(raw)
    mid = gm.header(m.get("Message-ID")) or key or hashlib.sha1(raw).hexdigest()
    mail_at = gm.when(m, raw) or at
    xml = chat_part(m)
    lines: list[dict] = []
    people: set[str] = set()
    if xml:
        try:
            root = ET.fromstring(xml.encode("utf-8", "replace"))
        except ET.ParseError:
            root = None
        for el in root.findall("cli:message", NS) if root is not None else []:
            frm, to = bare(el.get("from")), bare(el.get("to"))
            people.update(p for p in (frm, to) if p)
            t, prec = xml_time(el)
            body = el.find("cli:body", NS)
            lines.append(
                dict(
                    sender_email=frm,
                    at=t or mail_at,
                    at_precision=prec or ("mail" if mail_at else None),
                    text=clean(body.text if body is not None else None),
                    raw=dict(attrs={k.split("}")[-1]: v for k, v in el.attrib.items()}, to=to),
                )
            )
    if not lines:  # no usable XML: the mail's text is the whole line
        sender = bare(gm.parseaddr(gm.header(m.get("From")))[1])
        to = bare(gm.parseaddr(gm.header(m.get("To")))[1])
        people.update(p for p in (sender, to) if p)
        body = (
            next((gm.text_of(p) for p in m.walk() if p.get_content_type() == "text/plain"), None)
            if hasattr(gm, "text_of")
            else None
        )
        lines.append(
            dict(
                sender_email=sender,
                at=mail_at,
                at_precision="mail" if mail_at else None,
                text=clean(body),
                raw=dict(to=to),
            )
        )
    for i, x in enumerate(lines):
        x |= dict(
            ext_id=f"{mid}#{i}",
            kind="message",
            sender_name=None,
            sender_type=None,
            topic_id=None,
            attachments=[],
            raw=dict(
                x["raw"],
                mail=dict(
                    message_id=mid,
                    labels=labels,
                    thread=str(m.get("X-GM-THRID") or "").strip() or None,
                    subject=gm.header(m.get("Subject")) or None,
                ),
            ),
        )
    return dict(
        ext_id="",
        kind="chat",
        name=None,
        info=None,
        people=sorted(people),
        members=[dict(name=None, email=p, user_type=None) for p in sorted(people)],
        messages=lines,
        orphans=[],
    )


def group_id(people: list[str]) -> str:
    return hashlib.sha1("\n".join(people).encode()).hexdigest()[:24]


def is_chat(labels: list[str]) -> bool:
    return CHAT_LABEL in labels or "gmvault-chats" in labels


def gmail_mbox(path: str) -> Iterator[dict]:
    """Mails labelled Chat in a Gmail mbox, in file order (streaming)."""
    for batch in gm.split(path):
        for off, raw in batch:
            head = raw.split(b"\n\n", 1)[0]
            m = BytesParser(policy=policy.compat32).parsebytes(head + b"\n\n", headersonly=True)
            ls = gm.labels(m)
            if not is_chat(ls):
                continue
            c = mail_chat(raw, key=f"offset-{off}", labels=ls)
            if c:
                yield c


def gmvault(tree: str) -> Iterator[dict]:
    """Chat items of a gmvault backup (`gmail.gmvault.chat_items`), one per Message-ID."""
    seen: set[str] = set()
    for it in gmvault_items.chat_items(tree):
        key = it["msg_id"] or str(it["gm_id"] or hashlib.sha1(it["raw"]).hexdigest())
        if key in seen:
            continue
        seen.add(key)
        ts = it["internal_date"]
        c = mail_chat(
            it["raw"], key=key, at=datetime.fromtimestamp(ts, UTC) if ts is not None else None, labels=it["labels"]
        )
        if c:
            yield c
