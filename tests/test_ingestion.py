"""Actual HTTP/SQLite boundaries: identity, permission, immutable bytes and partial evidence."""
import base64
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import http.client
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

import pytest
from test_viewer_model import _current_payload

TOKEN = 'SYNTHETIC-INGESTION-TOKEN-9841'


class ServerProcess:
    def __init__(self, root, **limits):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.log = root / 'server.log'
        self.limits = limits
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0)); self.port = sock.getsockname()[1]
        self.url = f'http://127.0.0.1:{self.port}'
        self.process = None

    def start(self):
        self.output = self.log.open('ab')
        self.process = subprocess.Popen([sys.executable, '-m', 'signup031.ingestion_server',
            '--storage', str(self.root / 'store'), '--port', str(self.port)],
            env={**os.environ, 'QA_PROJECT_TOKENS': json.dumps({'demo': TOKEN, 'other': 'OTHER-SYNTHETIC-TOKEN-9876'}),
                 **{f'QA_{key.upper()}': str(value) for key, value in self.limits.items()}},
            stdout=self.output, stderr=subprocess.STDOUT)
        end = time.monotonic() + 10
        while time.monotonic() < end:
            if self.process.poll() is not None:
                raise AssertionError(self.log.read_text(errors='replace'))
            try:
                with urlopen(self.url + '/health', timeout=.2) as reply:
                    if reply.status == 200: return self
            except OSError:
                time.sleep(.05)
        raise AssertionError('owned HTTP server did not become ready')

    def stop(self):
        if self.process and self.process.poll() is None:
            self.process.terminate(); self.process.wait(timeout=10)
        self.output.close()


@pytest.fixture(scope='module')
def server(tmp_path_factory):
    assert importlib.util.find_spec('signup031.ingestion_server') is not None, '08 receiving server is not implemented'
    instance = ServerProcess(tmp_path_factory.mktemp('ingestion-server')).start()
    yield instance
    instance.stop()


def request(server, method, path, data=None, *, token=TOKEN):
    headers = {'Content-Type': 'application/json'}
    if token is not None: headers['Authorization'] = 'Bearer ' + token
    body = json.dumps(data).encode() if isinstance(data, dict) else data
    try:
        with urlopen(Request(server.url + path, body, headers, method=method), timeout=5) as reply:
            return reply.status, reply.read()
    except HTTPError as exc:
        return exc.code, exc.read()


def envelope(*, run=None, execution='source-execution', files=None, payload=None):
    payload = deepcopy(payload or _current_payload(execution_id=execution))
    payload['result']['business'].update(status='failed', message='Original assertion failure')
    payload['result']['pytest']['status'] = 'failed'
    raw = (json.dumps(payload, ensure_ascii=False, indent=2) + '\n').encode()
    return {'envelope_version': 1, 'project_id': 'demo', 'provider': 'local-ci',
            'source_run_id': run or uuid4().hex, 'attempt': 1, 'test_id': payload['tc_id'],
            'execution_id': payload['execution']['id'], 'provenance': {'commit': 'synthetic-commit'},
            'evidence_b64': base64.b64encode(raw).decode(), 'files': files or []}, raw


def test_idempotency_conflict_attempt_and_restart(server):
    event, original = envelope()
    code, body = request(server, 'POST', '/projects/demo/results', event)
    assert code == 201
    rid = json.loads(body)['id']
    assert request(server, 'POST', '/projects/demo/results', event)[0] == 200
    changed = deepcopy(event); changed['provenance']['commit'] = 'different'
    assert request(server, 'POST', '/projects/demo/results', changed)[0] == 409
    server.stop(); server.start()
    code, body = request(server, 'GET', f'/projects/demo/results/{rid}')
    assert code == 200 and base64.b64decode(json.loads(body)['envelope']['evidence_b64']) == original
    assert request(server, 'POST', '/projects/demo/results', event)[0] == 200
    changed = deepcopy(event); changed['attempt'] = 2
    changed['execution_id'] = 'new-execution'
    payload = json.loads(original); payload['execution']['id'] = 'new-execution'
    changed['evidence_b64'] = base64.b64encode(json.dumps(payload).encode()).decode()
    code, body = request(server, 'POST', '/projects/demo/results', changed)
    assert code == 201 and json.loads(body)['id'] != rid


def test_concurrent_same_event_is_one_row(server):
    event, _ = envelope()
    with ThreadPoolExecutor(max_workers=6) as pool:
        replies = list(pool.map(lambda _: request(server, 'POST', '/projects/demo/results', event), range(12)))
    assert sorted(code for code, _ in replies) == [200] * 11 + [201]
    assert len({json.loads(body)['id'] for _, body in replies}) == 1


