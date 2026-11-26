# Gmail (Google Takeout mbox)

`strata-takeout-gmail` loads the `All mail Including Spam and Trash.mbox` of a Google Takeout into schema `gmail` of the shared database.

- **Every message becomes a row** (`gmail.messages`): sender, recipients, subject, date, Gmail labels, sizes, unsubscribe/bulk/reply flags, thread id when the export has one, and the mbox byte offset (so the full message can always be re-read).
- **Only "keep" messages also get a body and attachments.** `rules.py` decides: mail you sent, and personal mail received (not bulk, no unsubscribe header, not a no-reply sender, not spam/trash, and Gmail's category is `personal` or absent). Everything else keeps its metadata only (`tier = other`, `tier_reason` says which rule).
- Attachments of kept mail are written content-addressed (`<sha[:2]>/<sha><ext>`) under the folder the job passes (`--attachments` or `STRATA_TAKEOUT_GMAIL_ATTACHMENTS`); the `attachments` table records name, type, size, hash and the relative path. Without a folder nothing is written.
- The load is idempotent: the mbox's size/mtime and the rule set's version (`rules.RULES`) are kept in `sources`; an unchanged file is skipped, a changed file or a new rule set replaces the (account, source) rows in one transaction. Changing the rules therefore re-tiers everything on the next `sync`.
- The rules are the first, deterministic tier. A classifier for the undecided middle (marketing vs purchases from the same sender) would write its verdicts as a later, separate step.

Run: `OMARCHY_DB_LIVE=1 strata-takeout-gmail ingest ACCOUNT TREE --attachments DIR` (or `sync` with `STRATA_TAKEOUT_GMAIL_ACCOUNTS=id[:label]=tree`). Reading is streaming (the file is never held in memory); about 3 ms per message.
