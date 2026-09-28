"""Local Jira HTTP contract, independent receiver/worker and immutable evidence."""
import base64
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
from threading import Thread, Event
import time

import pytest
from test_ingestion import ServerProcess, TOKEN, envelope, request

JIRA_TOKEN = 'synthetic-jira-secret-37491'


class JiraDouble:
    def __init__(self):
        self.calls, self.issues, self.comments = [], {}, {}
        self.mode = 'ok'
        self.entered = Event()
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def reply(self, code, data):
                raw = json.dumps(data).encode()
                self.send_response(code); self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                try: self.wfile.write(raw)
                except OSError: pass
            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                body = json.loads(raw) if 'application/json' in self.headers.get('Content-Type', '') else {}
                owner.calls.append((self.path, body)); owner.entered.set()
                if owner.mode.isdigit(): return self.reply(int(owner.mode), {'error': JIRA_TOKEN})
                if owner.mode == 'redirect':
                    self.send_response(307); self.send_header('Location', owner.url + '/forbidden'); self.end_headers(); return
                if self.path == '/rest/api/3/issue':
                    key = 'QA-' + str(len(owner.issues) + 1)
                    owner.issues[key] = body
                    result = {'id': str(len(owner.issues)), 'key': key}
                elif self.path.endswith('/comment'):
                    key = self.path.split('/')[-2]
                    rows = owner.comments.setdefault(key, [])
                    result = {'id': str(len(rows) + 1), **body}; rows.append(result)
                else:
                    return self.reply(403, {})
                if owner.mode == 'drop':
                    self.connection.shutdown(socket.SHUT_RDWR); self.connection.close(); return
                if owner.mode == 'delay': time.sleep(3)
                self.reply(201, [] if owner.mode == 'malformed' else result)
            def do_GET(self):
                if owner.mode == 'delay_get': time.sleep(2)
                parts = self.path.split('?')[0].split('/')
                key = parts[5] if len(parts) > 5 else ''
                if key not in owner.issues: return self.reply(404, {})
                issue = owner.issues[key]
                if '/properties/' in self.path:
                    prop = next((p for p in issue['properties'] if p['key'] == parts[-1]), None)
                    return self.reply(200 if prop else 404, prop or {})
                if '/comment/' in self.path:
                    comment = next((c for c in owner.comments.get(key, []) if c['id'] == parts[-1]), None)
                    return self.reply(200 if comment else 404, comment or {})
                self.reply(200, {'key': key, 'fields': {'project': {'key': issue['fields']['project']['key']}}})
        self.http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.url = 'http://127.0.0.1:' + str(self.http.server_port)
        self.thread = Thread(target=self.http.serve_forever, daemon=True); self.thread.start()
    def close(self): self.http.shutdown(); self.http.server_close(); self.thread.join()


@pytest.fixture
def jira_system(tmp_path):
    jira = JiraDouble()
    config = {'demo': {'enabled': True, 'base_url': jira.url, 'allow_loopback': True,
        'project_key': 'QA', 'issue_type_id': '10001', 'fields': {'labels': ['qa-evidence']},
        'allowed_statuses': ['failed'], 'allowed_classifications': ['미분류', '제품 결함'], 'attachments': True}}
    env = {'QA_JIRA_CONFIG': json.dumps(config), 'QA_JIRA_EMAIL': 'qa@example.invalid', 'QA_JIRA_TOKEN': JIRA_TOKEN}
    server = ServerProcess(tmp_path / 'receiver', jira_config=env['QA_JIRA_CONFIG'],
                           jira_email=env['QA_JIRA_EMAIL'], jira_token=JIRA_TOKEN).start()
    try: yield server, jira, env
    finally: server.stop(); jira.close()


def status(server, rid):
    code, body = request(server, 'GET', f'/projects/demo/results/{rid}/jira')
    assert code == 200, body
    return json.loads(body)


def receive(server, **kwargs):
    event, raw = envelope(**kwargs)
    code, body = request(server, 'POST', '/projects/demo/results', event)
    assert code == 201, body
    return json.loads(body)['id'], event, raw


