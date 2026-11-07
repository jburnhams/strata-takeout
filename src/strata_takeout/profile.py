"""filecat profiles from this package (entry point group filecat.profiles): they record archives with the same path rules the extractors used.

`takeout`  Google Takeout (strip 1: Takeout/, skip Drive/Takeout/); archives are found in `done_dir` with extraction-order markers in `marker_dir`.
Config `renamed_tsv`: the extractor's rename log (archive, original path, stored path); a path it moved aside (a file sharing a name with a folder becomes `<name>~file`) is recorded
under the name it really has on disk.
`export`   any other platform extracted by strata-takeout-inbox. Config: `strip` (default 0), `nested` (A/B path to skip, default none),
           `done_dir` (finished archives, ingested by `filecat run` if the inbox could not record them itself).
Duck-typed against filecat's profile interface, so this package does not import filecat.
"""
import os, re
from .paths import clean_parts, safe_parts

ARCHIVES = ('.zip', '.tgz', '.tar', '.tar.gz')

class Export:
    STRIP, NESTED = 0, ()
    def __init__(self, cfg, root, strip=None, skip=None):
        self.dest = root or ''
        self.strip = int(cfg.get('strip', self.STRIP) if strip is None else strip)
        self.nested = tuple(p for p in str(cfg.get('nested', '/'.join(self.NESTED))).split('/') if p)
        self.renamed_tsv, self.archive, self._overlay = cfg.get('renamed_tsv'), None, None
    def overlay(self):
        if self._overlay is None:
            self._overlay = {}
            try:
                for line in open(self.renamed_tsv, encoding='utf-8', errors='surrogateescape') if self.renamed_tsv else []:
                    a = line.rstrip('\n').split('\t')
                    if len(a) == 3: self._overlay[(a[0], a[1])] = a[2]
            except OSError: pass
        return self._overlay
    def meta(self, name):
        self.archive = name
        m = re.search(r'\d{8}T\d{6}Z', name); return m.group(0) if m else None             # export timestamp in takeout-20260209T204735Z-...
    def map(self, name):
        orig = '/'.join(clean_parts(name))
        try: parts = safe_parts(name, self.dest, self.nested, self.strip)
        except OSError: return ('error', orig, None)
        if parts is None: return ('skip', orig, None)
        rel = '/'.join(parts); o = '/'.join(orig.split('/')[self.strip:])
        stored = self.overlay().get((self.archive, rel), rel) if self.archive else rel
        return ('ok', stored, o if o != stored else None)
    @classmethod
    def pending_archives(cls, cfg):
        done, mk = cfg.get('done_dir'), cfg.get('marker_dir', '')
        if not done or not os.path.isdir(done): return []
        out = []
        for f in os.listdir(done):
            if f.startswith('.') or not f.endswith(ARCHIVES): continue
            m = os.path.join(mk, f + '.done'); out.append((os.stat(m).st_mtime if mk and os.path.exists(m) else os.stat(os.path.join(done, f)).st_mtime, os.path.join(done, f)))
        return [p for _, p in sorted(out)]

class Takeout(Export):
    STRIP, NESTED = 1, ('Drive', 'Takeout')
