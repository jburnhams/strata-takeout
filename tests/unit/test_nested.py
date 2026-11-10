import io
import os
import zipfile
import zlib

from strata_takeout import nested


def zbytes(files, deflate=True):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w", zipfile.ZIP_DEFLATED if deflate else zipfile.ZIP_STORED) as z:
        for k, v in files.items():
            z.writestr(k, v)
    return b.getvalue()


def crc(b):
    return zlib.crc32(b.encode() if isinstance(b, str) else b) & 0xFFFFFFFF


N1, N2, N3 = (
    "Takeout/Drive/Takeout/takeout-20260209T000000Z-1.zip",
    "Takeout/Drive/Takeout/takeout-20260609T000000Z-2.zip",
    "Takeout/Drive/Takeout/takeout-20260209T000000Z-9.zip",
)


def build(tmp_path):
    deep = zbytes({"Takeout/Deep/d.txt": "DEEP"})
    old = zbytes(
        {
            "Takeout/Photos/a.jpg": "A-v1",
            "Takeout/Photos/b.jpg": "B-old",
            "Takeout/Music/s.mp3": "SONG",
            N3: deep,
            "Takeout/X/clip.MP4": "THE-FILE",
            "Takeout/Same/e.txt": "EQUAL",
        }
    )
    new = zbytes(
        {
            "Takeout/Photos/a.jpg": "A-v2-newer",
            "Takeout/Docs/n.txt": "NOTE-new",
            "Takeout/X/clip.MP4/inner.mp4": "INNER",
        }
    )
    wrapper = tmp_path / "wrapper.zip"
    with zipfile.ZipFile(wrapper, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(N1, old)
        z.writestr(N2, new)
    return str(wrapper)


TREE = [
    (
        "Photos/b.jpg",
        "20261009T000000Z",
        len("B-newer"),
        crc("B-newer"),
    ),  # the tree has a NEWER export of b.jpg -> never extract the old nested one
    (
        "Docs/n.txt",
        "20260409T000000Z",
        len("NOTE-old"),
        crc("NOTE-old"),
    ),  # the tree has an OLDER version -> the nested one is newer, extract it
    ("Same/e.txt", "20260101T000000Z", len("EQUAL"), crc("EQUAL")),  # same bytes -> skip even though the tree is older
    ("Other/s.mp3", "20260101T000000Z", len("SONG"), crc("SONG")),
]  # the song exists under another name -> skip
CONTENT = [(s, c) for _, _, s, c in TREE]


def setup(tmp_path):
    w = build(tmp_path)
    c = nested.connect(str(tmp_path / "idx.sqlite"))
    nested.run_index(c, [("k1", w, N1), ("k2", w, N2)], "/srv/exports/gt", workers=2, log=lambda *_: None)
    nested.load_tree(c, TREE, CONTENT)
    return w, c


def test_index_reads_two_levels_without_extracting(tmp_path):
    w, c = setup(tmp_path)
    rels = {r[0]: (r[1], r[2]) for r in c.execute("SELECT rel, ts, chain FROM entries")}
    assert set(rels) >= {
        "Photos/a.jpg",
        "Photos/b.jpg",
        "Music/s.mp3",
        "Deep/d.txt",
        "Docs/n.txt",
        "X/clip.MP4",
        "X/clip.MP4/inner.mp4",
    }
    assert (
        rels["Deep/d.txt"][1] == N3 and rels["Deep/d.txt"][0] == "20260209T000000Z"
    )  # found one level deeper, with the inner zip's own export timestamp
    assert not any(r.startswith("Drive/Takeout") for r in rels)  # the nested zips themselves are not entries


def test_plan_extracts_only_winners_and_counts_every_skip_reason(tmp_path):
    w, c = setup(tmp_path)
    s = nested.make_plan(c)
    extract = {r[0]: r[1] for r in c.execute("SELECT rel, why FROM plan")}
    assert set(extract) == {"Photos/a.jpg", "Docs/n.txt", "Deep/d.txt", "X/clip.MP4", "X/clip.MP4/inner.mp4"}
    assert extract["Docs/n.txt"].startswith("newer version") and extract["Deep/d.txt"] == "new path"
    assert c.execute("SELECT crc FROM plan WHERE rel='Photos/a.jpg'").fetchone()[0] == crc(
        "A-v2-newer"
    )  # the 0609 version beats the 0209 one: the old one is never extracted
    why = {r[0] for r in c.execute("SELECT why FROM skipped")}
    assert why == {
        "older nested version (a newer nested copy wins)",
        "tree already has an equal or newer export of this path",
        "identical bytes already at this path in the tree",
        "same bytes already in the tree under another name",
    }
    assert (
        sum(n for k, (n, b) in s.items() if k.startswith("EXTRACT")) == 5
        and s["skip: older nested version (a newer nested copy wins)"][0] == 1
    )
    nested.make_plan(c, skip_same_content=False)
    assert "Music/s.mp3" in {r[0] for r in c.execute("SELECT rel FROM plan")}


def test_extract_writes_only_the_plan_keeps_file_and_folder_and_is_resumable(tmp_path):
    w, c = setup(tmp_path)
    nested.make_plan(c)
    dest = str(tmp_path / "gt-nested")
    logs = []
    nested.run_extract(c, dest, workers=2, min_free_gb=0, log=logs.append)
    got = sorted(
        os.path.relpath(os.path.join(d, f), dest)
        for d, _, fs in os.walk(dest)
        for f in fs
        if not f.startswith(".") and f != "renamed.tsv"
    )
    assert got == ["Deep/d.txt", "Docs/n.txt", "Photos/a.jpg", "X/clip.MP4/inner.mp4", "X/clip.MP4~file"]
    assert (
        open(os.path.join(dest, "Photos/a.jpg")).read() == "A-v2-newer"
        and open(os.path.join(dest, "X/clip.MP4~file")).read() == "THE-FILE"
    )
    assert not os.path.exists(os.path.join(dest, "Photos/b.jpg")) and not os.path.exists(
        os.path.join(dest, "Music")
    )  # nothing the tree already has
    assert c.execute("SELECT SUM(extracted) FROM units").fetchone()[0] == 2
    logs.clear()
    nested.run_extract(c, dest, workers=2, min_free_gb=0, log=logs.append)
    assert any("0 nested zips" in l for l in logs)  # a second run has nothing to do


def test_extract_stops_when_the_destination_is_low_on_space_and_keeps_the_unit_for_later(tmp_path):
    w, c = setup(tmp_path)
    nested.make_plan(c)
    dest = str(tmp_path / "out")
    logs = []
    nested.run_extract(
        c, dest, workers=1, min_free_gb=10**9, log=logs.append
    )  # demands an impossible amount of free space
    assert (
        any("low on free space" in l for l in logs) and c.execute("SELECT SUM(extracted) FROM units").fetchone()[0] == 0
    )


def test_indexing_is_resumable_and_a_missing_wrapper_is_recorded_not_fatal(tmp_path):
    w, c = setup(tmp_path)
    n = c.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    nested.run_index(c, [("k1", w, N1), ("k2", w, N2)], "/srv/exports/gt", workers=1, log=lambda *_: None)
    assert c.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == n  # no duplicates on a re-run
    nested.run_index(c, [("k3", str(tmp_path / "missing.zip"), N1)], "/srv/exports/gt", workers=1, log=lambda *_: None)
    assert (
        c.execute("SELECT indexed, note FROM units WHERE key='k3'").fetchone()[0] == 1
        and c.execute("SELECT note FROM units WHERE key='k3'").fetchone()[0]
    )


def test_paths_use_the_same_rules_as_the_main_tree():
    assert nested.rel_of("Takeout/Google Photos/a.jpg", "/srv/exports/gt") == "Google Photos/a.jpg"
    long = nested.rel_of("Takeout/Drive/" + "q" * 300 + ".txt", "/srv/exports/gt")
    assert "~" in long and len(long.split("/")[-1].encode()) <= 200
