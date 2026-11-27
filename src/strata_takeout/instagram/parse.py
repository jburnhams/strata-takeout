"""Read the JSON files of an Instagram export tree into the same plain dicts as `strata_takeout.facebook.parse` (one `message` shape for direct
messages, posts and comments). Direct messages share Facebook's format and parser; posts, comments, likes and follows have Instagram's own layouts.
Strings get the same UTF-8-read-as-latin-1 repair, times are seconds except in messages (milliseconds)."""

import glob
import os
from collections.abc import Iterator
from typing import Any

from strata_takeout.facebook import parse as fb
from strata_takeout.facebook.parse import Ids, fix, load, when  # noqa: F401  (re-exported: the reader interface)

ACTIVITY = "your_instagram_activity"
VIDEO = (".mp4", ".mov", ".webm")


def threads(tree: str, ids: Ids) -> Iterator[tuple[dict, list[str], list[dict]]]:
    yield from fb.threads(tree, ids, ACTIVITY)


def _media(items: Any) -> list[dict]:
    out: list[dict] = []
    for m in items or []:
        if isinstance(m, dict) and isinstance(m.get("uri"), str):
            kind = "video" if m["uri"].lower().endswith(VIDEO) else "photo"
            x = fb._att(kind, m["uri"], None, m.get("creation_timestamp"))
            if x:
                out.append(x)
    return out


def _post(x: dict, ids: Ids, audience: str) -> dict:
    ts = x.get("creation_timestamp") or (x.get("media") or [{}])[0].get("creation_timestamp")
    title = x.get("title") if isinstance(x.get("title"), str) and x["title"] else None
    if title is None:  # a one-media post keeps its caption on the media
        t = (x.get("media") or [{}])[0].get("title")
        title = t if isinstance(t, str) and t else None
    return dict(
        ext_id=ids.make("post", audience, ts, None),
        kind="post",
        audience=audience,
        audience_name=None,
        thread_id=None,
        at=when(ts),
        author=None,
        body=title,
        title=None,
        call_seconds=None,
        unsent=False,
        reactions=[],
        attachments=_media(x.get("media")),
    )


def _list(d: Any, key: str | None = None) -> list[dict]:
    if isinstance(d, dict) and key:
        d = d.get(key)
    return [x for x in d if isinstance(x, dict)] if isinstance(d, list) else []


def activity(tree: str, ids: Ids) -> Iterator[dict]:
    """The account's own posts (`media/posts_*.json`), archived posts, and the comments it made."""
    a = os.path.join(tree, ACTIVITY)
    for p in sorted(glob.glob(os.path.join(glob.escape(a), "media", "posts_*.json"))):
        for x in _list(load(p)):
            yield _post(x, ids, "timeline")
    for x in _list(load(os.path.join(a, "media", "archived_posts.json")), "ig_archived_post_media"):
        yield _post(x, ids, "archive")
    for p in sorted(glob.glob(os.path.join(glob.escape(a), "comments", "post_comments*.json"))):
        for x in _list(load(p)):
            sm = x.get("string_map_data") if isinstance(x.get("string_map_data"), dict) else {}
            ts = (sm.get("Time") or {}).get("timestamp")
            owner = (sm.get("Media Owner") or {}).get("value")
            body = (sm.get("Comment") or {}).get("value")
            owner = owner if isinstance(owner, str) else None
            yield dict(
                ext_id=ids.make("comment", f"post:{owner or ''}", ts, None),
                kind="comment",
                audience="post",
                audience_name=owner,
                thread_id=None,
                at=when(ts),
                author=None,
                body=body if isinstance(body, str) else None,
                title=None,
                call_seconds=None,
                unsent=False,
                reactions=[],
                attachments=_media(x.get("media_list_data")),
            )


def _label(x: dict, name: str) -> Any:
    for lv in x.get("label_values") or []:
        if isinstance(lv, dict) and lv.get("label") == name:
            return lv.get("value")
    return None


def likes(tree: str) -> Iterator[dict]:
    """Posts the account liked: the owner and link of each (the likes of comments are in `liked_comments.json`, same shape)."""
    for name in ("liked_posts.json", "liked_comments.json"):
        for x in _list(load(os.path.join(tree, ACTIVITY, "likes", name)), "likes_comment_likes"):
            owner, url = _label(x, "Title"), _label(x, "URL")
            yield dict(
                reaction="like",
                actor=None,
                target_title=" ".join(v for v in (owner, url) if isinstance(v, str)) or None,
                at=when(x.get("timestamp")),
            )


def follows(tree: str) -> Iterator[dict]:
    base = os.path.join(tree, "connections", "followers_and_following")
    spec = (
        ("follower", "followers_*.json", None),
        ("following", "following.json", "relationships_following"),
        ("close_friend", "close_friends.json", None),
    )
    for direction, pat, key in spec:
        for p in sorted(glob.glob(os.path.join(glob.escape(base), pat))):
            for x in _list(load(p), key):
                for s in x.get("string_list_data") or []:
                    if isinstance(s, dict):
                        name = s.get("value") if isinstance(s.get("value"), str) else x.get("title")
                        yield dict(
                            direction=direction,
                            name=name if isinstance(name, str) else None,
                            href=s.get("href") if isinstance(s.get("href"), str) else None,
                            at=when(s.get("timestamp")),
                        )


def json_files(tree: str) -> list[str]:
    """Every JSON file the readers above read (the fingerprint of a load)."""
    a = glob.escape(os.path.join(tree, ACTIVITY))
    out = glob.glob(os.path.join(a, "messages", "*", "*", "message_*.json"))
    for pat in (
        "media/posts_*.json",
        "media/archived_posts.json",
        "comments/post_comments*.json",
        "likes/liked_*.json",
    ):
        out += glob.glob(os.path.join(a, pat))
    out += glob.glob(os.path.join(glob.escape(tree), "connections", "followers_and_following", "*.json"))
    return sorted(out)
