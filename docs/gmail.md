# Gmail (Google Takeout mbox)

`strata-takeout-gmail` loads the `All mail Including Spam and Trash.mbox` of a Google Takeout into schema `gmail` of the shared database.

- **Every message becomes a row** (`gmail.messages`): sender, recipients, subject, date, Gmail labels, sizes, unsubscribe/bulk/reply flags, thread id when the export has one, and the mbox byte offset (so the full message can always be re-read).
- **Only "keep" messages also get a body and attachments.** `rules.py` decides: mail you sent, and personal mail received (not bulk, no unsubscribe header, not a no-reply sender, not spam/trash, and Gmail's category is `personal`; uncategorised mail is left as `other` with reason `uncategorised`, for a later classifier). Everything else keeps its metadata only (`tier = other`, `tier_reason` says which rule).
- Attachments of kept mail are written content-addressed (`<sha[:2]>/<sha><ext>`) under the folder the job passes (`--attachments` or `STRATA_TAKEOUT_GMAIL_ATTACHMENTS`); the `attachments` table records name, type, size, hash and the relative path. Without a folder nothing is written.
- The load is idempotent: the mbox's size/mtime and the rule set's version (`rules.RULES`) are kept in `sources`; an unchanged file is skipped, a changed file or a new rule set replaces the (account, source) rows in one transaction. Changing the rules therefore re-tiers everything on the next `sync`.
- The rules are the first, deterministic tier. A classifier for the undecided middle (marketing vs purchases from the same sender) hands its verdicts in as a JSON-lines file (`--verdicts FILE`, one `{"offset","label","confidence","source"}` per line, offset = the message's mbox offset). A verdict with `"keep": true` (or, without the flag, a label of `personal` or `transactional_keep`) promotes an `other` message to `keep` (reason `classified`); a verdict never demotes what the rules keep. The label, confidence and source are stored on the message. A new verdicts file reloads the mbox like a new rule version.

- **Dates:** `at` is the `Date` header. A message without one (Gmail's old chat logs) takes the date of its mbox `From ` separator line instead; only a message with neither stays undated.

Run: `OMARCHY_DB_LIVE=1 strata-takeout-gmail ingest ACCOUNT TREE --attachments DIR` (or `sync` with `STRATA_TAKEOUT_GMAIL_ACCOUNTS=id[:label]=tree`). Reading is streaming (the file is never held in memory); about 3 ms per message.

## Search by meaning (embeddings)
`strata-takeout-gmail embed` chunks the body of every kept message (paragraphs packed up to 1,400 characters, `embedding.chunk`), asks a strata-models service (role `text-embed`, found by `STRATA_MODELS_URL`; the model is the service's registry decision, bge-m3 at the time of writing, 1024 dimensions) for one vector per piece and stores it in `chunks` (pgvector, cosine HNSW index; offsets into `messages.body_text`, not the text twice). Each piece is embedded with a `From / Subject / Date` header so it knows who wrote it; quoted reply lines are dropped and links shortened first.

- Idempotent: `messages.embedded` holds `"<model>:<chunker>"`; only messages without it (new, reloaded, or embedded by another model or chunker version) are done. Junk (metadata-only) mail is never embedded.
- Automatic after a load: `sync`/`ingest --embed` (or `STRATA_TAKEOUT_GMAIL_EMBED=1`). Skipped quietly when `strata-models-client` is missing or the service is down; the next run catches up.
- `strata-takeout-gmail search "when did we book the flights"` prints the closest pieces. Install extra `[gmail]` brings the client.
