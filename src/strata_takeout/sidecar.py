"""Google Photos sidecars: `<media file>.supplemental-metadata.json` next to each photo or video in a Takeout.

Google's naming quirks, all seen in real exports: the sidecar name is cut when it gets long (`.supplemental-met.json`:
the part before `.json` is at most 46 characters), a second file with the same name is `name(1).jpg` while its
sidecar is `name.jpg.supplemental-metadata(1).json`, an edited copy `name-edited.jpg` has no sidecar of its own (it
uses the original's), and older exports used `<media file>.json`. `candidates` lists the sidecar names to try, most
likely first; `people` reads the names of the people Google lists in one.
"""

import json
import os
import re

SUFFIX = ".supplemental-metadata"
CUT = 46  # longest sidecar name before the final ".json" that Google writes
MEDIA = (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".webp", ".heic", ".heif", ".avif")


def candidates(name):
    """Sidecar file names that may belong to the media file `name`, most likely first."""
    base, ext = os.path.splitext(name)
    variants = [(base, ext, "")]  # (stem, ext, duplicate suffix such as "(1)")
    m = re.fullmatch(r"(.*)(\(\d+\))", base)
    if m:
        variants.append((m.group(1), ext, m.group(2)))
    for stem, e, dup in list(variants):
        m = re.fullmatch(r"(.*)-edit(?:ed?)?", stem)
        if m:
            variants.append((m.group(1), e, dup))
    out = []
    for stem, e, dup in variants:
        media = stem + e
        full = media + SUFFIX
        for cut in dict.fromkeys((full, full[:CUT])):
            out += [cut + dup + ".json", cut[:CUT] + dup + ".json"]
        out += [media + dup + ".json", (media + dup)[:CUT] + ".json"]
    return list(dict.fromkeys(out))


def find(directory, name):
    """Path of the sidecar of `name` in `directory`, or None."""
    try:
        present = set(os.listdir(directory))
    except OSError:
        return None
    for c in candidates(name):
        if c in present:
            return os.path.join(directory, c)
    return None


def people(path):
    """Names of the people listed in a sidecar (empty when it lists none); ValueError on malformed JSON."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("sidecar is not an object")
    return [p["name"] for p in data.get("people") or () if isinstance(p, dict) and p.get("name")]
