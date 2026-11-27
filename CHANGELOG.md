# Changelog

Newest first, grouped by day. One entry per change, in the same commit or PR as the change.

## 2026-11-27

**Google Chat / Hangouts: Takeout groups, old Gmail chat logs and gmvault backups into schema `googlechat` (`strata-takeout-googlechat`)**
One schema for every Google chat export, each source kept whole: `conversations` (the group's `info` JSON), `members`, `messages` (typed sender/time/text plus the complete source message in `raw`) and `attachments` (original and export names, matched file with size and SHA-256, `status` present | missing | orphan, `match` exact | unquoted | sequence | shared). Takeout `Groups/*/messages.json` is read with its media matched back to messages (the export's duplicate `name(n).ext` and `?`-in-name quirks); chat-log mails labelled Chat in a Gmail mbox, and the same mails in a gmvault tree, become per-line messages from their XML time stamps (falling back to the mail date, then the mbox separator date). Unit `takeout-googlechat.service` is started by the extract and inbox units; `migrations_googlechat/`, install extra `[googlechat]`, `docs/googlechat.md`.

**Gmail: undated messages take the mbox separator's date**
Messages without a `Date` header (Gmail's old chat logs have none) used to load with `at` empty; `parse.when()` now falls back to the date on the mbox `From ` separator line (UTC when it has no zone). A header always wins. `rules.RULES` moved to `r5`, so the next `sync` reloads an unchanged mbox and backfills `at` (idempotent after that).

**Gmail: a gmvault backup is a source for the loader**
`ingest`/`sync` detect a gmvault backup (`db/YYYY-MM.zip` with `.eml`/`.meta` pairs, or unzipped) and load it as source `gmvault` (offset = Gmail id): IMAP labels and flags become the rules' label names, `internal_date` dates a message with no `Date`, a repeated `Message-ID` is dropped, `gmvault-chats` items are skipped. `gmail/gmvault.py`; tests with synthetic month zips. See `docs/gmail.md`.

**Search by meaning: embeddings of kept Gmail bodies and WhatsApp conversation windows (`embed`, `search`)**
Both loaders can embed what they keep through a strata-models service (role `text-embed`, bge-m3, found by `STRATA_MODELS_URL`; extras `[gmail]` and `[whatsapp]` now include `strata-models-client`).
- Gmail: `chunks` (pgvector, HNSW; offsets into the body), `messages.embedded`; `strata-takeout-gmail embed|search`, `sync|ingest --embed`; sync also takes the verdicts file from setting `gmail_verdicts`. Junk is never embedded.
- WhatsApp: `windows` (30-minute-gap conversation windows with the same kind of vector, no phone numbers in the embedded text); rebuilt after each load, unchanged windows keep their vector; `embed|search`, `--embed`.
- `src/strata_takeout/embedding.py` (chunking, the service call) and `vectors.py` (a small copy of `strata_python.db.vectors`: the repo is still on `omarchy-db`, whose test fixtures clash with the newer strata-python's database plugin).
- Migrations `migrations_gmail/…chunks…`, `migrations_whatsapp/…windows…`; tests with a fake client, also run on Postgres (`pytest --pg`).

**Instagram: an export tree into the shared database (`strata-takeout-instagram`)**
Schema `instagram` (the Facebook tables plus `follows`; `migrations_instagram/`, `[tool.omarchy-db.apps.instagram]`, install extra `[instagram]`, unit `systemd/takeout-instagram.service` started by `OnSuccess=` of the inbox and extract units). Direct messages, own posts, archived posts and comments are `messages` with a `kind` and an `audience`; likes and message reactions, attachments (path, size, SHA-256) and followers/following are their own tables. The loader and parsing helpers are shared with `strata_takeout.facebook` (the loader now takes the models module and a reader). See `docs/instagram.md`.

**Facebook: an export tree into the shared database (`strata-takeout-facebook`)**
Schema `facebook` (`accounts`, `sources`, `threads`, `members`, `messages`, `reactions`, `attachments`; `migrations_facebook/`, `[tool.omarchy-db.apps.facebook]`, install extra `[facebook]`, unit `systemd/takeout-facebook.service` started by `OnSuccess=` of the inbox and extract units). Chat lines, posts and comments are all `messages` with a `kind` and an `audience`; reactions and attachments (path relative to the tree, size, SHA-256) are their own tables; the latin-1-read-as-UTF-8 garbling of every string is undone. Idempotent by a fingerprint of the JSON files. See `docs/facebook.md`.

**Inbox: archive names with spaces reach filecat as one argument**
`run_cmd` split command strings on whitespace, so `filecat ingest-archive` got `All Data Categories.1.zip` as four arguments, failed, and left the archive uncatalogued (extraction was unaffected). Strings are now split like a shell and the platform and path arguments are quoted.

**Gmail: a verdict can carry its own `keep` decision (rules r4)**
`{"offset", "keep", "label", ...}`: when `keep` is present it decides whether an `other` message is promoted, else the label does as before. Rules never lose what they keep.

## 2026-11-26

**Gmail: an attached message is an attachment, not part of the body**
A forwarded email attached as `.eml` (`message/rfc822`) used to be entered by the parser, merging its text into the outer body and recording no attachment. `gmail/parse.py` now treats it as one leaf and stores its bytes as the attachment.

**Gmail: a Takeout mbox into the shared database (`strata-takeout-gmail`)**
Schema `gmail` (`accounts`, `sources`, `messages`, `attachments`; `migrations_gmail/`, `[tool.omarchy-db.apps.gmail]`, install extra `[gmail]`). Every message is loaded as metadata; messages the rules pick (sent, and personal received) also keep their body text and attachments (written content-addressed under a folder the job passes). Idempotent: unchanged mbox and rule set are skipped, a changed one replaces its rows. See `docs/gmail.md`.

**WhatsApp: media found by content hash, installed by `scripts/install.sh`**
A message whose file name matches nothing in the media folders is now looked up by the backup's own SHA-256 (`media_hash`, base64) among the files of the same size, so renamed or moved files (and files in subfolders such as chat-export folders) link up. Only candidates of the right size are hashed. The media fingerprint now covers file sizes too. `scripts/install.sh` installs the `[whatsapp]` extra and links `strata-takeout-whatsapp` into `~/.local/bin`.

**WhatsApp: a decrypted Android backup into the shared database (`strata-takeout-whatsapp`)**
First WhatsApp source: the old-format `msgstore.db` (+ `wa.db`) of a decrypted Android backup. See `docs/whatsapp.md`.

- `src/strata_takeout/whatsapp/`: models (schema `whatsapp`: `accounts`, `sources`, `chats`, `contacts`, `participants`, `messages`), a tolerant read-only parser (a missing column reads as NULL; `media_wa_type` is TEXT in old files; junk sizes and times become NULL), and an idempotent loader (fingerprint in `sources`, replace per (account, source) in one transaction; media found by basename under the configured folders, and newly added media triggers a reload). CLI `strata-takeout-whatsapp sync|ingest|doctor|set`; accounts and media folders come from settings, never defaults. Install extra `[whatsapp]`.
- `migrations_whatsapp/`, `[tool.omarchy-db.apps.whatsapp]`: the second schema of this repo, which needs the database library's multi-app support (needs [database PR 15](https://github.com/jburnhams/database/pull/15), merged).
- Not loaded yet: chat exports (`_chat.txt`), the new-format `message` table, call log, quotes and receipts, the iOS backup.

## 2026-11-15

**Google Photos sidecars give filecat the people in each photo (branch `sidecar-labels`).** Pairs with strata-filecat's file labels (`filecat labels`): the `takeout` profile now says which files can carry labels and reads them.

- `src/strata_takeout/sidecar.py`: **new**. `candidates(name)` lists the sidecar names Google may have used (cut at 46 characters before `.json`, `name(1).jpg` -> `name.jpg.supplemental-metadata(1).json`, `-edited` copies use the original's sidecar, older `<file>.json`); `find`, `people`.
- `profile.py`: `Takeout.label_exts` (image extensions) and `Takeout.labels(rel)` -> `[("person", name)]` or None when there is no sidecar.
- Tests: `tests/unit/test_sidecar.py` (synthetic files only), including a long-named duplicate whose truncated literal name would otherwise pick up the original's sidecar.

## 2026-11-10

**Move to uv, ruff, pyright and the shared strata-python gate (branch `claude/project-thread-iw7199`)**
Jonathan asked for every Python project to use the same tooling as [python-app-template](https://github.com/jburnhams/python-app-template): uv, ruff with `ruff format`, pyright and ty, one `scripts/check` gate, and dependencies kept current. Behaviour of the program is unchanged.

- `pyproject.toml`, `uv.lock`: hatchling instead of setuptools (entry points and the `filecat.profiles` group are unchanged); a `dev` group (pytest tooling, ruff, pyright, ty, strata-python for its gate); ruff `E,F,I,B,UP,SIM,RUF,C4,PT,S,TID` with `pytest.importorskip` banned, line length 120 (was 400), pyright strict ratchet starting at `__init__.py`, ty, coverage floor 70 (73% measured with bsdtar installed). `requirements-test.txt`: **removed**. `scripts/check`: **new**.
- `build/`: the setuptools build output (`build/lib/...`) was tracked by mistake; **removed** from git and ignored.
- `src/`, `tests/`: `ruff format` and import sorting (mechanical). `inbox.py`: `noqa: S108` on the `/tmp` fallback for the lock file (only used when `XDG_RUNTIME_DIR` is unset). `tests/unit/test_manifest.py`: the stub HTTP handlers' `log_message` takes the base class's `(format, *args)`.
- `.github/workflows/unit.yml`: uv, current action versions, bsdtar installed, the gate once on 3.13, unit and integration tests with coverage on 3.11-3.13. `upgrade-strata-python.yml`: **new**, weekly PR that moves the library pin after the gate (it installs bsdtar first, as `unit.yml` does). `.github/dependabot.yml`: ecosystem `uv` instead of `pip`.
- `README.md`, `AGENTS.md`: commands for uv. `scripts/install.sh` already used uv and is unchanged.
- Still skipped on a machine without `bsdtar` (22 tests, with a reason; CI installs it): making a missing tool fail instead of skip is a follow-up.
