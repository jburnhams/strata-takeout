import http.server, json, os, shutil, threading, zipfile
import pytest
from strata_takeout import inbox, manifest as mf

def zbytes(tmp_path, name, files):
    p = tmp_path / name
    with zipfile.ZipFile(p, 'w') as z:
        for k, v in files.items(): z.writestr(k, v)
    return p.read_bytes()

def man(urls, created='2026-11-07T10:42:59.845528+00:00'):
    return {'instructions': 'Download each file using the export_url.', 'created_at': created, 'total_files': len(urls), 'version': '1.0',
            'data_files': [{'batch_index': i, 'export_url': u, 'category': 'c', 'part': 0, 'filename': f'{n}-000.zip'} for i, (n, u) in enumerate(urls)]}

def test_load_manifest_recognises_only_manifests(tmp_path):
    good = tmp_path / 'm.json'; good.write_text(json.dumps(man([('a', 'https://claude.ai/x')])))
    assert mf.load_manifest(str(good))['total_files'] == 1
    for text in ('not json', '[]', '{"data_files": []}', '{"data_files": [{"filename": "a.zip"}]}', '{"other": 1}'):
        p = tmp_path / 'o.json'; p.write_text(text); assert mf.load_manifest(str(p)) is None                  # an ordinary JSON export must stay a loose file

def test_stamp_and_allowed():
    assert mf.stamp({'created_at': '2026-11-07T10:42:59.845528+00:00'}) == '20261107T104259Z'
    assert mf.allowed('https://claude.ai/a/b', ('claude.ai',)) and not mf.allowed('http://claude.ai/a', ('claude.ai',)) and not mf.allowed('https://evil.example/a', ('claude.ai',))
    assert not mf.allowed('https://claude.ai.evil.example/a', ('claude.ai',)) and not mf.allowed('file:///etc/passwd', ('claude.ai',)) and mf.allowed('http://127.0.0.1:1/a', ('127.0.0.1',), allow_http=True)

def test_download_all_names_verifies_and_makes_one_request_per_file(tmp_path):
    z = zbytes(tmp_path, 'x.zip', {'a.json': '{}'}); calls = []
    def fetch(url, dest):
        calls.append(url); open(dest, 'wb').write(z); return len(z), len(z)
    d = man([('conversations', 'https://claude.ai/1'), ('projects', 'https://claude.ai/2')])
    done, failed = mf.download_all(d, str(tmp_path / 'in'), fetch=fetch, log=lambda *_: None)
    assert failed == [] and done == ['20261107T104259Z_conversations-000.zip', '20261107T104259Z_projects-000.zip'] and calls == ['https://claude.ai/1', 'https://claude.ai/2']
    assert sorted(os.listdir(tmp_path / 'in')) == done                                                        # no .part left behind
    calls.clear(); assert mf.download_all(d, str(tmp_path / 'in'), fetch=fetch, log=lambda *_: None) == (done, []) and calls == []        # a re-run fetches nothing again

def test_bad_downloads_are_discarded_reported_and_do_not_stop_the_rest(tmp_path):
    good = zbytes(tmp_path, 'g.zip', {'a': '1'})
    def fetch(url, dest):
        body = {'1': good, '2': good[:-5], '3': b'<html>expired</html>', '4': good}[url[-1]]
        open(dest, 'wb').write(body); return len(body), (len(good) if url[-1] == '2' else len(body))
    d = man([('a', 'https://claude.ai/1'), ('b', 'https://claude.ai/2'), ('c', 'https://claude.ai/3'), ('d', 'https://claude.ai/4')])
    logs = []; done, failed = mf.download_all(d, str(tmp_path / 'in'), fetch=fetch, log=logs.append)
    assert [n.split('_')[1] for n in done] == ['a-000.zip', 'd-000.zip'] and [n.split('_')[1] for n in failed] == ['b-000.zip', 'c-000.zip']
    assert not [f for f in os.listdir(tmp_path / 'in') if f.endswith('.part')] and any('truncated' in l for l in logs) and any('not a valid zip' in l for l in logs)

def test_links_on_other_hosts_are_never_requested(tmp_path):
    calls = []
    done, failed = mf.download_all(man([('a', 'https://attacker.example/x'), ('b', 'http://claude.ai/x'), ('c', 'file:///etc/passwd')]), str(tmp_path), fetch=lambda u, d: calls.append(u), log=lambda *_: None)
    assert done == [] and len(failed) == 3 and calls == []

def test_filename_cannot_escape_the_inbox_folder(tmp_path):
    z = zbytes(tmp_path, 'x.zip', {'a': '1'}); d = man([('a', 'https://claude.ai/1')]); d['data_files'][0]['filename'] = '../../evil.zip'
    def fetch(url, dest): open(dest, 'wb').write(z); return len(z), len(z)
    done, _ = mf.download_all(d, str(tmp_path / 'in'), fetch=fetch, log=lambda *_: None)
    assert done == ['20261107T104259Z_evil.zip'] and not (tmp_path / 'evil.zip').exists()

class OneShot(http.server.BaseHTTPRequestHandler):
    log = []
    def do_GET(self):
        OneShot.log.append(('GET', self.path))
        if self.path in OneShot.used: self.send_response(410); self.end_headers(); return                   # the link died after its first use
        OneShot.used.add(self.path); body = OneShot.bodies[self.path]
        self.send_response(200); self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_HEAD(self): OneShot.log.append(('HEAD', self.path)); self.send_response(200); self.end_headers()
    def log_message(self, *a): pass

