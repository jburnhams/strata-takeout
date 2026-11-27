# Instagram (JSON "Download your information")

`strata-takeout-instagram` loads an extracted Instagram export tree (the folder holding `your_instagram_activity/`) into schema `instagram` of the shared database. Same design as [Facebook](facebook.md) (and the same loader and parsing helpers, `strata_takeout.facebook`): everything someone wrote is a **message** row with a `kind` and an `audience`.

| `kind` | `audience` | `audience_name` | from |
|---|---|---|---|
| `chat` | `thread` | thread title | `your_instagram_activity/messages/<folder>/<thread>/message_*.json` (same format as Messenger) |
| `post` | `timeline` | | `media/posts_*.json` (caption from the post or its media) |
| `post` | `archive` | | `media/archived_posts.json` |
| `comment` | `post` | the account that owns the post | `comments/post_comments*.json` |

- `reactions`: reactions on direct messages (`message_id` set) and the account's own likes of posts and comments (`likes/liked_*.json`: `message_id` NULL, `target_title` = owner and link).
- `attachments`: photo/video/link per message, `path` relative to the tree root (a filecat collection rooted at the same tree joins on it), mime, size and SHA-256 when the file exists.
- `follows`: followers, following and close friends (`direction`, name, profile link, when).
- `threads` / `members`: one row per conversation and who is in it.
- Not loaded: `media/posts.json` (a second, label-value rendering of the same posts), stories, ads and insights, login and settings files.
- Strings get the same UTF-8-read-as-latin-1 repair as Facebook; ids, fingerprinting, rollback on an unreadable file: see the Facebook doc.

Run: `OMARCHY_DB_LIVE=1 strata-takeout-instagram ingest ACCOUNT TREE` (or `sync` with `STRATA_TAKEOUT_INSTAGRAM_ACCOUNTS=id[:label]=tree`).
