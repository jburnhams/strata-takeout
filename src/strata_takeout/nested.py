"""strata-takeout-nested: recover the content that is only inside nested earlier-export zips (Drive/Takeout/*.zip inside a Drive export).

Three stages, so nothing is extracted that a newer version would override or that the main tree already has in an equal or newer version:
  index    read the file list of every nested zip (and zips inside those, to MAX_DEPTH) without extracting: path, export timestamp, size, CRC32.
  plan     per path pick the winner among all nested versions (highest export timestamp, then newest mtime) and compare with the main tree:
             not in the tree                              -> extract   ("new")
             in the tree with an OLDER export, other bytes -> extract   ("newer")
             in the tree with an equal/newer export        -> skip      (the tree already has it)
             same bytes already in the tree (this path or any other) -> skip
           Everything is counted; nothing is written yet, so the plan can be reviewed.
  extract  one forward pass per nested zip, writing only the winners into DEST, a mirror of the main tree (merge with rsync/mv).
The wrapper zips are only read. State lives in an SQLite file next to DEST, so every stage is resumable.
"""

import collections
import multiprocessing
import os
import re
import shutil
import sqlite3
import time
import zipfile

from .paths import safe_parts
from .placer import Placer

SEP = "\x1f"  # separates the entry names of a chain (zip inside zip inside zip)
CHILD_PREFIX = "Drive/Takeout/"
MAX_DEPTH = 4
TS_RE = re.compile(r"\d{8}T\d{6}Z")
SCHEMA = """
CREATE TABLE IF NOT EXISTS units(key TEXT PRIMARY KEY, wrapper TEXT, entry TEXT, ts TEXT, indexed INTEGER DEFAULT 0, extracted INTEGER DEFAULT 0, note TEXT);
CREATE TABLE IF NOT EXISTS entries(id INTEGER PRIMARY KEY, unit TEXT, chain TEXT, name TEXT, ts TEXT, rel TEXT, size INTEGER, crc INTEGER, mtime INTEGER, hdr INTEGER);
CREATE INDEX IF NOT EXISTS entries_rel ON entries(rel);
CREATE INDEX IF NOT EXISTS entries_unit ON entries(unit);
CREATE TABLE IF NOT EXISTS tree(rel TEXT PRIMARY KEY, ts TEXT, size INTEGER, crc INTEGER);
CREATE TABLE IF NOT EXISTS content(size INTEGER, crc INTEGER, PRIMARY KEY(size, crc));
CREATE TABLE IF NOT EXISTS plan(entry_id INTEGER PRIMARY KEY, unit TEXT, chain TEXT, name TEXT, rel TEXT, ts TEXT, size INTEGER, crc INTEGER, why TEXT, done INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS skipped(entry_id INTEGER PRIMARY KEY, why TEXT);
"""


def export_ts(name):
    m = TS_RE.search(name or "")
    return m.group(0) if m else ""


def unit_key(entry_name, size, crc):
    return f"{os.path.basename(entry_name)}|{size}|{crc}"


