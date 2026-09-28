"""Self-contained HAR capture; never stores browser profiles or storageState."""
import hashlib
import json
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

from signup031.contract import utc_now
from signup031.storage import write_evidence


@contextmanager
def serve_demo(path):
    body = Path(path).read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/'
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


def finish_archive(root, *, execution_id, target_kind, target_url, navigation_url,
                   selector, label_pattern, observation, close_errors):
    root = Path(root)
    manifest = {
        'archive_version': 1, 'execution_id': execution_id,
        'target_kind': target_kind, 'target_url': target_url,
        'navigation_url': navigation_url, 'captured_at': utc_now(),
        'status': 'failed', 'reason': None, 'replay_verification': 'not_run',
        'procedure': {'name': 'SIGNUP-031', 'initial_length': 128, 'append': 'Z',
                      'selector': selector, 'label_pattern': label_pattern},
        'observed': {'initial_length': observation.initial_length,
                     'after_extra_length': observation.after_extra_length},
        'resources': [],
    }
    return _finish_manifest(root, manifest,
                            completed=observation.initial_length == 128 and observation.after_extra_length is not None,
                            close_errors=close_errors)


def finish_scenario_archive(root, *, execution_id, config, checks, completed, close_errors):
    manifest = {
        'archive_version': 2, 'execution_id': execution_id,
        'target_kind': 'configured_web', 'target_url': config['url'],
        'navigation_url': config['url'], 'captured_at': utc_now(),
        'status': 'failed', 'reason': None, 'replay_verification': 'not_run',
        'scenario_snapshot': config, 'observed_checks': checks, 'resources': [],
    }
    return _finish_manifest(Path(root), manifest, completed=completed, close_errors=close_errors)


def _finish_manifest(root, manifest, *, completed, close_errors):
    try:
        har_path = root / 'resources.har'
        har = json.loads(har_path.read_text(encoding='utf-8'))
        entries = har['log']['entries']
        for entry in entries:
            for side in ('request', 'response'):
                item = entry[side]
                item['cookies'] = []
                item['headers'] = [h for h in item.get('headers', [])
                                   if h['name'].lower() not in {
                                       'cookie', 'set-cookie', 'authorization',
                                       'proxy-authorization'}]
            # Only anonymous retrieval responses are eligible for offline playback.
            entry['request'].pop('postData', None)
        har['log']['entries'] = [e for e in entries if e['request']['method'] in ('GET', 'HEAD')]
        write_evidence(har_path, har)
        manifest['resources'] = [{'path': 'resources.har',
                                 'sha256': hashlib.sha256(har_path.read_bytes()).hexdigest()}]
        if close_errors or not completed:
            raise ValueError('recording incomplete: context close or scenario preparation failed')
        if not har['log']['entries']:
            raise ValueError('no recorded HTTP responses')
        manifest['status'] = 'recorded'
    except Exception as exc:
        manifest['reason'] = f'{type(exc).__name__}: {exc}'
    write_evidence(root / 'manifest.json', manifest)
    return {'status': manifest['status'], 'manifest': 'archive/manifest.json',
            'reason': manifest['reason'], 'replay_verification': 'not_run'}


def finish_manual_archive(root, *, execution_id, record, completed, close_errors):
    manifest = {'archive_version':3, 'execution_id':execution_id,
                'navigation_url':record['start_url'], 'captured_at':utc_now(),
                'manual_record':record, 'status':'failed', 'reason':None,
                'replay_verification':'not_run', 'resources':[]}
    return _finish_manifest(Path(root), manifest, completed=completed, close_errors=close_errors)


def load_archive(root):
    root = Path(root).resolve()
    manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
    if not isinstance(manifest, dict) or type(manifest.get('archive_version')) is not int or manifest['archive_version'] not in (1, 2, 3, 4):
        raise ValueError('unsupported archive version')
    if manifest.get('status') != 'recorded':
        raise ValueError('recording incomplete: ' + str(manifest.get('reason')))
    if manifest['archive_version'] == 1:
        from signup031.legacy_replay import validate_legacy_procedure
        validate_legacy_procedure(manifest)
    elif manifest['archive_version'] == 4:
        from signup031.selenium_recording import validate_selenium_record
        record = validate_selenium_record(manifest.get('selenium_record'))
        if record['start_url'] != manifest.get('navigation_url'):
            raise ValueError('Selenium URL mismatch')
        attachments = manifest.get('attachments')
        if not isinstance(attachments, list) or not any(a.get('path') == 'page.html' for a in attachments):
            raise ValueError('missing failure-time DOM attachment')
        for attachment in attachments:
            relative = Path(attachment['path'])
            path = (root / relative).resolve()
            if relative.is_absolute() or not path.is_relative_to(root):
                raise ValueError('attachment path escapes archive root')
            if hashlib.sha256(path.read_bytes()).hexdigest() != attachment['sha256']:
                raise ValueError('attachment hash mismatch')
    elif manifest['archive_version'] == 3:
        from signup031.manual_recording import validate_manual_record
        record = validate_manual_record(manifest.get('manual_record'))
        if record['start_url'] != manifest.get('navigation_url'):
            raise ValueError('manual URL mismatch')
    else:
        from signup031.web_scenario import validate_scenario, validate_observed_checks
        config = validate_scenario(manifest.get('scenario_snapshot'))
        validate_observed_checks(config, manifest.get('observed_checks'))
        if config['url'] != manifest.get('navigation_url'):
            raise ValueError('scenario URL mismatch')
    from urllib.parse import urlsplit
    if urlsplit(manifest.get('navigation_url', '')).scheme not in ('http', 'https'):
        raise ValueError('unsupported navigation URL')
    resources = manifest.get('resources')
    if not isinstance(resources, list) or len(resources) != 1:
        raise ValueError('missing resource inventory')
    resource = resources[0]
    relative = Path(resource['path'])
    if relative.is_absolute():
        raise ValueError('archive path must be relative')
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError('archive path escapes root')
    if hashlib.sha256(path.read_bytes()).hexdigest() != resource['sha256']:
        raise ValueError('archive resource hash mismatch')
    har = json.loads(path.read_text(encoding='utf-8'))
    def reject_external(value):
        if isinstance(value, dict):
            if '_file' in value:
                raise ValueError('external HAR body references are not allowed')
            for child in value.values():
                reject_external(child)
        elif isinstance(value, list):
            for child in value:
                reject_external(child)
    reject_external(har)
    if not isinstance(har.get('log', {}).get('entries'), list):
        raise ValueError('invalid HAR entries')
    return manifest, path
