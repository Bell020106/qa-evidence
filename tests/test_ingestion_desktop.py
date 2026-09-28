"""Actual separate receiver, original v4 bytes and Qt worker interaction."""
import base64
import hashlib
import json
import os
from pathlib import Path
from threading import Thread
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import time
import pytest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from test_ingestion import ServerProcess, TOKEN, envelope, request
from test_scenario_editor import until
from test_web_scenarios import web_site
from test_manual_recording import manual_site
from test_replay_qt import recorded_root


def v4_folder(root):
    """A small portable Selenium contract with real HAR-backed HTML and teardown failure."""
    from signup031.storage import write_evidence
    root.mkdir(parents=True)
    archive = root / 'archive'; archive.mkdir()
    url = 'http://127.0.0.1:9/offline'
    html = b'<html><body><h1>Original browser observation</h1></body></html>'
    record = {'title': 'test_file.py::test_fails_after_teardown', 'start_url': url, 'final_url': url,
              'actions': [], 'observed': {'inputs': [], 'text': 'Original browser observation'}, 'limitations': []}
    identity = {'nodeid': record['title'], 'phase': 'call', 'outcome': 'passed'}
    har = {'log': {'version': '1.2', 'creator': {'name': 'controlled', 'version': '1'}, 'entries': [{
        'startedDateTime': '2026-09-20T00:00:00Z', 'time': 0,
        'request': {'method': 'GET', 'url': url, 'httpVersion': 'HTTP/1.1', 'headers': [], 'queryString': [],
                    'cookies': [], 'headersSize': -1, 'bodySize': 0},
        'response': {'status': 200, 'statusText': 'OK', 'httpVersion': 'HTTP/1.1', 'headers': [], 'cookies': [],
                     'content': {'size': len(html), 'mimeType': 'text/html',
                                 'text': base64.b64encode(html).decode(), 'encoding': 'base64'},
                     'redirectURL': '', 'headersSize': -1, 'bodySize': len(html)},
        'cache': {}, 'timings': {'send': 0, 'wait': 0, 'receive': 0}}]}}
    write_evidence(archive / 'resources.har', har)
    (archive / 'page.html').write_bytes(html)
    resources_sha = hashlib.sha256((archive / 'resources.har').read_bytes()).hexdigest()
    dom_sha = hashlib.sha256(html).hexdigest()
    execution = 'controlled-v4-' + root.name
    manifest = {'archive_version': 4, 'execution_id': execution, 'navigation_url': url,
        'captured_at': '2026-09-20T00:00:00Z', 'status': 'recorded', 'reason': None,
        'replay_verification': 'not_run', 'selenium_record': record, 'test_identity': identity,
        'resources': [{'path': 'resources.har', 'sha256': resources_sha}],
        'attachments': [{'path': 'page.html', 'sha256': dom_sha}]}
    write_evidence(archive / 'manifest.json', manifest)
    reports = [{'when': 'setup', 'outcome': 'passed', 'message': '', 'wasxfail': None},
               {'when': 'call', 'outcome': 'passed', 'message': '', 'wasxfail': None},
               {'when': 'teardown', 'outcome': 'failed', 'message': 'Original teardown failure', 'wasxfail': None}]
    evidence = {'contract_version': '4', 'tc_id': identity['nodeid'],
        'execution': {'id': execution, 'started_at': '2026-09-20T00:00:00Z'},
        'environment': {'browser': 'chrome', 'browser_version': '152', 'driver_version': '152', 'python': '3.11', 'platform': 'Windows'},
        'test_identity': identity, 'selenium_record': record,
        'result': {'business': {'status': 'failed', 'phase': 'teardown', 'message': 'Original teardown failure'},
                   'pytest': {'status': 'failed', 'phase': 'teardown', 'message': 'Original teardown failure',
                              'reports': reports, 'session_exitcode': 1}},
        'capture': {'status': 'saved', 'errors': [], 'limits': {'per_response_bytes': 1000, 'total_response_bytes': 10000},
                    'source': 'original Selenium WebDriver session'},
        'evidence': {'screenshot': {'status': 'not_collected', 'path': 'archive/page.png', 'reason': None}},
        'replay': {'status': 'recorded', 'manifest': 'archive/manifest.json', 'reason': None},
        'post_run': {'cleanup': {'status': 'recorder_buffers_cleared', 'errors': [], 'driver': 'caller_owned_not_verified'}}}
    write_evidence(root / 'evidence.json', evidence)
    return root