@pytest.mark.parametrize('token,project', [(None, 'demo'), ('wrong-token', 'demo'), (TOKEN, 'other')],
                         ids=['missing', 'wrong', 'cross_project'])
def test_all_data_routes_reject_wrong_project_or_auth(server, token, project):
    event, _ = envelope(); event['project_id'] = project
    for method, path, data in [('POST', f'/projects/{project}/results', event),
        ('GET', f'/projects/{project}/results', None), ('GET', f'/projects/{project}/results/unknown', None),
        ('PUT', f'/projects/{project}/results/unknown/files?path=page.png', b'x'),
        ('GET', f'/projects/{project}/results/unknown/files?path=page.png', None)]:
        assert request(server, method, path, data, token=token)[0] in (401, 403)


@pytest.mark.parametrize('change', ['attempt', 'id', 'date', 'schema', 'phase', 'version', 'project', 'b64'])
def test_invalid_metadata_is_rejected(server, change):
    event, raw = envelope()
    payload = json.loads(raw)
    if change == 'attempt': event['attempt'] = True
    elif change == 'id': event['execution_id'] = 'different'
    elif change == 'project': event['project_id'] = 'other'
    elif change == 'b64': event['evidence_b64'] = '!'
    else:
        if change == 'date': payload['execution']['started_at'] = 'yesterday'
        elif change == 'schema': del payload['result']['business']
        elif change == 'phase': payload['result']['pytest']['status'] = 'passed'
        elif change == 'version': payload['contract_version'] = '999'
        event['evidence_b64'] = base64.b64encode(json.dumps(payload).encode()).decode()
    assert request(server, 'POST', '/projects/demo/results', event)[0] == 422


@pytest.mark.parametrize('path', ['../page.png', '/page.png', 'C:/outside.png', 'archive/../page.png',
                                  'archive\\page.png', 'evidence.json', 'x.exe'])
def test_attachment_inventory_rejects_unsafe_paths(server, path):
    event, _ = envelope(files=[{'path': path, 'size': 1, 'sha256': hashlib.sha256(b'x').hexdigest()}])
    assert request(server, 'POST', '/projects/demo/results', event)[0] == 422


def test_partial_upload_hash_disconnect_and_immutable_attachment(server):
    png = b'\x89PNG\r\n\x1a\nsynthetic-test-bytes'
    event, raw = envelope(files=[{'path': 'page.png', 'size': len(png), 'sha256': hashlib.sha256(png).hexdigest()}])
    _, body = request(server, 'POST', '/projects/demo/results', event); rid = json.loads(body)['id']
    path = f'/projects/demo/results/{rid}/files?path=page.png'
    assert request(server, 'GET', path)[0] == 404
    assert request(server, 'PUT', path, b'wrong-hash')[0] == 422
    conn = http.client.HTTPConnection('127.0.0.1', server.port)
    conn.putrequest('PUT', path); conn.putheader('Authorization', 'Bearer ' + TOKEN)
    conn.putheader('Content-Length', str(len(png))); conn.endheaders(); conn.send(png[:5]); conn.close()
    time.sleep(.1)
    assert request(server, 'GET', path)[0] == 404
    assert request(server, 'PUT', path, png)[0] == 200
    assert request(server, 'PUT', path, png)[0] == 200
    assert request(server, 'GET', path) == (200, png)
    assert request(server, 'PUT', path, png + b'changed')[0] == 422
    _, body = request(server, 'GET', f'/projects/demo/results/{rid}')
    assert base64.b64decode(json.loads(body)['envelope']['evidence_b64']) == raw


def test_metadata_and_attachment_size_boundaries(tmp_path):
    assert importlib.util.find_spec('signup031.ingestion_server') is not None
    small = ServerProcess(tmp_path / 'small', max_envelope_bytes=3000, max_attachment_bytes=32, max_total_bytes=40).start()
    try:
        assert request(small, 'POST', '/projects/demo/results', b'x' * 3001)[0] == 413
        event, _ = envelope(files=[{'path': 'page.png', 'size': 33, 'sha256': '0' * 64}])
        assert request(small, 'POST', '/projects/demo/results', event)[0] == 422
        event, _ = envelope(files=[{'path': 'page.png', 'size': 24, 'sha256': '0' * 64},
                                  {'path': 'other.png', 'size': 24, 'sha256': '0' * 64}])
        assert request(small, 'POST', '/projects/demo/results', event)[0] == 422
        event, _ = envelope(files=[{'path': 'page.png', 'size': 1, 'sha256': '0' * 64}])
        _, body = request(small, 'POST', '/projects/demo/results', event)
        assert request(small, 'PUT', '/projects/demo/results/' + json.loads(body)['id'] + '/files?path=page.png', b'x' * 33)[0] == 413
    finally: small.stop()


