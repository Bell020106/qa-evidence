"""Catch hard-coded TC behavior, lost snapshots, and unsafe offline fallbacks."""
from copy import deepcopy
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shutil
from threading import Thread

import pytest


@pytest.fixture
def web_site():
    class Handler(BaseHTTPRequestHandler):
        requests = 0

        def do_GET(self):
            Handler.requests += 1
            if self.path == '/password':
                body = '<label>Password<input id="password" type="password"></label>'
            else:
                body = '''<label>Search<input id="query"></label><button id="filter">Filter</button>
                <p id="result" hidden></p><script>
                document.querySelector('button').onclick=()=>{
                  const r=document.querySelector('p'); r.hidden=false;
                  r.textContent=document.querySelector('input').value==='pear'?'Pear: 2':'No matches';
                };</script>'''
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(body.encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f'http://127.0.0.1:{server.server_port}', server, Handler
    server.shutdown()
    server.server_close()
    thread.join()


def scenario_for(base, name):
    if name == 'password':
        return {'version': 1, 'id': 'SIGNUP-031', 'title': 'Password boundary',
                'url': base + '/password',
                'steps': [{'action': 'wait', 'locator': 'label', 'target': 'Password', 'value': ''},
                          {'action': 'fill', 'locator': 'css', 'target': '#password', 'value': 'a' * 128},
                          {'action': 'press', 'locator': 'css', 'target': '#password', 'value': 'Z'}],
                'checks': [{'kind': 'input_length', 'locator': 'css', 'target': '#password', 'expected': 128}]}
    return {'version': 1, 'id': 'FILTER-007', 'title': 'Fruit filtering', 'url': base + '/search',
            'steps': [{'action': 'fill', 'locator': 'label', 'target': 'Search', 'value': 'pear'},
                      {'action': 'click', 'locator': 'css', 'target': '#filter', 'value': ''}],
            'checks': [{'kind': 'text', 'locator': 'css', 'target': '#result', 'expected': 'Pear: 2'},
                       {'kind': 'visible', 'locator': 'css', 'target': '#result', 'expected': True}]}


@pytest.mark.parametrize('name,status,actual', [('password', 'failed', 129), ('search', 'passed', 'Pear: 2')])
def test_same_engine_records_snapshot_moves_and_replays_offline(web_site, tmp_path, name, status, actual):
    from signup031.web_runner import run_scenario
    from signup031.replay import ReplaySession
    from signup031.viewer_model import load_evidence_root
    base, server, handler = web_site
    config = scenario_for(base, name)
    original = deepcopy(config)
    result_path = run_scenario(config, tmp_path / 'runs')
    evidence = json.loads(result_path.read_text())
    assert evidence['result']['business']['status'] == status
    assert evidence['checks'][0]['actual'] == actual
    assert evidence['replay']['status'] == 'recorded'
    config['steps'][0]['value'] = 'changed later'
    assert evidence['scenario_snapshot'] == original
    server.shutdown()
    server.server_close()
    moved = tmp_path / 'moved'
    shutil.move(result_path.parent, moved)
    before = {p.relative_to(moved): p.read_bytes() for p in moved.rglob('*') if p.is_file()}
    count = handler.requests
    with ReplaySession(moved / 'archive', headless=True) as replay:
        assert replay.restore()['status'] == 'ready'
        if name == 'password':
            field = replay.page.locator('#password')
            field.press('Backspace')
            assert len(field.input_value()) == 128
        else:
            replay.page.get_by_label('Search').fill('apple')
            replay.page.locator('#filter').click()
            assert replay.page.locator('#result').inner_text() == 'No matches'
        replay.page.evaluate("fetch('/unrecorded').catch(()=>null)")
        assert replay.status()['status'] == 'limited'
    assert handler.requests == count
    assert {p.relative_to(moved): p.read_bytes() for p in moved.rglob('*') if p.is_file()} == before
    records = load_evidence_root(moved)
    assert len(records) == 1
    assert records[0].business_status == status
    assert records[0].tc_id == original['id']
    assert records[0].title == original['title']


@pytest.mark.parametrize('mutation,reason', [
    (lambda s: s.update(url='file:///secret'), 'URL'),
    (lambda s: s['steps'][0].update(action='javascript'), 'action'),
    (lambda s: s['steps'][0].update(target=''), 'target'),
    (lambda s: s['checks'][0].update(expected=True), 'expected'),
    (lambda s: s.update(code='print(1)'), 'unknown'),
])
def test_invalid_scenario_rejected_before_execution(mutation, reason):
    from signup031.web_scenario import validate_scenario
    config = scenario_for('http://127.0.0.1:1234', 'password')
    mutation(config)
    with pytest.raises(ValueError, match=reason):
        validate_scenario(config)


def test_bad_selector_is_preparation_failure_not_assertion_failure(web_site, tmp_path):
    from signup031.web_runner import run_scenario
    base, _, _ = web_site
    config = scenario_for(base, 'search')
    config['steps'][0]['target'] = '['
    config['steps'][0]['locator'] = 'css'
    payload = json.loads(run_scenario(config, tmp_path).read_text())
    assert payload['result']['business']['status'] == 'preparation_failed'
    assert 'step 1' in payload['result']['business']['message']
    assert payload['replay']['status'] == 'failed'


def test_result_cannot_replay_a_different_scenario_with_same_execution_id(web_site, tmp_path):
    from signup031.web_runner import run_scenario
    from signup031.viewer_model import load_evidence_root
    config = scenario_for(web_site[0], 'search')
    path = run_scenario(config, tmp_path)
    payload = json.loads(path.read_text())
    payload['scenario_snapshot']['title'] = 'Different scenario snapshot'
    path.write_text(json.dumps(payload), encoding='utf-8')
    record = load_evidence_root(tmp_path)[0]
    assert record.business_status == 'passed'
    assert record.archive_root is None
    assert 'scenario' in record.archive_message


def test_new_archive_missing_resource_disables_replay(web_site, tmp_path):
    from signup031.web_runner import run_scenario
    from signup031.viewer_model import load_evidence_root
    config = scenario_for(web_site[0], 'search')
    path = run_scenario(config, tmp_path)
    (path.parent / 'archive' / 'resources.har').unlink()
    record = load_evidence_root(tmp_path)[0]
    assert record.business_status == 'passed'
    assert record.archive_root is None
    assert 'FileNotFoundError' in record.archive_message
