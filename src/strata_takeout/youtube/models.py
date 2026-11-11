"""Tables of the `youtube` schema. One row per entry of a Google Takeout YouTube history page, whichever account it came from.

  accounts   a Google/YouTube account a Takeout belongs to (`main`, `brand`): label and, when known, its channel id.
  sources    which file each (account, source) was last loaded from, with its size/mtime, so `sync` skips unchanged files.
  activity   every entry of watch-history.html, search-history.html and My Activity/YouTube: what was done (`action`: Watched, Searched for,
             Subscribed to, Liked, ...), the video/post/channel it was about, when, and from which file. `watch-history` and `My Activity`
             overlap for watches, so queries on watches use the `watches` view (watch-history only, ads left out) or filter on `source`.

Times are timestamptz (UTC); Takeout writes them in the account's local zone with an abbreviation (BST/GMT), which the parser resolves.
  subscriptions, playlists, playlist_items, videos (the account's own uploads), own_channels, comments, live_chats, messages, music_tracks: the other YouTube files of the Takeout.
Portable types only, so the tests run on SQLite."""
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, false
from sqlalchemy.orm import Mapped, mapped_column

from omarchy_db import make_base

Base = make_base()


class Account(Base):
    __tablename__ = 'accounts'
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    label: Mapped[str | None] = mapped_column(default=None)
    channel_id: Mapped[str | None] = mapped_column(String(64), default=None)


class Activity(Base):
    __tablename__ = 'activity'
    __table_args__ = (UniqueConstraint('account', 'source', 'seq'), Index('ix_activity_account_at', 'account', 'at'), Index('ix_activity_video', 'video_id'), Index('ix_activity_channel', 'channel_id'))
    id: Mapped[int] = mapped_column(primary_key=True, init=False)
    account: Mapped[str] = mapped_column(ForeignKey('accounts.id'))
    source: Mapped[str] = mapped_column(String(32))               # watch-history | search-history | my-activity
    seq: Mapped[int] = mapped_column(Integer)                      # position in the file (0 = newest), makes a re-run replace instead of duplicate
    action: Mapped[str] = mapped_column(String(64))                # Watched, Viewed, Searched for, Subscribed to, Liked, Disliked, Dismissed, Visited, ...
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    title: Mapped[str | None] = mapped_column(Text, default=None)  # video title, search text, post text, ...
    url: Mapped[str | None] = mapped_column(Text, default=None)
    video_id: Mapped[str | None] = mapped_column(String(32), default=None)
    post_id: Mapped[str | None] = mapped_column(String(64), default=None)
    channel_id: Mapped[str | None] = mapped_column(String(64), default=None)
    channel_name: Mapped[str | None] = mapped_column(Text, default=None)
    details: Mapped[str | None] = mapped_column(Text, default=None)   # e.g. "Watched at 20:09", "Imported from Google Play Music", caption "From Google Ads"
    is_ad: Mapped[bool] = mapped_column(Boolean, server_default=false(), default=False)


class Source(Base):
    """One history file as last loaded: the fingerprint (size, mtime) `sync` compares to skip an unchanged file."""
    __tablename__ = 'sources'
    account: Mapped[str] = mapped_column(ForeignKey('accounts.id'), primary_key=True)
    source: Mapped[str] = mapped_column(String(32), primary_key=True)
    path: Mapped[str] = mapped_column(Text)
    size: Mapped[int] = mapped_column(BigInteger)
    mtime_ns: Mapped[int] = mapped_column(BigInteger)
    rows: Mapped[int] = mapped_column(Integer)
    loaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


def _acct(): return mapped_column(ForeignKey('accounts.id'), primary_key=True)


class Subscription(Base):
    """The channels the account is subscribed to (subscriptions.csv)."""
    __tablename__ = 'subscriptions'
    account: Mapped[str] = _acct()
    channel_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    channel_url: Mapped[str | None] = mapped_column(Text, default=None)
    title: Mapped[str | None] = mapped_column(Text, default=None)


class Playlist(Base):
    __tablename__ = 'playlists'
    account: Mapped[str] = _acct()
    playlist_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str | None] = mapped_column(Text, default=None)
    language: Mapped[str | None] = mapped_column(String(16), default=None)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    video_order: Mapped[str | None] = mapped_column(String(32), default=None)
    visibility: Mapped[str | None] = mapped_column(String(32), default=None)
    add_to_top: Mapped[bool | None] = mapped_column(Boolean, default=None)


