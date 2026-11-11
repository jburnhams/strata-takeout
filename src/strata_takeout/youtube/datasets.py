"""The non-history YouTube files of a Takeout (CSV), each a dataset: files under `YouTube and YouTube Music/` -> rows of one table. A dataset is always loaded whole (delete the account's rows, insert), so it is idempotent.
Takeout may hold several versions of one file (`videos.csv`, `videos(1).csv`, `videos(2).csv`): they are read in name order and a later row of the same key wins."""
import csv
import glob
import json
import os
import re
from datetime import datetime, timezone

from .models import Comment, LiveChat, Message, MusicTrack, OwnChannel, Playlist, PlaylistItem, Subscription, Video

ROOT = 'YouTube and YouTube Music'
csv.field_size_limit(1 << 30)


def timestamp(s):
    """ISO 8601 as written by Takeout (fractions up to nanoseconds, +00:00 or Z) -> aware datetime, None when blank."""
    s = (s or '').strip()
    if not s: return None
    s = re.sub(r'(\.\d{6})\d+', r'\1', s.replace('Z', '+00:00'))
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def num(s, kind=float):
    try: return kind(s) if (s or '').strip() != '' else None
    except ValueError: return None


def read(path):
    with open(path, encoding='utf-8-sig', newline='') as f: yield from csv.DictReader(f)


def versions(tree, rel):
    """`rel` (e.g. 'Video metadata/videos') plus its `(1)`, `(2)` twins, in order."""
    base = os.path.join(tree, ROOT, rel)
    return sorted(glob.glob(glob.escape(base) + '.csv') + glob.glob(glob.escape(base) + '(*).csv'), key=lambda p: (len(p), p))


def _merge(files, key, mapper):
    out = {}
    for p in files:
        for r in read(p):
            k = r.get(key)
            if k: out.setdefault(k, {}).update({a: b for a, b in mapper(r).items() if b is not None})
    return out


def subscriptions(tree, account):
    return [Subscription(account=account, channel_id=r['Channel ID'], channel_url=r.get('Channel URL') or None, title=r.get('Channel title') or None) for p in versions(tree, 'subscriptions/subscriptions') for r in read(p) if r.get('Channel ID')]


def playlists(tree, account):
    lists = [Playlist(account=account, playlist_id=r['Playlist ID'], title=r.get('Playlist title (original)') or None, language=r.get('Playlist title (original) language') or None, created_at=timestamp(r.get('Playlist create timestamp')), updated_at=timestamp(r.get('Playlist update timestamp')),
                      video_order=r.get('Playlist video order') or None, visibility=r.get('Playlist visibility') or None, add_to_top={'true': True, 'false': False}.get((r.get('Add new videos to top') or '').lower())) for p in versions(tree, 'playlists/playlists') for r in read(p) if r.get('Playlist ID')]
    items = []
    for p in sorted(glob.glob(os.path.join(glob.escape(tree), ROOT, 'playlists', '*.csv'))):
        name = os.path.basename(p)[:-4]
        if name == 'playlists': continue
        stem = re.sub(r' (videos|vid|vide|video)$', '', name)                               # Takeout cuts long names ("... mix vid.csv")
        norm = lambda t: re.sub(r'[^\w]', '_', t or '')                                    # the file name has `_` for characters a file name cannot hold (We_ve for We've)
        match = next((l for l in lists if norm(l.title) == norm(stem)), None) or next((l for l in lists if l.title and norm(l.title).startswith(norm(stem))), None)
        items += [PlaylistItem(account=account, playlist_title=stem, playlist_id=match.playlist_id if match else None, video_id=r['Video ID'], position=i, added_at=timestamp(r.get('Playlist video creation timestamp'))) for i, r in enumerate(read(p)) if r.get('Video ID')]
    return lists + items


def videos(tree, account):
    tags = lambda r: [r[k] for k in sorted((k for k in r if k.startswith('Tag ')), key=lambda k: int(k.split()[1])) if r.get(k)] or None
    v = _merge(versions(tree, 'Video metadata/videos'), 'Video ID', lambda r: dict(title=r.get('Video title (original)') or None, description=r.get('Video description (original)') or None, channel_id=r.get('Channel ID') or None, duration_ms=num(r.get('Approx duration (ms)'), int), language=r.get('Video audio language') or None,
                                                                                    category=r.get('Video category') or None, privacy=r.get('Privacy') or None, state=r.get('Video state') or None, created_at=timestamp(r.get('Video create timestamp')), published_at=timestamp(r.get('Video publish timestamp')), tags=tags(r)))
    # a recording of 0,0 is Takeout's "no location"
    for k, d in _merge(versions(tree, 'Video metadata/video recordings'), 'Video ID', lambda r: dict(altitude=num(r.get('Video recording altitude')), latitude=num(r.get('Video recording latitude')), longitude=num(r.get('Video recording longitude'))) if (num(r.get('Video recording latitude')), num(r.get('Video recording longitude'))) != (0.0, 0.0) else {}).items(): v.setdefault(k, {}).update(d)
    seg = lambda r, pre: ''.join(r[k] for k in sorted((k for k in r if k.startswith(pre)), key=lambda k: int(k.rsplit(' ', 1)[1])) if r.get(k)) or None
    for k, d in _merge(versions(tree, 'Video metadata/video texts'), 'Video ID', lambda r: dict(text_title=seg(r, 'Video title text segments'), text_description=seg(r, 'Video description text segments'), text_updated_at=timestamp(r.get('Video text update timestamp')))).items(): v.setdefault(k, {}).update(d)
    return [Video(account=account, video_id=k, **d) for k, d in v.items()]