def test_v4_separate_process_cli_download_and_offline_after_shutdown(tmp_path):
    import subprocess, sys
    from signup031.ingestion_client import IngestionClient
    from signup031.viewer_model import load_evidence_root
    from signup031.replay import ReplaySession
    source = v4_folder(tmp_path / 'v4-source')
    original = {p.relative_to(source): p.read_bytes() for p in source.rglob('*') if p.is_file()}
    receiver = ServerProcess(tmp_path / 'receiver').start()
    try:
        result = subprocess.run([sys.executable, '-m', 'signup031.ingestion_client', 'send',
            '--url', receiver.url, '--project', 'demo', '--provider', 'pytest-selenium', '--run', 'ci-expected-failure',
            '--folder', str(source)], env={**os.environ, 'QA_RESULT_TOKEN': TOKEN},
            capture_output=True, timeout=20)
        assert result.returncode == 0, result.stdout + result.stderr
        assert TOKEN.encode() not in result.stdout + result.stderr
        client = IngestionClient(receiver.url, 'demo', TOKEN)
        pc = tmp_path / 'pc'; assert client.download(pc)['complete']
    finally: receiver.stop()
    from signup031.investigation import InvestigationStore
    records = load_evidence_root(pc)
    assert len(records) == 1
    record = records[0]
    assert record.business_status == 'failed' and record.business_phase == 'teardown'
    assert record.archive_root is not None and 'call · passed' in record.selenium_details
    assert record.source_path.read_bytes() == original[Path('evidence.json')]
    for relative in ('archive/manifest.json', 'archive/resources.har', 'archive/page.html'):
        assert (record.source_path.parent / relative).read_bytes() == original[Path(relative)]
    with ReplaySession(record.archive_root, headless=True) as replay:
        assert replay.restore()['status'] == 'ready'
        assert replay.page.locator('h1').inner_text() == 'Original browser observation'
    note = InvestigationStore(pc).load(record.execution_id)
    assert 'Original teardown failure' in note['report']['actual']
    assert all((source / p).read_bytes() == raw for p, raw in original.items())


def test_interrupted_download_and_mismatched_detail_never_mark_replay_complete(tmp_path):
    from signup031.ingestion_client import IngestionClient, TransferError
    from signup031.viewer_model import load_evidence_root
    receiver = ServerProcess(tmp_path / 'receiver').start()
    source = v4_folder(tmp_path / 'v4-source')
    original = {p.relative_to(source): p.read_bytes() for p in source.rglob('*') if p.is_file()}
    try:
        sender = IngestionClient(receiver.url, 'demo', TOKEN)
        sender.send_folder(source, provider='pytest-selenium', run='ci-interruption',
                           provenance={'commit': 'abc123', 'workflow': 'synthetic-check'})
        client = IngestionClient(receiver.url, 'demo', TOKEN)
        actual_request = client._request
        def broken_request(method, path, *args, **kwargs):
            if method == 'GET' and '/files?' in path:
                raise TransferError('simulated interrupted download')
            return actual_request(method, path, *args, **kwargs)
        client._request = broken_request
        cache = tmp_path / 'pc'
        try:
            client.download(cache)
        except TransferError:
            pass
        records = load_evidence_root(cache)
        assert len(records) == 1 and records[0].archive_root is None
        sidecar = json.loads(next(cache.rglob('remote-source.json')).read_text())
        assert sidecar['transfer_status'] == 'partial'
        assert sidecar['provenance'] == {'commit': 'abc123', 'workflow': 'synthetic-check'}
        assert sidecar['server_origin'] == receiver.url
        client._request = actual_request
        assert client.download(cache)['complete']
        assert load_evidence_root(cache)[0].archive_root is not None
        listed = client.list_results()
        tampered = dict(listed['items'][0])
        tampered['files'] = []
        def mismatched(method, path, *args, **kwargs):
            status, raw = actual_request(method, path, *args, **kwargs)
            if method == 'GET' and '/results/' in path and '/files?' not in path:
                data = json.loads(raw); data['files'] = []
                return status, json.dumps(data).encode()
            return status, raw
        client._request = mismatched
        with pytest.raises(TransferError, match='inventory'):
            client.download(tmp_path / 'tampered')
        assert not list((tmp_path / 'tampered').rglob('evidence.json'))
        assert all((source / p).read_bytes() == raw for p, raw in original.items())
    finally: receiver.stop()


