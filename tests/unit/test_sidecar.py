import json

import pytest

from strata_takeout import sidecar
from strata_takeout.profile import Takeout


def test_candidates_cover_googles_naming_quirks():
    c = sidecar.candidates("IMG_1.jpg")
    assert c[0] == "IMG_1.jpg.supplemental-metadata.json" and "IMG_1.jpg.json" in c
    # a name long enough to be cut
    long = "img27012024-043138_000801.jpg"
    assert "img27012024-043138_000801.jpg.supplemental-met.json" in sidecar.candidates(long)
    # duplicate: name(1).jpg -> name.jpg.supplemental-metadata(1).json
    assert "IMG_1.jpg.supplemental-metadata(1).json" in sidecar.candidates("IMG_1(1).jpg")
    assert "IMG_1.jpg(1).json" in sidecar.candidates("IMG_1(1).jpg")
    # an edited copy uses the original's sidecar
    assert "IMG_1.jpg.supplemental-metadata.json" in sidecar.candidates("IMG_1-edited.jpg")
    assert "IMG_1.jpg.supplemental-metadata.json" in sidecar.candidates("IMG_1-edite.jpg")  # name cut by Google
    assert len(sidecar.candidates("IMG_1.jpg")) == len(set(sidecar.candidates("IMG_1.jpg")))


def test_people_and_malformed(tmp_path):
    (tmp_path / "a.json").write_text(json.dumps({"people": [{"name": "Ann"}, {"name": ""}, {"x": 1}, {"name": "Bo"}]}))
    assert sidecar.people(tmp_path / "a.json") == ["Ann", "Bo"]
    (tmp_path / "b.json").write_text(json.dumps({"title": "t"}))
    assert sidecar.people(tmp_path / "b.json") == []
    (tmp_path / "c.json").write_text("[1]")
    with pytest.raises(ValueError, match="object"):
        sidecar.people(tmp_path / "c.json")
    (tmp_path / "d.json").write_text("{no")
    with pytest.raises(ValueError):
        sidecar.people(tmp_path / "d.json")


def test_find_returns_none_without_a_sidecar_or_directory(tmp_path):
    (tmp_path / "x.jpg").write_bytes(b"1")
    assert sidecar.find(str(tmp_path), "x.jpg") is None
    assert sidecar.find(str(tmp_path / "missing"), "x.jpg") is None


def test_takeout_profile_labels_files_from_their_sidecars(tmp_path):
    d = tmp_path / "Google Photos" / "Album"
    d.mkdir(parents=True)
    for n in ("a.jpg", "a(1).jpg", "a-edited.jpg", "nope.heic", "doc.txt"):
        (d / n).write_bytes(b"1")
    (d / "a.jpg.supplemental-metadata.json").write_text(json.dumps({"people": [{"name": "Ann"}]}))
    (d / "a.jpg.supplemental-metadata(1).json").write_text(json.dumps({"people": [{"name": "Bo"}]}))
    p = Takeout({}, str(tmp_path))
    rel = "Google Photos/Album/"
    assert p.labels(rel + "a.jpg") == [("person", "Ann")]
    assert p.labels(rel + "a(1).jpg") == [("person", "Bo")]
    assert p.labels(rel + "a-edited.jpg") == [("person", "Ann")]
    assert p.labels(rel + "nope.heic") is None
    assert ".heic" in p.label_exts and ".txt" not in p.label_exts


def test_a_long_named_duplicate_gets_its_own_sidecar_not_the_originals(tmp_path):
    stem = "x" * 50
    for n in (f"{stem}.jpg", f"{stem}(1).jpg"):
        (tmp_path / n).write_bytes(b"1")
    original = sidecar.candidates(f"{stem}.jpg")[0]  # the original's (cut) sidecar name
    dup_name = (f"{stem}.jpg" + sidecar.SUFFIX)[: sidecar.CUT] + "(1).json"
    (tmp_path / original).write_text(json.dumps({"people": [{"name": "Ann"}]}))
    (tmp_path / dup_name).write_text(json.dumps({"people": [{"name": "Bo"}]}))
    assert sidecar.find(str(tmp_path), f"{stem}(1).jpg").endswith(dup_name)
    assert sidecar.find(str(tmp_path), f"{stem}.jpg").endswith(original)
