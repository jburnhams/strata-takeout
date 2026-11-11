"""Placer: write extracted files below a destination directory without ever failing on a file/folder name clash.

Drive allows a file and a folder with the same name; a filesystem does not. The folder keeps the name (it has children) and the file is stored as
`<name>~file`, whichever of the two arrives first. Every such move is reported through `on_rename(old_relative_path, new_relative_path)`.
"""

import os
import shutil

FILE_SUFFIX = "~file"


class Placer:
    def __init__(self, dest, on_rename=None):
        self.dest = os.path.abspath(dest)
        self.on_rename = on_rename or (lambda old, new: None)
        os.makedirs(self.dest, exist_ok=True)

    def _note(self, old, new):
        self.on_rename(os.path.relpath(old, self.dest), os.path.relpath(new, self.dest))

    def ensure_dir(self, d):
        """mkdir -p, but a file squatting on a directory name is moved aside to `<name>~file`."""
        rel = os.path.relpath(d, self.dest)
        if rel == ".":
            return
        cur = self.dest
        for part in rel.split(os.sep):
            cur = os.path.join(cur, part)
            if os.path.lexists(cur) and not os.path.isdir(cur):
                os.replace(cur, cur + FILE_SUFFIX)
                self._note(cur, cur + FILE_SUFFIX)
            if not os.path.isdir(cur):
                os.mkdir(cur)

    def put(self, path, src, mtime=None):
        """Write the stream `src` to `path` (absolute, below dest); returns the path actually used."""
        self.ensure_dir(os.path.dirname(path))
        if os.path.isdir(path):
            self._note(path, path + FILE_SUFFIX)
            path += FILE_SUFFIX
        with open(path, "wb") as out:
            shutil.copyfileobj(src, out, 1 << 20)
        if mtime:
            os.utime(path, (mtime, mtime))
        return path