def test_qt_worker_import_status_duplicate_cancel_and_preserve_local_qa(tmp_path):
    from signup031.viewer import EvidenceViewerWindow
    from signup031.investigation import InvestigationStore
    from signup031.viewer_model import load_evidence_root
    receiver = ServerProcess(tmp_path / 'receiver').start()
    app = QApplication.instance() or QApplication([])
    original = v4_folder(tmp_path / 'v4-source')
    from signup031.ingestion_client import IngestionClient
    IngestionClient(receiver.url, 'demo', TOKEN).send_folder(original, provider='pytest-selenium', run='ci-qt')
    pc = tmp_path / 'pc'
    window = EvidenceViewerWindow(pc, replay_headless=True)
    window.show()
    try:
        QTest.mouseClick(window.server_import_button, Qt.MouseButton.LeftButton)
        dialog = window.server_import_dialog
        dialog.url_edit.setText(receiver.url)
        dialog.project_edit.setText('demo')
        dialog.token_edit.setText(TOKEN)
        assert dialog.token_edit.echoMode() == dialog.token_edit.EchoMode.Password
        dialog.root_edit.setText(str(pc))
        QTest.mouseClick(dialog.import_button, Qt.MouseButton.LeftButton)
        assert not dialog.import_button.isEnabled()
        assert not dialog.root_edit.isEnabled()
        dialog.root_edit.setText(str(tmp_path / 'wrong-root'))
        QTest.mouseClick(dialog.import_button, Qt.MouseButton.LeftButton)
        until(lambda: dialog.process is None)
        assert '완료' in dialog.status_label.text()
        assert window.root == pc.resolve()
        assert dialog.root_edit.isEnabled()
        assert 'pytest-selenium' in window.source_label.text()
        assert receiver.url in window.source_label.text()
        dialog.root_edit.setText(str(pc))
        assert len(load_evidence_root(pc)) == 1
        record = load_evidence_root(pc)[0]
        note = InvestigationStore(pc).load(record.execution_id)
        note['notes'] = 'Human QA note retained'; InvestigationStore(pc).save(note)
        qa_bytes = {p: p.read_bytes() for p in (pc / '.qa').rglob('*') if p.is_file()}
        QTest.mouseClick(dialog.import_button, Qt.MouseButton.LeftButton)
        until(lambda: dialog.process is None)
        assert all(p.read_bytes() == value for p, value in qa_bytes.items())
        assert len(load_evidence_root(pc)) == 1
        assert dialog.token_edit.text() == TOKEN
        dialog.close()
    finally:
        receiver.stop(); window.close()