def own_channels(tree, account):
    ch = {}
    for p in versions(tree, 'channels/channel'):
        for r in read(p): ch[r['Channel ID']] = OwnChannel(account=account, channel_id=r['Channel ID'], title=r.get('Channel title (Original)') or None, visibility=r.get('Channel visibility') or None)
    for p in versions(tree, 'channels/channel URL configs'):
        for r in read(p):
            if r['Channel ID'] in ch: ch[r['Channel ID']].vanity_urls = sorted({v for k, v in r.items() if k.startswith('channel vanity URL') and v})
    for p in versions(tree, 'channels/channel feature data'):
        for r in read(p):
            if r['Channel ID'] in ch: ch[r['Channel ID']].settings = {k: v for k, v in r.items() if k != 'Channel ID' and v != ''}
    return list(ch.values())


def _segments(raw):
    """Comment text is a comma-separated run of JSON objects ({"text": ...}); the plain text is their `text`s joined."""
    try: return ''.join(x.get('text', '') for x in json.loads('[' + raw + ']'))
    except (ValueError, AttributeError): return raw


def comments(tree, account):
    return [Comment(account=account, comment_id=r['Comment ID'], channel_id=r.get('Channel ID') or None, created_at=timestamp(r.get('Comment create timestamp')), video_id=r.get('Video ID') or None, parent_id=r.get('Parent comment ID') or None, top_level_id=r.get('Top-level comment ID') or None,
                    text=_segments(r.get('Comment text') or ''), raw=r.get('Comment text') or None, price=r.get('Price') or None) for p in versions(tree, 'comments/comments') for r in read(p) if r.get('Comment ID')]


def live_chats(tree, account):
    return [LiveChat(account=account, chat_id=r['Live chat ID'], channel_id=r.get('Channel ID') or None, created_at=timestamp(r.get('Live chat create timestamp')), video_id=r.get('Video ID') or None, text=_segments(r.get('Live chat text') or ''), price=r.get('Price') or None) for p in versions(tree, 'live chats/live chats') for r in read(p) if r.get('Live chat ID')]


def messages(tree, account):
    return [Message(account=account, message_id=r['Message ID'], sender_channel_id=r.get('Sender channel ID') or None, created_at=timestamp(r.get('Message create timestamp (UTC)')), conversation_id=r.get('Conversation ID') or None, recipients=r.get('Recipient channel IDs') or None, video_id=r.get('Media shared video ID') or None)
            for p in sorted(glob.glob(os.path.join(glob.escape(tree), ROOT, 'messages', 'conversation_*.csv'))) for r in read(p) if r.get('Message ID')]


def duration(s):
    """'3m26.471s' / '1h2m3s' -> seconds."""
    m = re.fullmatch(r'(?:(\d+)h)?(?:(\d+)m)?(?:([\d.]+)s)?', (s or '').strip())
    return (int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60 + float(m.group(3) or 0)) if m and s.strip() else None


def music(tree, account):
    lib = [MusicTrack(account=account, kind='library', video_id=r.get('Video ID') or None, title=r.get('Song Title') or None, album=r.get('Album Title') or None, artist=r.get('Artist Name 1') or None) for p in versions(tree, 'music (library and uploads)/music library songs') for r in read(p)]
    up = [MusicTrack(account=account, kind='upload', title=r.get('Song Title') or None, album=r.get('Album Title') or None, artist=r.get('Artist Name 1') or None, artist2=r.get('Artist Name 2') or None, duration_s=duration(r.get('Duration'))) for p in versions(tree, 'music (library and uploads)/music uploads metadata') for r in read(p)]
    return lib + up


# name -> (loader, tables it fills, files whose size/mtime say whether it changed)
DATASETS = {
    'subscriptions': (subscriptions, [Subscription], ['subscriptions/subscriptions']),
    'playlists': (playlists, [PlaylistItem, Playlist], ['playlists/playlists', 'playlists/*']),
    'videos': (videos, [Video], ['Video metadata/videos', 'Video metadata/video recordings', 'Video metadata/video texts']),
    'own-channels': (own_channels, [OwnChannel], ['channels/channel', 'channels/channel URL configs', 'channels/channel feature data']),
    'comments': (comments, [Comment], ['comments/comments']),
    'live-chats': (live_chats, [LiveChat], ['live chats/live chats']),
    'messages': (messages, [Message], ['messages/conversation_*']),
    'music': (music, [MusicTrack], ['music (library and uploads)/music library songs', 'music (library and uploads)/music uploads metadata']),
}


def files_of(tree, name):
    """Every file the dataset reads (its fingerprint): each pattern with `.csv`, and for plain names the `(1)`, `(2)` twins too."""
    out = set()
    for rel in DATASETS[name][2]:
        base = os.path.join(glob.escape(tree), ROOT, rel)
        out.update(glob.glob(base + '.csv') if '*' in rel else glob.glob(glob.escape(os.path.join(tree, ROOT, rel)) + '.csv') + glob.glob(glob.escape(os.path.join(tree, ROOT, rel)) + '(*).csv'))
    return sorted(out)
