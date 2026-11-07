"""strata-takeout-safe-extract ARCHIVE DEST_DIR [MAP_FILE] [--strip N] [--nested A/B]

Extract a Takeout zip/tar/tgz into DEST_DIR, dropping the leading top-level directory, renaming anything the filesystem would refuse (see
paths.py) and skipping nested earlier exports (Drive/Takeout/), so extraction never fails on names. Every rename is appended to MAP_FILE as
"archive<TAB>original path<TAB>stored path". Exit 1 if any entry still failed. Used by scripts/google/takeout-extract when bsdtar fails
(bsdtar is faster and handles the normal case; it exits 1 on names over 255 bytes). Later archives overwrite earlier ones; links and devices are skipped.
"""
import os, shutil, sys, tarfile, time, zipfile
from .paths import NESTED, clean_parts, safe_parts

def extract(arch, dest, mapf=None, err=None, strip=1, nested=NESTED):
    err = err or sys.stderr; dest = os.path.abspath(dest); renames, failed = [], 0

    def target(name):
        parts = safe_parts(name, dest, nested, strip)
        if parts is None: return None
        orig = clean_parts(name)[strip:]
        if parts != orig: renames.append((os.path.basename(arch), '/'.join(orig), '/'.join(parts)))
        return os.path.join(dest, *parts) if parts else None

    def put(path, src, mtime):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb') as out: shutil.copyfileobj(src, out, 1 << 20)
        if mtime: os.utime(path, (mtime, mtime))

    if zipfile.is_zipfile(arch):
        with zipfile.ZipFile(arch) as z:
            for i in z.infolist():
                try:
                    p = target(i.filename)
                    if p is None: continue
                    if i.is_dir(): os.makedirs(p, exist_ok=True); continue
                    with z.open(i) as src: put(p, src, time.mktime(i.date_time + (0, 0, -1)))
                except Exception as e:
                    failed += 1; print(f'failed: {i.filename[:120]!r}: {e}', file=err)
    else:
        with tarfile.open(arch) as t:
            for m in t:
                try:
                    p = target(m.name)
                    if p is None: continue
                    if m.isdir(): os.makedirs(p, exist_ok=True)
                    elif m.isreg(): put(p, t.extractfile(m), m.mtime)
                except Exception as e:
                    failed += 1; print(f'failed: {m.name[:120]!r}: {e}', file=err)
    if renames and mapf:
        with open(mapf, 'a', encoding='utf-8', errors='surrogateescape') as f:
            for r in renames: f.write('\t'.join(r) + '\n')
    print(f'safe-extract {os.path.basename(arch)}: {len(renames)} renamed, {failed} failed', file=err)
    return failed

def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog='strata-takeout-safe-extract', description=__doc__.split('\n\n')[0])
    ap.add_argument('archive'); ap.add_argument('dest'); ap.add_argument('map', nargs='?')
    ap.add_argument('--strip', type=int, default=1, help='leading path components to drop (default 1: Takeout/)')
    ap.add_argument('--nested', default='/'.join(NESTED), help='path (A/B) whose entries are skipped; empty = skip nothing (default Drive/Takeout)')
    a = ap.parse_args(argv)
    sys.exit(1 if extract(a.archive, a.dest, a.map, strip=a.strip, nested=tuple(p for p in a.nested.split('/') if p)) else 0)

if __name__ == '__main__':
    main()
