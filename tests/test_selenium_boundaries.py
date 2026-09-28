import hashlib
import json
from pathlib import Path
import shutil
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from types import SimpleNamespace

import pytest
from test_selenium_recording import selenium_runs


@pytest.mark.parametrize('secret', ['a', 'D'])
def test_short_recognized_secret_preserves_shared_identity_and_pytest_state(tmp_path, secret):
    from signup031.selenium_recording import SeleniumRecorder
    from signup031.viewer_model import load_evidence_root
    driver = SimpleNamespace(capabilities={'browserName': 'unsupported'}, execute=lambda *args: None)
    recorder = SeleniumRecorder(driver, tmp_path, f'test_example.py::test_password[{secret}]')
    recorder.secrets.add(secret)
    assert recorder.redact(secret) == '[REDACTED]'
    assert recorder.redact(recorder.redact(secret)) == '[REDACTED]'
    recorder.closed = True
    recorder.capture(SimpleNamespace(outcome='failed', when='call'))
    path = recorder.finish([{'when': 'call', 'outcome': 'failed', 'message': secret, 'wasxfail': None}], 1)
    payload = json.loads(path.read_text(encoding='utf-8'))
    manifest = json.loads((path.parent / 'archive' / 'manifest.json').read_text(encoding='utf-8'))
    assert payload['test_identity'] == manifest['test_identity']
    assert payload['selenium_record'] == manifest['selenium_record']
    assert payload['result']['pytest']['reports'][0]['outcome'] == 'failed'
    assert payload['result']['pytest']['message'] == '[REDACTED]'
    assert recorder.redact('[REDACTED]') == '[REDACTED]'
    assert load_evidence_root(tmp_path)[0].business_status == 'failed'


@pytest.mark.parametrize('mode', ['absolute', 'traversal', 'junction', 'hash', 'missing', 'identity', 'missing-js', 'observation'])
def test_v4_integrity_and_missing_material(selenium_runs, tmp_path, mode):
    from signup031.archive import load_archive
    from signup031.viewer_model import load_evidence_root
    from signup031.replay import ReplaySession
    root = selenium_runs[0] / 'runs'
    original = next(r for r in load_evidence_root(root) if r.tc_id.endswith('::test_captions'))
    copy = tmp_path / 'copy'
    shutil.copytree(original.source_path.parent, copy)
    manifest_path = copy / 'archive' / 'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if mode == 'absolute':
        manifest['attachments'][0]['path'] = str((copy / 'archive' / 'page.html').resolve())
    elif mode == 'traversal':
        manifest['attachments'].append({'path': '../../outside.html', 'sha256': '0' * 64})
    elif mode == 'junction':
        outside = tmp_path / 'outside'
        outside.mkdir()
        (outside / 'payload').write_text('external')
        subprocess.run(['cmd', '/c', 'mklink', '/J', str(copy / 'archive' / 'alias'), str(outside)], check=True, capture_output=True)
        manifest['attachments'].append({'path': 'alias/payload', 'sha256': hashlib.sha256(b'external').hexdigest()})
    elif mode == 'hash':
        manifest['attachments'][0]['sha256'] = '0' * 64
    elif mode == 'missing':
        (copy / 'archive' / 'page.html').unlink()
    elif mode == 'identity':
        manifest['test_identity']['nodeid'] = 'different-test'
    elif mode == 'missing-js':
        har_path = copy / 'archive' / 'resources.har'
        har = json.loads(har_path.read_text(encoding='utf-8'))
        har['log']['entries'] = [row for row in har['log']['entries'] if not row['request']['url'].endswith('/captions.js')]
        har_path.write_text(json.dumps(har), encoding='utf-8')
        manifest['resources'][0]['sha256'] = hashlib.sha256(har_path.read_bytes()).hexdigest()
    else:
        manifest['selenium_record']['observed']['text'] = 'Different saved observation'
    manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
    if mode in ('identity', 'observation'):
        records = load_evidence_root(copy)
        assert records[0].archive_root is None
    if mode in ('missing-js', 'observation'):
        with ReplaySession(copy / 'archive', headless=True) as replay:
            assert replay.restore()['status'] == 'limited'
            assert replay.status()['reason'] or replay.status()['blocked_count']
    elif mode != 'identity':
        with pytest.raises((ValueError, OSError)):
            load_archive(copy / 'archive')


def test_actual_body_capture_limits(tmp_path):
    from selenium import webdriver
    from signup031.selenium_recording import SeleniumRecorder, recording_options
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'<p>Capture limits</p>' if self.path == '/' else b'x' * (900 if self.path == '/large' else 300)
            self.send_response(200); self.send_header('Content-Type', 'text/html' if self.path == '/' else 'text/plain')
            self.end_headers(); self.wfile.write(body)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True); thread.start()
    driver = webdriver.Chrome(options=recording_options(headless=True))
    try:
        recorder = SeleniumRecorder(driver, tmp_path, 'limits', max_body_bytes=512, max_total_bytes=1024)
        driver.get(f'http://127.0.0.1:{server.server_port}/')
        for url in ('/large', '/small-1', '/small-2', '/small-3', '/small-4'):
            driver.execute_async_script("const done=arguments[arguments.length-1];fetch(arguments[0]).then(r=>r.text()).then(done).catch(e=>done(String(e)));", url)
        recorder.capture(SimpleNamespace(outcome='failed', when='call'))
        path = recorder.finish([{'when': 'call', 'outcome': 'failed', 'message': 'original assertion', 'wasxfail': None}], 1)
        data = json.loads(path.read_text(encoding='utf-8'))
        assert data['capture']['status'] == 'limited'
        assert any('per-response' in reason for reason in data['selenium_record']['limitations'])
        assert any('total-response' in reason for reason in data['selenium_record']['limitations'])
        har = json.loads((path.parent / 'archive' / 'resources.har').read_text(encoding='utf-8'))
        assert all(not row['request']['url'].endswith('/large') for row in har['log']['entries'])
        assert sum(row['response']['content']['size'] for row in har['log']['entries']) <= 1024
        assert data['capture']['limits'] == {'per_response_bytes': 512, 'total_response_bytes': 1024}
    finally:
        driver.quit()
        server.shutdown(); server.server_close(); thread.join()
