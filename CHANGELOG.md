# Changelog

Newest first, grouped by day. One entry per change, in the same commit or PR as the change.

## 2026-11-26 (later)

**Mail: an mbox (Gmail Takeout) into the shared database, keeping only what the rules say is worth keeping (`strata-takeout-mail`)**
First mail source. Every message gets a metadata row; bodies and attachments are stored only for messages marked `keep`. See `docs/mail.md`.

- `src/strata_takeout/mail/`: models (schema `mail`: `accounts`, `sources`, `messages`, `attachments`), a streaming mbox scanner (the 20 GB file is never loaded; each message is re-read by byte offset), `rules.py` (keep: Sent, Gmail keep labels, senders the owner has written to; drop: bulk headers and junk labels with no keep signal; the rest `undecided` for a classifier; keep wins a conflict) and an idempotent loader (fingerprint in `sources`, replace per (account, source) in one transaction; attachments written content-addressed under a root the job passes). CLI `strata-takeout-mail sync|ingest|refill|doctor|set`; accounts and the attachments folder come from settings, never defaults. Install extra `[mail]`.
- `migrations_mail/`, `[tool.omarchy-db.apps.mail]`: the third schema of this repo.
- `refill` reads bodies for rows a classifier has since moved to `keep`, without rescanning. Not yet: the classifier, embeddings, IMAP sources.

## 2026-11-26

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
