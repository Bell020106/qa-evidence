import hashlib
import json
import shutil
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from signup031.browser_check import run_browser_boundary_check


@pytest.mark.parametrize('limit,after', [('', 129), ('maxlength="128"', 128)])
def test_capture_move_server_stop_replay_and_block_network(tmp_path, limit, after):
    from signup031.replay import ReplaySession

    class Handler(BaseHTTPRequestHandler):
        count = 0

        def do_GET(self):
            type(self).count += 1
            body = (f'<label>Password<input id="password" type="password" {limit}></label>'
                    '<output id="count"></output><script src="/app.js"></script>')
            if self.path == '/app.js':
                body = "document.querySelector('input').oninput=e=>document.querySelector('output').textContent=e.target.value.length"
            self.send_response(200)
            self.send_header('Content-Type', 'text/javascript' if self.path == '/app.js' else 'text/html')
            self.send_header('Set-Cookie', 'session=secret')
            self.end_headers()
            self.wfile.write(body.encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f'http://127.0.0.1:{server.server_port}/'
    try:
        result = run_browser_boundary_check(
            target_kind='live', target_url=url, target_name=None, selector='#password',
            label_pattern=None, screenshot_path=tmp_path / 'run' / 'screen.png',
            archive_directory=tmp_path / 'run' / 'archive', execution_id='test-run',
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert result.archive['status'] == 'recorded'
    moved = tmp_path / 'moved'
    shutil.move(str(tmp_path / 'run'), moved)
    root = moved / 'archive'
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in root.iterdir()}
    assert 'session=secret' not in (root / 'resources.har').read_text()
    with ReplaySession(root, headless=True) as replay:
        assert replay.restore()['status'] == 'ready'
        field = replay.page.locator('#password')
        assert len(field.input_value()) == after
        field.press('Backspace')
        assert len(field.input_value()) == after - 1
        field.press_sequentially('XY')
        assert len(field.input_value()) == (130 if after == 129 else 128)
        assert replay.page.locator('#count').inner_text() == str(len(field.input_value()))
        # Start a receiver at another live port: unmatched HTTP/WS must never reach it.
        receiver = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        worker = Thread(target=receiver.serve_forever, daemon=True)
        worker.start()
        count = Handler.count
        try:
            port = receiver.server_port
            replay.page.evaluate(f"fetch('http://127.0.0.1:{port}/missing').catch(()=>null)")
            replay.page.evaluate(f"new WebSocket('ws://127.0.0.1:{port}/socket')")
            replay.page.wait_for_timeout(150)
            assert Handler.count == count
            assert replay.status()['status'] == 'limited'
            assert {event['kind'] for event in replay.blocked} >= {'http', 'websocket'}
        finally:
            receiver.shutdown()
            receiver.server_close()
            worker.join()
    assert {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in root.iterdir()} == before