def test_unknown_size_upload_enforces_cumulative_bytes_and_missing_manifest_is_partial(tmp_path):
    small = ServerProcess(tmp_path / 'unknown-size', max_total_bytes=40).start()
    try:
        one, two = b'\x89PNG\r\n\x1a\n' + b'a' * 17, b'\x89PNG\r\n\x1a\n' + b'b' * 17
        event, _ = envelope(files=[{'path': 'one.png', 'size': None, 'sha256': hashlib.sha256(one).hexdigest()},
                                    {'path': 'two.png', 'size': None, 'sha256': hashlib.sha256(two).hexdigest()}])
        code, raw = request(small, 'POST', '/projects/demo/results', event)
        assert code == 201
        rid = json.loads(raw)['id']
        first = f'/projects/demo/results/{rid}/files?path=one.png'
        second = f'/projects/demo/results/{rid}/files?path=two.png'
        assert request(small, 'PUT', first, one) == (200, b'{"status":"saved"}')
        assert request(small, 'PUT', second, two)[0] in (413, 422)
        assert request(small, 'GET', first) == (200, one)
        assert request(small, 'GET', second)[0] == 404
        event, original = envelope()
        payload = json.loads(original)
        payload['replay'] = {'status': 'recorded', 'manifest': 'archive/manifest.json', 'reason': None}
        event['evidence_b64'] = base64.b64encode(json.dumps(payload).encode()).decode()
        code, raw = request(small, 'POST', '/projects/demo/results', event)
        assert code == 422 or json.loads(raw)['transfer_status'] == 'partial'
    finally: small.stop()


def test_har_embedded_authentication_token_is_not_stored(server):
    encoded = base64.b64encode(TOKEN.encode()).decode()
    har = json.dumps({'log': {'entries': [{'response': {'content': {'encoding': 'base64', 'text': encoded}}}]}}).encode()
    event, _ = envelope(files=[{'path': 'archive/resources.har', 'size': len(har), 'sha256': hashlib.sha256(har).hexdigest()}])
    _, raw = request(server, 'POST', '/projects/demo/results', event)
    rid = json.loads(raw)['id']
    code, _ = request(server, 'PUT', f'/projects/demo/results/{rid}/files?path=archive%2Fresources.har', har)
    assert code == 422
    assert request(server, 'GET', f'/projects/demo/results/{rid}/files?path=archive%2Fresources.har')[0] == 404


def test_pagination_is_bounded_and_project_scoped(server):
    for _ in range(3): request(server, 'POST', '/projects/demo/results', envelope()[0])
    _, body = request(server, 'GET', '/projects/demo/results?limit=2')
    first = json.loads(body); assert len(first['items']) == 2 and first['next_cursor']
    _, body = request(server, 'GET', '/projects/demo/results?limit=2&cursor=' + str(first['next_cursor']))
    second = json.loads(body)
    assert not {row['id'] for row in first['items']} & {row['id'] for row in second['items']}
    assert request(server, 'GET', '/projects/demo/results?limit=101')[0] == 422
    assert request(server, 'GET', '/projects/demo/results?cursor=wrong')[0] == 422


