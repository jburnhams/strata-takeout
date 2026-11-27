# Changelog

Newest first, grouped by day. One entry per change, in the same commit or PR as the change.

## 2026-11-27

**Inbox: archive names with spaces reach filecat as one argument**
`run_cmd` split command strings on whitespace, so `filecat ingest-archive` got `All Data Categories.1.zip` as four arguments, failed, and left the archive uncatalogued (extraction was unaffected). Strings are now split like a shell and the platform and path arguments are quoted.

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

## 2026-11-10

**Move to uv, ruff, pyright and the shared strata-python gate (branch `claude/project-thread-iw7199`)**
Jonathan asked for every Python project to use the same tooling as [python-app-template](https://github.com/jburnhams/python-app-template): uv, ruff with `ruff format`, pyright and ty, one `scripts/check` gate, and dependencies kept current. Behaviour of the program is unchanged.

- `pyproject.toml`, `uv.lock`: hatchling instead of setuptools (entry points and the `filecat.profiles` group are unchanged); a `dev` group (pytest tooling, ruff, pyright, ty, strata-python for its gate); ruff `E,F,I,B,UP,SIM,RUF,C4,PT,S,TID` with `pytest.importorskip` banned, line length 120 (was 400), pyright strict ratchet starting at `__init__.py`, ty, coverage floor 70 (73% measured with bsdtar installed). `requirements-test.txt`: **removed**. `scripts/check`: **new**.
- `build/`: the setuptools build output (`build/lib/...`) was tracked by mistake; **removed** from git and ignored.
- `src/`, `tests/`: `ruff format` and import sorting (mechanical). `inbox.py`: `noqa: S108` on the `/tmp` fallback for the lock file (only used when `XDG_RUNTIME_DIR` is unset). `tests/unit/test_manifest.py`: the stub HTTP handlers' `log_message` takes the base class's `(format, *args)`.
- `.github/workflows/unit.yml`: uv, current action versions, bsdtar installed, the gate once on 3.13, unit and integration tests with coverage on 3.11-3.13. `upgrade-strata-python.yml`: **new**, weekly PR that moves the library pin after the gate (it installs bsdtar first, as `unit.yml` does). `.github/dependabot.yml`: ecosystem `uv` instead of `pip`.
- `README.md`, `AGENTS.md`: commands for uv. `scripts/install.sh` already used uv and is unchanged.
- Still skipped on a machine without `bsdtar` (22 tests, with a reason; CI installs it): making a missing tool fail instead of skip is a follow-up.
