import json
import shutil
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest


@pytest.fixture
def manual_site():
    class Handler(BaseHTTPRequestHandler):
        count = 0
        def do_GET(self):
            Handler.count += 1
            body = '''<label>Query<input id="query"></label><button id="apply">Apply</button>
            <p id="result">Initial</p><button id="replace">Replace input</button>
            <input type="password" id="password"><input data-sensitive id="secret">
            <input id="dup"><input id="dup">
            <script>
            document.querySelector('#apply').onclick=()=>document.querySelector('#result').textContent=document.querySelector('#query').value;
            document.querySelector('#replace').onclick=()=>document.querySelector('#query').outerHTML='<input id="query">';
            </script>'''
            if self.path == '/note':
                body = '''<textarea id="note"></textarea><button id="show">Show</button><output></output>
                <script>document.querySelector('button').onclick=()=>document.querySelector('output').textContent=document.querySelector('textarea').value;</script>'''
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


@pytest.mark.parametrize('page_kind', ['search', 'note'])
def test_real_events_saved_from_original_session_and_replayed_after_move(manual_site, tmp_path, page_kind):
    from signup031.manual_recording import ManualRecorder
    from signup031.replay import ReplaySession
    from signup031.viewer_model import load_evidence_root
    base, server, handler = manual_site
    with ManualRecorder(base + '/' + page_kind, '직접 조작', tmp_path / 'runs', headless=True) as recorder:
        page = recorder.page
        if page_kind == 'search':
            page.locator('#query').press_sequentially('old text')
            page.locator('#replace').click()
            page.locator('#query').fill('검색 배')
            page.locator('#query').press('Backspace')
            page.locator('#query').press_sequentially('pear')
            page.locator('#apply').click()
            expected = '검색 pear'
        else:
            page.locator('#note').fill('붙여넣은 텍스트')
            page.locator('#note').press('Backspace')
            page.locator('#show').click()
            expected = '붙여넣은 텍스'
        before_save_requests = handler.count
        path = recorder.save()
        assert handler.count == before_save_requests  # save must not re-run the site
        assert recorder.save() == path  # duplicate save is idempotent
    payload = json.loads(path.read_text(encoding='utf-8'))
    assert payload['result']['business']['status'] == 'unjudged'
    assert 'pytest' not in payload['result']
    recording = payload['manual_record']
    fills = [a for a in recording['actions'] if a['action'] == 'fill']
    assert fills[-1]['value'] == expected
    assert len(fills) <= 2
    assert recording['actions'][-1]['action'] == 'click'
    assert recording['limitations'] == []
    server.shutdown()
    server.server_close()
    moved = tmp_path / 'moved'
    shutil.move(path.parent, moved)
    original = {p.relative_to(moved): p.read_bytes() for p in moved.rglob('*') if p.is_file()}
    with ReplaySession(moved / 'archive', headless=True) as replay:
        assert replay.restore()['status'] == 'ready'
        assert expected in replay.page.inner_text('body')
        field = replay.page.locator('#query' if page_kind == 'search' else '#note')
        field.fill('changed offline')
        replay.page.locator('#apply' if page_kind == 'search' else '#show').click()
        assert 'changed offline' in replay.page.inner_text('body')
        replay.page.evaluate("fetch('/not-recorded').catch(()=>null)")
        replay.page.evaluate("new WebSocket('ws://127.0.0.1:9/missing')")
        replay.page.wait_for_timeout(100)
        assert replay.status()['status'] == 'limited'
    assert {p.relative_to(moved): p.read_bytes() for p in moved.rglob('*') if p.is_file()} == original
    record = load_evidence_root(moved)[0]
    assert record.business_status == 'unjudged'
    assert record.status_label == '수동 기록 · 판정 미입력'
    assert record.archive_root == moved / 'archive'


def test_sensitive_and_ambiguous_elements_are_omitted_with_reasons(manual_site, tmp_path):
    from signup031.manual_recording import ManualRecorder
    from signup031.replay import ReplaySession
    with ManualRecorder(manual_site[0], '부분 기록', tmp_path, headless=True) as recorder:
        recorder.page.locator('#password').fill('DO-NOT-RECORD-123')
        recorder.page.locator('#secret').fill('PRIVATE-VALUE-456')
        recorder.page.locator('#dup').nth(0).fill('ambiguous')
        recorder.page.evaluate("document.body.insertAdjacentHTML('beforeend','<iframe srcdoc=\"frame\"></iframe>')")
        recorder.page.wait_for_timeout(50)
        path = recorder.save()
    payload = json.loads(path.read_text(encoding='utf-8'))
    serialized = json.dumps(payload['manual_record'])
    assert 'DO-NOT-RECORD-123' not in serialized and 'PRIVATE-VALUE-456' not in serialized
    reasons = payload['manual_record']['limitations']
    assert any('sensitive' in reason for reason in reasons)
    assert any('ambiguous' in reason for reason in reasons)
    assert any('iframe' in reason for reason in reasons)
    with ReplaySession(path.parent / 'archive', headless=True) as replay:
        assert replay.restore()['status'] == 'limited'


