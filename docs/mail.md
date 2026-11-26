# Mail

`strata-takeout-mail` loads an mbox (Gmail Takeout's `All mail` file, labels in `X-Gmail-Labels`) into schema `mail` of the shared database. The original mbox is never touched or deleted.

## What is stored
- **Every message** gets a metadata row (`messages`): sender, recipients, subject, time, labels, flags (`is_sent`, `has_unsubscribe`, `is_bulk`), sizes and its byte offset in the mbox. Junk costs a row, not a body.
- **Kept messages only** also get `body_text` (plain text; HTML with tags stripped when there is no plain part) and `attachments` rows. The attachment bytes are written under a root the job passes, named by SHA-256, so equal files are stored once and filecat can catalogue the folder.
- `decision` is `keep`, `drop` or `undecided`; `decided_by` names the rule (or later the model).

## Rules (`rules.py`)
Keep when any of: the message is in Sent; it carries a keep label (`Starred`, `Category Personal|Purchases|Travel|Bills`); its sender is an address the owner has written to. Drop when only junk signals hold: `List-Unsubscribe`, `Precedence: bulk|list|junk` or `Auto-Submitted`, or a junk label (`Spam`, `Category Promotions|Social|Forums`). Keep wins a conflict. Everything else is `undecided`: the classifier's job (planned: embeddings plus a small model, with an LLM labelling the middle band).

## Running it
```
strata-takeout-mail ingest ACCOUNT /path/to/All-mail.mbox --attachments /path/to/dir
strata-takeout-mail sync            # STRATA_TAKEOUT_MAIL_ACCOUNTS=id[:label]=mbox,...  STRATA_TAKEOUT_MAIL_ATTACHMENTS=id=dir,...
strata-takeout-mail refill ACCOUNT  # bodies for rows since marked keep, no rescan
```
Idempotent: an unchanged mbox (size, mtime) is skipped; a changed one replaces its rows in one transaction. The real database needs `OMARCHY_DB_LIVE=1`.
