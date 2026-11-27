# Google Chat / Hangouts (schema `googlechat`)

`strata-takeout-googlechat` loads every Google chat export into one schema, **keeping each source's data whole** (typed columns for the common fields plus the leftovers as JSON) so nothing is lost and a cross-platform view can be layered on top later.

| source | input | what it is |
|---|---|---|
| `takeout` | the Takeout `Google Chat/` folder (`Groups/<DM or Space id>/{group_info,messages}.json` + media) | Hangouts history (from ~2013) and Google Chat |
| `gmail` | a Gmail mbox (`All mail….mbox`) | the old Gmail/GTalk chat logs: mails labelled Chat whose XML part holds a run of chat lines |
| `gmvault` | a gmvault backup (`chats*.zip` or the unzipped `<id>.eml[.gz]` + `<id>.meta`), read with `gmail/gmvault.py` (the mail loader's reader) | the chat-log mails (label `gmvault-chats`) of an older backup; a backup holds each chat twice, one is kept |

## Tables
- `conversations`: a Takeout group (`kind` dm | space, `ext_id` the group folder name, `info` the whole `group_info.json`) or, for the mail sources, a set of people (`kind` chat, `ext_id` a hash of the addresses: the same people always land in one conversation). First/last time and message count are kept.
- `members`: name, address, user type as the source gives them (mail chats: addresses only).
- `messages`: sender, `at` (UTC), `at_precision` (`s` Takeout; `ms` a line's own stamp; `mail` the mail's `Date` header, or the mbox separator's date when it has none), `text`, `topic_id`, `has_attachment` and `raw`, the source's complete message (Takeout: annotations, url/youtube/drive metadata, `attached_files`; mail: the line's XML attributes plus message id, labels, thread, subject).
- `attachments`: per `attached_files` entry `original_name`, `export_name`, and, when found, `path` (relative to the tree root), size, SHA-256, mime. `status` is `present`, `missing` (referenced, no file) or `orphan` (a file nothing refers to, `message_id` NULL); `match` says how the file was found: `exact`, `unquoted` (the export name has a URL query such as `…?account_id=0`, the file was stored with `_`), `sequence` (the n-th reference to a name gets `name(n).ext`), `shared` (a name the export reused).
- `sources`: fingerprint per (account, source); an unchanged input is skipped, a changed one replaces that (account, source) in one transaction.

## Run
`OMARCHY_DB_LIVE=1 strata-takeout-googlechat ingest ACCOUNT PATH...` (any mix of a Takeout tree, a Gmail mbox or a folder holding one, a gmvault tree), or `sync` with `STRATA_TAKEOUT_GOOGLECHAT_ACCOUNTS=id[:label]=path[;path...][,...]`. The unit `systemd/takeout-googlechat.service` runs `sync` via `OnSuccess=` of the extract and inbox units. Mail chats are streamed from the mbox (it is never held in memory); a chat line takes its time from its own stamp, then the mail's `Date`, then the separator line.

Change a model → `dbtool -p googlechat revision -m "..."`, `dbtool -p googlechat check`; tests: `pytest` and `pytest --pg`.
