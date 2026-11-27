"""`unified.messages_all`: one row per chat line, post or comment of every platform that is loaded, so a question across WhatsApp, Facebook, Instagram and Google
chat is a single query. A plain view over the platforms' own tables: nothing is copied, the platform tables stay the full-fidelity source, dropping the view loses nothing.

Columns: platform, source (the platform's own source value), account, conversation_id (text), conversation_name, sender, at, text, kind, has_attachment,
source_id (the row id in the platform's `messages` table, as text, to join back).

Postgres only (`security_invoker`: the reader's rights apply, so the per-app schema owners need no grants). The parts are the platforms whose schema exists when the view is built; a
platform loaded later needs `strata-takeout-unified build` again (the view is replaced, nothing else changes). Changing a platform column the view reads needs the view dropped first."""

from sqlalchemy import text
from sqlalchemy.engine import Connection

COLUMNS = (
    "platform, source, account, conversation_id, conversation_name, sender, at, text, kind, has_attachment, source_id"
)

WHATSAPP = """
SELECT 'whatsapp'::text AS platform, m.source, m.account, m.chat_jid::text AS conversation_id,
       coalesce(c.subject, cc.display_name, cc.wa_name, m.chat_jid)::text AS conversation_name,
       (CASE WHEN m.from_me THEN 'me' ELSE coalesce(ct.display_name, ct.wa_name, m.sender_jid, m.chat_jid) END)::text AS sender,
       m.at, coalesce(m.text, m.caption) AS text, m.kind::text AS kind,
       (m.media_mime IS NOT NULL OR m.media_name IS NOT NULL OR m.media_path IS NOT NULL OR m.media_file IS NOT NULL) AS has_attachment,
       m.id::text AS source_id
FROM whatsapp.messages m
LEFT JOIN whatsapp.chats c ON c.account = m.account AND c.source = m.source AND c.jid = m.chat_jid
LEFT JOIN whatsapp.contacts cc ON cc.account = m.account AND cc.source = m.source AND cc.jid = m.chat_jid
LEFT JOIN whatsapp.contacts ct ON ct.account = m.account AND ct.source = m.source AND ct.jid = m.sender_jid
"""

# Facebook and Instagram share one shape (Instagram's schema is Facebook's plus follows)
META = """
SELECT '{schema}'::text AS platform, m.source, m.account,
       coalesce(m.thread_id, m.audience || ':' || coalesce(m.audience_name, ''))::text AS conversation_id,
       coalesce(t.title, m.audience_name)::text AS conversation_name,
       m.author::text AS sender, m.at, m.body AS text, m.kind::text AS kind,
       EXISTS (SELECT 1 FROM {schema}.attachments a WHERE a.message_id = m.id) AS has_attachment,
       m.id::text AS source_id
FROM {schema}.messages m
LEFT JOIN {schema}.threads t ON t.account = m.account AND t.source = m.source AND t.thread_id = m.thread_id
"""

GOOGLECHAT = """
SELECT 'googlechat'::text AS platform, m.source, m.account, m.conversation_id::text AS conversation_id,
       coalesce(c.name, c.ext_id)::text AS conversation_name,
       coalesce(m.sender_name, m.sender_email)::text AS sender, m.at, m.text AS text, m.kind::text AS kind,
       m.has_attachment AS has_attachment, m.id::text AS source_id
FROM googlechat.messages m
JOIN googlechat.conversations c ON c.id = m.conversation_id
"""

PARTS = {
    "whatsapp": WHATSAPP,
    "facebook": META.format(schema="facebook"),
    "instagram": META.format(schema="instagram"),
    "googlechat": GOOGLECHAT,
}
EMPTY = """
SELECT NULL::text AS platform, NULL::text AS source, NULL::text AS account, NULL::text AS conversation_id, NULL::text AS conversation_name,
       NULL::text AS sender, NULL::timestamptz AS at, NULL::text AS text, NULL::text AS kind, NULL::boolean AS has_attachment, NULL::text AS source_id
WHERE false
"""


def present(conn: Connection) -> list[str]:
    """The platform schemas that exist (with their messages table) right now."""
    rows = conn.execute(
        text(
            "SELECT table_schema FROM information_schema.tables WHERE table_name = 'messages' AND table_schema = ANY(:s)"
        ),
        {"s": list(PARTS)},
    )
    have = {r[0] for r in rows}
    return [p for p in PARTS if p in have]


def sql(platforms: list[str]) -> str:
    body = "\nUNION ALL\n".join(f"({PARTS[p].strip()})" for p in platforms) if platforms else EMPTY.strip()
    return f"CREATE OR REPLACE VIEW unified.messages_all WITH (security_invoker = true) AS\n{body}"


def build(conn: Connection) -> list[str]:
    """(Re)create the view over the platforms that exist; returns them. A no-op on a database that is not Postgres."""
    if conn.dialect.name != "postgresql":
        return []
    have = present(conn)
    conn.execute(text("CREATE SCHEMA IF NOT EXISTS unified"))
    conn.execute(text("DROP VIEW IF EXISTS unified.messages_all"))  # a column list may differ from the old one
    conn.execute(text(sql(have)))
    return have
