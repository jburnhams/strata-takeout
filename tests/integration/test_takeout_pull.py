"""Runs the real scripts/google/takeout-pull with a stub rclone on PATH (needs bash)."""
import os, shutil, subprocess
import pytest

pytestmark = pytest.mark.skipif(not shutil.which('bash'), reason='needs bash')
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(REPO, 'scripts/google/takeout-pull')

@pytest.fixture
def env(tmp_path):
    bin_ = tmp_path / 'bin'; bin_.mkdir(); log = tmp_path / 'rclone.log'
    (bin_ / 'rclone').write_text(f'#!/bin/bash\necho "$@" >> {log}\ncase "$1" in lsd|lsf|dedupe|move) exit 0;; esac\nexit 0\n'); (bin_ / 'rclone').chmod(0o755)
    (bin_ / 'ntfy').write_text(f'#!/bin/bash\necho "$@" >> {tmp_path}/ntfy.log\n'); (bin_ / 'ntfy').chmod(0o755)
    e = dict(os.environ, PATH=f'{bin_}:{os.environ["PATH"]}', EXPORTS_ROOT=str(tmp_path / 'ex'), STAGE=str(tmp_path / 'stage'), XDG_RUNTIME_DIR=str(tmp_path), XDG_STATE_HOME=str(tmp_path / 'state'), NTFY_SEND=str(bin_ / 'ntfy'), MIN_FREE_GB='0', FREE_GB='3000')
    def run(*args, **kw): return subprocess.run(['bash', SCRIPT, *args], env={**e, **kw}, capture_output=True, text=True, timeout=60)
    return type('E', (), dict(run=staticmethod(run), log=log, ntfy=tmp_path / 'ntfy.log'))

def moves(env): return [l for l in env.log.read_text().splitlines() if l.startswith('move ')] if env.log.exists() else []

def test_archives_pass_moves_zips_to_the_stage_in_name_order_with_verified_delete(env):
    r = env.run(); assert r.returncode == 0, r.stderr
    (m,) = moves(env); assert '--drive-use-trash=false' in m and '--order-by name,ascending' in m and '--max-transfer 100G' in m and '--include *.zip' in m and '/stage' in m

def test_files_pass_excludes_archives_and_goes_to_loose(env):
    env.run('--files'); (m,) = moves(env); assert '--exclude *.zip' in m and '/ex/google-takeout/loose' in m and '--transfers 1' in m

def test_pauses_and_notifies_once_when_the_disk_is_nearly_full(env):
    r = env.run(MIN_FREE_GB='999999999'); assert r.returncode == 0 and 'downloads paused' in r.stdout and moves(env) == []
    assert env.ntfy.read_text().count('Takeout downloads paused') == 1
    env.run(MIN_FREE_GB='999999999'); assert env.ntfy.read_text().count('Takeout downloads paused') == 1           # second run within 6 h: no second push
    env.run('--files', MIN_FREE_GB='999999999'); assert len(moves(env)) == 1                                       # the non-archive pass is not paused

def test_optional_stage_cap_pauses_while_the_stage_is_full_and_is_off_by_default(env, tmp_path):
    (tmp_path / 'stage').mkdir(); big = tmp_path / 'stage' / 'big.zip'
    with open(big, 'wb') as f: f.truncate(3_000_000_000)                                                     # 3 GB sparse file
    r = env.run(STAGE_MAX='2'); assert 'waiting for takeout-extract' in r.stdout and moves(env) == []
    env.run(); assert len(moves(env)) == 1                                                                   # default: no stage cap, only the free-space rule

def test_batch_is_sized_to_the_room_above_the_floor_and_never_crosses_it(env):
    env.run(FREE_GB='3000', MIN_FREE_GB='400'); assert '--max-transfer 100G' in moves(env)[-1]                                 # plenty of room: the normal batch
    env.run(FREE_GB='480', MIN_FREE_GB='400'); assert '--max-transfer 30G' in moves(env)[-1]                                   # 480 - 400 floor - 10 streams x 5 GB = 30 GB of room
    n = len(moves(env)); r = env.run(FREE_GB='455', MIN_FREE_GB='400'); assert 'downloads paused' in r.stdout and len(moves(env)) == n   # room 5 GB < 10: pause
    assert env.ntfy.exists()

def test_dry_run_is_passed_to_rclone(env):
    env.run('--dry-run'); assert all('--dry-run' in m for m in moves(env)) and moves(env)

def test_duplicate_names_are_renamed_apart_before_moving(env):
    env.run(); first = env.log.read_text().splitlines(); idx = [i for i, l in enumerate(first) if l.startswith('dedupe ')]
    assert idx and idx[0] < [i for i, l in enumerate(first) if l.startswith('move ')][0] and '--dedupe-mode rename' in first[idx[0]]
