"""strata-takeout-inbox: ingest export archives dropped into ~/dropbox/takeouts/<platform>/ (an SMB share, so any device can drop files there).

  <INBOX>/<platform>/<file>   becomes   <EXPORTS>/<platform>/tree/...     (archives, extracted)
                                        <EXPORTS>/<platform>/loose/<file>  (anything that is not an archive; a dropped folder moves in whole)
                                        <DONE>/<platform>/<file>           (the original archive, kept; rename if on the same filesystem as the inbox)
and the archive is recorded in the filecat collection <platform> (profile `export`, created on first use).

A file is touched only when it has stopped growing: its mtime and ctime are older than SETTLE seconds and its size is unchanged over a short
re-check, so a half-finished upload over SMB is never taken. Names starting with '.', temp suffixes (.part .partial .tmp .crdownload) and README
files are ignored. Archives are processed oldest first (by mtime) so a newer export overwrites an older one. bsdtar extracts (any format libarchive
reads: zip, tar*, 7z, rar); if it fails (e.g. a name over 255 bytes) the Python extractor retries with names cut + hashed and logged in <platform>/renamed.tsv.
A failing archive stays in the inbox and the run exits 1 (the systemd unit alerts through ntfy). Nothing is ever deleted from the inbox before its extraction succeeded.
Platform `google` is special: its archives are handed to the Google pipeline (renamed into STAGE, where takeout-extract extracts them oldest export first into the same tree
as the Drive deliveries); its other files go to google-takeout/loose.
A JSON file that is a download manifest (a list of one-time links, as claude.ai exports) is not stored: its files are downloaded into the platform folder (see manifest.py)
and extracted by a later run.
Env: INBOX, EXPORTS_ROOT, DONE_ROOT, STAGE, MANIFEST_HOSTS (claude.ai), SETTLE (600), FILECAT (command, default ~/.local/bin/filecat), NTFY_SEND.
"""
import os, re, shutil, subprocess, sys, time
from . import manifest as mf
from .safe_extract import extract as safe_extract

ARCHIVE_EXT = ('.zip', '.tgz', '.tar.gz', '.tar', '.tar.bz2', '.tbz2', '.tar.xz', '.txz', '.7z', '.rar')
TEMP_EXT = ('.part', '.partial', '.tmp', '.crdownload', '.download')

def platform_name(d): return re.sub(r'[^a-z0-9._-]+', '-', d.lower()).strip('-')

def is_archive(name): return name.lower().endswith(ARCHIVE_EXT)

def settled(path, settle, recheck=2.0, now=None):
    """True when the entry is old enough and its size did not change over `recheck` seconds."""
    now = time.time() if now is None else now
    try:
        st = os.stat(path)
        if now - st.st_mtime < settle or now - st.st_ctime < settle: return False
        if recheck:
            time.sleep(recheck); st2 = os.stat(path)
            if st2.st_size != st.st_size or st2.st_mtime != st.st_mtime: return False
        return True
    except OSError: return False

def unique(dest_dir, name):
    p = os.path.join(dest_dir, name); n = 1
    while os.path.exists(p): p = os.path.join(dest_dir, f'{name}.{n}'); n += 1
    return p

def move(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True); shutil.move(src, dst)

def run_cmd(cmd, **kw):
    return subprocess.run(cmd if isinstance(cmd, list) else cmd.split(), capture_output=True, text=True, **kw)

def ensure_collection(filecat, platform, exports, done_dir):
    """Create the filecat collection on first use; a failing filecat never blocks the extraction (the archive is in done_dir for `filecat run` to pick up)."""
    root = os.path.join(exports, platform)
    r = run_cmd(f'{filecat} collection list')
    if r.returncode == 0 and re.search(rf'^{re.escape(platform)} \[', r.stdout, re.M): return True
    r = run_cmd(f'{filecat} collection add {platform} --profile export --root tree={root}/tree --root loose={root}/loose,autoscan --set strip=0 --set done_dir={done_dir}')
    return r.returncode == 0

def process_archive(path, platform, exports, done_root, filecat, notify, log):
    tree = os.path.join(exports, platform, 'tree'); os.makedirs(tree, exist_ok=True)
    r = run_cmd(['bsdtar', '-xf', path, '-C', tree, '--no-same-owner', '--no-same-permissions'])
    if r.returncode != 0:
        log(f'  bsdtar failed ({(r.stderr or "").strip().splitlines()[-1][:100] if r.stderr else r.returncode}); retrying with the safe extractor')
        failed = safe_extract(path, tree, os.path.join(exports, platform, 'renamed.tsv'), strip=0, nested=())
        if failed: log(f'  FAILED: {failed} entries could not be extracted'); return False
    done_dir = os.path.join(done_root, platform)
    dest = unique(done_dir, os.path.basename(path)); move(path, dest)
    if ensure_collection(filecat, platform, exports, done_dir):
        r = run_cmd(f'{filecat} ingest-archive {platform} {dest} --root tree --strip 0')
        if r.returncode != 0: log(f'  catalogue: could not record ({(r.stderr or r.stdout).strip()[-100:]}); filecat run will retry from {done_dir}')
    else: log('  catalogue: filecat unavailable; filecat run will pick the archive up from done/')
    notify(f'Takeout inbox: {platform}', f'{os.path.basename(path)} extracted into {tree}')
    return True

