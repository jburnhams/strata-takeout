"""Path rules for extracted export archives: what a name inside an archive becomes on disk.

Shared by the extractor (safe_extract) and the filecat profile, so the catalogue records exactly the paths that were written.
Limits: 255 bytes per path component and 4096 per path on ext4; Windows/SMB clients stop at 260. Hence the leading `Takeout/` is dropped
(paths stay short) and over-long components are cut and given a hash of the original name (unique, deterministic).
"""
import hashlib, os

NAME_LIMIT, NAME_TARGET, PATH_LIMIT = 255, 200, 4000
NESTED = ('Drive', 'Takeout')           # Google delivers Takeout-to-Drive into Drive/Takeout, so each Drive export re-packs earlier exports: skipped

def blen(s): return len(s.encode('utf-8', 'surrogateescape'))

def cut(s, n): return s.encode('utf-8', 'surrogateescape')[:n].decode('utf-8', 'ignore')

def shorten(comp, target=NAME_TARGET):
    """comp cut to <= target bytes (on a character boundary) with ~<10 hex of sha1(comp)> before the extension."""
    h = hashlib.sha1(comp.encode('utf-8', 'surrogateescape')).hexdigest()[:10]
    stem, ext = os.path.splitext(comp)
    if blen(ext) > 20 or not stem: stem, ext = comp, ''
    return cut(stem, target - len(h) - 1 - blen(ext)) + '~' + h + ext

def clean_parts(name):
    """Components of an archive entry name without empty, '.' and '..' parts (an archive can never write outside its destination)."""
    return [p for p in name.replace('\\', '/').split('/') if p not in ('', '.', '..')]

def safe_parts(name, dest, nested=NESTED, strip=1):
    """Stored path components for an archive entry, below `dest`. The first `strip` components (Takeout/) are dropped.
    Returns None for entries under the nested earlier-exports directory (`nested`, empty = none); raises OSError if the path cannot be made short enough."""
    parts = clean_parts(name)[strip:]
    if nested and tuple(parts[:len(nested)]) == tuple(nested): return None
    parts = [shorten(p) if blen(p) > NAME_LIMIT else p for p in parts]
    while parts and blen(dest) + 1 + sum(blen(p) + 1 for p in parts) > PATH_LIMIT:
        i = max(range(len(parts)), key=lambda k: blen(parts[k]))
        if blen(parts[i]) <= 40: raise OSError('path too long even when shortened')
        parts[i] = shorten(parts[i], 40)
    return parts
