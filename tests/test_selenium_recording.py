"""Native Selenium + child pytest, followed by offline playback of original responses."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest


@pytest.fixture(scope='module')
def selenium_runs(tmp_path_factory):
    root = tmp_path_factory.mktemp('selenium-capture')
    (root / 'driver-pids.jsonl').touch()
    class Handler(BaseHTTPRequestHandler):
        hits = []
        def do_GET(self):
            Handler.hits.append(self.path)
            bodies = {
                '/captions': ('text/html', '<link rel="stylesheet" href="/style.css"><button id="toggle">Captions</button><p id="subtitle"></p><script src="/captions.js"></script>'),
                '/style.css': ('text/css', '#subtitle{color:blue}'),
                '/captions.js': ('text/javascript', "document.querySelector('#toggle').onclick=async()=>{let p=document.querySelector('#subtitle');p.textContent=p.textContent?'':await(await fetch('/subtitle.txt')).text()};"),
                '/subtitle.txt': ('text/plain', 'Original subtitle'),
                '/form': ('text/html', '<input id="query"><button id="apply">Apply</button><p id="result"></p><script src="/form.js"></script>'),
                '/form.js': ('text/javascript', "document.querySelector('#apply').onclick=()=>document.querySelector('#result').textContent=document.querySelector('#query').value"),
                '/private': ('text/html', '<input type="password" id="password"><input data-sensitive id="secret">'),
                '/unsupported': ('text/html', '<button id="post">Post</button><script>document.querySelector("button").onclick=()=>fetch("/write",{method:"POST",body:"local"});</script>'),
                '/early': ('text/html', '<p>Early close</p>')}
            mime, body = bodies.get(self.path, ('text/plain', ''))
            self.send_response(200)
            self.send_header('Content-Type', mime + '; charset=utf-8')
            if self.path == '/private':
                self.send_header('Set-Cookie', 'session=SECRET-COOKIE-789')
                self.send_header('Authorization', 'Bearer PRIVATE-TOKEN-012')
            self.end_headers(); self.wfile.write(body.encode())
        def do_POST(self):
            Handler.hits.append('POST ' + self.path)
            self.send_response(200); self.end_headers(); self.wfile.write(b'unsupported')
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True); thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    test = root / 'test_native.py'
    test.write_text('''import os, pytest
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from signup031.selenium_recording import recording_options

PASSWORDS = ['PRIVATE-PASSWORD-123', 'PRIVATE-PASSWORD-234']

@pytest.fixture
def driver(selenium_record):
    browser = webdriver.Chrome(options=recording_options(headless=True))
    selenium_record(browser)
    with open(os.environ['LOCAL_DRIVER_PIDS'], 'a') as stream:
        import json
        stream.write(json.dumps({'pid': browser.service.process.pid})+'\\n')
    yield browser
    if browser.service.process is not None and browser.service.process.poll() is None:
        browser.quit()

def test_captions(driver):
    driver.get(os.environ['LOCAL_SELENIUM_URL'] + '/captions')
    driver.find_element(By.ID, 'toggle').click()
    WebDriverWait(driver, 5).until(lambda d: d.find_element(By.ID, 'subtitle').text == 'Original subtitle')
    assert driver.find_element(By.ID, 'subtitle').text == 'Expected translated subtitle'

def test_form(driver):
    driver.get(os.environ['LOCAL_SELENIUM_URL'] + '/form')
    driver.find_element(By.ID, 'query').send_keys('original input')
    driver.find_element(By.ID, 'apply').click()
    assert driver.find_element(By.ID, 'result').text == 'healthy expected result'

@pytest.mark.parametrize('password', PASSWORDS)
def test_sensitive(driver, password):
    driver.get(os.environ['LOCAL_SELENIUM_URL'] + '/private')
    driver.find_element(By.ID, 'password').send_keys(password)
    driver.find_element(By.ID, 'secret').send_keys('PRIVATE-FIELD-456')
    assert False, f'Password {password} must be redacted'

def test_unsupported(driver):
    driver.get(os.environ['LOCAL_SELENIUM_URL'] + '/unsupported')
    driver.find_element(By.ID, 'post').click()
    driver.execute_script("new WebSocket('ws://127.0.0.1:9/missing'); window.open('about:blank')")
    assert driver.title == "unsupported workflow succeeded"

def test_early_close(driver):
    driver.get(os.environ['LOCAL_SELENIUM_URL'] + '/early')
    driver.quit()
    assert False, 'original failure after quit'

@pytest.fixture
def skipped_driver(driver):
    pytest.skip('original setup skip')

def test_setup_skip(skipped_driver):
    assert False

@pytest.fixture
def teardown_driver(driver):
    yield driver
    raise RuntimeError('original teardown failure')

def test_teardown_failure(teardown_driver):
    teardown_driver.get(os.environ['LOCAL_SELENIUM_URL'] + '/teardown')
    assert True

@pytest.fixture
def broken_capture(selenium_record):
    browser = webdriver.Chrome(options=recording_options(headless=True))
    recorder = selenium_record(browser)
    (recorder.archive / 'page.png').mkdir()
    yield browser
    browser.quit()

def test_capture_failure(broken_capture):
    broken_capture.get(os.environ['LOCAL_SELENIUM_URL'] + '/capture-error')
    assert False, 'assertion preserved despite screenshot write failure'

@pytest.fixture
def no_network_logging(selenium_record):
    options = webdriver.ChromeOptions()
    options.add_argument('--headless=new')
    browser = webdriver.Chrome(options=options)
    selenium_record(browser)
    yield browser
    browser.quit()

def test_network_capture_unavailable(no_network_logging):
    no_network_logging.get(os.environ['LOCAL_SELENIUM_URL'] + '/no-network')
    assert False, 'assertion preserved despite network capture unavailable'

def test_success(driver):
    driver.get(os.environ['LOCAL_SELENIUM_URL'] + '/success')
    assert True

@pytest.fixture
def setup_failure_driver(driver):
    driver.get(os.environ['LOCAL_SELENIUM_URL'] + '/setup-error')
    raise RuntimeError('original setup failure')

def test_setup_failure(setup_failure_driver):
    assert False

@pytest.mark.xfail(reason='original expected failure')
def test_xfail(driver):
    driver.get(os.environ['LOCAL_SELENIUM_URL'] + '/xfail')
    assert False
''', encoding='utf-8')
    env = {**os.environ, 'LOCAL_SELENIUM_URL': base, 'LOCAL_DRIVER_PIDS': str(root / 'driver-pids.jsonl')}
    try:
        log = root / 'pytest-child.log'
        with log.open('wb') as output:
            result = subprocess.run([sys.executable, '-m', 'pytest', '-p', 'signup031.selenium_plugin', str(test),
                                     '--rootdir', str(root), '-o', 'cache_dir=' + str(root / '.pytest_cache'),
                                     '--selenium-artifacts', str(root / 'runs'), '-vv', '-s', '-o', 'faulthandler_timeout=10'],
                                    env=env, stdout=output, stderr=subprocess.STDOUT, timeout=90)
        result.stdout, result.stderr = log.read_bytes(), b''
        yield root, server, Handler, result
    finally:
        server.shutdown(); server.server_close(); thread.join()


def test_original_pytest_failure_and_native_artifacts(selenium_runs):
    root, server, handler, result = selenium_runs
    assert result.returncode == 1, result.stdout.decode(errors='replace') + result.stderr.decode(errors='replace')
    assert b'8 failed, 2 passed, 1 skipped, 1 xfailed, 2 errors' in result.stdout
    payloads = {json.loads(p.read_text(encoding='utf-8'))['tc_id'].rsplit('::', 1)[-1]: (p, json.loads(p.read_text(encoding='utf-8')))
                for p in (root / 'runs').rglob('evidence.json')}
    assert len(payloads) == 10
    for name in ('test_captions', 'test_form'):
        path, payload = payloads[name]
        assert payload['contract_version'] == '4'
        assert payload['result']['business']['status'] == 'failed'
        assert payload['result']['pytest']['session_exitcode'] == 1
        assert 'AssertionError' in payload['result']['pytest']['message']
        assert payload['environment']['browser_version']
        assert payload['selenium_record']['actions']
        assert (path.parent / payload['evidence']['screenshot']['path']).is_file()
        assert payload['capture']['status'] == 'saved'
    assert handler.hits.count('/captions') == 1 and handler.hits.count('/form') == 1
    assert handler.hits.count('/subtitle.txt') == 1
    assert payloads['test_unsupported'][1]['result']['business']['status'] == 'failed'
    assert payloads['test_early_close'][1]['result']['business']['status'] == 'failed'
    assert payloads['test_early_close'][1]['capture']['status'] == 'failed'
    assert 'original failure after quit' in payloads['test_early_close'][1]['result']['pytest']['message']
    private_runs = [value for key, value in payloads.items() if key.startswith('test_sensitive[')]
    assert len(private_runs) == 2 and private_runs[0][1]['tc_id'] != private_runs[1][1]['tc_id']
    for private_path, private in private_runs:
        contents = b''.join(p.read_bytes() for p in private_path.parent.rglob('*') if p.is_file())
        for secret in (b'PRIVATE-PASSWORD-123', b'PRIVATE-PASSWORD-234', b'PRIVATE-FIELD-456', b'SECRET-COOKIE-789', b'PRIVATE-TOKEN-012'):
            assert secret not in contents
        assert private['capture']['status'] == 'limited'
        assert '[REDACTED]' in private['tc_id']
        assert private['test_identity']['nodeid'] == private['tc_id']
        manifest = json.loads((private_path.parent / 'archive' / 'manifest.json').read_text(encoding='utf-8'))
        assert manifest['test_identity'] == private['test_identity']
    limits = payloads['test_unsupported'][1]['selenium_record']['limitations']
    assert any('POST' in reason for reason in limits)
    assert any('tab' in reason for reason in limits)
    assert any('WebSocket' in reason for reason in limits)
    assert 'test_setup_skip' not in payloads
    teardown = payloads['test_teardown_failure'][1]
    assert teardown['result']['business']['status'] == 'failed'
    assert teardown['result']['business']['phase'] == 'teardown'
    assert teardown['test_identity']['phase'] == 'call'
    assert teardown['test_identity']['outcome'] == 'passed'
    assert 'original teardown failure' in teardown['result']['pytest']['message']
    for name in ('test_capture_failure', 'test_network_capture_unavailable'):
        assert payloads[name][1]['capture']['status'] == 'failed'
        assert payloads[name][1]['result']['business']['status'] == 'failed'
    assert 'test_success' not in payloads
    assert payloads['test_setup_failure'][1]['result']['business']['status'] == 'preparation_failed'
    assert payloads['test_setup_failure'][1]['test_identity']['phase'] == 'setup'
    assert 'test_xfail' not in payloads
    # Driver services from the original sessions must have exited normally.
    import ctypes
    for row in (root / 'driver-pids.jsonl').read_text().splitlines():
        pid = json.loads(row)['pid']
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if handle:
            code = ctypes.c_ulong()
            ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            ctypes.windll.kernel32.CloseHandle(handle)
            assert code.value != 259
    assert not list((root / 'runs').rglob('*.tmp'))


def test_offline_original_responses_move_interaction_and_integrity(selenium_runs, tmp_path):
    from signup031.replay import ReplaySession
    from signup031.viewer_model import load_evidence_root
    from signup031.investigation import InvestigationStore
    root, server, handler, _ = selenium_runs
    records = load_evidence_root(root / 'runs')
    assert len(records) == 10
    assert all(r.business_status != 'incomplete' for r in records)
    for name, selector, value in [('test_captions', '#toggle', ''), ('test_form', '#query', 'offline changed')]:
        record = next(r for r in records if r.tc_id.endswith('::' + name))
        assert record.archive_root is not None, record.message
        copied = tmp_path / name
        shutil.copytree(record.source_path.parent, copied)
        before = {p.relative_to(copied): p.read_bytes() for p in copied.rglob('*') if p.is_file()}
        count = len(handler.hits)
        with ReplaySession(copied / 'archive', headless=True) as replay:
            assert replay.restore()['status'] == 'ready'
            if name == 'test_captions':
                assert replay.page.locator('#subtitle').inner_text() == 'Original subtitle'
                replay.page.locator(selector).click()
                assert replay.page.locator('#subtitle').inner_text() == ''
                replay.page.locator(selector).click()
                replay.page.wait_for_function("document.querySelector('#subtitle').textContent === 'Original subtitle'")
            else:
                replay.page.locator(selector).fill(value)
                replay.page.locator('#apply').click()
                assert replay.page.locator('#result').inner_text() == value
            replay.page.evaluate("""fetch('/never-recorded').catch(()=>null);
                fetch('/write',{method:'POST',body:'offline'}).catch(()=>null);
                new WebSocket('ws://127.0.0.1:9/offline');
                navigator.serviceWorker.register('/worker.js').catch(()=>null)""")
            tab = replay.context.new_page()
            with pytest.raises(Exception):
                tab.goto(record.target_url + '/unrecorded-tab')
            tab.close()
            replay.page.wait_for_timeout(100)
            assert replay.status()['status'] == 'limited'
        assert len(handler.hits) == count  # source is alive, but receives zero replay traffic
        assert all((copied / p).read_bytes() == data for p, data in before.items())
        report = InvestigationStore(tmp_path).load(record.execution_id)
        assert report['report']['steps'] != '미수집'
        payload = json.loads(record.source_path.read_text(encoding='utf-8'))
        assert payload['environment']['browser_version'] in report['report']['environment']
        assert payload['environment']['driver_version'] in report['report']['environment']
        (copied / 'archive' / 'page.html').write_text('corrupt', encoding='utf-8')
        with pytest.raises(ValueError, match='hash'):
            ReplaySession(copied / 'archive', headless=True)


def test_native_records_in_qt_and_after_server_shutdown(selenium_runs, tmp_path):
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PySide6.QtWidgets import QApplication
    from signup031.viewer import EvidenceViewerWindow
    from test_scenario_editor import until
    root, server, handler, _ = selenium_runs
    server.shutdown(); server.server_close()
    moved = tmp_path / 'moved'
    shutil.copytree(root / 'runs', moved)
    app = QApplication.instance() or QApplication([])
    window = EvidenceViewerWindow(moved, replay_headless=True)
    window.show()
    try:
        record = next(r for r in window.records if r.tc_id.endswith('::test_captions'))
        window._scenario_recorded(str(record.source_path))
        assert window.status_badge.text() == '실패'
        assert not window.draft_button.isEnabled()
        assert 'pytest' in window.pytest_value.text()
        assert '수집기 메모리' in window.cleanup_value.text() and '미확인' in window.cleanup_value.text()
        window.start_replay(headless=True)
        until(lambda: '수동 테스트 가능' in window.replay_label.text() or '복원 실패' in window.replay_label.text())
        assert '수동 테스트 가능' in window.replay_label.text()
        window.stop_replay(); until(lambda: window.replay_process is None)
        window.open_investigation()
        assert '실패' in window.investigation_dialog.original_label.text()
        window.investigation_dialog.close()
        teardown = next(r for r in window.records if r.tc_id.endswith('::test_teardown_failure'))
        window._scenario_recorded(str(teardown.source_path))
        assert window.status_badge.text() == '실패'
        assert 'call · passed' in window.checks_label.text()
        assert 'teardown · failed' in window.checks_label.text()
    finally:
        window.stop_replay(); until(lambda: window.replay_process is None)
        window.close()
