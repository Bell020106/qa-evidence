"""GitHub-like local subprocess boundary for CI verdicts and receiver delivery."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from test_ingestion import ServerProcess, TOKEN
from test_ingestion_desktop import v4_folder


def invoke(*args, env=None):
    return subprocess.run([sys.executable, '-m', 'signup031.ci_delivery', *map(str, args)],
                          env={**os.environ, **(env or {})}, capture_output=True, timeout=30)


def test_test_entrypoint_preserves_pass_fail_and_preparation_error(tmp_path):
    passed = tmp_path / 'test_pass.py'
    passed.write_text('def test_ok(): assert True\n')
    failed = tmp_path / 'test_fail.py'
    failed.write_text('def test_wrong(): assert False, "original assertion"\n')
    for target, name, code, status in ((passed, 'pass', 0, 'passed'),
                                       (failed, 'fail', 1, 'failed'),
                                       (tmp_path / 'missing.py', 'prepare', 4, 'preparation_error')):
        output = tmp_path / name
        result = invoke('test', '--output', output, '--target', target)
        assert result.returncode == code, result.stdout + result.stderr
        summary = json.loads((output / 'test-status.json').read_text())
        assert summary['pytest_exit_code'] == code
        assert summary['test_status'] == status
        assert (output / summary['pytest_log']).is_file()
        assert summary['capture_id'] in summary['pytest_log']
        evidence=list((output / 'evidence').rglob('evidence.json'))
        if status=='failed':
            assert len(evidence)==1
            payload=json.loads(evidence[0].read_text(encoding='utf-8'))
            assert payload['result']['pytest']['status']=='failed'
            assert payload['result']['pytest']['session_exitcode']==1
        else:
            assert evidence==[]
        if code in (0, 1):
            assert (output / summary['junit_path']).is_file()
            assert summary['capture_id'] in summary['junit_path']


def test_send_entrypoint_uses_actual_cli_and_preserves_github_identity(tmp_path):
    receiver = ServerProcess(tmp_path / 'receiver').start()
    output = tmp_path / 'ci'
    environment = {'QA_SERVER_URL': receiver.url, 'QA_PROJECT_ID': 'demo', 'QA_RESULT_TOKEN': TOKEN,
                   'GITHUB_RUN_ID': '12345', 'GITHUB_RUN_ATTEMPT': '2',
                   'GITHUB_REPOSITORY': 'team/sample', 'GITHUB_SHA': 'a' * 40,
                   'GITHUB_WORKFLOW': 'Product regression', 'GITHUB_JOB': 'regression'}
    try:
        marker = tmp_path / 'test_ok.py'; marker.write_text('def test_ok(): assert True\n')
        assert invoke('test', '--output', output, '--target', marker, env=environment).returncode == 0
        capture = json.loads((output / 'test-status.json').read_text())['capture_id']
        v4_folder(output / 'evidence' / capture / 'source')
        first = invoke('send', '--output', output, env=environment)
        assert first.returncode == 0, first.stdout + first.stderr
        summary = json.loads((output / 'transfer-status.json').read_text())
        assert summary['transfer_status'] == 'complete' and len(summary['results']) == 1
        assert TOKEN.encode() not in first.stdout + first.stderr
        assert TOKEN not in (output / 'transfer-status.json').read_text()
        second = invoke('send', '--output', output, env=environment)
        assert second.returncode == 0
        second_summary = json.loads((output / 'transfer-status.json').read_text())
        assert second_summary['results'][0]['id'] == summary['results'][0]['id']
        assert second_summary['results'][0]['metadata_status'] == 'duplicate'
        changed_env = {**environment, 'GITHUB_RUN_ATTEMPT': '3'}
        reused = invoke('send', '--output', output, env=changed_env)
        assert reused.returncode == 2
        assert json.loads((output / 'transfer-status.json').read_text())['transfer_status'] == 'invalid_identity'
        new_output = tmp_path / 'new-attempt'
        assert invoke('test', '--output', new_output, '--target', marker, env=changed_env).returncode == 0
        new_capture = json.loads((new_output / 'test-status.json').read_text())['capture_id']
        v4_folder(new_output / 'evidence' / new_capture / 'source')
        third = invoke('send', '--output', new_output, env=changed_env)
        assert third.returncode == 0
        third_summary = json.loads((new_output / 'transfer-status.json').read_text())
        assert third_summary['results'][0]['id'] != summary['results'][0]['id']

        other_env = {**environment, 'GITHUB_REPOSITORY': 'other/sample'}
        other_output = tmp_path / 'other-repository'
        assert invoke('test', '--output', other_output, '--target', marker, env=other_env).returncode == 0
        other_capture = json.loads((other_output / 'test-status.json').read_text())['capture_id']
        v4_folder(other_output / 'evidence' / other_capture / 'source')
        assert invoke('send', '--output', other_output, env=other_env).returncode == 0

        collision_env = {**environment, 'GITHUB_WORKFLOW': 'Changed workflow'}
        collision_output = tmp_path / 'collision'
        assert invoke('test', '--output', collision_output, '--target', marker, env=collision_env).returncode == 0
        collision_capture = json.loads((collision_output / 'test-status.json').read_text())['capture_id']
        v4_folder(collision_output / 'evidence' / collision_capture / 'source')
        assert invoke('send', '--output', collision_output, env=collision_env).returncode == 2
        assert json.loads((collision_output / 'transfer-status.json').read_text())['transfer_status'] == 'failed'

        from signup031.ingestion_client import IngestionClient
        rows = IngestionClient(receiver.url, 'demo', TOKEN).list_results()['items']
        assert len(rows) == 3
        for row in rows:
            envelope = row['envelope']
            assert envelope['source_run_id'] in ('team/sample:12345:regression',
                                                 'other/sample:12345:regression')
            assert envelope['attempt'] in (2, 3)
            assert envelope['provenance'] == {
                'repository': envelope['source_run_id'].split(':')[0], 'workflow': 'Product regression',
                'commit': 'a' * 40, 'job': 'regression', 'github_run_id': '12345'}
    finally: receiver.stop()


def test_send_entrypoint_reports_missing_token_server_error_and_no_evidence(tmp_path):
    output = tmp_path / 'ci'
    common = {'QA_SERVER_URL': 'http://127.0.0.1:1', 'QA_PROJECT_ID': 'demo',
              'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '1',
              'GITHUB_REPOSITORY': 'team/repo', 'GITHUB_SHA': 'b' * 40,
              'GITHUB_WORKFLOW': 'Demo', 'GITHUB_JOB': 'demo'}
    marker = tmp_path / 'test_ok.py'; marker.write_text('def test_ok(): assert True\n')
    assert invoke('test', '--output', output, '--target', marker, env=common).returncode == 0
    capture = json.loads((output / 'test-status.json').read_text())['capture_id']
    v4_folder(output / 'evidence' / capture / 'source')
    missing = invoke('send', '--output', output, env={**common, 'QA_RESULT_TOKEN': ''})
    assert missing.returncode == 2
    assert json.loads((output / 'transfer-status.json').read_text())['transfer_status'] == 'unconfigured'
    unavailable = invoke('send', '--output', output, env={**common, 'QA_RESULT_TOKEN': TOKEN})
    assert unavailable.returncode == 2
    assert json.loads((output / 'transfer-status.json').read_text())['transfer_status'] == 'failed'
    assert TOKEN.encode() not in unavailable.stdout + unavailable.stderr

    empty = tmp_path / 'empty'
    assert invoke('test', '--output', empty, '--target', marker, env=common).returncode == 0
    no_result = invoke('send', '--output', empty, env={**common, 'QA_RESULT_TOKEN': TOKEN})
    assert no_result.returncode == 2
    assert json.loads((empty / 'transfer-status.json').read_text())['transfer_status'] == 'no_evidence'


def test_new_preparation_failure_never_sends_stale_evidence_from_reused_root(tmp_path):
    receiver = ServerProcess(tmp_path / 'receiver').start()
    output = tmp_path / 'reused'; v4_folder(output / 'evidence' / 'previous')
    environment = {'QA_SERVER_URL': receiver.url, 'QA_PROJECT_ID': 'demo', 'QA_RESULT_TOKEN': TOKEN,
                   'GITHUB_RUN_ID': '777', 'GITHUB_RUN_ATTEMPT': '2',
                   'GITHUB_REPOSITORY': 'team/sample', 'GITHUB_SHA': 'c' * 40,
                   'GITHUB_WORKFLOW': 'Product regression', 'GITHUB_JOB': 'regression'}
    try:
        failed = invoke('test', '--output', output, '--target', tmp_path / 'missing.py', env=environment)
        assert failed.returncode == 4
        assert json.loads((output / 'test-status.json').read_text())['test_status'] == 'preparation_error'
        sent = invoke('send', '--output', output, env=environment)
        assert sent.returncode == 0
        assert json.loads((output / 'transfer-status.json').read_text())['transfer_status'] == 'not_attempted_preparation_error'
        from signup031.ingestion_client import IngestionClient
        assert IngestionClient(receiver.url, 'demo', TOKEN).list_results()['items'] == []
    finally: receiver.stop()


def test_in_progress_run_blocks_stale_delivery_before_new_pytest_exits(tmp_path):
    receiver = ServerProcess(tmp_path / 'receiver').start()
    output = tmp_path / 'reused'; v4_folder(output / 'evidence' / 'previous')
    slow = tmp_path / 'test_slow.py'
    slow.write_text('import os, time\ndef test_slow():\n'
                    '    time.sleep(2)\n'
                    '    assert "GITHUB_OUTPUT" not in os.environ\n')
    output_file = tmp_path / 'github-output.txt'
    environment = {**os.environ, 'QA_SERVER_URL': receiver.url, 'QA_PROJECT_ID': 'demo',
                   'QA_RESULT_TOKEN': TOKEN, 'GITHUB_RUN_ID': '888', 'GITHUB_RUN_ATTEMPT': '1',
                   'GITHUB_REPOSITORY': 'team/sample', 'GITHUB_SHA': 'e' * 40,
                   'GITHUB_WORKFLOW': 'Product regression', 'GITHUB_JOB': 'regression',
                   'GITHUB_OUTPUT': str(output_file)}
    process = subprocess.Popen([sys.executable, '-m', 'signup031.ci_delivery', 'test',
                                '--output', str(output), '--target', str(slow)],
                               env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status_path = output / 'test-status.json'
            if status_path.is_file() and json.loads(status_path.read_text())['test_status'] == 'in_progress':
                break
            time.sleep(.03)
        current = json.loads(status_path.read_text())
        assert current['test_status'] == 'in_progress'
        while not output_file.is_file() and time.monotonic() < deadline:
            time.sleep(.01)
        assert output_file.read_text().strip() == 'capture_id=' + current['capture_id']
        transfer = invoke('send', '--output', output, env=environment)
        assert transfer.returncode == 2
        assert json.loads((output / 'transfer-status.json').read_text())['transfer_status'] == 'in_progress'
        from signup031.ingestion_client import IngestionClient
        assert IngestionClient(receiver.url, 'demo', TOKEN).list_results()['items'] == []
        assert process.wait(timeout=10) == 0
        assert output_file.read_text().splitlines() == ['capture_id=' + current['capture_id']]
    finally:
        if process.poll() is None: process.kill(); process.wait()
        receiver.stop()


def test_synthetic_browser_success_and_failure_reach_pc_with_original_ci_source(tmp_path):
    from PySide6.QtWidgets import QApplication
    from signup031.ingestion_client import IngestionClient
    from signup031.viewer import EvidenceViewerWindow
    from signup031.viewer_model import load_evidence_root

    receiver = ServerProcess(tmp_path / 'receiver').start()
    synthetic = Path(__file__).resolve().parents[1] / 'review_checks' / 'ci_synthetic_test.py'
    environment = {'QA_SERVER_URL': receiver.url, 'QA_PROJECT_ID': 'demo', 'QA_RESULT_TOKEN': TOKEN,
                   'GITHUB_RUN_ID': '45678', 'GITHUB_RUN_ATTEMPT': '1',
                   'GITHUB_REPOSITORY': 'team/browser-suite', 'GITHUB_SHA': 'd' * 40,
                   'GITHUB_WORKFLOW': 'Controlled demo', 'GITHUB_JOB': 'browser-demo'}
    outputs = []
    try:
        for name, expected_exit, expected_status in [('test_ci_success', 0, 'passed'),
                                                       ('test_ci_intentional_failure', 1, 'failed')]:
            output = tmp_path / name
            original = invoke('test', '--output', output, '--target', f'{synthetic}::{name}', env=environment)
            assert original.returncode == expected_exit, original.stdout + original.stderr
            evidence=list((output / 'evidence').rglob('evidence.json'))
            if expected_status=='passed':
                assert evidence==[]
                not_sent=invoke('send','--output',output,env=environment)
                assert not_sent.returncode==2
                assert json.loads((output/'transfer-status.json').read_text())['transfer_status']=='no_evidence'
                continue
            assert len(evidence)==1
            source=evidence[0]
            payload = json.loads(source.read_text())
            assert payload['result']['pytest']['status'] == expected_status
            assert payload['result']['pytest']['session_exitcode'] == expected_exit
            sent = invoke('send', '--output', output, env=environment)
            assert sent.returncode == 0, sent.stdout + sent.stderr
            outputs.append((source, source.read_bytes()))
        pc = tmp_path / 'pc'
        assert len(IngestionClient(receiver.url, 'demo', TOKEN).download(pc)['complete']) == 1
        records = load_evidence_root(pc)
        assert {r.business_status for r in records} == {'failed'}
        app = QApplication.instance() or QApplication([])
        window = EvidenceViewerWindow(pc, replay_headless=True)
        window.show()
        try:
            assert len(window.records)==1 and window.run_list.count()==1
            for index in range(window.run_list.count()):
                window.run_list.setCurrentRow(index)
                label = window.source_label.text()
                for expected in ('team/browser-suite', '45678', 'Controlled demo', 'browser-demo', 'd' * 40):
                    assert expected in label
                assert ' / 1' in label
        finally: window.close()
        assert all(source.read_bytes() == raw for source, raw in outputs)
        assert all(TOKEN.encode() not in path.read_bytes() for path in pc.rglob('*') if path.is_file())
        assert TOKEN.encode() not in receiver.log.read_bytes()
    finally: receiver.stop()


def test_pytest_timeout_stops_owned_child_and_never_reuses_old_junit(tmp_path):
    output = tmp_path / 'reused'
    marker = tmp_path / 'pid.txt'
    slow = tmp_path / 'test_slow.py'
    slow.write_text('import os, pathlib, time\n'
                    'def test_slow():\n'
                    '    pathlib.Path(os.environ["PID_MARKER"]).write_text(str(os.getpid()))\n'
                    '    time.sleep(10)\n')
    (output / 'runs' / 'old').mkdir(parents=True)
    (output / 'runs' / 'old' / 'junit.xml').write_text('<old/>')
    environment = {'PID_MARKER': str(marker)}
    expired = invoke('test', '--output', output, '--target', slow,
                     '--timeout-seconds', '1.5', env=environment)
    assert expired.returncode == 124, expired.stdout + expired.stderr
    status = json.loads((output / 'test-status.json').read_text())
    assert status['test_status'] == 'preparation_error' and status['reason'] == 'timeout'
    assert status['junit_present'] is False
    assert not (output / status['junit_path']).exists()
    assert (output / 'runs' / 'old' / 'junit.xml').read_text() == '<old/>'
    assert marker.is_file()
    import ctypes
    pid = int(marker.read_text())
    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
    if handle:
        exit_code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
        ctypes.windll.kernel32.CloseHandle(handle)
        assert exit_code.value != 259


def test_receiver_timeout_is_transfer_failure_without_changing_pytest_result(tmp_path):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    class Slow(BaseHTTPRequestHandler):
        def do_POST(self):
            time.sleep(3)
            self.send_response(500); self.end_headers()

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Slow)
    thread = Thread(target=server.serve_forever, daemon=True); thread.start()
    output = tmp_path / 'ci'
    marker = tmp_path / 'test_ok.py'; marker.write_text('def test_ok(): assert True\n')
    environment = {'QA_SERVER_URL': f'http://127.0.0.1:{server.server_port}',
                   'QA_PROJECT_ID': 'demo', 'QA_RESULT_TOKEN': TOKEN,
                   'GITHUB_RUN_ID': '900', 'GITHUB_RUN_ATTEMPT': '1',
                   'GITHUB_REPOSITORY': 'team/sample', 'GITHUB_SHA': 'f' * 40,
                   'GITHUB_WORKFLOW': 'Product regression', 'GITHUB_JOB': 'regression'}
    try:
        original = invoke('test', '--output', output, '--target', marker, env=environment)
        assert original.returncode == 0
        capture = json.loads((output / 'test-status.json').read_text())['capture_id']
        v4_folder(output / 'evidence' / capture / 'source')
        start = time.monotonic()
        sent = invoke('send', '--output', output, '--timeout-seconds', '.3', env=environment)
        assert sent.returncode == 2, sent.stdout + sent.stderr
        assert time.monotonic() - start < 2
        assert json.loads((output / 'transfer-status.json').read_text())['transfer_status'] == 'failed'
        assert json.loads((output / 'test-status.json').read_text())['test_status'] == 'passed'
        assert TOKEN.encode() not in sent.stdout + sent.stderr
    finally:
        server.shutdown(); server.server_close(); thread.join()


def test_product_entrypoint_keeps_windows_pytest_temp_paths_short(tmp_path):
    target = Path(__file__).resolve().parents[1] / 'tests' / 'test_investigation.py'
    output = tmp_path / 'ci'
    result = invoke('test', '--output', output, '--target',
                    f'{target}::test_notes_report_retest_move_and_originals_unchanged')
    assert result.returncode == 0, result.stdout + result.stderr
    status = json.loads((output / 'test-status.json').read_text())
    assert status['test_status'] == 'passed'


def test_product_junit_does_not_expose_synthetic_receiver_token(tmp_path):
    target = Path(__file__).resolve().parents[1] / 'tests' / 'test_ingestion.py'
    output = tmp_path / 'ci'
    result = invoke('test', '--output', output, '--target',
                    f'{target}::test_all_data_routes_reject_wrong_project_or_auth')
    assert result.returncode == 0, result.stdout + result.stderr
    status = json.loads((output / 'test-status.json').read_text())
    assert TOKEN.encode() not in (output / status['junit_path']).read_bytes()