def connect(path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    c = sqlite3.connect(path, timeout=120)
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(SCHEMA)
    return c


def mtime_of(info):
    try:
        return int(time.mktime(info.date_time + (0, 0, -1)))
    except (OverflowError, ValueError):
        return 0


def rel_of(name, gt_root):
    try:
        parts = safe_parts(name, gt_root, (), 1)
    except OSError:
        return None
    return "/".join(parts) if parts else None


def is_child_zip(rel):
    return rel.startswith(CHILD_PREFIX) and rel.lower().endswith(".zip")


def walk(zf, chain, ts, gt_root, depth, rows, errors):
    """Collect (chain, name, ts, rel, size, crc, mtime, hdr) for every file below `zf`, descending into nested zips."""
    for info in sorted(zf.infolist(), key=lambda i: i.header_offset):
        if info.is_dir():
            continue
        rel = rel_of(info.filename, gt_root)
        if rel is None:
            continue
        if is_child_zip(rel):
            if depth >= MAX_DEPTH:
                errors.append(f"depth limit: {info.filename}")
                continue
            try:
                with zf.open(info) as f, zipfile.ZipFile(f) as cz:
                    walk(cz, chain + [info.filename], export_ts(info.filename) or ts, gt_root, depth + 1, rows, errors)
            except Exception as e:
                errors.append(f"{info.filename}: {type(e).__name__} {str(e)[:60]}")
            continue
        rows.append(
            (SEP.join(chain), info.filename, ts, rel, info.file_size, info.CRC, mtime_of(info), info.header_offset)
        )


def index_unit(job):
    """Worker: list one level-1 nested zip taken from a wrapper zip. -> (key, rows, errors)."""
    key, wrapper, entry, gt_root = job
    rows, errors = [], []
    try:
        with zipfile.ZipFile(wrapper) as wz:
            info = wz.getinfo(entry)
            with wz.open(info) as f, zipfile.ZipFile(f) as nz:
                walk(nz, [], export_ts(entry), gt_root, 1, rows, errors)
    except Exception as e:
        errors.append(f"{type(e).__name__} {str(e)[:80]}")
    return key, rows, errors


def run_index(c, units, gt_root, workers=3, log=print):
    """units: [(key, wrapper_path, entry_name_in_wrapper)]. Already indexed units are skipped."""
    for key, wrapper, entry in units:
        c.execute(
            "INSERT OR IGNORE INTO units(key, wrapper, entry, ts) VALUES(?,?,?,?)",
            (key, wrapper, entry, export_ts(entry)),
        )
    c.commit()
    todo = [
        (k, w, e, gt_root)
        for k, w, e in units
        if not c.execute("SELECT indexed FROM units WHERE key=?", (k,)).fetchone()[0]
    ]
    log(f"index: {len(todo)} of {len(units)} nested zips to read")
    n = 0
    with multiprocessing.Pool(workers) as pool:
        for key, rows, errors in pool.imap_unordered(index_unit, todo):
            c.execute("DELETE FROM entries WHERE unit=?", (key,))
            c.executemany(
                "INSERT INTO entries(unit, chain, name, ts, rel, size, crc, mtime, hdr) VALUES(?,?,?,?,?,?,?,?,?)",
                [(key,) + r for r in rows],
            )
            c.execute("UPDATE units SET indexed=1, note=? WHERE key=?", ("; ".join(errors)[:300] or None, key))
            c.commit()
            n += 1
            if n % 10 == 0 or errors:
                log(
                    f"  [{time.strftime('%H:%M:%S')}] {n}/{len(todo)} read{' (' + errors[0][:70] + ')' if errors else ''}"
                )


def load_tree(c, tree_rows, content_rows):
    """tree_rows: (rel, ts, size, crc) of the file now in the main tree; content_rows: (size, crc) of every file ever extracted into it."""
    c.execute("DELETE FROM tree")
    c.execute("DELETE FROM content")
    c.executemany("INSERT OR REPLACE INTO tree VALUES(?,?,?,?)", tree_rows)
    c.executemany("INSERT OR IGNORE INTO content VALUES(?,?)", content_rows)
    c.commit()


def make_plan(c, skip_same_content=True):
    """Fill `plan`/`skipped` from `entries` and `tree`. Returns {reason: (files, bytes)}."""
    c.execute("DELETE FROM plan")
    c.execute("DELETE FROM skipped")
    # the winner per path among nested versions: newest export, then newest mtime, then last indexed
    c.execute("""CREATE TEMP TABLE IF NOT EXISTS winner AS SELECT 1 AS entry_id WHERE 0""")
    c.execute("DELETE FROM winner")
    c.execute("""INSERT INTO winner SELECT e.id FROM entries e WHERE NOT EXISTS (SELECT 1 FROM entries f WHERE f.rel = e.rel AND
                 (f.ts > e.ts OR (f.ts = e.ts AND (f.mtime > e.mtime OR (f.mtime = e.mtime AND f.id > e.id)))))""")
    c.execute(
        "INSERT INTO skipped SELECT e.id, 'older nested version (a newer nested copy wins)' FROM entries e WHERE e.id NOT IN (SELECT entry_id FROM winner)"
    )
    c.execute("""INSERT INTO skipped SELECT e.id, 'tree already has an equal or newer export of this path' FROM entries e JOIN winner w ON w.entry_id = e.id JOIN tree t ON t.rel = e.rel
                 WHERE t.ts >= e.ts AND NOT (t.size = e.size AND t.crc = e.crc)""")
    c.execute("""INSERT INTO skipped SELECT e.id, 'identical bytes already at this path in the tree' FROM entries e JOIN winner w ON w.entry_id = e.id JOIN tree t ON t.rel = e.rel
                 WHERE t.size = e.size AND t.crc = e.crc""")
    if skip_same_content:
        c.execute("""INSERT OR IGNORE INTO skipped SELECT e.id, 'same bytes already in the tree under another name' FROM entries e JOIN winner w ON w.entry_id = e.id
                     WHERE e.rel NOT IN (SELECT rel FROM tree) AND EXISTS (SELECT 1 FROM content k WHERE k.size = e.size AND k.crc = e.crc)""")
    c.execute("""INSERT INTO plan(entry_id, unit, chain, name, rel, ts, size, crc, why) SELECT e.id, e.unit, e.chain, e.name, e.rel, e.ts, e.size, e.crc,
                 CASE WHEN t.rel IS NULL THEN 'new path' ELSE 'newer version of a path already in the tree' END
                 FROM entries e JOIN winner w ON w.entry_id = e.id LEFT JOIN tree t ON t.rel = e.rel
                 WHERE e.id NOT IN (SELECT entry_id FROM skipped)""")
    c.commit()
    return summary(c)


def summary(c):
    out = collections.OrderedDict()
    for why, n, b in c.execute("SELECT why, COUNT(*), COALESCE(SUM(size),0) FROM plan GROUP BY why ORDER BY 3 DESC"):
        out["EXTRACT: " + why] = (n, b)
    for why, n, b in c.execute(
        "SELECT s.why, COUNT(*), COALESCE(SUM(e.size),0) FROM skipped s JOIN entries e ON e.id = s.entry_id GROUP BY s.why ORDER BY 3 DESC"
    ):
        out["skip: " + why] = (n, b)
    return out


def extract_unit(job):
    """Worker: one forward pass over a level-1 nested zip, writing the planned files. -> (key, written, errors)."""
    key, wrapper, entry, wanted, dest, min_free = job
    written, errors = [], []
    if shutil.disk_usage(dest).free < min_free:
        return key, written, ["stopped: destination low on free space"]
    needed = set()
    for chain, _name in wanted:
        parts = chain.split(SEP) if chain else []
        for i in range(len(parts) + 1):
            needed.add(SEP.join(parts[:i]))
    placer = Placer(dest, lambda old, new: written.append(("RENAME", old, new)))

    def go(zf, chain):
        cs = SEP.join(chain)
        for info in sorted(zf.infolist(), key=lambda i: i.header_offset):
            if info.is_dir():
                continue
            if (cs, info.filename) in wanted:
                rel = wanted[(cs, info.filename)]
                try:
                    with zf.open(info) as src:
                        path = placer.put(os.path.join(dest, rel), src, mtime_of(info))
                    written.append(("OK", rel, os.path.relpath(path, dest), info.file_size))
                except Exception as e:
                    errors.append(f"{rel}: {type(e).__name__} {str(e)[:60]}")
                    try:
                        os.remove(os.path.join(dest, rel))
                    except OSError:
                        pass
            elif SEP.join(chain + [info.filename]) in needed and info.filename.lower().endswith(".zip"):
                try:
                    with zf.open(info) as f, zipfile.ZipFile(f) as cz:
                        go(cz, chain + [info.filename])
                except Exception as e:
                    errors.append(f"{info.filename}: {type(e).__name__} {str(e)[:60]}")

    try:
        with zipfile.ZipFile(wrapper) as wz, wz.open(wz.getinfo(entry)) as f, zipfile.ZipFile(f) as nz:
            go(nz, [])
    except Exception as e:
        errors.append(f"{type(e).__name__} {str(e)[:80]}")
    return key, written, errors


def run_extract(c, dest, workers=3, min_free_gb=300, log=print):
    os.makedirs(dest, exist_ok=True)
    jobs = []
    for key, wrapper, entry in c.execute(
        "SELECT key, wrapper, entry FROM units WHERE extracted=0 AND key IN (SELECT DISTINCT unit FROM plan)"
    ).fetchall():
        wanted = {
            (ch, nm): rel
            for ch, nm, rel in c.execute("SELECT chain, name, rel FROM plan WHERE unit=? AND done=0", (key,))
        }
        if wanted:
            jobs.append((key, wrapper, entry, wanted, dest, min_free_gb * 10**9))
    log(f"extract: {len(jobs)} nested zips with files to write")
    n = bytes_ = 0
    with (
        open(os.path.join(dest, ".extracted.tsv"), "a") as manifest,
        open(os.path.join(dest, "renamed.tsv"), "a") as renamed,
        multiprocessing.Pool(workers) as pool,
    ):
        for key, written, errors in pool.imap_unordered(extract_unit, jobs):
            for w in written:
                if w[0] == "OK":
                    manifest.write(f"{w[1]}\t{w[2]}\t{w[3]}\t{key}\n")
                    bytes_ += w[3]
                else:
                    renamed.write(f"{key}\t{w[1]}\t{w[2]}\n")
            manifest.flush()
            renamed.flush()
            n += 1
            if errors:
                log(f"  {key[:48]}: {len(errors)} problem(s): {errors[0][:90]}")
            if not any(e.startswith("stopped") for e in errors) and not errors:
                c.execute("UPDATE units SET extracted=1 WHERE key=?", (key,))
            elif any(e.startswith("stopped") for e in errors):
                log("  stopping: destination low on free space")
                pool.terminate()
                break
            c.commit()
            if n % 5 == 0:
                log(f"  [{time.strftime('%H:%M:%S')}] {n}/{len(jobs)} zips, {bytes_ / 1e9:.1f} GB written")
    log(f"extract: {n} zips processed, {bytes_ / 1e9:.1f} GB written")


def fmt(n, b):
    return f"{n:>9,} files {b / 1e9:>9,.1f} GB"


def main(argv=None):
    import argparse

    ap = argparse.ArgumentParser(prog="strata-takeout-nested", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    ap.add_argument("--dest", default="/srv/exports/gt-nested")
    ap.add_argument("--index", default=None)
    ap.add_argument("--gt-root", default="/srv/exports/gt")
    ap.add_argument("--collection", default="google-takeout")
    ap.add_argument(
        "--zips",
        nargs="*",
        default=[os.path.expanduser("~/.cache/takeout-done"), os.path.expanduser("~/.cache/takeout-stage")],
    )
    for name in ("index", "extract"):
        p = sub.add_parser(name)
        p.add_argument("--workers", type=int, default=3)
    sub.choices["index"].add_argument("--limit", type=int)
    sub.choices["extract"].add_argument("--min-free-gb", type=int, default=300)
    p = sub.add_parser("plan")
    p.add_argument(
        "--keep-same-content",
        action="store_true",
        help="also extract files whose bytes exist in the tree under another name",
    )
    sub.add_parser("status")
    a = ap.parse_args(argv)
    c = connect(a.index or os.path.join(os.path.dirname(os.path.abspath(a.dest)), ".gt-nested-index.sqlite"))
    if a.cmd == "index":
        from filecat.db import (
            connect as fc_connect,  # the catalogue says which nested zips were skipped, and in which wrapper
        )

        fc = fc_connect()
        cid = fc.execute("select id from collections where name=?", (a.collection,)).fetchall()[0][0]
        src = {i: n for i, n in fc.execute("select id, name from sources where collection_id=?", (cid,)).fetchall()}
        seen = {}
        for path, sid, size, crc in fc.execute(
            "select path, source_id, size, crc32 from files where collection_id=? and state='skipped-nested' order by path, source_id",
            (cid,),
        ).fetchall():
            seen.setdefault(unit_key(path, int(size), int(crc)), (path, sid))
        units = []
        for key, (path, sid) in sorted(seen.items(), key=lambda kv: export_ts(kv[0])):
            w = next((os.path.join(d, src[sid]) for d in a.zips if os.path.exists(os.path.join(d, src[sid]))), None)
            if w:
                units.append((key, w, path if path.startswith("Takeout/") else "Takeout/" + path))
            else:
                print("wrapper not found:", src[sid])
        run_index(c, units[: a.limit] if a.limit else units, a.gt_root, a.workers)
    elif a.cmd == "plan":
        from filecat.db import connect as fc_connect

        fc = fc_connect()
        cid = fc.execute("select id from collections where name=?", (a.collection,)).fetchall()[0][0]
        tree = [
            (p, ts or "", int(s), int(cr))
            for p, ts, s, cr in fc.execute(
                "select path, export_ts, size, crc32 from files where collection_id=? and root='gt' and current=1 and state='ok'",
                (cid,),
            ).fetchall()
        ]
        content = [
            (int(s), int(cr))
            for s, cr in fc.execute(
                "select distinct size, crc32 from files where collection_id=? and root='gt' and state='ok'", (cid,)
            ).fetchall()
        ]
        print(f"main tree: {len(tree):,} current files, {len(content):,} distinct contents")
        load_tree(c, tree, content)
        for k, (n, b) in make_plan(c, not a.keep_same_content).items():
            print(f"  {k:<64}{fmt(n, b)}")
    elif a.cmd == "extract":
        run_extract(c, a.dest, a.workers, a.min_free_gb)
    else:
        print(
            "nested zips:",
            c.execute("select count(*), sum(indexed), sum(extracted) from units").fetchone(),
            "(total, indexed, extracted)",
        )
        print("indexed entries:", c.execute("select count(*) from entries").fetchone()[0])
        [print(f"  {k:<64}{fmt(n, b)}") for k, (n, b) in summary(c).items()]


if __name__ == "__main__":
    main()
