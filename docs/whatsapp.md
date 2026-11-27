# WhatsApp

Why a phone backup: "Export chat" is manual, one chat at a time, capped (about 10k messages with media) and drops replies and reactions; the Drive and iCloud backups cannot be downloaded in a usable form; "Request account info" has no messages; the desktop and web apps keep only a partial cache. A **decrypted backup** of the phone is the one complete source.

## Sources
| Source | State |
|---|---|
| Android `msgstore.db` (old format, table `messages`) + `wa.db` | loaded by `strata-takeout-whatsapp` (schema `whatsapp`) |
| Android new format (`message`, `jid`, `chat` tables) | not yet: needs a backup in that format to test against |
| iOS encrypted backup (`ChatStorage.sqlite`) | planned, same schema, source `ios-chatstorage` |
| `WhatsApp Chat - <name>` folders (`_chat.txt` + media) | left alone; a partial, lossy fallback |

## Getting a database
- **Android, current:** turn on end-to-end encrypted backup in WhatsApp with the 64-digit key (store the key in the password manager, never in a repo), make a local backup, copy `msgstore.db.crypt15` and the `Media` folder off the phone, decrypt with [wa-crypt-tools](https://github.com/ElDavoo/wa-crypt-tools).
- **Old backups** (`.crypt12`/`.crypt14`) need the `key` file of the phone they came from; without it they cannot be opened.
- **iOS:** an encrypted local backup (Finder, or `idevicebackup2` from Linux), then extract `ChatStorage.sqlite`.

## Loading
```
strata-takeout-whatsapp ingest ACCOUNT TREE --media DIR [--media DIR2]   # one backup
strata-takeout-whatsapp sync                                               # every configured account
```
`TREE` holds (or is) `msgstore.db`, with `wa.db` next to it. The job passes the accounts and media folders as settings, never defaults: `STRATA_TAKEOUT_WHATSAPP_ACCOUNTS=id[:label]=tree,...` and `STRATA_TAKEOUT_WHATSAPP_MEDIA=id=dir[:dir...],...`. The real database needs `OMARCHY_DB_LIVE=1`. Idempotent: the fingerprint (size and mtime of both files, the media folders and how many files they hold) sits in `sources`; unchanged is skipped, otherwise all rows of (account, source) are replaced in one transaction.

## Tables (schema `whatsapp`)
`accounts`, `sources`, `chats` (jid, subject, group flag, first/last time, count), `contacts`, `participants`, `messages` (kind, raw type code, text, caption, media fields, `media_file` = path under the media root when found, location, starred). `media_file` joins to a filecat collection over the same folder (`filecat collection add whatsapp --root media=DIR`, then `filecat run`).

## Gotchas
- Old databases declare `media_wa_type` as TEXT; sizes can be junk (loaded as NULL); timestamps outside 2001-2100 load as NULL.
- Media is matched by file name (two files with the same name in different folders resolve to the first found); when that finds nothing, by the backup's SHA-256 among files of the same size, which also finds renamed or moved files and files in subfolders.
- Message text is personal data: nothing here logs it, and tests use synthetic databases only.
