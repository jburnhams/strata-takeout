import os, zipfile
from strata_takeout.safe_extract import extract, main

def mk(path, files):
    with zipfile.ZipFile(path, 'w') as z:
        for k, v in files.items(): z.writestr(k, v)
    return str(path)

def files_under(d): return sorted(os.path.relpath(os.path.join(dp, f), d) for dp, _, fs in os.walk(d) for f in fs)

def test_extract_strips_renames_skips_and_logs(tmp_path, capsys):
    z = mk(tmp_path / 'a.zip', {'Takeout/Drive/ok.txt': 'ok', 'Takeout/Drive/' + 'q' * 300 + '.txt': 'long', 'Takeout/Drive/Takeout/nested.zip': 'n', 'Takeout/Drive/Takeout2/y.txt': 'y'})
    dest, mp = tmp_path / 'gt', tmp_path / 'renamed.tsv'
    assert extract(z, str(dest), str(mp)) == 0
    names = files_under(dest)
    assert 'Drive/ok.txt' in names and 'Drive/Takeout2/y.txt' in names and not any('nested' in n for n in names)
    long = [n for n in names if n.startswith('Drive/q')]
    assert len(long) == 1 and len(os.path.basename(long[0]).encode()) <= 200
    rows = mp.read_text().splitlines(); assert len(rows) == 1
    archive, orig, stored = rows[0].split('\t'); assert archive == 'a.zip' and orig == 'Drive/' + 'q' * 300 + '.txt' and stored == long[0]
    assert '1 renamed, 0 failed' in capsys.readouterr().err

def test_later_archive_overwrites_earlier_and_rerun_is_stable(tmp_path):
    old = mk(tmp_path / 'old.zip', {'Takeout/Photos/d.json': 'v1', 'Takeout/Photos/' + 'z' * 300: 'old'})
    new = mk(tmp_path / 'new.zip', {'Takeout/Photos/d.json': 'v2-newer', 'Takeout/Photos/' + 'z' * 300: 'new'})
    dest = tmp_path / 'gt'
    extract(old, str(dest)); extract(new, str(dest)); before = files_under(dest); extract(new, str(dest))
    assert (dest / 'Photos/d.json').read_text() == 'v2-newer' and files_under(dest) == before and len(before) == 2              # the long name maps to the same file every time
    assert [p for p in (dest / 'Photos').iterdir() if p.name.startswith('z')][0].read_text() == 'new'

def test_sets_mtime_from_the_zip(tmp_path):
    p = tmp_path / 't.zip'
    with zipfile.ZipFile(p, 'w') as z: z.writestr(zipfile.ZipInfo('Takeout/a.txt', (2020, 5, 17, 10, 30, 0)), 'x')
    extract(str(p), str(tmp_path / 'o')); assert abs((tmp_path / 'o/a.txt').stat().st_mtime - __import__('time').mktime((2020, 5, 17, 10, 30, 0, 0, 0, -1))) < 2

def test_tar_gz(tmp_path):
    import tarfile, io
    p = tmp_path / 't.tgz'
    with tarfile.open(p, 'w:gz') as t:
        info = tarfile.TarInfo('Takeout/Docs/a.txt'); info.size = 2; t.addfile(info, io.BytesIO(b'hi'))
    assert extract(str(p), str(tmp_path / 'o')) == 0 and (tmp_path / 'o/Docs/a.txt').read_text() == 'hi'

def test_main_usage_and_exit_code(tmp_path, capsys):
    import pytest
    with pytest.raises(SystemExit): main([])
    z = mk(tmp_path / 'a.zip', {'Takeout/a.txt': 'x'})
    with pytest.raises(SystemExit) as e: main([z, str(tmp_path / 'o')])
    assert e.value.code == 0
