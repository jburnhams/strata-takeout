# strata-takeout progress

## 2026-11-07: created
Moved here from `~/.local/bin` on a home Linux box, where it was written the same day while the first full Google Takeout (3.2 TiB, 789 files on Drive) was being pulled. The scripts are unchanged except that the tool paths (`SAFE_EXTRACT`, `FILECAT`) are now configurable. The Python parts (path rules, safe extractor, filecat profile) became a package with tests.

Decisions:
- Google first, as scripts; no premature plugin interface (see docs/platforms.md). Port the bash to Python only with tests, not during a live download.
- Standard library only. External tools: rclone (fetch), bsdtar (fast extraction), optionally filecat.
- Staging on the NVMe, archives kept in `done/` after extraction, loose non-archive files in `loose/`, `dupes.tsv` and `renamed.tsv` as the records of what was renamed.
- Everything that touches data is covered by the integration tests in `tests/integration/test_takeout_extract.py` (oldest-first, the download gate, nested skip, long names, twins, corrupt archive).

Open: `takeout-pull` tests (needs a fake rclone), a second platform, a request/reminder helper for tier-B services, own Google client id.

## 2026-11-11: second Google account (brand YouTube channel)
A Takeout of a second account (brand channel "whyIsItSoLoud", own uploads, My Activity, YouTube) was dropped in `~/dropbox/takeouts/google2/`. Decision: it is its own inbox platform, `google2` -> `/srv/exports/google2/tree/` + its own filecat collection, never `google/` (that merges into `/srv/exports/gt` and would overwrite the main account's files at the same paths). Inbox change: platforms whose name starts with `google` extract with `Takeout/` stripped (`strip_for`), so both trees have the same layout (`YouTube and YouTube Music/...`, `My Activity/...`) and the same parsers work on either. Not routed through the Drive pipeline (no Drive for this account; the manual download is the fetch).

## 2026-11-11: YouTube history into the shared database
`strata-takeout-youtube` (`src/strata_takeout/youtube/`, install extra `[youtube]`): parses `watch-history.html`, `search-history.html` and `My Activity/YouTube/My Activity.html` of every configured Google tree into schema `youtube` of the shared database (tables `accounts`, `activity`, `sources`, view `watches`; models and Alembic migrations here, `[tool.omarchy-db]` in pyproject). Accounts are the `youtube_accounts` setting (strata-python Settings; default main = `/srv/exports/gt`, brand = `/srv/exports/google2/tree`), so a new Google tree is one more `id=tree` entry. Idempotent: `sources` records each file's size and mtime; an unchanged file is skipped, a changed one replaces all rows of its (account, source) in one transaction. Runs through `takeout-youtube.service`, started by `OnSuccess=` of `takeout-extract.service` and `takeout-inbox.service`. Takeout times are local with an abbreviation (BST/GMT), stored as UTC. `my-activity` repeats `watch-history` watches: count watches through the `watches` view.