class PlaylistItem(Base):
    """One video in a playlist (`<title> videos.csv`); playlist_id is null when the file's name matches no playlist."""
    __tablename__ = 'playlist_items'
    id: Mapped[int] = mapped_column(primary_key=True, init=False)
    account: Mapped[str] = mapped_column(ForeignKey('accounts.id'))
    playlist_title: Mapped[str] = mapped_column(Text)
    video_id: Mapped[str] = mapped_column(String(32), index=True)
    position: Mapped[int] = mapped_column(Integer)
    playlist_id: Mapped[str | None] = mapped_column(String(64), default=None, index=True)
    added_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class Video(Base):
    """The account's own uploads (Video metadata/: videos, video texts, video recordings; later files of the same video win)."""
    __tablename__ = 'videos'
    account: Mapped[str] = _acct()
    video_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    title: Mapped[str | None] = mapped_column(Text, default=None)
    description: Mapped[str | None] = mapped_column(Text, default=None)
    channel_id: Mapped[str | None] = mapped_column(String(64), default=None)
    duration_ms: Mapped[int | None] = mapped_column(BigInteger, default=None)
    language: Mapped[str | None] = mapped_column(String(16), default=None)
    category: Mapped[str | None] = mapped_column(String(64), default=None)
    privacy: Mapped[str | None] = mapped_column(String(32), default=None)
    state: Mapped[str | None] = mapped_column(String(32), default=None)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    tags: Mapped[list | None] = mapped_column(sa.JSON, default=None)
    latitude: Mapped[float | None] = mapped_column(Float, default=None)
    longitude: Mapped[float | None] = mapped_column(Float, default=None)
    altitude: Mapped[float | None] = mapped_column(Float, default=None)
    text_title: Mapped[str | None] = mapped_column(Text, default=None)           # video texts.csv: the segments joined
    text_description: Mapped[str | None] = mapped_column(Text, default=None)
    text_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class OwnChannel(Base):
    __tablename__ = 'own_channels'
    account: Mapped[str] = _acct()
    channel_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str | None] = mapped_column(Text, default=None)
    visibility: Mapped[str | None] = mapped_column(String(32), default=None)
    vanity_urls: Mapped[list | None] = mapped_column(sa.JSON, default=None)
    settings: Mapped[dict | None] = mapped_column(sa.JSON, default=None)         # channel feature data, as it came


class Comment(Base):
    __tablename__ = 'comments'
    account: Mapped[str] = _acct()
    comment_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    channel_id: Mapped[str | None] = mapped_column(String(64), default=None)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    video_id: Mapped[str | None] = mapped_column(String(32), default=None, index=True)
    parent_id: Mapped[str | None] = mapped_column(String(128), default=None)
    top_level_id: Mapped[str | None] = mapped_column(String(128), default=None)
    text: Mapped[str | None] = mapped_column(Text, default=None)                 # plain text of the segments
    raw: Mapped[str | None] = mapped_column(Text, default=None)                  # the JSON segments (mentions, links) as exported
    price: Mapped[str | None] = mapped_column(String(32), default=None)


class LiveChat(Base):
    __tablename__ = 'live_chats'
    account: Mapped[str] = _acct()
    chat_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    channel_id: Mapped[str | None] = mapped_column(String(64), default=None)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    video_id: Mapped[str | None] = mapped_column(String(32), default=None)
    text: Mapped[str | None] = mapped_column(Text, default=None)
    price: Mapped[str | None] = mapped_column(String(32), default=None)


class Message(Base):
    """Direct messages (messages/conversation_*.csv): sender, recipients and a shared video; Takeout has no message text."""
    __tablename__ = 'messages'
    account: Mapped[str] = _acct()
    message_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    sender_channel_id: Mapped[str | None] = mapped_column(String(64), default=None)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    conversation_id: Mapped[str | None] = mapped_column(String(64), default=None)
    recipients: Mapped[str | None] = mapped_column(Text, default=None)
    video_id: Mapped[str | None] = mapped_column(String(32), default=None)


class MusicTrack(Base):
    """YouTube Music: `library` songs (video id known) and `upload`s (metadata of the uploaded files)."""
    __tablename__ = 'music_tracks'
    id: Mapped[int] = mapped_column(primary_key=True, init=False)
    account: Mapped[str] = mapped_column(ForeignKey('accounts.id'))
    kind: Mapped[str] = mapped_column(String(16))
    title: Mapped[str | None] = mapped_column(Text, default=None)
    album: Mapped[str | None] = mapped_column(Text, default=None)
    artist: Mapped[str | None] = mapped_column(Text, default=None)
    artist2: Mapped[str | None] = mapped_column(Text, default=None)
    duration_s: Mapped[float | None] = mapped_column(Float, default=None)
    video_id: Mapped[str | None] = mapped_column(String(32), default=None)