def test_real_http_one_use_links_are_used_exactly_once_with_no_probing(tmp_path):
    OneShot.used, OneShot.log = set(), []
    OneShot.bodies = {'/e/1': zbytes(tmp_path, 'a.zip', {'a': '1'}), '/e/2': zbytes(tmp_path, 'b.zip', {'b': '2'})}
    srv = http.server.HTTPServer(('127.0.0.1', 0), OneShot); threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f'http://127.0.0.1:{srv.server_port}'
    d = man([('one', base + '/e/1'), ('two', base + '/e/2')])
    done, failed = mf.download_all(d, str(tmp_path / 'in'), hosts=('127.0.0.1',), allow_http=True, log=lambda *_: None)
    again = mf.download_all(d, str(tmp_path / 'in'), hosts=('127.0.0.1',), allow_http=True, log=lambda *_: None)
    srv.shutdown()
    assert len(done) == 2 and failed == [] and again == (done, []) and OneShot.log == [('GET', '/e/1'), ('GET', '/e/2')]

@pytest.mark.skipif(not shutil.which('bsdtar'), reason='needs bsdtar')
def test_inbox_downloads_the_manifest_files_then_extracts_them_on_the_next_run(tmp_path):
    inb, ex, done = tmp_path / 'inbox', tmp_path / 'ex', tmp_path / 'done'
    z1 = zbytes(tmp_path, 'c.zip', {'conversations.json': '[]'}); z2 = zbytes(tmp_path, 'p.zip', {'projects.json': '{}'})
    (inb / 'claude').mkdir(parents=True); (inb / 'claude' / 'manifest-abc.json').write_text(json.dumps(man([('conversations', 'https://claude.ai/1'), ('projects', 'https://claude.ai/2')])))
    bodies = {'https://claude.ai/1': z1, 'https://claude.ai/2': z2}
    def fetch(url, dest): open(dest, 'wb').write(bodies[url]); return len(bodies[url]), len(bodies[url])
    args = dict(settle=0, recheck=0, filecat='true', log=lambda *_: None)
    notes = []; assert inbox.run(str(inb), str(ex), str(done), fetch=fetch, notify=lambda *a: notes.append(a), **args) == (1, 0)
    assert sorted(os.listdir(inb / 'claude')) == ['20261107T104259Z_conversations-000.zip', '20261107T104259Z_projects-000.zip']
    assert (ex / 'claude/loose/manifests/manifest-abc.json').exists() and not (ex / 'claude/tree').exists() and notes
    assert inbox.run(str(inb), str(ex), str(done), **args) == (2, 0)                                           # second run: the zips are extracted and filed
    assert (ex / 'claude/tree/conversations.json').exists() and (ex / 'claude/tree/projects.json').exists() and len(os.listdir(done / 'claude')) == 2

@pytest.mark.skipif(not shutil.which('bsdtar'), reason='needs bsdtar')
def test_retry_never_re_requests_a_link_that_was_already_used_even_after_its_file_was_filed(tmp_path):
    inb, ex, done = tmp_path / 'inbox', tmp_path / 'ex', tmp_path / 'done'
    z = zbytes(tmp_path, 'c.zip', {'a.json': '[]'}); (inb / 'claude').mkdir(parents=True); mp = inb / 'claude' / 'manifest-abc.json'
    mp.write_text(json.dumps(man([('good', 'https://claude.ai/1'), ('bad', 'https://claude.ai/2')]))); calls = []; healthy = {'2': False}
    def fetch(url, dest):
        calls.append(url); body = z if (url.endswith('1') or healthy['2']) else b'expired'; open(dest, 'wb').write(body); return len(body), len(body)
    args = dict(settle=0, recheck=0, filecat='true', log=lambda *_: None, fetch=fetch)
    assert inbox.run(str(inb), str(ex), str(done), **args) == (0, 1) and mp.exists()                              # run 1: good fetched, bad failed, manifest kept
    assert inbox.run(str(inb), str(ex), str(done), **args)[1] == 1                                                # run 2: the good zip is extracted and filed away; bad fails again
    assert not [f for f in os.listdir(inb / 'claude') if 'good' in f] and calls.count('https://claude.ai/1') == 1     # ... and its link was never requested a second time
    healthy['2'] = True; assert inbox.run(str(inb), str(ex), str(done), **args)[1] == 0 and calls.count('https://claude.ai/1') == 1       # run 3: only the missing file is fetched, manifest filed
    assert not mp.exists() and (ex / 'claude/loose/manifests/manifest-abc.json').exists() and not [f for f in os.listdir(inb / 'claude') if f.startswith('.')]

@pytest.mark.skipif(not shutil.which('bsdtar'), reason='needs bsdtar')
def test_a_file_that_keeps_failing_is_given_up_once_with_an_alert_instead_of_retrying_forever(tmp_path):
    inb, ex, done = tmp_path / 'inbox', tmp_path / 'ex', tmp_path / 'done'; (inb / 'claude').mkdir(parents=True); mp = inb / 'claude' / 'manifest-abc.json'
    mp.write_text(json.dumps(man([('dead', 'https://claude.ai/9')]))); calls = []; notes = []
    def fetch(url, dest): calls.append(url); open(dest, 'wb').write(b'gone'); return 4, 4
    args = dict(settle=0, recheck=0, filecat='true', log=lambda *_: None, fetch=fetch, notify=lambda *a: notes.append(a))
    for _ in range(2): assert inbox.run(str(inb), str(ex), str(done), **args)[1] == 1 and mp.exists()
    assert inbox.run(str(inb), str(ex), str(done), **args)[1] == 1 and not mp.exists() and (ex / 'claude/loose/manifests/manifest-abc.json').exists()    # third failure: given up
    assert len(calls) == 3 and notes and 'INCOMPLETE' in notes[-1][0]
    assert inbox.run(str(inb), str(ex), str(done), **args) == (0, 0) and len(calls) == 3                          # nothing left to retry
