"""Tables of the `googlechat` schema: everything Google's chat products wrote, whichever export it came from. Each source keeps ALL of its data: typed columns for the
common fields plus the leftovers (`raw`), so nothing in the export is lost and a cross-platform view can be layered on top later.

  accounts       an account id the job chose (`main`): label only, nothing about the person.
  sources        which input each (account, source) was last loaded from, with a fingerprint, so `sync` skips an unchanged one. `source` is
                 `takeout` (the Takeout `Google Chat/` folder: Hangouts history since ~2013 and Chat), `gmail` (the old Gmail/GTalk chat logs in a Gmail mbox),
                 `gmvault` (the same logs from a gmvault backup).
  conversations  one per Takeout group (DM or space) or, for the old Gmail chat logs, one per set of people talking (the same people always land in the
                 same conversation). `kind` is dm | space | chat. `info` is the group's whole `group_info.json`.
  members        who is in a conversation: name, e-mail address, user type, as the source gives them.
  messages       one row per chat line. `sender_*` the author, `at` when (UTC; `at_precision` says s | ms | ... of the source), `text`, `kind` message | system,
                 `topic_id` the thread inside a Takeout group, `ext_id` a stable id. `raw` is the source's complete message object (Takeout: the whole message JSON
                 with annotations, url/youtube/drive metadata and attached_files; Gmail chat: the line's XML attributes and the mail's message id, labels).
  attachments    files of a message: the export's `original_name` and `export_name`, the matched `path` relative to the tree root with size and SHA-256 when the
                 file exists, `status` (present | missing) and `match` (how the file was found: exact | unquoted | sequence). A file on disk that no message refers to
                 is kept too, with `message_id` NULL and `status` orphan.

Times are timestamptz (UTC). Portable types only (tests run on SQLite); `raw` and `info` are JSON (JSONB on Postgres)."""

from datetime import datetime

from omarchy_db import make_base
from sqlalchemy import JSON, BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

Base = make_base()
Json = JSON().with_variant(JSONB(), "postgresql")


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


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (UniqueConstraint("account", "source", "ext_id"),)
    id: Mapped[int] = mapped_column(primary_key=True, init=False)
    account: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    source: Mapped[str] = mapped_column(String(32))
    ext_id: Mapped[str] = mapped_column(
        String(128)
    )  # Takeout: the group folder name; Gmail chat: a hash of the people in it
    kind: Mapped[str] = mapped_column(String(16))
    name: Mapped[str | None] = mapped_column(Text, default=None)
    participants: Mapped[int] = mapped_column(Integer, default=0)
    first_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    last_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    n_messages: Mapped[int] = mapped_column(Integer, default=0)
    info: Mapped[dict | None] = mapped_column(Json, default=None)


class Member(Base):
    __tablename__ = "members"
    id: Mapped[int] = mapped_column(primary_key=True, init=False)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"))
    name: Mapped[str | None] = mapped_column(Text, default=None)
    email: Mapped[str | None] = mapped_column(Text, default=None)
    user_type: Mapped[str | None] = mapped_column(String(32), default=None)


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("account", "source", "ext_id"),
        Index("ix_messages_conversation_at", "conversation_id", "at"),
        Index("ix_messages_at", "at"),
    )
    id: Mapped[int] = mapped_column(primary_key=True, init=False)
    account: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    source: Mapped[str] = mapped_column(String(32))
    ext_id: Mapped[str] = mapped_column(String(160))
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(16), default="message")
    sender_name: Mapped[str | None] = mapped_column(Text, default=None)
    sender_email: Mapped[str | None] = mapped_column(Text, default=None)
    sender_type: Mapped[str | None] = mapped_column(String(32), default=None)
    at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    at_precision: Mapped[str | None] = mapped_column(
        String(8), default=None
    )  # s | ms | mail (the mail's own date) | separator
    text: Mapped[str | None] = mapped_column(Text, default=None)
    topic_id: Mapped[str | None] = mapped_column(String(64), default=None)
    has_attachment: Mapped[bool] = mapped_column(Boolean, default=False)
    raw: Mapped[dict | None] = mapped_column(Json, default=None)


class Attachment(Base):
    __tablename__ = "attachments"
    id: Mapped[int] = mapped_column(primary_key=True, init=False)
    account: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    source: Mapped[str] = mapped_column(String(32))
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"))
    message_id: Mapped[int | None] = mapped_column(ForeignKey("messages.id", ondelete="CASCADE"), default=None)
    seq: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(12), default="present")
    match: Mapped[str | None] = mapped_column(String(12), default=None)
    original_name: Mapped[str | None] = mapped_column(Text, default=None)
    export_name: Mapped[str | None] = mapped_column(Text, default=None)
    path: Mapped[str | None] = mapped_column(Text, default=None)
    mime: Mapped[str | None] = mapped_column(String(128), default=None)
    size: Mapped[int | None] = mapped_column(BigInteger, default=None)
    sha256: Mapped[str | None] = mapped_column(String(64), default=None)
