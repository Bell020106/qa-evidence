"""Small CI entrypoints that keep pytest, receiver transfer and artifacts distinct."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from uuid import uuid4


IDENTITY_KEYS = ('GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT', 'GITHUB_REPOSITORY',
                 'GITHUB_SHA', 'GITHUB_WORKFLOW', 'GITHUB_JOB')


def _identity():
    return {key: os.environ.get(key, '') for key in IDENTITY_KEYS}


def _write_status(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _stop_owned_process(process):
    if process.poll() is not None:
        return
    if os.name == 'nt':
        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
                       check=False)
    else:
        process.kill()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def run_tests(output, targets, *, timeout_seconds=900):
    if not 0 < timeout_seconds <= 3600:
        raise ValueError('invalid test timeout')
    output = Path(output).absolute()
    output.mkdir(parents=True, exist_ok=True)
    capture_id = uuid4().hex
    run_dir = output / 'runs' / capture_id
    run_dir.mkdir(parents=True)
    evidence_root = output / 'evidence' / capture_id
    log_path = run_dir / 'pytest.log'
    junit_path = run_dir / 'junit.xml'
    manifest_path = output / 'test-status.json'
    base = {'capture_id': capture_id, 'github_identity': _identity(),
            'pytest_log': log_path.relative_to(output).as_posix(),
            'junit_path': junit_path.relative_to(output).as_posix()}
    _write_status(manifest_path, {**base, 'test_status': 'in_progress',
                                  'pytest_exit_code': None, 'junit_present': False,
                                  'evidence_count': 0})
    github_output = os.environ.get('GITHUB_OUTPUT')
    if github_output:
        with Path(github_output).open('a', encoding='utf-8') as output_file:
            output_file.write('capture_id=' + capture_id + '\n')
    command = [sys.executable, '-m', 'pytest', '-p', 'no:cacheprovider',
               '-p', 'signup031.selenium_plugin',
               *targets, '--selenium-artifacts', str(evidence_root),
               '--junitxml', str(junit_path), '-q']
    environment = dict(os.environ)
    # The runner owns the Actions step outputs; nested tests must not overwrite them.
    for key in ('QA_RESULT_TOKEN', 'GITHUB_OUTPUT', 'GITHUB_ENV', 'GITHUB_STEP_SUMMARY'):
        environment.pop(key, None)
    process = None
    reason = None
    try:
        with log_path.open('wb') as log_file:
            process = subprocess.Popen(command, stdout=log_file, stderr=subprocess.STDOUT,
                                       env=environment)
            try:
                code = process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                _stop_owned_process(process)
                code, reason = 124, 'timeout'
            except KeyboardInterrupt:
                _stop_owned_process(process)
                _write_status(manifest_path, {**base, 'test_status': 'interrupted',
                                              'pytest_exit_code': 130, 'junit_present': junit_path.is_file(),
                                              'evidence_count': 0})
                return 130
    except OSError as exc:
        log_path.write_text(f'pytest launch failed: {type(exc).__name__}\n', encoding='utf-8')
        code, reason = 2, 'launch_error'
    with log_path.open('rb') as log_file:
        for chunk in iter(lambda: log_file.read(65536), b''):
            sys.stdout.buffer.write(chunk)
    _write_status(manifest_path, {**base,
        'test_status': 'passed' if code == 0 else 'failed' if code == 1 else 'preparation_error',
        'pytest_exit_code': code,
        'reason': reason,
        'junit_present': junit_path.is_file(),
        'evidence_count': len(list(evidence_root.rglob('evidence.json'))),
    })
    return code


def send_results(output, *, timeout_seconds=60):
    if not 0 < timeout_seconds <= 600:
        raise ValueError('invalid transfer timeout')
    output = Path(output).absolute()
    output.mkdir(parents=True, exist_ok=True)
    status_path = output / 'transfer-status.json'
    token = os.environ.get('QA_RESULT_TOKEN', '')
    url = os.environ.get('QA_SERVER_URL', '')
    project = os.environ.get('QA_PROJECT_ID', '')
    fields = _identity()
    manifest = output / 'test-status.json'
    try:
        run = json.loads(manifest.read_text(encoding='utf-8'))
        capture_id = run['capture_id']
        if not re.fullmatch(r'[0-9a-f]{32}', capture_id) or run['github_identity'] != fields:
            raise ValueError('run identity mismatch')
    except (OSError, ValueError, KeyError, TypeError):
        _write_status(status_path, {'transfer_status': 'invalid_identity', 'results': []})
        print('receiver transfer refused: no matching test run identity')
        return 2
    if run.get('test_status') == 'in_progress':
        _write_status(status_path, {'transfer_status': 'in_progress', 'results': []})
        print('receiver transfer refused: current test run is in progress')
        return 2
    if run.get('test_status') not in ('passed', 'failed', 'preparation_error'):
        _write_status(status_path, {'transfer_status': 'not_attempted_interrupted', 'results': []})
        print('receiver transfer not attempted: current test run was interrupted')
        return 2
    _write_status(status_path, {'transfer_status': 'in_progress',
                                'capture_id': capture_id, 'results': []})
    sources = sorted((output / 'evidence' / capture_id).rglob('evidence.json'))
    if not sources and run.get('test_status') == 'preparation_error':
        _write_status(status_path, {'transfer_status': 'not_attempted_preparation_error', 'results': []})
        print('receiver transfer not attempted: pytest preparation produced no evidence')
        return 0
    if not all((token, url, project, *fields.values())):
        _write_status(status_path, {'transfer_status': 'unconfigured', 'results': []})
        print('receiver transfer unconfigured: URL, project, token and GitHub identity are required')
        return 2
    try:
        attempt = int(fields['GITHUB_RUN_ATTEMPT'])
        if attempt < 1 or attempt > 1_000_000:
            raise ValueError()
    except ValueError:
        _write_status(status_path, {'transfer_status': 'invalid_identity', 'results': []})
        print('receiver transfer has invalid GitHub run attempt')
        return 2
    if not sources:
        _write_status(status_path, {'transfer_status': 'no_evidence', 'results': []})
        print('receiver transfer failed: no original evidence was produced')
        return 2
    results, failures = [], []
    exit_code = 0
    for source in sources:
        command = [sys.executable, '-m', 'signup031.ingestion_client', 'send',
                   '--url', url, '--project', project, '--provider', 'github-actions',
                   '--run', fields['GITHUB_REPOSITORY'] + ':' + fields['GITHUB_RUN_ID'] + ':' + fields['GITHUB_JOB'],
                   '--attempt', str(attempt), '--folder', str(source.parent),
                   '--repository', fields['GITHUB_REPOSITORY'],
                   '--workflow', fields['GITHUB_WORKFLOW'], '--commit', fields['GITHUB_SHA'],
                   '--job', fields['GITHUB_JOB'], '--github-run-id', fields['GITHUB_RUN_ID']]
        try:
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                stdout, _stderr = process.communicate(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                _stop_owned_process(process)
                exit_code = 2
                failures.append({'source': source.parent.name, 'reason': 'timeout'})
                continue
            if process.returncode in (0, 3):
                results.append(json.loads(stdout))
                if process.returncode == 3 and exit_code == 0:
                    exit_code = 3
            else:
                exit_code = 2
                failures.append({'source': source.parent.name, 'cli_exit_code': process.returncode})
        except (OSError, subprocess.TimeoutExpired, ValueError, json.JSONDecodeError) as exc:
            exit_code = 2
            failures.append({'source': source.parent.name, 'reason': type(exc).__name__})
    transfer_status = 'complete' if exit_code == 0 else 'partial' if exit_code == 3 else 'failed'
    _write_status(status_path, {'transfer_status': transfer_status, 'results': results, 'failures': failures})
    print(f'receiver transfer: {transfer_status}; complete={len(results)}; failed={len(failures)}')
    return exit_code


def main(argv=None):
    parser = argparse.ArgumentParser(description='Run tests and transfer their original evidence in distinct CI steps')
    commands = parser.add_subparsers(dest='command', required=True)
    testing = commands.add_parser('test')
    testing.add_argument('--output', type=Path, required=True)
    testing.add_argument('--target', action='append', required=True)
    testing.add_argument('--timeout-seconds', type=float, default=900)
    sending = commands.add_parser('send')
    sending.add_argument('--output', type=Path, required=True)
    sending.add_argument('--timeout-seconds', type=float, default=60)
    args = parser.parse_args(argv)
    return (run_tests(args.output, args.target, timeout_seconds=args.timeout_seconds)
            if args.command == 'test' else send_results(args.output, timeout_seconds=args.timeout_seconds))


if __name__ == '__main__':
    raise SystemExit(main())
