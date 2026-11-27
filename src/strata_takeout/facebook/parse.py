"""Read the JSON files of a Facebook export tree into plain dicts, one `message` shape for chat lines, posts and comments.

Facebook writes every string as UTF-8 bytes that were then read as latin-1 ("Ã©" for "é"): `fix` undoes it on every string, and leaves a string
alone when it is not that kind of text. Chat times are milliseconds, everything else seconds. A message has no id of its own in the export, so
`ext_id` is built from what identifies it (kind, thread or audience, time, author) plus a counter for exact repeats."""

import glob
import hashlib
import json
import os
import re
from collections import Counter
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

ACTIVITY = "your_facebook_activity"
OWN = re.compile(r"^(?:his|her|their|your|its) own\b", re.I)
OWNER = re.compile(
    r"^(?P<who>.+?)['’]s?\s+(?:post|photo|video|comment|album|story|reel|link|note|event|memory|status)", re.I
)
COMMENT_TITLE = re.compile(r"^(?P<a>.+?)\s(?:commented on|replied to)\s(?P<t>.+?)\.?$", re.S)
GROUP_TITLE = re.compile(r"\b(?:posted|wrote|shared)\b.*?\b(?:in|to|on)\s+(?P<g>.+?)\.?$", re.S)


def fix(s: str) -> str:
    """Undo UTF-8-read-as-latin-1; a string that is not that stays as it is."""
    try:
        return s.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s


def fix_tree(o: Any) -> Any:
    if isinstance(o, str):
        return fix(o)
    if isinstance(o, list):
        return [fix_tree(x) for x in o]
    if isinstance(o, dict):
        return {fix(k): fix_tree(v) for k, v in o.items()}
    return o


def load(path: str) -> Any:
    try:
        with open(path, encoding="utf-8") as f:
            return fix_tree(json.load(f))
    except (OSError, ValueError):
        return None


def when(ts: Any, ms: bool = False) -> datetime | None:
    if not isinstance(ts, (int, float)) or ts <= 0:
        return None
    try:
        return datetime.fromtimestamp(ts / 1000 if ms else ts, UTC)
    except (OverflowError, OSError, ValueError):
        return None


class Ids:
    """ext_id: stable across loads and re-exports; the counter only separates exact repeats of (kind, where, time, author)."""

    def __init__(self) -> None:
        self.seen: Counter[str] = Counter()

    def make(self, kind: str, where: str | None, ts: Any, author: str | None) -> str:
        key = f"{kind}|{where or ''}|{ts}|{author or ''}"
        n = self.seen[key]
        self.seen[key] += 1
        return hashlib.sha1(f"{key}|{n}".encode()).hexdigest()


def _att(kind: str, uri: Any = None, url: Any = None, ts: Any = None) -> dict | None:
    if not (isinstance(uri, str) and uri) and not (isinstance(url, str) and url):
        return None
    return dict(
        kind=kind,
        path=uri if isinstance(uri, str) and uri else None,
        url=url if isinstance(url, str) else None,
        taken_at=when(ts),
    )


def attachments_of(items: Any) -> list[dict]:
    """The `attachments[].data[]` of a post or comment: media files, external links."""
    out: list[dict] = []
    for a in items or []:
        for d in (a or {}).get("data", []) if isinstance(a, dict) else []:
            if not isinstance(d, dict):
                continue
            if isinstance(d.get("media"), dict):
                m = d["media"]
                kind = "video" if str(m.get("uri", "")).lower().endswith((".mp4", ".mov", ".webm")) else "photo"
                x = _att(kind, m.get("uri"), None, m.get("creation_timestamp"))
            elif isinstance(d.get("external_context"), dict):
                x = _att("link", None, d["external_context"].get("url"))
            else:
                x = None
            if x:
                out.append(x)
    return out


def chat_attachments(m: dict) -> list[dict]:
    out: list[dict] = []
    for key, kind in (
        ("photos", "photo"),
        ("videos", "video"),
        ("gifs", "gif"),
        ("audio_files", "audio"),
        ("files", "file"),
    ):
        for d in m.get(key) or []:
            if isinstance(d, dict):
                x = _att(kind, d.get("uri"), None, d.get("creation_timestamp"))
                if x:
                    out.append(x)
    if isinstance(m.get("sticker"), dict):
        x = _att("sticker", m["sticker"].get("uri"))
        if x:
            out.append(x)
    if isinstance(m.get("share"), dict) and isinstance(m["share"].get("link"), str):
        x = _att("link", None, m["share"]["link"])
        if x:
            out.append(x)
    return out


