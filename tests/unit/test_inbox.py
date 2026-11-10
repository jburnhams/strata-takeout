import os
import shutil
import time
import zipfile

import pytest

from strata_takeout import inbox

pytestmark = pytest.mark.skipif(not shutil.which("bsdtar"), reason="needs bsdtar")


def mkzip(path, files, mtime=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        for k, v in files.items():
            z.writestr(k, v)
    if mtime:
        os.utime(path, (mtime, mtime))
    return path


@pytest.fixture
def w(tmp_path):
    class W:
        inbox, exports, done = tmp_path / "inbox", tmp_path / "ex", tmp_path / "done"
        calls, notes, logs = [], [], []

        def run(self, **kw):
            return inbox.run(
                str(self.inbox),
                str(self.exports),
                str(self.done),
                settle=0,
                recheck=0,
                filecat="fc",
                notify=lambda *a: self.notes.append(a),
                log=self.logs.append,
                **kw,
            )

    return W()


@pytest.fixture(autouse=True)
def fake_filecat(monkeypatch):
    calls = []

    def fake(cmd, **kw):
        calls.append(cmd if isinstance(cmd, str) else " ".join(cmd))

        class R:
            returncode = 0
            stdout = ""
            stderr = ""

        return (
            R()
            if (isinstance(cmd, str) and cmd.startswith("fc"))
            else inbox.subprocess.run(
                cmd if isinstance(cmd, list) else cmd.split(), capture_output=True, text=True, **kw
            )
        )

    monkeypatch.setattr(inbox, "run_cmd", fake)
    return calls


def test_archive_is_extracted_catalogued_and_filed(w, fake_filecat):
    mkzip(
        w.inbox / "Meta" / "facebook-export.zip",
        {"facebook-jon/posts/a.json": "{}", "facebook-jon/photos/p.jpg": "img"},
    )
    assert w.run() == (1, 0)
    assert (
        w.exports / "meta/tree/facebook-jon/posts/a.json"
    ).read_text() == "{}"  # strip 0: the platform's own top folder is kept
    assert (w.done / "meta/facebook-export.zip").exists() and not (w.inbox / "Meta/facebook-export.zip").exists()
    assert any(c.startswith("fc collection add meta --profile export") for c in fake_filecat) and any(
        c.startswith("fc ingest-archive meta ") for c in fake_filecat
    )
    assert w.notes and "meta" in w.notes[0][0]


def test_oldest_archive_first_so_newer_overwrites(w):
    now = time.time()
    mkzip(w.inbox / "x" / "new.zip", {"d/a.txt": "NEW"}, now - 100)
    mkzip(w.inbox / "x" / "old.zip", {"d/a.txt": "OLD"}, now - 9999)
    w.run()
    assert (w.exports / "x/tree/d/a.txt").read_text() == "NEW"


def test_non_archives_and_folders_go_to_loose_unchanged(w):
    (w.inbox / "reddit").mkdir(parents=True)
    (w.inbox / "reddit/posts.csv").write_text("a,b")
    (w.inbox / "reddit/export").mkdir()
    (w.inbox / "reddit/export/x.txt").write_text("x")
    assert w.run() == (2, 0)
    assert (w.exports / "reddit/loose/posts.csv").read_text() == "a,b" and (
        w.exports / "reddit/loose/export/x.txt"
    ).exists()


def test_files_still_arriving_and_junk_are_left_alone(w):
    d = w.inbox / "x"
    mkzip(d / "ok.zip", {"a": "1"})
    (d / "half.zip.part").write_bytes(b"x")
    (d / ".hidden.zip").write_bytes(b"x")
    (d / "README.txt").write_text("hi")
    fresh = mkzip(d / "fresh.zip", {"b": "2"})
    assert (
        inbox.run(str(w.inbox), str(w.exports), str(w.done), settle=3600, recheck=0, filecat="fc", log=w.logs.append)
        == (0, 0)
        and fresh.exists()
    )  # everything is younger than the settle time
    assert any("still arriving" in l for l in w.logs)
    assert w.run() == (2, 0)  # ok.zip and fresh.zip (settle 0) are taken
    assert (d / "half.zip.part").exists() and (d / ".hidden.zip").exists() and (d / "README.txt").exists()


def test_size_change_during_recheck_means_still_arriving(tmp_path):
    p = tmp_path / "f.zip"
    p.write_bytes(b"x")
    os.utime(p, (1, 1))
    assert inbox.settled(str(p), 0, recheck=0, now=time.time()) is True
    assert inbox.settled(str(p), 10**12, recheck=0, now=time.time()) is False
    import threading

    t = threading.Timer(0.3, lambda: p.write_bytes(b"xxxxxxxx"))
    t.start()
    assert inbox.settled(str(p), 0, recheck=0.8, now=time.time() + 10) is False
    t.join()


def test_long_names_fall_back_to_the_safe_extractor_and_are_logged(w):
    mkzip(w.inbox / "p" / "a.zip", {"top/ok.txt": "ok", "top/" + "q" * 300 + ".txt": "long"})
    assert w.run() == (1, 0)
    names = os.listdir(w.exports / "p/tree/top")
    assert "ok.txt" in names and any("~" in n for n in names)
    assert (w.exports / "p/renamed.tsv").read_text().count("\n") == 1


def test_a_corrupt_archive_stays_in_the_inbox_and_fails_the_run(w):
    bad = w.inbox / "p" / "bad.zip"
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b"not a zip")
    mkzip(w.inbox / "p" / "good.zip", {"a.txt": "1"}, time.time() - 10)
    os.utime(bad, (time.time() - 1000,) * 2)
    assert (
        w.run() == (1, 1) and bad.exists() and (w.exports / "p/tree/a.txt").exists()
    )  # the bad one does not stop the good one


