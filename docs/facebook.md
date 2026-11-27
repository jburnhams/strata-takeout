# Facebook (JSON "Download your information")

`strata-takeout-facebook` loads an extracted Facebook export tree (the folder holding `your_facebook_activity/`) into schema `facebook` of the shared database. Everything someone wrote is one kind of row, a **message**; only the audience differs.

| `kind` | `audience` | `audience_name` | from |
|---|---|---|---|
| `chat` | `thread` | thread title | `messages/<folder>/<thread>/message_*.json` |
| `post` | `timeline` | | `posts/your_posts*.json` |
| `post` | `profile` | the page/profile posted on | `posts/posts_on_other_pages_and_profiles.json` |
| `post` | `group` | the group (from the sentence the export gives) | `groups/group_posts_and_comments.json` |
| `comment` | `group` | the group | `groups/your_comments_in_groups.json` |
| `comment` | `post` | whose post (`self` for one's own) | `comments_and_reactions/comments.json` |

- `reactions`: emoji reactions on chat messages (`message_id` set) and the account's own likes elsewhere (`message_id` NULL, `target_title` = the export's sentence).
- `attachments`: photo/video/gif/audio/file/sticker/link per message; `path` is relative to the tree root (so a filecat collection rooted at the same tree joins on it), plus mime, size and SHA-256 when the file exists.
- `threads` / `members`: one row per Messenger conversation and who is in it.
- `parent_id` exists for replies but the export rarely says what a comment answers; the raw `title` sentence is kept so a later parser can fill it in.
- Facebook writes every string as UTF-8 bytes read as latin-1; the parser undoes that on all strings.
- Message ids: the export has none, so `ext_id` is a SHA-1 of kind, thread/audience, time and author plus a counter for exact repeats; a re-export of the same data gives the same ids.
- Idempotent: a fingerprint of the JSON files (count, total size, newest mtime) is kept in `sources`; unchanged is skipped, changed replaces the account's rows in one transaction. Loading hashes every attachment file once per load.

Run: `OMARCHY_DB_LIVE=1 strata-takeout-facebook ingest ACCOUNT TREE` (or `sync` with `STRATA_TAKEOUT_FACEBOOK_ACCOUNTS=id[:label]=tree`).
