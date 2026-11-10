import os
import zipfile

from strata_takeout.safe_extract import extract, main


def mk(path, files):
    with zipfile.ZipFile(path, "w") as z:
        for k, v in files.items():
            z.writestr(k, v)
    return str(path)


def files_under(d):
    return sorted(os.path.relpath(os.path.join(dp, f), d) for dp, _, fs in os.walk(d) for f in fs)


def test_extract_strips_renames_skips_and_logs(tmp_path, capsys):
    z = mk(
        tmp_path / "a.zip",
        {
            "Takeout/Drive/ok.txt": "ok",
            "Takeout/Drive/" + "q" * 300 + ".txt": "long",
            "Takeout/Drive/Takeout/nested.zip": "n",
            "Takeout/Drive/Takeout2/y.txt": "y",
        },
    )
    dest, mp = tmp_path / "gt", tmp_path / "renamed.tsv"
    assert extract(z, str(dest), str(mp)) == 0
    names = files_under(dest)
    assert "Drive/ok.txt" in names and "Drive/Takeout2/y.txt" in names and not any("nested" in n for n in names)
    long = [n for n in names if n.startswith("Drive/q")]
    assert len(long) == 1 and len(os.path.basename(long[0]).encode()) <= 200
    rows = mp.read_text().splitlines()
    assert len(rows) == 1
    archive, orig, stored = rows[0].split("\t")
    assert archive == "a.zip" and orig == "Drive/" + "q" * 300 + ".txt" and stored == long[0]
    assert "1 renamed, 0 failed" in capsys.readouterr().err


def test_later_archive_overwrites_earlier_and_rerun_is_stable(tmp_path):
    old = mk(tmp_path / "old.zip", {"Takeout/Photos/d.json": "v1", "Takeout/Photos/" + "z" * 300: "old"})
    new = mk(tmp_path / "new.zip", {"Takeout/Photos/d.json": "v2-newer", "Takeout/Photos/" + "z" * 300: "new"})
    dest = tmp_path / "gt"
    extract(old, str(dest))
    extract(new, str(dest))
    before = files_under(dest)
    extract(new, str(dest))
    assert (
        (dest / "Photos/d.json").read_text() == "v2-newer" and files_under(dest) == before and len(before) == 2
    )  # the long name maps to the same file every time
    assert [p for p in (dest / "Photos").iterdir() if p.name.startswith("z")][0].read_text() == "new"


def test_sets_mtime_from_the_zip(tmp_path):
    p = tmp_path / "t.zip"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr(zipfile.ZipInfo("Takeout/a.txt", (2020, 5, 17, 10, 30, 0)), "x")
    extract(str(p), str(tmp_path / "o"))
    assert (
        abs((tmp_path / "o/a.txt").stat().st_mtime - __import__("time").mktime((2020, 5, 17, 10, 30, 0, 0, 0, -1))) < 2
    )


def test_tar_gz(tmp_path):
    import io
    import tarfile

    p = tmp_path / "t.tgz"
    with tarfile.open(p, "w:gz") as t:
        info = tarfile.TarInfo("Takeout/Docs/a.txt")
        info.size = 2
        t.addfile(info, io.BytesIO(b"hi"))
    assert extract(str(p), str(tmp_path / "o")) == 0 and (tmp_path / "o/Docs/a.txt").read_text() == "hi"


def test_main_usage_and_exit_code(tmp_path, capsys):
    import pytest

    with pytest.raises(SystemExit):
        main([])
    z = mk(tmp_path / "a.zip", {"Takeout/a.txt": "x"})
    with pytest.raises(SystemExit) as e:
        main([z, str(tmp_path / "o")])
    assert e.value.code == 0


def test_cli_strip_and_nested_options(tmp_path):
    import pytest

    z = mk(tmp_path / "a.zip", {"top/Drive/Takeout/n.zip": "n", "top/keep.txt": "k"})
    with pytest.raises(SystemExit) as e:
        main([z, str(tmp_path / "o"), "--strip", "0", "--nested", ""])
    assert e.value.code == 0 and files_under(tmp_path / "o") == ["top/Drive/Takeout/n.zip", "top/keep.txt"]


def test_a_file_and_a_folder_with_the_same_name_both_survive_whichever_arrives_first(tmp_path):
    name = "Takeout/Drive/clip.MP4"
    folder = mk(tmp_path / "folder.zip", {name + "/inner.mp4": "INNER", name + "/meta.json": "{}"})
    plain = mk(tmp_path / "file.zip", {name: "THE-FILE"})
    # order 1: folder first, then the file
    d1 = tmp_path / "one"
    m1 = tmp_path / "one.tsv"
    assert extract(folder, str(d1)) == 0 and extract(plain, str(d1), str(m1)) == 0
    assert (d1 / "Drive/clip.MP4/inner.mp4").read_text() == "INNER" and (
        d1 / "Drive/clip.MP4~file"
    ).read_text() == "THE-FILE"
    assert m1.read_text().strip().split("\t")[1:] == [
        "Drive/clip.MP4",
        "Drive/clip.MP4~file",
    ]  # the original path -> where it was stored
    # order 2: the file first, then the folder: the file is moved aside, the folder takes the name
    d2 = tmp_path / "two"
    assert extract(plain, str(d2)) == 0 and extract(folder, str(d2)) == 0
    assert (
        (d2 / "Drive/clip.MP4/inner.mp4").read_text() == "INNER"
        and (d2 / "Drive/clip.MP4~file").read_text() == "THE-FILE"
        and (d2 / "Drive/clip.MP4").is_dir()
    )
    # same final state either way, and extracting again changes nothing
    before = files_under(d2)
    extract(plain, str(d2))
    extract(folder, str(d2))
    assert files_under(d2) == before == files_under(d1)


def test_a_file_where_a_deeper_path_needs_a_directory(tmp_path):
    a = mk(tmp_path / "a.zip", {"Takeout/x/y": "FILE-Y"})
    b = mk(tmp_path / "b.zip", {"Takeout/x/y/z.txt": "Z"})
    d = tmp_path / "o"
    assert extract(a, str(d)) == 0 and extract(b, str(d)) == 0
    assert (d / "x/y/z.txt").read_text() == "Z" and (d / "x/y~file").read_text() == "FILE-Y"