def process_manifest(path, d, platform, pdir, exports, notify, log, fetch=None, hosts=None, allow_http=False):
    """Download every file of a manifest into the platform's inbox folder (they are ingested on a later run), then file the manifest.
    A hidden state file next to the manifest remembers what was fetched (links are one-use) and how often each file failed; a file that failed 3 times is given up."""
    state = os.path.join(pdir, '.' + os.path.basename(path) + '.state'); exhausted = []
    got, bad = mf.download_all(d, pdir, hosts or mf.DEFAULT_HOSTS, fetch or mf.http_fetch, allow_http, log, state_path=state, exhausted=exhausted)
    retry = [b for b in bad if b not in exhausted]
    if retry: log(f'  {len(retry)} of {len(d["data_files"])} files not fetched; the manifest stays in the inbox and only those are retried'); return False
    move(path, unique(os.path.join(exports, platform, 'loose', 'manifests'), os.path.basename(path)))
    if os.path.exists(state): os.remove(state)
    if exhausted:
        notify(f'Takeout inbox: {platform} export INCOMPLETE', f'{len(exhausted)} of {len(d["data_files"])} files could not be downloaded (links used or expired): {", ".join(exhausted)}. Request a new export.')
        log(f'  gave up on {len(exhausted)} file(s); request a new export'); return False
    notify(f'Takeout inbox: {platform}', f'downloaded {len(got)} files from {os.path.basename(path)}; they are extracted on the next run')
    return True

def run(inbox, exports, done_root, settle=600, recheck=2.0, filecat='filecat', notify=lambda *a: None, log=print, now=None, stage=None, fetch=None, hosts=None, allow_http=False):
    """Returns (processed, failed). Platform directories and the files in them are handled in a stable order, archives oldest first."""
    processed = failed = 0
    if not os.path.isdir(inbox): log(f'inbox {inbox} does not exist'); return 0, 0
    for d in sorted(e for e in os.listdir(inbox) if not e.startswith('.') and os.path.isdir(os.path.join(inbox, e))):
        platform = platform_name(d)
        if not platform: continue
        pdir = os.path.join(inbox, d)
        entries = [e for e in os.listdir(pdir) if not e.startswith('.') and not e.lower().endswith(TEMP_EXT) and not e.lower().startswith('readme')]
        entries.sort(key=lambda e: (os.stat(os.path.join(pdir, e)).st_mtime, e))
        for e in entries:
            path = os.path.join(pdir, e)
            if not settled(path, settle, recheck, now): log(f'{platform}/{e}: still arriving, skipped'); continue
            log(f'{platform}/{e}')
            try:
                if platform == 'google' and os.path.isfile(path) and is_archive(e) and stage:
                    move(path, unique(stage, e)); log(f'  handed to the Google pipeline: {stage}'); ok = True
                elif os.path.isfile(path) and e.lower().endswith('.json') and mf.load_manifest(path): ok = process_manifest(path, mf.load_manifest(path), platform, pdir, exports, notify, log, fetch, hosts, allow_http)
                elif os.path.isfile(path) and is_archive(e): ok = process_archive(path, platform, exports, done_root, filecat, notify, log)
                else:                                          # not an archive (csv, json, mbox, a dropped folder): keep it as it came
                    loose = os.path.join(exports, 'google-takeout' if platform == 'google' else platform, 'loose'); move(path, unique(loose, e)); ok = True
            except Exception as ex:
                log(f'  FAILED: {ex}'); ok = False
            processed += ok; failed += not ok
    return processed, failed

def main(argv=None):
    import fcntl
    lock = open(os.path.join(os.environ.get('XDG_RUNTIME_DIR', '/tmp'), 'strata-takeout-inbox.lock'), 'w')            # one run at a time: two runs would both use the same one-time links
    try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError: print('inbox: another run is in progress'); sys.exit(0)
    home = os.path.expanduser('~')
    exports = os.environ.get('EXPORTS_ROOT', '/srv/exports')
    if 'EXPORTS_ROOT' not in os.environ and not os.path.ismount(exports): sys.exit(f'{exports} is not mounted')
    notify_cmd = os.environ.get('NTFY_SEND', os.path.join(home, '.local/bin/ntfy-send'))
    def notify(title, msg):
        if 'EXPORTS_ROOT' in os.environ and 'NTFY_SEND' not in os.environ: return
        try: subprocess.run([notify_cmd, title, msg, 'low', 'inbox_tray'], timeout=120, capture_output=True)
        except Exception: pass
    p, f = run(os.path.expanduser(os.environ.get('INBOX', '~/dropbox/takeouts')), exports, os.path.expanduser(os.environ.get('DONE_ROOT', '~/.cache/takeout-done')),
               int(os.environ.get('SETTLE', '600')), float(os.environ.get('RECHECK', '2')), os.environ.get('FILECAT', os.path.join(home, '.local/bin/filecat')), notify,
               stage=os.path.expanduser(os.environ.get('STAGE', '~/.cache/takeout-stage')), hosts=tuple(h for h in os.environ.get('MANIFEST_HOSTS', 'claude.ai').split(',') if h))
    print(f'inbox: {p} processed, {f} failed')
    sys.exit(1 if f else 0)

if __name__ == '__main__':
    main()
