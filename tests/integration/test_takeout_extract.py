"""Runs the real scripts/google/takeout-extract against fake exports (needs bash and bsdtar)."""
import os, shutil, subprocess, sys, time, zipfile
import pytest

pytestmark = pytest.mark.skipif(not (shutil.which('bsdtar') and shutil.which('bash')), reason='needs bsdtar and bash')
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(REPO, 'scripts/google/takeout-extract')

def mkzip(path, files, mtime=None):
    with zipfile.ZipFile(path, 'w') as z:
        for k, v in files.items(): z.writestr(k, v)
    if mtime: os.utime(path, (mtime, mtime))
    return path

@pytest.fixture
def env(tmp_path):
    ex, stage = tmp_path / 'ex', tmp_path / 'stage'; (ex / 'google-takeout/archives').mkdir(parents=True); stage.mkdir()
    e = dict(os.environ, EXPORTS_ROOT=str(ex), STAGE=str(stage), XDG_RUNTIME_DIR=str(tmp_path), SETTLE='0', TAKEOUT_RUNNING='0',
             SAFE_EXTRACT=f'{sys.executable} -m strata_takeout.safe_extract', FILECAT='true', PYTHONPATH=os.path.join(REPO, 'src'))
    def run(**kw): return subprocess.run(['bash', SCRIPT], env={**e, **kw}, capture_output=True, text=True, timeout=120)
    return type('E', (), dict(ex=ex, stage=stage, gt=ex / 'gt', G=ex / 'google-takeout', run=staticmethod(run)))

def test_oldest_export_first_so_newer_wins_and_archives_move_to_done(env):
    mkzip(env.stage / 'takeout-20261009T000000Z-001.zip', {'Takeout/Photos/a.json': 'NEW'}); mkzip(env.G / 'archives/takeout-20260209T000000Z-001.zip', {'Takeout/Photos/a.json': 'OLD'})
    r = env.run(); assert r.returncode == 0, r.stderr
    assert (env.gt / 'Photos/a.json').read_text() == 'NEW' and sorted(os.listdir(env.G / 'done')) == ['takeout-20260209T000000Z-001.zip', 'takeout-20261009T000000Z-001.zip']
    assert not os.listdir(env.stage) and not os.listdir(env.G / 'archives')

def test_waits_while_downloading_and_never_passes_the_oldest_partial(env):
    mkzip(env.stage / 'takeout-20260209T000000Z-001.zip', {'Takeout/a.txt': 'old'}); mkzip(env.stage / 'takeout-20261009T000000Z-001.zip', {'Takeout/b.txt': 'new'})
    (env.stage / 'takeout-20260409T000000Z-002.zip.abcd.partial').write_bytes(b'x')
    r = env.run(TAKEOUT_RUNNING='1'); assert r.returncode == 0
    assert (env.gt / 'a.txt').exists() and not (env.gt / 'b.txt').exists()                                  # the 0409 export is still arriving: 1009 must wait
    (env.stage / 'takeout-20260409T000000Z-002.zip.abcd.partial').unlink(); env.run(); assert (env.gt / 'b.txt').exists()

def test_no_partial_in_flight_while_running_waits(env):
    mkzip(env.stage / 'takeout-20260209T000000Z-001.zip', {'Takeout/a.txt': 'x'})
    r = env.run(TAKEOUT_RUNNING='1'); assert 'waiting' in r.stdout and not (env.gt / 'a.txt').exists()

def test_skips_nested_drive_takeout_but_keeps_lookalikes(env):
    mkzip(env.stage / 'takeout-20261009T000000Z-001.zip', {'Takeout/Drive/Takeout/nested.zip': 'n', 'Takeout/Drive/Takeout2/ok.txt': 'k', 'Takeout/Drive/docs/Takeout.txt': 'd'})
    env.run(); assert sorted(os.path.relpath(os.path.join(d, f), env.gt) for d, _, fs in os.walk(env.gt) for f in fs) == ['Drive/Takeout2/ok.txt', 'Drive/docs/Takeout.txt']

def test_long_names_fall_back_to_the_safe_extractor_and_are_logged(env):
    mkzip(env.stage / 'takeout-20261009T000000Z-001.zip', {'Takeout/Drive/fine.txt': 'f', 'Takeout/Drive/' + 'y' * 300 + '.txt': 'long'})
    r = env.run(); assert r.returncode == 0, r.stderr
    names = os.listdir(env.gt / 'Drive'); assert 'fine.txt' in names and any('~' in n for n in names)
    assert (env.G / 'renamed.tsv').read_text().count('\n') == 1 and os.listdir(env.G / 'done')

def test_renamed_twins_wait_for_each_other_and_extract_by_mtime(env):
    now = time.time()
    (env.G / 'dupes.tsv').write_text('takeout-20261009T000000Z-3-004.zip\t2\n')
    mkzip(env.stage / 'takeout-20261009T000000Z-3-004-1.zip', {'Takeout/Photos/a.json': 'NEWER'}, now)                 # rclone numbers the NEWER file -1
    r = env.run(); assert 'waiting for the twin' in r.stdout and not (env.gt / 'Photos/a.json').exists()
    mkzip(env.stage / 'takeout-20261009T000000Z-3-004-2.zip', {'Takeout/Photos/a.json': 'OLDER'}, now - 86400)
    env.run(); assert (env.gt / 'Photos/a.json').read_text() == 'NEWER' and len(os.listdir(env.G / 'done')) == 2

def test_loose_files_are_moved_and_partials_left_alone(env):
    (env.stage / 'clip.mp4').write_bytes(b'v'); (env.stage / 'x.zip.partial').write_bytes(b'p'); mkzip(env.stage / 'takeout-20260209T000000Z-001.zip', {'Takeout/a.txt': 'x'})
    env.run(); assert (env.G / 'loose/clip.mp4').exists() and (env.stage / 'x.zip.partial').exists()

def test_a_corrupt_archive_fails_loudly_and_stays_put(env):
    (env.stage / 'takeout-20260209T000000Z-001.zip').write_bytes(b'not a zip at all')
    r = env.run(); assert r.returncode == 1 and 'extract failed' in r.stderr and (env.stage / 'takeout-20260209T000000Z-001.zip').exists()