def test_qt_cancel_stalled_worker_keeps_events_responsive(tmp_path):
    from signup031.viewer import EvidenceViewerWindow
    class Slow(BaseHTTPRequestHandler):
        def do_GET(self):
            time.sleep(3)
            self.send_response(200); self.end_headers(); self.wfile.write(b'{}')
        def log_message(self, *args): pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Slow)
    thread = Thread(target=server.serve_forever, daemon=True); thread.start()
    app = QApplication.instance() or QApplication([])
    window = EvidenceViewerWindow(tmp_path / 'pc', replay_headless=True); window.show()
    try:
        QTest.mouseClick(window.server_import_button, Qt.MouseButton.LeftButton)
        dialog = window.server_import_dialog
        dialog.url_edit.setText(f'http://127.0.0.1:{server.server_port}')
        dialog.project_edit.setText('demo'); dialog.token_edit.setText(TOKEN)
        dialog.root_edit.setText(str(tmp_path / 'pc'))
        QTest.mouseClick(dialog.import_button, Qt.MouseButton.LeftButton)
        assert dialog.process is not None and not dialog.import_button.isEnabled()
        until(lambda: dialog.worker_pid is not None)
        worker_pid = dialog.worker_pid
        ping = []
        from PySide6.QtCore import QTimer
        QTimer.singleShot(10, lambda: ping.append(True))
        until(lambda: bool(ping))
        QTest.mouseClick(dialog.cancel_button, Qt.MouseButton.LeftButton)
        until(lambda: dialog.process is None)
        assert '취소' in dialog.status_label.text()
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, worker_pid)
        if handle:
            code = ctypes.c_ulong()
            ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            ctypes.windll.kernel32.CloseHandle(handle)
            assert code.value != 259
        before = {p: p.read_bytes() for p in (tmp_path / 'pc').rglob('*') if p.is_file()}
        QTest.qWait(100)
        assert all(path.read_bytes() == data for path, data in before.items())
    finally:
        window.close(); server.shutdown(); server.server_close(); thread.join()


def test_qt_partial_attachment_preserves_original_failure_and_disables_replay(tmp_path):
    from signup031.viewer import EvidenceViewerWindow
    from signup031.ingestion_contract import build_bundle
    from signup031.viewer_model import load_evidence_root
    receiver = ServerProcess(tmp_path / 'receiver').start()
    source = v4_folder(tmp_path / 'v4-source')
    envelope, _ = build_bundle(source, 'demo', 'pytest-selenium', 'ci-partial')
    code, _ = request(receiver, 'POST', '/projects/demo/results', envelope)
    assert code == 201
    app = QApplication.instance() or QApplication([])
    pc = tmp_path / 'pc'
    window = EvidenceViewerWindow(pc, replay_headless=True); window.show()
    try:
        QTest.mouseClick(window.server_import_button, Qt.MouseButton.LeftButton)
        dialog = window.server_import_dialog
        dialog.url_edit.setText(receiver.url); dialog.project_edit.setText('demo')
        dialog.token_edit.setText(TOKEN); dialog.root_edit.setText(str(pc))
        QTest.mouseClick(dialog.import_button, Qt.MouseButton.LeftButton)
        until(lambda: dialog.process is None)
        assert '부분 첨부' in dialog.status_label.text()
        record = load_evidence_root(pc)[0]
        assert record.business_status == 'failed' and record.archive_root is None
        assert '다운로드 미완료' in record.archive_message
        assert not window.replay_button.isEnabled()
        dialog.close()
    finally: receiver.stop(); window.close()