def threads(tree: str, ids: Ids) -> Iterator[tuple[dict, list[str], list[dict]]]:
    """(thread, member names, messages) for every Messenger conversation folder."""
    base = os.path.join(tree, ACTIVITY, "messages")
    seen: dict[str, tuple[dict, list[str], list[dict]]] = {}
    for p in sorted(glob.glob(os.path.join(glob.escape(base), "*", "*", "message_*.json"))):
        d = load(p)
        if not isinstance(d, dict) or not isinstance(d.get("messages"), list):
            continue
        folder, tid = os.path.basename(os.path.dirname(os.path.dirname(p))), os.path.basename(os.path.dirname(p))
        names = [
            x["name"] for x in d.get("participants") or [] if isinstance(x, dict) and isinstance(x.get("name"), str)
        ]
        t = seen.setdefault(
            tid,
            (
                dict(
                    thread_id=tid,
                    folder=folder,
                    title=d.get("title") if isinstance(d.get("title"), str) else None,
                    participants=len(names),
                    still_member=d.get("is_still_participant")
                    if isinstance(d.get("is_still_participant"), bool)
                    else None,
                ),
                names,
                [],
            ),
        )
        for m in d["messages"]:
            if not isinstance(m, dict) or not isinstance(m.get("timestamp_ms"), (int, float)):
                continue
            who = m.get("sender_name") if isinstance(m.get("sender_name"), str) else None
            t[2].append(
                dict(
                    ext_id=ids.make("chat", tid, int(m["timestamp_ms"]), who),
                    kind="chat",
                    audience="thread",
                    audience_name=t[0]["title"],
                    thread_id=tid,
                    at=when(m["timestamp_ms"], ms=True),
                    author=who,
                    body=m.get("content") if isinstance(m.get("content"), str) else None,
                    title=None,
                    call_seconds=m.get("call_duration") if isinstance(m.get("call_duration"), int) else None,
                    unsent=bool(m.get("is_unsent")),
                    reactions=[
                        dict(reaction=r.get("reaction"), actor=r.get("actor"), at=None)
                        for r in m.get("reactions") or []
                        if isinstance(r, dict)
                    ],
                    attachments=chat_attachments(m),
                )
            )
    yield from seen.values()


def _post_text(data: Any) -> str | None:
    parts = [x["post"] for x in data or [] if isinstance(x, dict) and isinstance(x.get("post"), str) and x["post"]]
    return "\n\n".join(parts) or None


def _wrap(d: Any, key: str) -> list[dict]:
    """Some files are a bare list, some `{key: [...]}`."""
    if isinstance(d, dict):
        d = d.get(key)
    return [x for x in d if isinstance(x, dict)] if isinstance(d, list) else []


def _label(x: dict, name: str) -> dict:
    for lv in x.get("label_values") or []:
        if isinstance(lv, dict) and lv.get("label") == name:
            return lv
    return {}


