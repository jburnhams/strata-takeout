"""The filecat profile `takeout` (entry point group filecat.profiles): records archives with the same path rules the extractor used.

Collection config (JSON, `filecat collection add --set key=value`): `done_dir` (extracted archives are moved here) and `marker_dir`
(`<archive>.done` files whose mtime is the extraction time) let `filecat run` ingest new archives by itself, in extraction order.
Duck-typed against filecat's profile interface, so this package does not import filecat.
"""
import os, re
from .paths import clean_parts, safe_parts

class Takeout:
    def __init__(self, cfg, root, strip=None, skip=None):
        self.dest = root or ''
    def meta(self, name):
        m = re.search(r'\d{8}T\d{6}Z', name); return m.group(0) if m else None             # export timestamp in takeout-20260209T204735Z-...
    def map(self, name):
        orig = '/'.join(clean_parts(name))
        try: parts = safe_parts(name, self.dest)
        except OSError: return ('error', orig, None)
        if parts is None: return ('skip', orig, None)
        rel = '/'.join(parts); o = '/'.join(orig.split('/')[1:])
        return ('ok', rel, o if o != rel else None)
    @classmethod
    def pending_archives(cls, cfg):
        done, mk = cfg.get('done_dir'), cfg.get('marker_dir', '')
        if not done or not os.path.isdir(done): return []
        out = []
        for f in os.listdir(done):
            if f.startswith('.') or not f.endswith(('.zip', '.tgz', '.tar', '.tar.gz')): continue
            m = os.path.join(mk, f + '.done'); out.append((os.stat(m).st_mtime if os.path.exists(m) else os.stat(os.path.join(done, f)).st_mtime, os.path.join(done, f)))
        return [p for _, p in sorted(out)]
