"""Parse Google Takeout YouTube history pages (watch-history.html, search-history.html, My Activity/YouTube/My Activity.html): one `outer-cell`
per entry, `Action <a href>title</a><br>[<a channel>name</a><br>][detail<br>...]<timestamp><br>` plus a caption block (products, details, why).
Standard library only."""

import html
import re
from datetime import UTC, datetime, timedelta, timezone

CELL = re.compile(r'<div class="outer-cell')
BODY = re.compile(r'<div class="content-cell[^"]*body-1">(.*?)</div>', re.S)
CAPTION = re.compile(r'<div class="content-cell[^"]*caption">(.*?)</div></div></div>', re.S)
LINK = re.compile(r'<a href="([^"]*)">(.*?)</a>', re.S)
TS = re.compile(r"^(\d{1,2}) (\w+) (\d{4}), (\d{2}):(\d{2}):(\d{2}) (\w+)$")
MONTHS = {
    m: i + 1 for i, m in enumerate(["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])
}
ZONES = {
    "GMT": 0,
    "UTC": 0,
    "BST": 1,
    "CET": 1,
    "CEST": 2,
}  # Takeout names the account's local zone; London in practice
VIDEO = re.compile(r"(?:[?&]v=|youtu\.be/|/shorts/)([\w-]{11})")
CHANNEL = re.compile(r"youtube\.com/(?:channel/([\w-]+)|(@[^/?]+)|user/([^/?]+)|c/([^/?]+))")
POST = re.compile(r"youtube\.com/post/([\w-]+)")


def parse_time(s):
    m = TS.match(s.strip())
    if not m:
        return None
    d, mon, y, hh, mm, ss, z = m.groups()
    if z not in ZONES:
        raise ValueError(f"unknown time zone {z!r} in {s!r}")
    return datetime(
        int(y), MONTHS[mon[:3]], int(d), int(hh), int(mm), int(ss), tzinfo=timezone(timedelta(hours=ZONES[z]))
    ).astimezone(UTC)


def _text(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s)).replace("\xa0", " ").strip()


def parse_cell(cell):
    """One outer-cell's HTML as a dict, or None when it has no timestamp (not an entry)."""
    m = BODY.search(cell)
    if not m:
        return None
    segs = [s for s in re.split(r"<br\s*/?>", m.group(1)) if s.strip()]
    at = parse_time(_text(segs[-1])) if segs else None
    if at is None:
        return None
    segs = segs[:-1]
    first = segs[0] if segs else ""
    lk = LINK.search(first)
    action = _text(first[: lk.start()] if lk else first)
    url = html.unescape(lk.group(1)) if lk else None
    title = _text(lk.group(2)) if lk else None
    out = dict(
        action=action, at=at, title=title, url=url, video_id=None, post_id=None, channel_id=None, channel_name=None
    )
    if url:
        v = VIDEO.search(url)
        p = POST.search(url)
        out["video_id"] = v.group(1) if v else None
        out["post_id"] = p.group(1) if p else None
    details = []
    for s in segs[1:]:
        l = LINK.search(s)
        cm = CHANNEL.search(html.unescape(l.group(1))) if l else None
        if cm and out["channel_name"] is None:
            out["channel_id"] = next(g for g in cm.groups() if g)
            out["channel_name"] = _text(l.group(2))
        else:
            details.append(_text(s))
    if not lk and segs:
        out["action"] = _text(segs[0])
        details = [_text(s) for s in segs[1:]]  # entries without a link (survey answers)
    cap = CAPTION.search(cell)
    cap = cap.group(1) if cap else ""
    cd = re.search(r"<b>Details:</b><br>((?:&emsp;[^<]*<br>)+)", cap)
    if cd:
        details += [_text(x) for x in re.findall(r"&emsp;([^<]*)<br>", cd.group(1))]
    if not out["action"] and (out["title"] or "").startswith("Shared"):
        out["action"] = "Shared"  # My Activity's shared-video rows have only a link
    out["details"] = "; ".join(d for d in details if d) or None
    out["is_ad"] = bool(cd and "Google Ads" in cd.group(1))
    return out


def parse_file(path):
    """Entries of one history file, newest first (the file's order)."""
    with open(path, encoding="utf-8") as f:
        s = f.read()
    for cell in CELL.split(s)[1:]:
        e = parse_cell(cell)
        if e:
            yield e
