# strata-takeout progress

## 2026-11-07: created
Moved here from `~/.local/bin` on a home Linux box, where it was written the same day while the first full Google Takeout (3.2 TiB, 789 files on Drive) was being pulled. The scripts are unchanged except that the tool paths (`SAFE_EXTRACT`, `FILECAT`) are now configurable. The Python parts (path rules, safe extractor, filecat profile) became a package with tests.

Decisions:
- Google first, as scripts; no premature plugin interface (see docs/platforms.md). Port the bash to Python only with tests, not during a live download.
- Standard library only. External tools: rclone (fetch), bsdtar (fast extraction), optionally filecat.
- Staging on the NVMe, archives kept in `done/` after extraction, loose non-archive files in `loose/`, `dupes.tsv` and `renamed.tsv` as the records of what was renamed.
- Everything that touches data is covered by the integration tests in `tests/integration/test_takeout_extract.py` (oldest-first, the download gate, nested skip, long names, twins, corrupt archive).

Open: `takeout-pull` tests (needs a fake rclone), a second platform, a request/reminder helper for tier-B services, own Google client id.
