"""Tables of the `facebook` schema. Everything someone wrote is a MESSAGE: a chat line, a post, a comment. Only the audience differs.

  accounts      an account id the job chose (`main`): label only, nothing about the person.
  sources       which export tree each (account, source) was last loaded from, with a fingerprint of its JSON files, so `sync` skips an unchanged one.
  threads       one per Messenger conversation: its folder (inbox, archived_threads, ...), title, how many people, still a member.
  members       who is in a thread, as the export lists them.
  messages      one row per chat line / post / comment. `kind` is chat | post | comment. `audience` says who it was said to: `thread` (a chat; `thread_id`
                set), `timeline` (own timeline), `profile` (someone's page/profile, `audience_name`), `group` (`audience_name` = the group when the export
                names it), `post` (a comment under someone's post: `audience_name` = whose post, `self` for the person's own). `parent_id` links a reply to
                what it answers where the export says (it rarely does; the raw activity `title` is kept so a later parser can fill it in).
                `ext_id` is a stable id built from kind, thread/audience, time, author and a counter for exact repeats: a re-export gives the same ids.
  reactions     emoji reactions: on a chat message (`message_id` set, `actor` = who reacted) or the person's own likes of things elsewhere
                (`message_id` NULL, `target_title` = the export's sentence about it, `at` set).
  attachments   media and links of a message: kind photo/video/gif/audio/file/sticker/link, `path` relative to the tree root (the same path a filecat
                collection of that tree uses), mime, size and SHA-256 of the file when it exists on disk, `url` for a link.

Times are timestamptz (UTC). Portable types only (tests run on SQLite)."""

from datetime import datetime

from omarchy_db import make_base
from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, false
from sqlalchemy.orm import Mapped, mapped_column

Base = make_base()


class Account(Base):
    __tablename__ = "accounts"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    label: Mapped[str | None] = mapped_column(default=None)


class Source(Base):
    __tablename__ = "sources"
    account: Mapped[str] = mapped_column(ForeignKey("accounts.id"), primary_key=True)
    source: Mapped[str] = mapped_column(String(32), primary_key=True)
    path: Mapped[str] = mapped_column(Text)
    size: Mapped[int] = mapped_column(BigInteger)  # total bytes of the JSON files read
    mtime_ns: Mapped[int] = mapped_column(BigInteger)  # newest of them
    rows: Mapped[int] = mapped_column(Integer)
    loaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Thread(Base):
    __tablename__ = "threads"
    account: Mapped[str] = mapped_column(ForeignKey("accounts.id"), primary_key=True)
    source: Mapped[str] = mapped_column(String(32), primary_key=True)
    thread_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    folder: Mapped[str | None] = mapped_column(String(32), default=None)
    title: Mapped[str | None] = mapped_column(Text, default=None)
    participants: Mapped[int] = mapped_column(Integer, default=0)
    still_member: Mapped[bool | None] = mapped_column(Boolean, default=None)


class Member(Base):
    __tablename__ = "members"
    account: Mapped[str] = mapped_column(ForeignKey("accounts.id"), primary_key=True)
    source: Mapped[str] = mapped_column(String(32), primary_key=True)
    thread_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), primary_key=True)


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("account", "source", "ext_id"),
        Index("ix_fb_messages_at", "account", "at"),
        Index("ix_fb_messages_thread", "account", "thread_id", "at"),
        Index("ix_fb_messages_kind", "account", "kind", "audience"),
    )
    id: Mapped[int] = mapped_column(primary_key=True, init=False)
    account: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    source: Mapped[str] = mapped_column(String(32))
    ext_id: Mapped[str] = mapped_column(String(40))
    kind: Mapped[str] = mapped_column(String(16))
    audience: Mapped[str] = mapped_column(String(16))
    audience_name: Mapped[str | None] = mapped_column(Text, default=None)
    thread_id: Mapped[str | None] = mapped_column(String(128), default=None)
    at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    author: Mapped[str | None] = mapped_column(Text, default=None)
    body: Mapped[str | None] = mapped_column(Text, default=None)
    title: Mapped[str | None] = mapped_column(
        Text, default=None
    )  # the export's own sentence ("X commented on Y's post")
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("messages.id", ondelete="SET NULL"), default=None)
    call_seconds: Mapped[int | None] = mapped_column(Integer, default=None)
    unsent: Mapped[bool] = mapped_column(Boolean, server_default=false(), default=False)


class Reaction(Base):
    __tablename__ = "reactions"
    __table_args__ = (Index("ix_fb_reactions_message", "message_id"),)
    id: Mapped[int] = mapped_column(primary_key=True, init=False)
    account: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    source: Mapped[str] = mapped_column(String(32))
    reaction: Mapped[str | None] = mapped_column(Text, default=None)
    actor: Mapped[str | None] = mapped_column(Text, default=None)
    message_id: Mapped[int | None] = mapped_column(ForeignKey("messages.id", ondelete="CASCADE"), default=None)
    target_title: Mapped[str | None] = mapped_column(Text, default=None)
    at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class Attachment(Base):
    __tablename__ = "attachments"
    __table_args__ = (Index("ix_fb_attachments_sha", "sha256"), Index("ix_fb_attachments_message", "message_id"))
    id: Mapped[int] = mapped_column(primary_key=True, init=False)
    message_id: Mapped[int] = mapped_column(ForeignKey("messages.id", ondelete="CASCADE"))
    seq: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(16))
    path: Mapped[str | None] = mapped_column(Text, default=None)
    url: Mapped[str | None] = mapped_column(Text, default=None)
    mime: Mapped[str | None] = mapped_column(String(128), default=None)
    size: Mapped[int | None] = mapped_column(BigInteger, default=None)
    sha256: Mapped[str | None] = mapped_column(String(64), default=None)
    taken_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