def activity(tree: str, ids: Ids) -> Iterator[dict]:
    """Posts, posts elsewhere, group posts, comments (own timeline, others' posts, groups)."""
    a = os.path.join(tree, ACTIVITY)
    for p in sorted(glob.glob(os.path.join(glob.escape(a), "posts", "your_posts*.json"))):
        for x in _wrap(load(p), "status_updates"):
            ts = x.get("timestamp")
            yield dict(
                ext_id=ids.make("post", "timeline", ts, None),
                kind="post",
                audience="timeline",
                audience_name=None,
                thread_id=None,
                at=when(ts),
                author=None,
                body=_post_text(x.get("data")),
                title=x.get("title") if isinstance(x.get("title"), str) else None,
                call_seconds=None,
                unsent=False,
                reactions=[],
                attachments=attachments_of(x.get("attachments")),
            )
    for x in _wrap(load(os.path.join(a, "posts", "posts_on_other_pages_and_profiles.json")), "posts"):
        ts = x.get("timestamp")
        target = _label(x, "Target").get("value")
        media = [
            y
            for m in _label(x, "Media").get("media") or []
            if isinstance(m, dict) and (y := _att("photo", m.get("uri"), None, m.get("creation_timestamp")))
        ]
        yield dict(
            ext_id=ids.make("post", f"profile:{target or ''}", ts, None),
            kind="post",
            audience="profile",
            audience_name=target if isinstance(target, str) else None,
            thread_id=None,
            at=when(ts),
            author=None,
            body=_label(x, "Message").get("value") if isinstance(_label(x, "Message").get("value"), str) else None,
            title=None,
            call_seconds=None,
            unsent=False,
            reactions=[],
            attachments=media,
        )
    for x in _wrap(load(os.path.join(a, "groups", "group_posts_and_comments.json")), "group_posts_v2"):
        ts, title = x.get("timestamp"), x.get("title") if isinstance(x.get("title"), str) else None
        g = GROUP_TITLE.search(title or "")
        yield dict(
            ext_id=ids.make("post", f"group:{title or ''}", ts, None),
            kind="post",
            audience="group",
            audience_name=g.group("g") if g else None,
            thread_id=None,
            at=when(ts),
            author=None,
            body=_post_text(x.get("data")),
            title=title,
            call_seconds=None,
            unsent=False,
            reactions=[],
            attachments=attachments_of(x.get("attachments")),
        )
    for x in _wrap(load(os.path.join(a, "groups", "your_comments_in_groups.json")), "group_comments_v2"):
        yield from _comment(x, ids, group=True)
    for x in _wrap(load(os.path.join(a, "comments_and_reactions", "comments.json")), "comments_v2"):
        yield from _comment(x, ids, group=False)


def _comment(x: dict, ids: Ids, group: bool) -> Iterator[dict]:
    c = None
    for d in x.get("data") or []:
        if isinstance(d, dict) and isinstance(d.get("comment"), dict):
            c = d["comment"]
    ts = (c or {}).get("timestamp") or x.get("timestamp")
    title = x.get("title") if isinstance(x.get("title"), str) else None
    who = c.get("author") if c and isinstance(c.get("author"), str) else None
    if group:
        aud, name = "group", c.get("group") if c and isinstance(c.get("group"), str) else None
    else:
        aud, name = "post", None
        m = COMMENT_TITLE.match(title or "")
        if m:
            t = m.group("t")
            o = OWNER.match(t)
            name = "self" if OWN.match(t) else o.group("who") if o else None
    yield dict(
        ext_id=ids.make("comment", f"{aud}:{name or ''}", ts, who),
        kind="comment",
        audience=aud,
        audience_name=name,
        thread_id=None,
        at=when(ts),
        author=who,
        body=c.get("comment") if c and isinstance(c.get("comment"), str) else None,
        title=title,
        call_seconds=None,
        unsent=False,
        reactions=[],
        attachments=attachments_of(x.get("attachments")),
    )


def likes(tree: str) -> Iterator[dict]:
    """The person's own reactions to things elsewhere (not attached to any message of ours)."""
    for p in sorted(
        glob.glob(os.path.join(glob.escape(tree), ACTIVITY, "comments_and_reactions", "likes_and_reactions*.json"))
    ):
        for x in _wrap(load(p), "likes_and_reactions_v2"):
            r = None
            for d in x.get("data") or []:
                if isinstance(d, dict) and isinstance(d.get("reaction"), dict):
                    r = d["reaction"]
            yield dict(
                reaction=r.get("reaction") if r else None,
                actor=r.get("actor") if r else None,
                target_title=x.get("title") if isinstance(x.get("title"), str) else None,
                at=when(x.get("timestamp")),
            )


def json_files(tree: str) -> list[str]:
    """Every JSON file `threads`, `activity` and `likes` read (the fingerprint of a load)."""
    a = glob.escape(os.path.join(tree, ACTIVITY))
    out = glob.glob(os.path.join(a, "messages", "*", "*", "message_*.json"))
    for pat in (
        "posts/your_posts*.json",
        "posts/posts_on_other_pages_and_profiles.json",
        "groups/group_posts_and_comments.json",
        "groups/your_comments_in_groups.json",
        "comments_and_reactions/comments.json",
        "comments_and_reactions/likes_and_reactions*.json",
    ):
        out += glob.glob(os.path.join(a, pat))
    return sorted(out)
