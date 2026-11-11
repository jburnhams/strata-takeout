"""Tables of the `youtube` schema. One row per entry of a Google Takeout YouTube history page, whichever account it came from.

  accounts   a Google/YouTube account a Takeout belongs to (`main`, `brand`): label and, when known, its channel id.
  sources    which file each (account, source) was last loaded from, with its size/mtime, so `sync` skips unchanged files.
  activity   every entry of watch-history.html, search-history.html and My Activity/YouTube: what was done (`action`: Watched, Searched for,
             Subscribed to, Liked, ...), the video/post/channel it was about, when, and from which file. `watch-history` and `My Activity`
             overlap for watches, so queries on watches use the `watches` view (watch-history only, ads left out) or filter on `source`.

Times are timestamptz (UTC); Takeout writes them in the account's local zone with an abbreviation (BST/GMT), which the parser resolves.
Portable types only, so the tests run on SQLite."""
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, false
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