def test_early_close_is_not_a_successful_save(manual_site, tmp_path):
    from signup031.manual_recording import ManualRecorder
    with ManualRecorder(manual_site[0], '조기 종료', tmp_path, headless=True) as recorder:
        recorder.page.close()
        path = recorder.save()
    payload = json.loads(path.read_text(encoding='utf-8'))
    assert payload['recording']['status'] == 'failed'
    assert payload['replay']['status'] == 'failed'


def test_ime_completion_and_pending_final_input_are_saved(manual_site, tmp_path):
    from signup031.manual_recording import ManualRecorder
    with ManualRecorder(manual_site[0] + '/note', '입력 완료', tmp_path, headless=True) as recorder:
        field = recorder.page.locator('#note')
        field.focus()
        field.dispatch_event('compositionstart')
        recorder.page.keyboard.insert_text('한글 입력')
        field.dispatch_event('compositionend', {'data':'한글 입력'})
        path = recorder.save()
    record = json.loads(path.read_text(encoding='utf-8'))['manual_record']
    assert [a['value'] for a in record['actions'] if a['action'] == 'fill'] == ['한글 입력']
    assert record['observed']['inputs'][0]['value'] == '한글 입력'


def test_unsupported_new_tab_navigation_and_file_upload_are_reported(manual_site, tmp_path):
    from signup031.manual_recording import ManualRecorder
    with ManualRecorder(manual_site[0], '지원 범위', tmp_path, headless=True) as recorder:
        recorder.page.evaluate("document.body.insertAdjacentHTML('beforeend','<input type=file id=file>')")
        recorder.page.locator('#file').set_input_files({'name':'local.txt','mimeType':'text/plain','buffer':b'private file'})
        recorder.page.evaluate("document.body.insertAdjacentHTML('beforeend','<iframe srcdoc=\"frame\"></iframe>')")
        recorder.context.new_page()
        recorder.page.goto(manual_site[0] + '/note')
        path = recorder.save()
    payload = json.loads(path.read_text(encoding='utf-8'))
    reasons = payload['manual_record']['limitations']
    assert any('new tab' in s for s in reasons)
    assert any('navigation' in s for s in reasons)
    assert any('unsupported input' in s for s in reasons)
    assert any('iframe' in s for s in reasons)
    assert payload['recording']['status'] == 'limited'


def test_save_failure_keeps_original_actions_and_disables_replay(manual_site, tmp_path):
    from signup031.manual_recording import ManualRecorder
    with ManualRecorder(manual_site[0], '저장 오류', tmp_path, headless=True) as recorder:
        recorder.page.locator('#query').fill('keep original')
        (recorder.root / 'page.png').mkdir()  # real file-system failure, not mocked browser
        path = recorder.save()
    payload = json.loads(path.read_text(encoding='utf-8'))
    assert payload['recording']['status'] == 'failed'
    assert payload['manual_record']['actions'][-1]['value'] == 'keep original'
    assert payload['replay']['status'] == 'failed'


def test_manual_observation_mismatch_fails_restore(manual_site, tmp_path):
    from signup031.manual_recording import ManualRecorder
    from signup031.replay import ReplaySession
    from signup031.viewer_model import load_evidence_root
    with ManualRecorder(manual_site[0] + '/note', '불일치', tmp_path, headless=True) as recorder:
        recorder.page.locator('#note').fill('original')
        path = recorder.save()
    manifest_path = path.parent / 'archive' / 'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    manifest['manual_record']['observed']['inputs'][0]['value'] = 'different'
    manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
    assert load_evidence_root(tmp_path)[0].archive_root is None
    with ReplaySession(manifest_path.parent, headless=True) as replay:
        with pytest.raises(ValueError, match='saved input differs'):
            replay.restore()


@pytest.mark.parametrize('fail_start', [False, True])
def test_unsaved_close_removes_owned_har_and_preserves_saved_runs(manual_site, tmp_path, fail_start):
    from signup031.manual_recording import ManualRecorder
    with ManualRecorder(manual_site[0], 'previous', tmp_path, headless=True) as previous:
        saved = previous.save()
    original = {p: p.read_bytes() for p in saved.parent.rglob('*') if p.is_file()}
    url = 'http://127.0.0.1:1/unavailable' if fail_start else manual_site[0]
    recorder = ManualRecorder(url, 'unsaved', tmp_path, headless=True)
    if fail_start:
        with pytest.raises(Exception, match='net::'):
            recorder.__enter__()
    else:
        with recorder:
            recorder.page.locator('#query').fill('unsaved private input')
    assert not list(recorder.root.rglob('*.har'))
    assert not list(recorder.root.rglob('evidence.json'))
    assert all(p.read_bytes() == contents for p, contents in original.items())
