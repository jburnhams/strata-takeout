# Cross-platform view (`unified.messages_all`)

One query across every chat platform: `unified.messages_all` is a plain Postgres view, a `UNION ALL` with one `SELECT` per loaded platform schema (`whatsapp`, `facebook`, `instagram`, `googlechat`). **Nothing is copied**: each platform keeps its own tables with all its data, the view only maps the common fields, and dropping it loses nothing.

| column | meaning |
|---|---|
| `platform` | `whatsapp`, `facebook`, `instagram`, `googlechat` |
| `source` | the platform's own source value (`takeout`, `gmail`, `gmvault`, a backup, ...) |
| `account` | the account id the job chose |
| `conversation_id`, `conversation_name` | the chat/thread (text). WhatsApp: jid and subject or contact name; Facebook/Instagram: thread and title, or for posts and comments `audience:owner` and the owner; Google chat: the conversation row |
| `sender` | who wrote it (`me` for the WhatsApp owner) |
| `at` | UTC time |
| `text` | the line, post or comment text (WhatsApp: the caption when there is no text) |
| `kind` | the platform's own kind (`text`, `image`, `chat`, `post`, `comment`, `message`, ...) |
| `has_attachment` | the message has a file, media or link attached |
| `source_id` | the row id in the platform's `messages` table, as text: `JOIN whatsapp.messages m ON m.id::text = v.source_id` for the full record |

```sql
select platform, count(*), min(at), max(at) from unified.messages_all group by 1;
select * from unified.messages_all where text ilike '%holiday%' order by at;
```

## Run
`strata-takeout-unified build` migrates schema `unified` and (re)creates the view over the platform schemas that exist at that moment (real database: `OMARCHY_DB_LIVE=1`). **Run it again after a new platform is loaded for the first time**: a platform that did not exist when the view was built is not in it. Changing a platform column the view reads needs the view dropped first (`build` drops and recreates it). With no platform loaded the view exists and is empty.

## Notes
- Postgres only (`security_invoker`: the reader's rights apply, so the per-app schema owners need no grants on each other). The app is `engine = "postgres"`, so its tests (`tests/unit/test_unified.py`) always run on a throwaway Postgres.
- A plain view is always current but scans the platform tables: fine for filtering by time, account or conversation. If a full scan gets slow, make it a materialized view refreshed after each loader run.
- Posts and comments (Facebook, Instagram) carry no thread, so their `conversation_id` is `audience:owner`.
