"""Tables of the `mail` schema. One account = one mailbox, read from whichever source (a Gmail Takeout mbox, later IMAP).

  accounts      an id the job chose (`main`): label only, nothing about the person.
  sources       which mbox each (account, source) was last loaded from, with its size/mtime, so `sync` skips an unchanged one.
  messages      EVERY message gets a metadata row (sender, subject, time, labels, flags, sizes, where it sits in the mbox).
                `decision` is `keep`, `drop` or `undecided`, `decided_by` says which rule or model; only `keep` rows carry
                `body_text` and attachment rows. A later pass (classifier) moves `undecided` rows to keep/drop and `fill`
                reads the mbox again at `mbox_offset` for the new keeps, so nothing is lost by not storing a body first.
  attachments   one row per attachment of a kept message; the bytes live on disk under the job's attachments root at
                `path` (content-addressed by `sha256`, so equal files are stored once) and are catalogued by filecat.

Times are timestamptz (UTC). Portable types only (tests run on SQLite)."""

from datetime import datetime

from omarchy_db import make_base
from sqlalchemy import JSON, BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
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
    rows: Mapped[int] = mapped_column(Integer)
    loaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("account", "source", "mbox_offset"),
        Index("ix_mail_messages_at", "account", "sent_at"),
        Index("ix_mail_messages_from", "account", "from_addr"),
        Index("ix_mail_messages_decision", "account", "decision"),
        Index("ix_mail_messages_msgid", "message_id"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    source: Mapped[str] = mapped_column(String(32))
    mbox_offset: Mapped[int] = mapped_column(BigInteger)  # byte offset of the message's `From ` line
    mbox_size: Mapped[int] = mapped_column(BigInteger)
    message_id: Mapped[str | None] = mapped_column(Text, default=None)  # the Message-ID header
    thread_id: Mapped[str | None] = mapped_column(String(32), default=None)  # Gmail's X-GM-THRID when present
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    from_addr: Mapped[str | None] = mapped_column(Text, default=None)
    from_name: Mapped[str | None] = mapped_column(Text, default=None)
    to_addrs: Mapped[list | None] = mapped_column(JSON, default=None)  # To and Cc addresses, lower case
    subject: Mapped[str | None] = mapped_column(Text, default=None)
    labels: Mapped[list | None] = mapped_column(JSON, default=None)  # Gmail labels (X-Gmail-Labels)
    is_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    has_unsubscribe: Mapped[bool] = mapped_column(Boolean, default=False)  # List-Unsubscribe header
    is_bulk: Mapped[bool] = mapped_column(Boolean, default=False)  # Precedence bulk/list/junk or Auto-Submitted
    text_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    attachment_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    decision: Mapped[str] = mapped_column(String(12), default="undecided")  # keep | drop | undecided
    decided_by: Mapped[str | None] = mapped_column(String(64), default=None)  # rule name or model
    body_text: Mapped[str | None] = mapped_column(Text, default=None)  # only for kept messages


class Attachment(Base):
    __tablename__ = "attachments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    message_pk: Mapped[int] = mapped_column(ForeignKey("messages.id", ondelete="CASCADE"), index=True)
    size: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    filename: Mapped[str | None] = mapped_column(Text, default=None)
    mime: Mapped[str | None] = mapped_column(String(128), default=None)
    path: Mapped[str | None] = mapped_column(
        Text, default=None
    )  # relative to the attachments root; NULL if skipped (too big)
