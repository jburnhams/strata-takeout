"""Download manifests: an export that is not one archive but a JSON list of one-time download links (claude.ai's data export is one).

    {"instructions": "...", "created_at": "2026-11-07T10:42:59+00:00", "data_files": [{"export_url": "https://...", "filename": "conversations-000.zip", ...}], ...}

`strata-takeout-inbox` recognises such a file by its shape, downloads every listed file once into the platform's inbox folder and files the manifest.
Properties that matter, because these links expire after 24 hours and may die as soon as they have been used:
  * exactly one request per file, no probing (no HEAD, no retries of a completed download), one file at a time;
  * written to `<name>.part`, accepted only when the size matches Content-Length (if sent) and the zip verifies (every entry's CRC is read);
    a bad download is deleted and reported, the other files are still fetched, and the manifest stays in the inbox so only the missing files are retried;
  * the saved name is `<export timestamp>_<filename>` (basename only), because the next export has the same file names again;
  * only https links on an allow-listed host are followed (MANIFEST_HOSTS, default claude.ai): a manifest dropped on the share cannot make this machine fetch arbitrary URLs.
"""
import html, json, os, re, shutil, time, urllib.parse, urllib.request, zipfile

DEFAULT_HOSTS = ('claude.ai',)
UA = 'strata-takeout/0.1 (+https://github.com/jburnhams/strata-takeout)'

def load_manifest(path):
    """The parsed manifest if `path` looks like one, else None (a normal JSON file that must stay a loose file)."""
    if os.path.getsize(path) > 5_000_000: return None
    try: d = json.load(open(path, encoding='utf-8'))
    except (OSError, ValueError): return None
    files = d.get('data_files') if isinstance(d, dict) else None
    if not isinstance(files, list) or not files: return None
    if not all(isinstance(f, dict) and isinstance(f.get('export_url'), str) and isinstance(f.get('filename'), str) for f in files): return None
    return d

def stamp(manifest):
    m = re.search(r'(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)', str(manifest.get('created_at', '')))
    return ''.join(m.groups()[:3]) + 'T' + ''.join(m.groups()[3:]) + 'Z' if m else time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())

def allowed(url, hosts, allow_http=False):
    u = urllib.parse.urlparse(url)
    return (u.scheme == 'https' or (allow_http and u.scheme == 'http')) and (u.hostname or '') in hosts

def verify_zip(path):
    try:
        with zipfile.ZipFile(path) as z: return z.testzip() is None and len(z.infolist()) > 0
    except (zipfile.BadZipFile, OSError): return False

class NeedsBrowser(IOError):
    """The link answers with a web page (HTML), not a file: it only works inside a logged-in browser session (claude.ai/export/<id>/download/<token>)."""

def http_fetch(url, dest, timeout=120):
    """One GET, streamed to `dest`; returns (bytes written, Content-Length or None)."""
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        if 'text/html' in (r.headers.get('Content-Type') or '').lower(): raise NeedsBrowser('the link serves a web page, not a file: open it in a logged-in browser')
        length = r.headers.get('Content-Length')
        with open(dest, 'wb') as out: shutil.copyfileobj(r, out, 1 << 20)
    return os.path.getsize(dest), int(length) if length and length.isdigit() else None

def load_state(path):
    try: d = json.load(open(path)); return set(d.get('fetched', [])), dict(d.get('attempts', {}))
    except (OSError, ValueError, TypeError, AttributeError): return set(), {}

def save_state(path, fetched, attempts):
    tmp = path + '.tmp'; json.dump({'fetched': sorted(fetched), 'attempts': attempts}, open(tmp, 'w')); os.replace(tmp, path)

def download_all(manifest, dest_dir, hosts=DEFAULT_HOSTS, fetch=http_fetch, allow_http=False, log=print, state_path=None, max_attempts=3, exhausted=None):
    """-> (fetched names, failed names). A state file (`state_path`) remembers which files were fetched, so a retry never re-requests a link that was
    already used even though the file has since been extracted and filed away, and counts failed attempts: a file that failed `max_attempts` times is added
    to the `exhausted` list (if given) and not requested again. Files already present under their final name are skipped too."""
    os.makedirs(dest_dir, exist_ok=True); st = stamp(manifest); done, failed = [], []
    fetched, attempts = load_state(state_path) if state_path else (set(), {})
    for f in manifest['data_files']:
        name = f"{st}_{os.path.basename(f['filename'])}"; final = os.path.join(dest_dir, name); part = final + '.part'
        if name in fetched or os.path.exists(final): log(f'  {name}: already fetched'); done.append(name); continue
        if attempts.get(name, 0) >= max_attempts:
            log(f'  {name}: gave up after {attempts[name]} attempts'); failed.append(name)
            if exhausted is not None: exhausted.append(name)
            continue
        if not allowed(f['export_url'], hosts, allow_http): log(f'  {name}: link not on an allowed host, skipped'); failed.append(name); continue
        try:
            size, length = fetch(f['export_url'], part)
            if length is not None and size != length: raise IOError(f'truncated: got {size} of {length} bytes')
            if name.lower().endswith('.zip') and not verify_zip(part): raise IOError('not a valid zip')
            os.replace(part, final); log(f'  {name}: {size/1e6:.1f} MB ok'); done.append(name); fetched.add(name)
            if state_path: save_state(state_path, fetched, attempts)
        except Exception as e:
            if os.path.exists(part): os.remove(part)
            log(f'  {name}: FAILED ({type(e).__name__}: {str(e)[:100]})'); failed.append(name)
            attempts[name] = max_attempts if isinstance(e, NeedsBrowser) else attempts.get(name, 0) + 1          # a page is not going to turn into a file: do not retry
            if attempts[name] >= max_attempts and exhausted is not None: exhausted.append(name)
            if state_path: save_state(state_path, fetched, attempts)
    return done, failed

def links_page(manifest, names, platform='export'):
    """A small HTML page with a clickable link per file, to open in a browser that is logged in. Contains the one-time links: delete it after use."""
    st = stamp(manifest); rows = []
    for f in manifest['data_files']:
        n = f"{st}_{os.path.basename(f['filename'])}"
        if n in names: rows.append(f'<li><a href="{html.escape(f["export_url"], quote=True)}">{html.escape(os.path.basename(f["filename"]))}</a> <small>({html.escape(str(f.get("category", "")))})</small></li>')
    return ('<!doctype html><meta charset="utf-8"><title>Download the %s export</title><body style="font:16px sans-serif;max-width:40em;margin:2em auto">'
            '<h2>Download the %s export</h2><p>These links only work in a browser where you are <b>logged in</b> to the service (the program that watches this folder cannot log in). '
            'Click each link once, let the file finish downloading, then save/move the files into this folder (<code>takeouts/%s</code>) and they are picked up automatically. '
            'The links expire %s and may stop working once used.</p><ol>%s</ol><p><small>This page holds one-time links: delete it afterwards.</small></p>'
            % (html.escape(platform), html.escape(platform), html.escape(platform), 'about 24 hours after %s' % html.escape(str(manifest.get('created_at', '?'))[:19]), ''.join(rows)))