def test_cli_client_partial_cache_qa_preservation_and_server_restart(server, tmp_path):
    from signup031.ingestion_client import IngestionClient
    from signup031.viewer_model import load_evidence_root
    from signup031.investigation import InvestigationStore
    path = tmp_path / 'original' / 'evidence.json'; path.parent.mkdir()
    payload = _current_payload(execution_id=uuid4().hex)
    payload['result']['business'].update(status='failed', message='CI original assertion')
    payload['result']['pytest']['status'] = 'failed'
    path.write_text(json.dumps(payload)); original = path.read_bytes()
    result = subprocess.run([sys.executable, '-m', 'signup031.ingestion_client', 'send',
        '--url', server.url, '--project', 'demo', '--run', uuid4().hex, '--provider', 'local-ci', '--folder', str(path.parent),
        '--repository', 'synthetic/repository', '--workflow', 'test-suite', '--commit', 'abc123'],
        env={**os.environ, 'QA_RESULT_TOKEN': TOKEN}, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    assert TOKEN.encode() not in result.stdout + result.stderr
    client = IngestionClient(server.url, 'demo', TOKEN)
    cache = tmp_path / 'pc'; client.download(cache)
    record = next(r for r in load_evidence_root(cache) if r.execution_id == payload['execution']['id'])
    assert record.business_status == 'failed' and record.source_path.read_bytes() == original
    sidecar = json.loads((record.source_path.parent / 'remote-source.json').read_text())
    assert sidecar['provenance'] == {'repository': 'synthetic/repository', 'workflow': 'test-suite', 'commit': 'abc123'}
    store = InvestigationStore(cache); note = store.load(record.execution_id); note['notes'] = 'QA local note'; store.save(note)
    before = {p: p.read_bytes() for p in cache.rglob('*') if p.is_file()}
    client.download(cache)
    assert all(p.read_bytes() == value for p, value in before.items())
    assert path.read_bytes() == original
    assert all(TOKEN.encode() not in p.read_bytes() for p in cache.rglob('*') if p.is_file())
    assert TOKEN.encode() not in server.log.read_bytes()


def test_client_refuses_remote_http_and_cross_host_redirect(tmp_path):
    from signup031.ingestion_client import IngestionClient, TransferError
    with pytest.raises(TransferError, match='HTTPS'):
        IngestionClient('http://example.com', 'demo', TOKEN)
    with pytest.raises(TransferError, match='token'):
        IngestionClient('http://127.0.0.1:8765', 'demo', 'bad\r\nHeader: injected')
    class Sink(BaseHTTPRequestHandler):
        received = []
        def do_GET(self):
            Sink.received.append(self.headers.get('Authorization'))
            self.send_response(200); self.end_headers(); self.wfile.write(b'{}')
        def log_message(self, *args): pass
    sink = ThreadingHTTPServer(('127.0.0.1', 0), Sink)
    sink_thread = Thread(target=sink.serve_forever, daemon=True); sink_thread.start()
    class Redirect(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header('Location', f'http://127.0.0.1:{sink.server_port}/token')
            self.end_headers()
        def log_message(self, *args): pass
    front = ThreadingHTTPServer(('127.0.0.1', 0), Redirect)
    front_thread = Thread(target=front.serve_forever, daemon=True); front_thread.start()
    try:
        client = IngestionClient(f'http://127.0.0.1:{front.server_port}', 'demo', TOKEN)
        with pytest.raises(TransferError, match='redirect'):
            client.list_results()
        assert Sink.received == []
    finally:
        front.shutdown(); front.server_close(); front_thread.join()
        sink.shutdown(); sink.server_close(); sink_thread.join()


def test_cli_does_not_echo_token_from_a_rejected_source(tmp_path):
    folder = tmp_path / 'source'; folder.mkdir()
    (folder / 'evidence.json').write_text('{"secret":"' + TOKEN + '","contract_version":"999"}')
    result = subprocess.run([sys.executable, '-m', 'signup031.ingestion_client', 'send',
        '--url', 'http://127.0.0.1:9', '--project', 'demo', '--provider', 'local-ci',
        '--run', 'reject', '--folder', str(folder)], env={**os.environ, 'QA_RESULT_TOKEN': TOKEN},
        capture_output=True, timeout=10)
    assert result.returncode == 2
    assert TOKEN.encode() not in result.stdout + result.stderr


def test_download_has_total_deadline_even_while_http_response_trickles(tmp_path):
    from signup031.ingestion_client import IngestionClient, TransferError
    class Trickle(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers()
            for _ in range(30):
                try: self.wfile.write(b' '); self.wfile.flush()
                except OSError: break
                time.sleep(.03)
        def log_message(self, *args): pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Trickle)
    thread = Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        client = IngestionClient(f'http://127.0.0.1:{server.server_port}', 'demo', TOKEN)
        start = time.monotonic()
        with pytest.raises(TransferError, match='deadline'):
            client.download(tmp_path / 'pc', max_seconds=.15)
        assert time.monotonic() - start < .6
    finally:
        server.shutdown(); server.server_close(); thread.join()


def test_server_owned_attachment_directory_junction_cannot_escape(tmp_path):
    receiver = ServerProcess(tmp_path / 'receiver').start()
    try:
        outside = tmp_path / 'outside'; outside.mkdir()
        link = receiver.root / 'store' / 'attachments'
        subprocess.run(['cmd', '/c', 'mklink', '/J', str(link), str(outside)],
                       capture_output=True, check=True)
        png = b'\x89PNG\r\n\x1a\nlocal'
        event, _ = envelope(files=[{'path': 'page.png', 'size': len(png), 'sha256': hashlib.sha256(png).hexdigest()}])
        code, raw = request(receiver, 'POST', '/projects/demo/results', event)
        assert code == 201
        rid = json.loads(raw)['id']
        assert request(receiver, 'PUT', f'/projects/demo/results/{rid}/files?path=page.png', png)[0] == 422
        assert list(outside.iterdir()) == []
    finally: receiver.stop()
