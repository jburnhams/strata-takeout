import os

from strata_takeout.profile import Takeout


def test_map_and_meta():
    p = Takeout({}, "/srv/exports/gt")
    assert p.meta("takeout-20260209T204735Z-12-078.zip") == "20260209T204735Z" and p.meta("other.zip") is None
    assert p.map("Takeout/Drive/a.txt") == ("ok", "Drive/a.txt", None) or p.map("Takeout/Drive/a.txt")[:2] == (
        "ok",
        "Drive/a.txt",
    )
    assert p.map("Takeout/Drive/Takeout/x.zip") == ("skip", "Takeout/Drive/Takeout/x.zip", None)
    kind, rel, orig = p.map("Takeout/Drive/" + "q" * 300)
    assert kind == "ok" and "~" in rel and orig.endswith("q" * 300)
    assert p.map("Takeout/" + "/".join(["d"] * 3000))[0] == "error"


def test_pending_archives_in_extraction_order(tmp_path):
    done, mk = tmp_path / "done", tmp_path / "mk"
    done.mkdir()
    mk.mkdir()
    for i, n in enumerate(["b.zip", "a.zip", "c.tgz"]):
        (done / n).write_bytes(b"x")
        (mk / (n + ".done")).write_text("")
        os.utime(mk / (n + ".done"), (1000 + i, 1000 + i))
    (done / ".hidden.zip").write_bytes(b"x")
    (done / "notes.txt").write_bytes(b"x")
    assert [os.path.basename(p) for p in Takeout.pending_archives({"done_dir": str(done), "marker_dir": str(mk)})] == [
        "b.zip",
        "a.zip",
        "c.tgz",
    ]
    assert Takeout.pending_archives({}) == [] and Takeout.pending_archives({"done_dir": str(tmp_path / "nope")}) == []


def test_export_profile_keeps_the_top_folder_and_skips_nothing_by_default():
    from strata_takeout.profile import Export

    p = Export({}, "/srv/exports/meta/tree")
    assert (
        p.map("facebook-jon/posts/a.json") == ("ok", "facebook-jon/posts/a.json", None)
        and p.map("x/Drive/Takeout/y.zip")[0] == "ok"
    )
    q = Export({"strip": "1", "nested": "Backups/old"}, "/d")
    assert q.map("top/Backups/old/x")[0] == "skip" and q.map("top/a")[1] == "a"


def test_export_profile_pending_archives_come_from_done_dir_by_mtime(tmp_path):
    from strata_takeout.profile import Export

    for i, n in enumerate(["b.zip", "a.zip"]):
        (tmp_path / n).write_bytes(b"x")
        os.utime(tmp_path / n, (1000 + i, 1000 + i))
    assert [os.path.basename(x) for x in Export.pending_archives({"done_dir": str(tmp_path)})] == ["b.zip", "a.zip"]


def test_paths_moved_aside_by_the_extractor_are_recorded_under_their_real_name(tmp_path):
    from strata_takeout.profile import Takeout

    tsv = tmp_path / "renamed.tsv"
    tsv.write_text("a.zip\tDrive/clip.MP4\tDrive/clip.MP4~file\nother.zip\tDrive/x\tDrive/x~file\n")
    p = Takeout({"renamed_tsv": str(tsv)}, "/d")
    p.meta("a.zip")
    assert p.map("Takeout/Drive/clip.MP4") == ("ok", "Drive/clip.MP4~file", "Drive/clip.MP4")  # the file moved aside
    assert (
        p.map("Takeout/Drive/clip.MP4/inner.mp4")[1] == "Drive/clip.MP4/inner.mp4"
        and p.map("Takeout/Drive/x")[1] == "Drive/x"
    )  # other archives' renames do not apply
    q = Takeout({"renamed_tsv": str(tmp_path / "missing.tsv")}, "/d")
    q.meta("a.zip")
    assert q.map("Takeout/Drive/clip.MP4")[1] == "Drive/clip.MP4"