def test_qt_disconnected_and_bad_auth_show_errors_without_false_results(tmp_path):
    from signup031.viewer import EvidenceViewerWindow

    app = QApplication.instance() or QApplication([])
    pc = tmp_path / 'pc'
    window = EvidenceViewerWindow(pc, replay_headless=True); window.show()
    receiver = None
    try:
        QTest.mouseClick(window.server_import_button, Qt.MouseButton.LeftButton)
        dialog = window.server_import_dialog
        dialog.url_edit.setText('http://127.0.0.1:1')
        dialog.project_edit.setText('demo'); dialog.token_edit.setText(TOKEN)
        dialog.root_edit.setText(str(pc))
        QTest.mouseClick(dialog.import_button, Qt.MouseButton.LeftButton)
        until(lambda: dialog.process is None)
        assert '가져오기 실패' in dialog.status_label.text()
        assert 'connection failed' in dialog.status_label.text()
        assert not list(pc.rglob('evidence.json'))

        receiver = ServerProcess(tmp_path / 'receiver').start()
        dialog.url_edit.setText(receiver.url)
        dialog.token_edit.setText('WRONG-SYNTHETIC-TOKEN-1234')
        QTest.mouseClick(dialog.import_button, Qt.MouseButton.LeftButton)
        until(lambda: dialog.process is None)
        assert '가져오기 실패' in dialog.status_label.text()
        assert 'HTTP 401' in dialog.status_label.text()
        assert not list(pc.rglob('evidence.json'))
        dialog.close()
    finally:
        if receiver: receiver.stop()
        window.close()


def test_existing_v2_v3_original_contracts_roundtrip_without_server(tmp_path, web_site, manual_site):
    from signup031.web_runner import run_scenario
    from signup031.manual_recording import ManualRecorder
    from signup031.ingestion_client import IngestionClient
    from signup031.viewer_model import load_evidence_root
    from signup031.replay import ReplaySession
    from test_web_scenarios import scenario_for
    v2 = run_scenario(scenario_for(web_site[0], 'password'), tmp_path / 'v2-runs').parent
    with ManualRecorder(manual_site[0] + '/search', 'Original manual', tmp_path / 'v3-runs', headless=True) as recorder:
        recorder.page.locator('#query').fill('original')
        recorder.page.locator('#apply').click()
        v3 = recorder.save().parent
    sources = [v2, v3]
    originals = [{p.relative_to(folder): p.read_bytes() for p in folder.rglob('*') if p.is_file()} for folder in sources]
    receiver = ServerProcess(tmp_path / 'receiver').start()
    try:
        client = IngestionClient(receiver.url, 'demo', TOKEN)
        for index, folder in enumerate(sources):
            result = client.send_folder(folder, provider='local-suite', run=f'old-{index}')
            assert not result['failed'] and not result['missing']
        cache = tmp_path / 'pc'; assert len(client.download(cache)['complete']) == 2
    finally: receiver.stop()
    records = load_evidence_root(cache)
    assert {r.business_status for r in records} == {'failed', 'unjudged'}
    for record in records:
        index = 0 if record.business_status == 'failed' else 1
        for name, data in originals[index].items():
            if name.as_posix() in ('evidence.json', 'page.png', 'archive/manifest.json', 'archive/resources.har'):
                assert (record.source_path.parent / name).read_bytes() == data
        assert record.archive_root is not None
        with ReplaySession(record.archive_root, headless=True) as replay:
            assert replay.restore()['status'] in ('ready', 'limited')


def test_existing_v1_archive_roundtrip_after_server_shutdown(recorded_root, tmp_path):
    from signup031.ingestion_client import IngestionClient
    from signup031.viewer_model import load_evidence_root
    from signup031.replay import ReplaySession
    source = next(recorded_root.rglob('evidence.json')).parent
    original = {p.relative_to(source): p.read_bytes() for p in source.rglob('*') if p.is_file()}
    receiver = ServerProcess(tmp_path / 'receiver').start()
    try:
        client = IngestionClient(receiver.url, 'demo', TOKEN)
        sent = client.send_folder(source, provider='local-pytest', run='legacy-1')
        assert not sent['missing'] and not sent['failed']
        cache = tmp_path / 'pc'
        assert len(client.download(cache)['complete']) == 1
    finally: receiver.stop()
    record = load_evidence_root(cache)[0]
    assert record.business_status == 'failed' and record.archive_root is not None
    for name in ('evidence.json', 'archive/manifest.json', 'archive/resources.har'):
        assert (record.source_path.parent / name).read_bytes() == original[Path(name)]
    with ReplaySession(record.archive_root, headless=True) as replay:
        assert replay.restore()['status'] == 'ready'