def worker(server, env, *, wait=True, **options):
    command = [sys.executable, '-m', 'signup031.jira_worker', '--storage', str(server.root / 'store'), '--once']
    for key, value in options.items(): command += ['--' + key.replace('_', '-'), str(value)]
    proc = subprocess.Popen(command, env={**os.environ, **env}, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if not wait: return proc
    out, err = proc.communicate(timeout=15)
    assert proc.returncode == 0, (out, err)
    assert JIRA_TOKEN.encode() not in out + err


def test_atomic_duplicate_receive_and_competing_workers_create_one_issue(jira_system):
    server, jira, env = jira_system
    event, original = envelope()
    with ThreadPoolExecutor(4) as pool:
        replies = list(pool.map(lambda _: request(server, 'POST', '/projects/demo/results', event), range(6)))
    rid = json.loads(replies[0][1])['id']
    state = status(server, rid)
    assert len(state['jobs']) == 1 and state['jobs'][0]['status'] == 'pending'
    with ThreadPoolExecutor(2) as pool: list(pool.map(lambda _: worker(server, env), range(2)))
    state = status(server, rid)
    assert state['issue_key'] == 'QA-1' and state['jobs'][0]['status'] == 'sent'
    assert len(jira.issues) == 1
    sent = jira.issues['QA-1']
    assert sent['fields']['description']['type'] == 'doc'
    assert sent['fields']['issuetype'] == {'id': '10001'}
    assert sent['fields']['labels'] == ['qa-evidence']
    paragraphs = [p['content'][0]['text'] for p in sent['fields']['description']['content']]
    assert 'Expected: {"maximum_length": 128}' in paragraphs
    assert 'Actual: {"initial_length": 128, "after_extra_length": 128}' in paragraphs
    assert event['execution_id'] in json.dumps(sent)
    assert sent['properties'][0]['value']['event_id'] == state['jobs'][0]['id']
    assert base64.b64decode(json.loads(request(server, 'GET', f'/projects/demo/results/{rid}')[1])['envelope']['evidence_b64']) == original


@pytest.mark.parametrize('mode,expected', [('401', 'configuration_error'), ('400', 'configuration_error'),
    ('429', 'retryable'), ('500', 'uncertain'), ('drop', 'uncertain'), ('redirect', 'configuration_error')])
def test_transport_outcomes_do_not_blindly_repeat_post(jira_system, mode, expected):
    server, jira, env = jira_system
    jira.mode = mode
    rid, _, _ = receive(server)
    worker(server, env)
    assert status(server, rid)['jobs'][0]['status'] == expected
    worker(server, env)
    assert len(jira.calls) == 1
    assert all('/forbidden' not in path for path, _ in jira.calls)
    assert JIRA_TOKEN not in json.dumps(status(server, rid)) + server.log.read_text(errors='replace')


def test_uncertain_requires_verified_key_and_survives_worker_death(jira_system):
    server, jira, env = jira_system
    jira.mode = 'delay'
    rid, _, _ = receive(server)
    proc = worker(server, env, wait=False, lease_seconds=1, timeout=.5)
    try:
        assert jira.entered.wait(5)
        proc.kill(); proc.communicate(timeout=5)
        time.sleep(1.1)
        worker(server, env, lease_seconds=1, timeout=.5)
        state = status(server, rid)
        assert state['jobs'][0]['status'] == 'uncertain' and len(jira.calls) == 1
        path = f'/projects/demo/results/{rid}/jira/recover'
        assert request(server, 'POST', path, {'job_id': state['jobs'][0]['id'], 'issue_key': 'OTHER-1'})[0] == 422
        assert request(server, 'POST', path, {'job_id': state['jobs'][0]['id'], 'issue_key': 'QA-1'})[0] == 200
        assert status(server, rid)['issue_key'] == 'QA-1'
    finally:
        if proc.poll() is None: proc.kill(); proc.communicate()


def complete_failure(execution):
    event, _ = envelope(execution=execution)
    payload = json.loads(base64.b64decode(event['evidence_b64']))
    payload['environment'] = {'browser': 'chrome', 'browser_version': '152', 'driver_version': '152',
                              'platform': 'Windows', 'python': '3.11', 'server': 'staging-a'}
    event['provenance'].update(expected_criteria_version='TC-spec-v3', error_signature='length-over-limit')
    event['evidence_b64'] = base64.b64encode(json.dumps(payload).encode()).decode()
    return event


def post_event(server, event):
    code, raw = request(server, 'POST', '/projects/demo/results', event)
    assert code == 201, raw
    return json.loads(raw)['id']


def test_complete_identity_waits_for_uncertain_create_and_distinguishes_missing_or_changed_fields(jira_system):
    server, jira, env = jira_system
    first = complete_failure('first'); rid1 = post_event(server, first)
    jira.mode = 'drop'; worker(server, env)
    rid2 = post_event(server, complete_failure('second'))
    worker(server, env)
    assert len(jira.calls) == 1
    assert status(server, rid2)['jobs'][0]['status'] == 'pending'
    job = status(server, rid1)['jobs'][0]
    assert request(server, 'POST', f'/projects/demo/results/{rid1}/jira/recover', {'job_id': job['id'], 'issue_key': 'QA-1'})[0] == 200
    jira.mode = 'ok'; worker(server, env)
    assert status(server, rid2)['issue_key'] == 'QA-1' and len(jira.comments['QA-1']) == 1
    for index, change in enumerate(('expected', 'environment', 'missing')):
        event = complete_failure('changed-' + str(index))
        if change == 'expected': event['provenance']['expected_criteria_version'] = 'TC-spec-v4'
        else:
            payload = json.loads(base64.b64decode(event['evidence_b64']))
            if change == 'environment': payload['environment']['server'] = 'staging-b'
            else: payload['environment'].pop('server')
            event['evidence_b64'] = base64.b64encode(json.dumps(payload).encode()).decode()
        changed = post_event(server, event); worker(server, env)
        assert status(server, changed)['issue_key'] != 'QA-1'
    assert len(jira.issues) == 4


def test_qa_versions_retests_comment_uncertainty_and_no_local_overwrite(jira_system, tmp_path):
    from signup031.ingestion_client import IngestionClient
    from signup031.investigation import InvestigationStore
    server, jira, env = jira_system
    rid, event, original = receive(server, execution='initial-failure')
    worker(server, env)
    client = IngestionClient(server.url, 'demo', TOKEN)
    root = tmp_path / 'pc'; client.download(root)
    store = InvestigationStore(root)
    doc = store.load('initial-failure'); doc['notes'] = 'QA confirms length boundary'; doc = store.save(doc)
    path = f'/projects/demo/results/{rid}/qa'
    update = {'expected_revision': 0, 'document': doc}
    code, raw = request(server, 'PUT', path, update)
    assert code == 200, raw
    assert request(server, 'PUT', path, update)[0] == 200
    different = deepcopy(update); different['document']['notes'] = 'stale writer'
    different['document']['history'][-1]['notes'] = 'stale writer'
    assert request(server, 'PUT', path, different)[0] == 409
    assert status(server, rid)['qa_revision'] == 1
    jira.mode = 'drop'; worker(server, env)
    state = status(server, rid)
    comment = [j for j in state['jobs'] if j['kind'] == 'comment'][0]
    assert comment['status'] == 'uncertain'
    worker(server, env); assert len(jira.comments['QA-1']) == 1
    assert request(server, 'POST', f'/projects/demo/results/{rid}/jira/recover',
        {'job_id': comment['id'], 'issue_key': 'QA-1', 'comment_id': '1'})[0] == 200
    success = complete_failure('explicit-success')
    payload = json.loads(base64.b64decode(success['evidence_b64']))
    payload['execution']['started_at'] = '2026-10-01T00:00:00Z'
    payload['result']['business']['status'] = 'passed'; payload['result']['pytest']['status'] = 'passed'
    success['evidence_b64'] = base64.b64encode(json.dumps(payload).encode()).decode()
    post_event(server, success); client.download(root)
    doc = store.link_retest(doc, 'explicit-success', 'same boundary after fix')
    assert request(server, 'PUT', path, {'expected_revision': 1, 'document': doc})[0] == 200
    jira.mode = 'ok'; worker(server, env)
    assert len(jira.issues) == 1 and len(jira.comments['QA-1']) == 2
    assert 'explicit-success' in json.dumps(jira.comments['QA-1'][-1])
    local = store.load('initial-failure'); local['notes'] = 'unsent local edit'; local = store.save(local)
    client.download(root)
    assert store.load('initial-failure')['notes'] == 'unsent local edit'
    assert status(server, rid)['qa']['notes'] == 'QA confirms length boundary'
    assert base64.b64decode(json.loads(request(server, 'GET', f'/projects/demo/results/{rid}')[1])['envelope']['evidence_b64']) == original
    assert all('transitions' not in p for p, _ in jira.calls)


def test_project_auth_original_hash_and_retest_provenance_are_enforced(jira_system, tmp_path):
    from signup031.ingestion_client import IngestionClient
    from signup031.investigation import InvestigationStore
    server, jira, env = jira_system
    rid, _, _ = receive(server)
    root = tmp_path / 'pc'; IngestionClient(server.url, 'demo', TOKEN).download(root)
    store = InvestigationStore(root); doc = store.save(store.load('source-execution'))
    for suffix, method, body in [('jira', 'GET', None), ('qa', 'PUT', {'expected_revision': 0, 'document': doc}),
                                  ('jira/recover', 'POST', {'issue_key': 'QA-1'})]:
        assert request(server, method, f'/projects/other/results/{rid}/{suffix}', body)[0] == 401
        assert request(server, method, f'/projects/other/results/{rid}/{suffix}', body, token='OTHER-SYNTHETIC-TOKEN-9876')[0] == 404
    doc['source']['sha256'] = '0' * 64
    assert request(server, 'PUT', f'/projects/demo/results/{rid}/qa', {'expected_revision': 0, 'document': doc})[0] == 422
    assert not jira.calls


@pytest.mark.parametrize('config', ['{broken', '{"demo": 3}', '{"demo": {"enabled": true}}'])
def test_invalid_jira_configuration_never_rolls_back_original_receive(tmp_path, config):
    server = ServerProcess(tmp_path / 'receiver', jira_config=config).start()
    try:
        rid, _, original = receive(server)
        assert status(server, rid)['jobs'][0]['status'] == 'configuration_error'
        assert base64.b64decode(json.loads(request(server, 'GET', f'/projects/demo/results/{rid}')[1])['envelope']['evidence_b64']) == original
    finally: server.stop()


def test_disabled_project_does_not_starve_other_enabled_project(jira_system):
    server, jira, env = jira_system
    receive(server)
    config = json.loads(env['QA_JIRA_CONFIG']); config['other'] = deepcopy(config['demo'])
    server.stop(); server.limits['jira_config'] = json.dumps(config); server.start()
    event, _ = envelope(); event['project_id'] = 'other'
    code, raw = request(server, 'POST', '/projects/other/results', event, token='OTHER-SYNTHETIC-TOKEN-9876')
    assert code == 201
    config['demo']['enabled'] = False
    worker(server, {**env, 'QA_JIRA_CONFIG': json.dumps(config)})
    assert len(jira.issues) == 1


def test_attachment_error_keeps_successful_issue_and_separate_job(jira_system):
    import hashlib
    server, jira, env = jira_system
    content = b'\x89PNG\r\n\x1a\n'
    rid, _, _ = receive(server, files=[{'path': 'capture.png', 'sha256': hashlib.sha256(content).hexdigest(), 'size': 8}])
    assert request(server, 'PUT', f'/projects/demo/results/{rid}/files?path=capture.png', content)[0] == 200
    worker(server, env); worker(server, env)
    state = status(server, rid)
    assert state['issue_key'] == 'QA-1'
    assert [j['status'] for j in state['jobs']] == ['sent', 'configuration_error']
    assert jira.calls[-1][0] == '/rest/api/3/issue/QA-1/attachments'


def test_retry_is_bounded_and_secret_in_decoded_evidence_is_rejected(jira_system):
    server, jira, env = jira_system
    jira.mode = '429'
    rid, _, _ = receive(server)
    for _ in range(4):
        worker(server, env)
        with sqlite3.connect(server.root / 'store' / 'results.sqlite3') as conn:
            conn.execute('UPDATE jira_jobs SET next_at=0')
    assert len(jira.calls) == 3
    assert status(server, rid)['jobs'][0]['status'] == 'configuration_error'
    event, _ = envelope()
    payload = json.loads(base64.b64decode(event['evidence_b64']))
    payload['result']['business']['message'] = JIRA_TOKEN
    event['evidence_b64'] = base64.b64encode(json.dumps(payload).encode()).decode()
    assert request(server, 'POST', '/projects/demo/results', event)[0] == 422


def test_cli_supplies_explicit_failure_identity_without_editing_original(jira_system, tmp_path):
    from test_ingestion_desktop import v4_folder
    server, jira, env = jira_system
    folder = v4_folder(tmp_path / 'original'); original = (folder / 'evidence.json').read_bytes()
    result_ids = []
    for run in ('first-run', 'second-run'):
        reply = subprocess.run([sys.executable, '-m', 'signup031.ingestion_client', 'send', '--url', server.url,
            '--project', 'demo', '--provider', 'local-ci', '--run', run, '--folder', str(folder),
            '--expected-criteria-version', 'TC-17-v3', '--error-signature', 'teardown-resource-leak',
            '--environment-id', 'staging-a'], env={**os.environ, 'QA_RESULT_TOKEN': TOKEN}, capture_output=True, timeout=10)
        assert reply.returncode == 0, reply.stderr
        result_ids.append(json.loads(reply.stdout)['id'])
        worker(server, env)
    # Attachment jobs may run before the second result comment; creation still remains unique.
    assert len(jira.issues) == 1
    assert status(server, result_ids[1])['issue_key'] == 'QA-1'
    assert (folder / 'evidence.json').read_bytes() == original


def test_qt_investigation_publishes_saved_qa_and_refreshes_after_worker(jira_system, tmp_path):
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PySide6.QtWidgets import QApplication
    from signup031.ingestion_client import IngestionClient
    from signup031.investigation_dialog import InvestigationDialog
    from test_scenario_editor import until
    server, jira, env = jira_system
    rid, _, _ = receive(server, execution='qt-failure')
    root = tmp_path / 'pc'; IngestionClient(server.url, 'demo', TOKEN).download(root)
    worker(server, env)
    app = QApplication.instance() or QApplication([])
    dialog = InvestigationDialog(root, 'qt-failure')
    try:
        assert hasattr(dialog, 'jira_button'), 'investigation needs an actual Jira sync entrypoint'
        dialog.notes.setPlainText('PC authored QA note'); assert dialog.save()
        dialog.jira_button.click()
        sync = dialog.jira_dialog
        sync.token_edit.setText(TOKEN)
        sync.start_action('refresh')
        try: until(lambda: sync.process is None, 10)
        except AssertionError:
            raise AssertionError((sync.status_label.text(), sync.process.state(), sync.process.errorString(),
                                  bytes(sync.process.readAllStandardError()).decode(errors='replace')))
        assert 'QA-1' in sync.details.toPlainText()
        sync.start_action('publish')
        until(lambda: sync.process is None, 10)
        assert status(server, rid)['qa']['notes'] == 'PC authored QA note'
        assert TOKEN not in sync.details.toPlainText()
        sync.close()
        worker(server, env)
        dialog.jira_button.click(); sync = dialog.jira_dialog
        sync.token_edit.setText(TOKEN); sync.start_action('refresh')
        until(lambda: sync.process is None, 10)
        assert 'sent' in sync.details.toPlainText()
    finally:
        if getattr(dialog, 'jira_dialog', None): dialog.jira_dialog.close()
        dialog.close(); app.processEvents()


def test_qt_uncertain_recovery_cancel_is_responsive_and_refresh_restores_state(jira_system, tmp_path):
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PySide6.QtCore import QTimer
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from signup031.ingestion_client import IngestionClient
    from signup031.investigation_dialog import InvestigationDialog
    from test_scenario_editor import until
    server, jira, env = jira_system
    rid, _, _ = receive(server, execution='cancel-recovery')
    jira.mode = 'drop'; worker(server, env)
    root = tmp_path / 'pc'; IngestionClient(server.url, 'demo', TOKEN).download(root)
    app = QApplication.instance() or QApplication([])
    dialog = InvestigationDialog(root, 'cancel-recovery'); dialog.open_jira(); sync = dialog.jira_dialog
    try:
        sync.token_edit.setText(TOKEN); sync.key_edit.setText('QA-1')
        jira.mode = 'delay_get'
        ticks = []; timer = QTimer(); timer.timeout.connect(lambda: ticks.append(1)); timer.start(20)
        sync.start_action('recover'); QTest.qWait(250)
        assert len(ticks) >= 3 and sync.process is not None
        sync.cancel(); until(lambda: sync.process is None, 3)
        assert '취소' in sync.status_label.text()
        # The server can finish read-only verification after the PC cancels its wait.
        QTest.qWait(4500); jira.mode = 'ok'
        sync.start_action('refresh'); until(lambda: sync.process is None, 5)
        assert 'QA-1' in sync.details.toPlainText() and 'sent' in sync.details.toPlainText()
        assert len(jira.issues) == 1 and len(jira.calls) == 1
        timer.stop()
    finally: sync.close(); dialog.close(); app.processEvents()


def test_explicit_remote_qa_import_refuses_to_replace_local_notes(jira_system, tmp_path):
    from signup031.ingestion_client import IngestionClient
    from signup031.investigation import InvestigationStore
    server, _, _ = jira_system
    rid, _, _ = receive(server)
    client = IngestionClient(server.url, 'demo', TOKEN)
    root = tmp_path / 'pc'; client.download(root)
    store = InvestigationStore(root)
    doc = store.load('source-execution'); doc['notes'] = 'server investigation'; doc = store.save(doc)
    client.publish_qa(rid, doc, 0)
    other = tmp_path / 'other-pc'; client.download(other)
    other_store = InvestigationStore(other)
    other_store.import_remote(client.jira_status(rid)['qa'])
    local = other_store.load('source-execution'); local['notes'] = 'local revision'; other_store.save(local)
    with pytest.raises(ValueError, match='로컬 QA'): other_store.import_remote(doc)
    assert other_store.load('source-execution')['notes'] == 'local revision'


def test_recovery_enqueues_attachments_and_explicit_retry_excludes_uncertain(jira_system):
    server, jira, env = jira_system
    rid, _, _ = receive(server, files=[{'path': 'capture.png', 'sha256': 'a' * 64, 'size': 8}])
    jira.mode = 'drop'; worker(server, env)
    job = status(server, rid)['jobs'][0]
    retry_path = f'/projects/demo/results/{rid}/jira/retry'
    assert request(server, 'POST', retry_path, {'job_id': job['id']})[0] == 422
    assert request(server, 'POST', f'/projects/demo/results/{rid}/jira/recover', {'job_id': job['id'], 'issue_key': 'QA-1'})[0] == 200
    assert [j['kind'] for j in status(server, rid)['jobs']] == ['create', 'attachment']
    other, _, _ = receive(server)
    jira.mode = '401'
    worker(server, env)  # missing attachment remains retryable
    worker(server, env)  # second create is definitively refused
    failed = status(server, other)['jobs'][0]
    assert failed['status'] == 'configuration_error'
    assert request(server, 'POST', f'/projects/demo/results/{other}/jira/retry', {'job_id': failed['id']})[0] == 200
    jira.mode = 'ok'; worker(server, env)
    assert status(server, other)['jobs'][0]['status'] == 'sent'


def test_malformed_success_response_is_uncertain_without_killing_worker(jira_system):
    server, jira, env = jira_system
    rid, _, _ = receive(server)
    jira.mode = 'malformed'; worker(server, env)
    assert status(server, rid)['jobs'][0]['status'] == 'uncertain'
    second, _, _ = receive(server)
    jira.mode = 'ok'; worker(server, env)
    assert status(server, second)['jobs'][0]['status'] == 'sent'
