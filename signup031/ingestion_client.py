"""Bounded CLI and desktop client for immutable remote execution results."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from signup031.ingestion_contract import (MAX_ATTACHMENT_BYTES, MAX_ENVELOPE_BYTES,
    build_bundle, digest, relative_path, safe_path, validate_envelope, validate_manifest)


class TransferError(ValueError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        raise TransferError('HTTP redirect refused; credentials were not forwarded')


def validated_origin(url):
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.path not in ('', '/') or parsed.query or parsed.fragment:
        raise TransferError('server URL must be an HTTPS origin or loopback HTTP origin')
    if parsed.scheme == 'http' and parsed.hostname.lower() not in ('localhost', '127.0.0.1', '::1'):
        raise TransferError('remote server requires HTTPS')
    if not (1 <= (parsed.port or (443 if parsed.scheme == 'https' else 80)) <= 65535):
        raise TransferError('invalid server port')
    return url.rstrip('/')


class IngestionClient:
    def __init__(self, url, project, token, *, timeout=10):
        self.origin = validated_origin(url)
        if not isinstance(project, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', project):
            raise TransferError('invalid project identifier')
        if not isinstance(token, str) or not token or any(ord(char) < 33 or ord(char) > 126 for char in token):
            raise TransferError('valid project bearer token is required')
        self.project, self._token, self.timeout = project, token, timeout
        self._opener = build_opener(_NoRedirect)

    def _request(self, method, path, body=None, *, maximum=MAX_ENVELOPE_BYTES + 100_000, deadline=None):
        headers = {'Authorization': 'Bearer ' + self._token}
        if body is not None and method != 'GET':
            headers['Content-Type'] = 'application/json' if isinstance(body, dict) else 'application/octet-stream'
        raw = json.dumps(body, ensure_ascii=False, separators=(',', ':')).encode() if isinstance(body, dict) else body
        request = Request(self.origin + path, data=raw, headers=headers, method=method)
        if deadline is not None and time.monotonic() >= deadline:
            raise TransferError('total download deadline exceeded')
        timeout = min(self.timeout, max(.001, deadline - time.monotonic())) if deadline is not None else self.timeout
        try:
            with self._opener.open(request, timeout=timeout) as reply:
                if int(reply.headers.get('Content-Length', '0')) > maximum:
                    raise TransferError('server reply exceeds byte limit')
                chunks, length = [], 0
                while True:
                    if deadline is not None and time.monotonic() >= deadline:
                        raise TransferError('total download deadline exceeded')
                    block = reply.read1(min(65536, maximum + 1 - length))
                    if not block: break
                    length += len(block)
                    if length > maximum: raise TransferError('server reply exceeds byte limit')
                    chunks.append(block)
                return reply.status, b''.join(chunks)
        except HTTPError as exc:
            # Never echo server body or URL: they may reflect a credential or private data.
            raise TransferError(f'server returned HTTP {exc.code}') from None
        except (URLError, TimeoutError, OSError) as exc:
            raise TransferError('server connection failed: ' + type(exc).__name__) from None

    def send_folder(self, folder, *, provider, run, attempt=1, test_id=None, provenance=None):
        envelope, contents = build_bundle(folder, self.project, provider, run, attempt,
                                          test_id=test_id, provenance=provenance)
        status, raw = self._request('POST', f'/projects/{quote(self.project)}/results', envelope)
        response = json.loads(raw)
        result_id = response['id']
        uploaded, failed = [], []
        for path, body in contents.items():
            try:
                self._request('PUT', f'/projects/{quote(self.project)}/results/{result_id}/files?' +
                    urlencode({'path': path}), body, maximum=200_000)
                uploaded.append(path)
            except TransferError as exc:
                failed.append({'path': path, 'reason': str(exc)})
        missing = [item['path'] for item in envelope['files'] if item['path'] not in contents]
        return {'id': result_id, 'metadata_status': 'new' if status == 201 else 'duplicate',
                'original_status': json.loads(base64.b64decode(envelope['evidence_b64']))['result']['business']['status'],
                'uploaded': uploaded, 'missing': missing, 'failed': failed}

    def list_results(self, *, limit=50, cursor=None):
        query = urlencode({'limit': limit, **({'cursor': cursor} if cursor is not None else {})})
        _, raw = self._request('GET', f'/projects/{quote(self.project)}/results?{query}')
        return json.loads(raw)

    def jira_status(self, result_id):
        if not re.fullmatch(r'[0-9a-f]{32}', result_id): raise TransferError('invalid result ID')
        _, raw = self._request('GET', f'/projects/{quote(self.project)}/results/{result_id}/jira')
        return json.loads(raw)

    def publish_qa(self, result_id, document, expected_revision):
        if not re.fullmatch(r'[0-9a-f]{32}', result_id): raise TransferError('invalid result ID')
        _, raw = self._request('PUT', f'/projects/{quote(self.project)}/results/{result_id}/qa',
                              {'document': document, 'expected_revision': expected_revision})
        return json.loads(raw)

    def recover_jira(self, result_id, job_id, issue_key, comment_id=None):
        if not re.fullmatch(r'[0-9a-f]{32}', result_id): raise TransferError('invalid result ID')
        _, raw = self._request('POST', f'/projects/{quote(self.project)}/results/{result_id}/jira/recover',
                              {'job_id': job_id, 'issue_key': issue_key, 'comment_id': comment_id})
        return json.loads(raw)

    def retry_jira(self, result_id, job_id):
        if not re.fullmatch(r'[0-9a-f]{32}', result_id): raise TransferError('invalid result ID')
        _, raw = self._request('POST', f'/projects/{quote(self.project)}/results/{result_id}/jira/retry', {'job_id': job_id})
        return json.loads(raw)

    def download(self, root, *, cancel=None, progress=None, max_seconds=60):
        """Fetch all pages to a separate cache; never replace source or QA bytes."""
        root = Path(root).absolute()
        root.mkdir(parents=True, exist_ok=True)
        cache_name = '_server_results/' + digest((self.origin + '\0' + self.project).encode())[:16]
        saved, partial = [], []
        if max_seconds <= 0: raise TransferError('invalid total download deadline')
        deadline = time.monotonic() + max_seconds
        cursor, seen = None, set()
        while True:
            if cancel and cancel(): break
            if time.monotonic() >= deadline: raise TransferError('total download deadline exceeded')
            query = urlencode({'limit': 50, **({'cursor': cursor} if cursor is not None else {})})
            _, page_raw = self._request('GET', f'/projects/{quote(self.project)}/results?{query}', deadline=deadline)
            page = json.loads(page_raw)
            for listed in page['items']:
                if cancel and cancel(): break
                if time.monotonic() >= deadline: raise TransferError('total download deadline exceeded')
                result_id = listed['id']
                if not re.fullmatch(r'[0-9a-f]{32}', result_id): raise TransferError('invalid server result ID')
                _, raw = self._request('GET', f'/projects/{quote(self.project)}/results/{result_id}', deadline=deadline)
                detail = json.loads(raw)
                env = detail['envelope']
                evidence, payload = validate_envelope(env)
                if detail['id'] != result_id or env['project_id'] != self.project:
                    raise TransferError('server result identity mismatch')
                listed_env = dict(listed['envelope'])
                detail_env = dict(env)
                detail_env.pop('evidence_b64', None)
                if listed_env != detail_env:
                    raise TransferError('server list/detail identity mismatch')
                def inventory(rows):
                    if not isinstance(rows, list) or len(rows) != len(env['files']):
                        raise TransferError('server attachment inventory mismatch')
                    found = {}
                    for item in rows:
                        if not isinstance(item, dict) or set(item) != {'path', 'size', 'sha256', 'available'} or type(item['available']) is not bool:
                            raise TransferError('server attachment inventory mismatch')
                        if item['path'] in found: raise TransferError('server attachment inventory mismatch')
                        found[item['path']] = {key: item[key] for key in ('path', 'size', 'sha256')}
                    if found != {item['path']: item for item in env['files']}:
                        raise TransferError('server attachment inventory mismatch')
                    return rows
                inventory(listed['files']); inventory(detail['files'])
                run_relative = cache_name + '/' + result_id
                location = safe_path(root, run_relative)
                location.mkdir(parents=True, exist_ok=True)
                sidecar = safe_path(root, run_relative + '/remote-source.json')
                def write_source(state, missing):
                    source = {'transport_version': 1, 'result_id': result_id, 'project_id': self.project,
                              'server_origin': self.origin, 'provider': env['provider'],
                              'source_run_id': env['source_run_id'], 'attempt': env['attempt'],
                              'test_id': env['test_id'], 'provenance': env['provenance'],
                              'transfer_status': state, 'missing_files': missing}
                    encoded = (json.dumps(source, ensure_ascii=False, indent=2) + '\n').encode()
                    if not sidecar.exists() or sidecar.read_bytes() != encoded:
                        self._atomic_write(root, run_relative + '/remote-source.json', encoded)
                write_source('partial', [item['path'] for item in detail['files']])
                evidence_path = safe_path(root, run_relative + '/evidence.json')
                if evidence_path.exists():
                    if evidence_path.read_bytes() != evidence:
                        raise TransferError('cached original evidence conflicts with server result')
                else:
                    self._atomic_write(root, run_relative + '/evidence.json', evidence)
                missing = []
                for item in detail['files']:
                    if cancel and cancel():
                        missing.append(item['path']); continue
                    name = relative_path(item['path'])
                    destination = safe_path(root, run_relative + '/' + name)
                    if not item['available']:
                        missing.append(name); continue
                    if destination.is_file():
                        existing = destination.read_bytes()
                        if digest(existing) != item['sha256'] or (item['size'] is not None and len(existing) != item['size']):
                            raise TransferError('cached attachment conflicts with server result')
                        continue
                    _, content = self._request('GET', f'/projects/{quote(self.project)}/results/{result_id}/files?'+
                        urlencode({'path': name}), maximum=min(MAX_ATTACHMENT_BYTES, item['size'] or MAX_ATTACHMENT_BYTES) + 1,
                        deadline=deadline)
                    if digest(content) != item['sha256'] or (item['size'] is not None and len(content) != item['size']):
                        raise TransferError('downloaded attachment size/hash mismatch')
                    if name == 'archive/manifest.json': validate_manifest(content, payload, env['files'])
                    if name == 'timeline.json':
                        from signup031.timeline import validate_bytes
                        validate_bytes(content,payload['execution']['id'],payload['timeline'])
                    if name == 'test-context.json':
                        from signup031.test_context import validate_bytes
                        validate_bytes(content,payload['execution']['id'],payload['test_context'])
                    self._atomic_write(root, run_relative + '/' + name, content)
                state = 'partial' if missing else 'complete'
                write_source(state, missing)
                # Remote QA/Jira is a separate snapshot, never a replacement for local .qa edits.
                _, jira_raw = self._request('GET', f'/projects/{quote(self.project)}/results/{result_id}/jira', deadline=deadline)
                self._atomic_write(root, run_relative + '/remote-jira.json', jira_raw)
                (partial if missing else saved).append(str(evidence_path))
                if progress: progress({'id': result_id, 'status': state})
            next_cursor = page['next_cursor']
            if next_cursor in seen: raise TransferError('server pagination cursor repeated')
            if not next_cursor or (cancel and cancel()): break
            seen.add(next_cursor); cursor = next_cursor
        return {'complete': saved, 'partial': partial, 'cancelled': bool(cancel and cancel())}

    @staticmethod
    def _atomic_write(root, relative, content):
        path = safe_path(root, relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = safe_path(root, relative + '.' + os.urandom(8).hex() + '.tmp')
        try:
            temporary.write_bytes(content)
            temporary.replace(path)
        finally:
            if temporary.exists(): temporary.unlink()


def main(argv=None):
    parser = argparse.ArgumentParser(description='Send an existing local test result to the authenticated receiver')
    commands = parser.add_subparsers(dest='command', required=True)
    send = commands.add_parser('send')
    send.add_argument('--url', required=True)
    send.add_argument('--project', required=True)
    send.add_argument('--provider', required=True)
    send.add_argument('--run', required=True)
    send.add_argument('--attempt', type=int, default=1)
    send.add_argument('--folder', type=Path, required=True)
    send.add_argument('--commit')
    send.add_argument('--repository')
    send.add_argument('--workflow')
    send.add_argument('--job')
    send.add_argument('--github-run-id')
    send.add_argument('--expected-criteria-version')
    send.add_argument('--error-signature')
    send.add_argument('--environment-id')
    args = parser.parse_args(argv)
    token = os.environ.get('QA_RESULT_TOKEN', '')
    try:
        client = IngestionClient(args.url, args.project, token)
        provenance = {key: value for key, value in (
            ('repository', args.repository), ('workflow', args.workflow), ('commit', args.commit),
            ('job', args.job), ('github_run_id', args.github_run_id),
            ('expected_criteria_version', args.expected_criteria_version), ('error_signature', args.error_signature),
            ('environment_id', args.environment_id)) if value}
        result = client.send_folder(args.folder, provider=args.provider, run=args.run,
                                    attempt=args.attempt, provenance=provenance)
        print(json.dumps(result, ensure_ascii=False))
        return 3 if result['missing'] or result['failed'] else 0
    except (TransferError, ValueError, OSError, KeyError, TypeError) as exc:
        reason = str(exc).replace(token, '[REDACTED]') if token else str(exc)
        print(json.dumps({'status': 'transfer_error', 'reason': reason[:300]}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == '__main__': raise SystemExit(main())
