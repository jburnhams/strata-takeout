# strata-takeout

Download, extract and catalogue the data exports of your online accounts, unattended, onto an encrypted disk. Google Takeout works today (a 3+ TiB export pulled from Drive, extracted oldest-first and catalogued); Meta, X, Amazon, Apple, Reddit, Flickr, Microsoft and more are planned: [docs/platforms.md](docs/platforms.md).

## What the Google pipeline does
```
Drive "Takeout" --rclone move--> stage (NVMe, capped) --takeout-extract--> /srv/exports/gt/ (tree)   archives -> done/
        \--(non-archives, direct)--> /srv/exports/google-takeout/loose/                              catalogue -> strata-filecat
```
- **`takeout-pull`** moves finished files off Drive and deletes each remote file only after rclone verified size and hash, so Drive space is freed as you go. Archives go to a staging directory on fast disk in batches (default 100 GB, only while the stage holds < 300 GB); `--files` handles non-archives (videos, mbox) straight to the data disk. Duplicate-named files on Drive are renamed apart first (`rclone dedupe --dedupe-mode rename`).
- **`takeout-extract`** extracts oldest export first so newer overwrites older, waits while an older export is still downloading, skips the nested `Drive/Takeout/` (earlier exports that Drive re-packs), drops the leading `Takeout/` to keep paths short, falls back to `strata-takeout-safe-extract` when a name is over 255 bytes (cut + hash, logged in `renamed.tsv`), and moves each archive to `done/`.
- **Catalogue**: each extracted archive is recorded in [strata-filecat](https://github.com/jburnhams/strata-filecat) via the `takeout` profile (`strata_takeout.profile`): size, CRC32, later SHA-256, versions per path, duplicates.
- Alerts through `OnFailure=` on each systemd unit (and a quiet push when archives land).

## Install
Needs rclone (a remote named `gdrive` with full `drive` scope; use your own client id, rclone's shared one is rate limited), bsdtar (`libarchive`), and uv.
```bash
scripts/install.sh              # venv at ~/.local/share/strata/venv, tools linked into ~/.local/bin
cp systemd/*.service systemd/*.timer ~/.config/systemd/user/ && systemctl --user daemon-reload
systemctl --user enable --now takeout-pull.timer takeout-pull-files.timer takeout-extract.timer filecat.timer
sudo scripts/setup-exports-disk prepare     # optional: LUKS2 + ext4 data disk (edit the serial/model at the top first; it wipes the disk)
```
Paths and limits are environment variables with defaults (`EXPORTS_ROOT=/srv/exports`, `STAGE=~/.cache/takeout-stage`, `STAGE_MAX`, `BATCH`, `TRANSFERS`, `KEEP_ARCHIVES`, `MIN_AGE`, `SETTLE`); see the header of each script.

## Develop
```bash
pip install -r requirements-test.txt && pip install -e .
pytest                       # unit + integration (the integration tests run the real bash script; need bsdtar)
ruff check src tests
```
See [AGENTS.md](AGENTS.md) for the rules that matter (ordering, deleting from the remote, replacing running scripts) and [docs/progress.md](docs/progress.md) for the log.
