# Platforms: what exists, what is planned, and the contract

An export has three stages. A platform plugin supplies the first two; the rest is shared.

1. **Fetch**: get the archives off the platform. Best case the platform delivers into a cloud drive we can script (Google: Takeout to Drive, then `rclone move`, which verifies and deletes remote copies as it goes); otherwise an emailed or in-app download link (needs the logged-in browser session), a public API, or nothing (phone only).
2. **Extract**: archive format and layout, the path rules (what to strip, what to skip, what is nested), ordering (oldest export first so newer overwrites older), name problems (over-long, odd characters, duplicate names).
3. **Catalogue**: record what arrived in [strata-filecat](https://github.com/jburnhams/strata-filecat) (size, CRC32 from the archive index, SHA-256 later), via a `filecat.profiles` plugin that mirrors the extract path rules (`strata_takeout.profile`).

Shared, platform-independent: staging on fast disk with a size cap and batches, the safe extractor for over-long names, "twin" handling for duplicate names, notifications, systemd units, the install script.

## The inbox (works for every platform today)
`strata-takeout-inbox` ingests anything dropped into `<inbox>/<platform>/` (see the README). It is the universal fetch: the human is the fetcher. Per-platform automation (rclone for cloud-drive delivery, API clients, readiness detection) is an upgrade on top, and platform-specific path rules or quirks are added as a filecat profile plus an `export`-style config (`strip`, `nested`).

## Plugin contract (to settle when the second platform arrives)
Today Google is a set of scripts (`scripts/google/`) plus the Python parts (`paths.py`, `safe_extract.py`, `profile.py`). When Meta/X/Amazon follow, extract what they share before generalising: do not design the interface from one example. Expected shape:
- `fetch`: a command that moves finished archives into the staging dir (and removes them remotely only after verification); exit non-zero on failure.
- `paths`: a function mapping an entry name to a stored path or a skip (as `paths.safe_parts`), plus its filecat profile.
- `order`: how archives of one platform sort (Google: export timestamp in the name, then mtime, then part).
- `request` (manual platforms): the instructions, and a reminder date, because the request step is a human clicking in a web UI.

## Roadmap (tiers: A = scriptable, B = request in a web UI then download, C = phone only)
| Platform | Tier | Route | Status |
|---|---|---|---|
| Google (Takeout) | A | Takeout to Drive, rclone | **running** (first full export, 3+ TiB) |
| Garmin | A | Connect API (separate job, garmin-sync) | done elsewhere |
| GitHub | A | API / `Export account data` | repo clones only; issues/PRs missing |
| Microsoft (OneDrive, Outlook) | A | rclone `onedrive`, IMAP | todo |
| Flickr | A/B | API key, `Request my Flickr data` | todo |
| LiveJournal | A | `ljdump` | todo |
| Wikipedia | A | MediaWiki API (contributions, uploads, watchlist) | todo |
| Strava | A/B | bulk export, API | manual exports only |
| Meta (Facebook, Instagram, Threads) | B | Accounts Center, Download your information (JSON) | todo |
| X (Twitter) | B | Download an archive of your data | todo |
| Claude | B | Settings, Privacy, Export data | todo |
| OpenAI (ChatGPT) | B | Settings, Data controls, Export data | todo |
| Amazon | B | Request My Data | todo |
| Apple | B | privacy.apple.com, Get a copy of your data (parts up to ~25 GB) | todo |
| Yahoo | B | Download your data; mail over IMAP | todo |
| Reddit | B | data request (CSV), up to ~30 days | todo |
| WhatsApp | C | per-chat export on the phone | todo |

The routes are from general knowledge and must be checked on each first real export. Account names and logins belong in a password manager, never in this repo.

## Lessons from the Claude export (2026-11-07)
- The claude.ai export is not an archive: it arrives as a small **JSON manifest** (`data_files[]` with `export_url`, `filename`, `category`; eight files: conversations, projects, memories, feedback, design_chats, frames, claude_code_cloud, light_metadata). The instructions say the links expire after 24 hours and may stop working once used.
- The links are `claude.ai/export/<uuid>/download/<token>`: a **front-end page route**. An unauthenticated GET returns the web app's HTML (status 200, `text/html`, Cloudflare), the logged-in browser then fetches the zip. So it cannot be automated without the user's browser session, and extracting a session cookie or calling the internal API is fragile and not something to do.
- What the pipeline does instead (`manifest.py`): one request per link, the response is accepted only if it is a complete, valid zip; an HTML response is recognised at once ("needs a browser"), the files are not retried, and `README-download-links-<stamp>.html` (a page of clickable links) is written into the platform's inbox folder with one push saying what to do. The user opens it in a logged-in browser, saves the zips into the same folder, and the inbox ingests them as usual. Saved names are `<export timestamp>_<file>` when the pipeline downloads, plain `conversations-000.zip` when a browser does (the next export reuses those names; the inbox keeps both, `.1` suffix).
- The downloader stays for platforms whose links are direct file URLs: https only, host allow-list (`MANIFEST_HOSTS`), a state file so a used link is never requested again even after its file was filed, three failures and it gives up (alert once), a run lock so a manual and a timer run never use the same one-time link.
- Mistake worth remembering: the first version deleted a rejected response, so the first failure could not be diagnosed without spending another request on a possibly one-use link. A rejected download should keep a few bytes of evidence (status, content type, first bytes).

## Lessons from the first run (Google, 2026-11-07)
- rclone move + `--drive-use-trash=false` frees Drive space as files land; use `--order-by name` and extract oldest export first.
- Drive allows several files with the same name; rclone skips them silently. `rclone dedupe --dedupe-mode rename` renames them apart (the **newer** file gets `-1`), so extract by modification time and wait for all twins.
- A Drive export re-packs the Drive `Takeout` folder, i.e. earlier exports: skip `Drive/Takeout/`.
- ext4 rejects names over 255 bytes; bsdtar extracts everything else and exits 1: fall back to the safe extractor (cut + hash).
- Putting downloads and extraction on the same SMR disk starved the extractor: stage zips on the NVMe with a size cap.
- rclone's shared Google client id is rate limited and being retired in 2026: use your own.
- A running oneshot unit is `activating`, not `active` (alert helpers must treat it as alive); editing a running bash script in place breaks it: replace the file (new inode).