def test_filecat_failure_does_not_block_extraction(w, monkeypatch):
    def fake(cmd, **kw):
        if isinstance(cmd, str) and cmd.startswith("fc"):

            class R:
                returncode = 1
                stdout = ""
                stderr = "boom"

            return R()
        return inbox.subprocess.run(cmd, capture_output=True, text=True, **kw)

    monkeypatch.setattr(inbox, "run_cmd", fake)
    mkzip(w.inbox / "p" / "a.zip", {"a.txt": "1"})
    assert w.run() == (1, 0) and (w.exports / "p/tree/a.txt").exists() and (w.done / "p/a.zip").exists()


def test_platform_names_are_normalised_and_name_collisions_kept(w):
    assert inbox.platform_name("Meta (Facebook)") == "meta-facebook"
    mkzip(w.inbox / "x" / "a.zip", {"a": "1"})
    w.run()
    mkzip(w.inbox / "x" / "a.zip", {"a": "2"})
    w.run()
    assert sorted(os.listdir(w.done / "x")) == [
        "a.zip",
        "a.zip.1",
    ]  # a second archive with the same name never overwrites the first


def test_google_archives_are_handed_to_the_google_stage_not_extracted_here(w, tmp_path):
    stage = tmp_path / "stage"
    mkzip(w.inbox / "google" / "takeout-20260209T000000Z-001.zip", {"Takeout/Drive/a.txt": "x"})
    (w.inbox / "google" / "extra.json").write_text("{}")
    assert inbox.run(
        str(w.inbox),
        str(w.exports),
        str(w.done),
        settle=0,
        recheck=0,
        filecat="fc",
        log=w.logs.append,
        stage=str(stage),
    ) == (2, 0)
    assert (stage / "takeout-20260209T000000Z-001.zip").exists() and not (
        w.exports / "google"
    ).exists()  # takeout-extract does the rest
    assert (w.exports / "google-takeout/loose/extra.json").exists()


def test_a_second_run_while_one_is_in_progress_exits_quietly(tmp_path, monkeypatch):
    import fcntl

    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("EXPORTS_ROOT", str(tmp_path / "ex"))
    monkeypatch.setenv("INBOX", str(tmp_path / "in"))
    held = open(tmp_path / "strata-takeout-inbox.lock", "w")
    fcntl.flock(held, fcntl.LOCK_EX)
    with pytest.raises(SystemExit) as e:
        inbox.main([])
    assert e.value.code == 0
