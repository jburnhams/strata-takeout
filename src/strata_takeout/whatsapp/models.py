"""Tables of the `whatsapp` schema. One account = one WhatsApp account's history, from whichever backup it was read.

  accounts       an account id the job chose (`main`): label only, nothing about the person.
  sources        which database file each (account, source) was last loaded from, with its size/mtime, so `sync` skips
                 an unchanged one. A source is one backup (`android-msgstore`, later `ios-chatstorage`).
  chats          one per conversation: its JID, group subject, group or not, first/last message time and count.
  contacts       the address book the backup carried (wa.db): JID, number, display and WhatsApp names.
  participants   group membership as the backup recorded it.
  windows        stretches of a conversation (30-minute gaps split them) with an embedding, for search by meaning (`strata-takeout-whatsapp embed|search`).
  messages       every message, whatever its kind: `kind` is text/image/video/audio/document/gif/sticker/location/
                 contact/call/system/deleted/other, `type_code` the raw WhatsApp code. `media_file` is the media
                 file's path relative to the account's media root when it was found there, so filecat's catalogue
                 of that folder joins to it.

Times are timestamptz (UTC). Rows of different sources can describe the same message; (chat_jid, key_id, from_me)
finds them, and nothing here merges them, so each source reloads on its own. Portable types only (tests run on SQLite)."""

from datetime import datetime

from omarchy_db import make_base
from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from strata_takeout.vectors import Vector, hnsw_index

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
    size: Mapped[int] = mapped_column(BigInteger)
    mtime_ns: Mapped[int] = mapped_column(BigInteger)
    rows: Mapped[int] = mapped_column(Integer)
    loaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Chat(Base):
    __tablename__ = "chats"
    account: Mapped[str] = mapped_column(ForeignKey("accounts.id"), primary_key=True)
    source: Mapped[str] = mapped_column(String(32), primary_key=True)
    jid: Mapped[str] = mapped_column(String(128), primary_key=True)
    subject: Mapped[str | None] = mapped_column(Text, default=None)
    is_group: Mapped[bool] = mapped_column(Boolean, default=False)
    first_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    last_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    message_count: Mapped[int] = mapped_column(Integer, default=0)


class Contact(Base):
    __tablename__ = "contacts"
    account: Mapped[str] = mapped_column(ForeignKey("accounts.id"), primary_key=True)
    source: Mapped[str] = mapped_column(String(32), primary_key=True)
    jid: Mapped[str] = mapped_column(String(128), primary_key=True)
    number: Mapped[str | None] = mapped_column(String(64), default=None)
    display_name: Mapped[str | None] = mapped_column(Text, default=None)
    wa_name: Mapped[str | None] = mapped_column(Text, default=None)
    is_whatsapp_user: Mapped[bool | None] = mapped_column(Boolean, default=None)


class Participant(Base):
    __tablename__ = "participants"
    account: Mapped[str] = mapped_column(ForeignKey("accounts.id"), primary_key=True)
    source: Mapped[str] = mapped_column(String(32), primary_key=True)
    group_jid: Mapped[str] = mapped_column(String(128), primary_key=True)
    jid: Mapped[str] = mapped_column(String(128), primary_key=True)
    is_admin: Mapped[bool | None] = mapped_column(Boolean, default=None)


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("account", "source", "row_id"),
        Index("ix_wa_messages_chat_at", "account", "chat_jid", "at"),
        Index("ix_wa_messages_key", "chat_jid", "key_id"),
        Index("ix_wa_messages_at", "at"),
    )
    id: Mapped[int] = mapped_column(primary_key=True, init=False)
    account: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    source: Mapped[str] = mapped_column(String(32))
    row_id: Mapped[int] = mapped_column(BigInteger)  # the backup's own row id: makes a reload replace, never duplicate
    chat_jid: Mapped[str] = mapped_column(String(128))
    key_id: Mapped[str | None] = mapped_column(String(64), default=None)  # WhatsApp's message id, unique within a chat
    from_me: Mapped[bool] = mapped_column(Boolean, default=False)
    sender_jid: Mapped[str | None] = mapped_column(
        String(128), default=None
    )  # who spoke in a group (null: the chat itself)
    at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    kind: Mapped[str] = mapped_column(String(16), default="other")
    type_code: Mapped[int | None] = mapped_column(Integer, default=None)
    status: Mapped[int | None] = mapped_column(Integer, default=None)
    text: Mapped[str | None] = mapped_column(Text, default=None)
    caption: Mapped[str | None] = mapped_column(Text, default=None)
    media_mime: Mapped[str | None] = mapped_column(String(128), default=None)
    media_name: Mapped[str | None] = mapped_column(Text, default=None)
    media_size: Mapped[int | None] = mapped_column(BigInteger, default=None)
    media_seconds: Mapped[int | None] = mapped_column(Integer, default=None)
    media_hash: Mapped[str | None] = mapped_column(
        String(64), default=None
    )  # base64 SHA-256 of the file, as WhatsApp keeps it
    media_path: Mapped[str | None] = mapped_column(
        Text, default=None
    )  # as the backup recorded it (Media/WhatsApp Images/...)
    media_file: Mapped[str | None] = mapped_column(
        Text, default=None
    )  # path under the media root when the file was found
    latitude: Mapped[float | None] = mapped_column(Float, default=None)
    longitude: Mapped[float | None] = mapped_column(Float, default=None)
    starred: Mapped[bool] = mapped_column(Boolean, default=False)


class Window(Base):
    """A stretch of one conversation (text messages less than WINDOW_GAP_MIN apart, a bounded size) with its embedding, for search by meaning.
    Only the span is kept (`first_row_id`..`last_row_id` of the chat's messages); the text is composed from the message rows when embedding. Rebuilt
    after every load; a window whose text did not change keeps its vector (`text_sha`)."""

    __tablename__ = "windows"
    __table_args__ = (
        Index("ix_wa_windows_chat", "account", "source", "chat_jid", "start_at"),
        hnsw_index("ix_wa_windows_embedding_hnsw", "embedding"),
    )
    id: Mapped[int] = mapped_column(primary_key=True, init=False)
    account: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    source: Mapped[str] = mapped_column(String(32))
    chat_jid: Mapped[str] = mapped_column(String(128))
    first_row_id: Mapped[int] = mapped_column(BigInteger)
    last_row_id: Mapped[int] = mapped_column(BigInteger)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    n_messages: Mapped[int] = mapped_column(Integer)
    chars: Mapped[int] = mapped_column(Integer)
    text_sha: Mapped[str] = mapped_column(
        String(40)
    )  # SHA-1 of the composed text: unchanged text keeps its vector across reloads
    embedded: Mapped[str | None] = mapped_column(String(96), default=None)  # "<model>:<chunker>"; null = not yet
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1024), default=None)
