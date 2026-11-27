"""Tables of the `gmail` schema: one row per message of a Gmail mbox, whichever account it came from.

  accounts      an account id the job chose (`main`): label only.
  sources       which mbox each (account, source) was last loaded from, with its size/mtime and the rule set's version, so `sync` skips an unchanged file.
  messages      EVERY message, as metadata: sender, recipients, subject, Gmail labels, sizes, flags (unsubscribe, bulk, reply) and the `tier` the
                rules gave it. Only a `keep` message has `body_text` and attachment rows: junk keeps its metadata so nothing is lost and a later,
                better classifier can promote it (re-run the load: the mbox offset says where the full message still is).
  attachments   the non-text parts of kept messages: name, type, size, SHA-256 and where the bytes were written (relative to the job's attachment
                folder, content-addressed, so a file attached twice is stored once).

Times are timestamptz (UTC). Portable types only (tests run on SQLite)."""

from datetime import datetime

from omarchy_db import make_base
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
)
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
    size: Mapped[int] = mapped_column(BigInteger)
    mtime_ns: Mapped[int] = mapped_column(BigInteger)
    rules: Mapped[str] = mapped_column(String(32))
    rows: Mapped[int] = mapped_column(Integer)
    loaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("account", "source", "offset"),
        Index("ix_messages_account_at", "account", "at"),
        Index("ix_messages_from", "from_addr"),
        Index("ix_messages_tier", "account", "tier"),
        Index("ix_messages_thread", "thread_id"),
    )
    id: Mapped[int] = mapped_column(primary_key=True, init=False)
    account: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    source: Mapped[str] = mapped_column(String(32))
    offset: Mapped[int] = mapped_column(BigInteger)  # byte offset of the message in the mbox
    size: Mapped[int] = mapped_column(BigInteger)  # bytes in the mbox
    at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    message_id: Mapped[str | None] = mapped_column(Text, default=None)  # the Message-ID header
    thread_id: Mapped[str | None] = mapped_column(String(32), default=None)  # X-GM-THRID when the export has it
    in_reply_to: Mapped[str | None] = mapped_column(Text, default=None)
    from_addr: Mapped[str | None] = mapped_column(Text, default=None)
    from_name: Mapped[str | None] = mapped_column(Text, default=None)
    to_addrs: Mapped[str | None] = mapped_column(Text, default=None)  # space-separated lower-case addresses of To
    cc_addrs: Mapped[str | None] = mapped_column(Text, default=None)
    subject: Mapped[str | None] = mapped_column(Text, default=None)
    labels: Mapped[str | None] = mapped_column(Text, default=None)  # Gmail labels, lower-case, comma-separated
    text_bytes: Mapped[int] = mapped_column(BigInteger, default=0)  # decoded size of the text/plain + text/html parts
    attach_bytes: Mapped[int] = mapped_column(BigInteger, default=0)  # decoded size of every other part
    n_attachments: Mapped[int] = mapped_column(Integer, default=0)
    has_unsubscribe: Mapped[bool] = mapped_column(Boolean, server_default=false(), default=False)
    is_bulk: Mapped[bool] = mapped_column(Boolean, server_default=false(), default=False)
    tier: Mapped[str] = mapped_column(String(16), default="other")  # keep | other
    tier_reason: Mapped[str | None] = mapped_column(String(32), default=None)  # which rule said so
    label: Mapped[str | None] = mapped_column(
        String(32), default=None
    )  # a classifier's verdict (personal, transactional_keep, notification, marketing), when a verdicts file gave one
    label_confidence: Mapped[float | None] = mapped_column(Float, default=None)
    label_source: Mapped[str | None] = mapped_column(String(64), default=None)  # which classifier/run said so
    body_text: Mapped[str | None] = mapped_column(Text, default=None)  # kept messages only


class Attachment(Base):
    __tablename__ = "attachments"
    __table_args__ = (Index("ix_attachments_sha", "sha256"),)
    id: Mapped[int] = mapped_column(primary_key=True, init=False)
    message_id: Mapped[int] = mapped_column(ForeignKey("messages.id", ondelete="CASCADE"))
    seq: Mapped[int] = mapped_column(Integer)
    filename: Mapped[str | None] = mapped_column(Text, default=None)
    content_type: Mapped[str | None] = mapped_column(String(128), default=None)
    size: Mapped[int] = mapped_column(BigInteger, default=0)
    sha256: Mapped[str | None] = mapped_column(String(64), default=None)
    stored_path: Mapped[str | None] = mapped_column(
        Text, default=None
    )  # relative to the attachment folder; NULL when not written
    inline: Mapped[bool] = mapped_column(
        Boolean, server_default=false(), default=False
    )  # an embedded image/part, not a named attachment
