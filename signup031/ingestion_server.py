"""Separate authenticated SQLite service. Original bytes and source keys are immutable."""
import argparse
import asyncio
import base64
import hmac
import json
import os
from pathlib import Path
import sqlite3
from uuid import uuid4
from signup031 import jira_delivery

from signup031.ingestion_contract import (MAX_ATTACHMENT_BYTES, MAX_ENVELOPE_BYTES, MAX_TOTAL_BYTES,
    digest, is_link, relative_path, safe_path, validate_envelope, validate_manifest)


def create_app(storage, tokens, *, max_envelope_bytes=MAX_ENVELOPE_BYTES,
               max_attachment_bytes=MAX_ATTACHMENT_BYTES, max_total_bytes=MAX_TOTAL_BYTES):
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import Response
    from starlette.requests import ClientDisconnect
    # Request annotations resolve under postponed/dynamic endpoint signature inspection.
    globals()['Request'] = Request
    if not isinstance(tokens, dict) or not tokens or any(not isinstance(key, str) or not isinstance(value, str) or len(value) < 16 or '\n' in value for key, value in tokens.items()):
        raise ValueError('QA_PROJECT_TOKENS must contain project tokens of at least 16 characters')
    root = Path(storage).absolute()
    if is_link(root): raise ValueError('linked server storage')
    root.mkdir(parents=True, exist_ok=True)
    db = safe_path(root, 'results.sqlite3')

    def connect():
        safe_path(root, 'results.sqlite3')
        connection = sqlite3.connect(db, timeout=15)
        connection.row_factory = sqlite3.Row
        return connection

    with connect() as conn:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('''CREATE TABLE IF NOT EXISTS results (
            seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL, project TEXT NOT NULL,
            provider TEXT NOT NULL, source_run TEXT NOT NULL, attempt INTEGER NOT NULL, test_id TEXT NOT NULL,
            fingerprint TEXT NOT NULL, envelope TEXT NOT NULL, evidence BLOB NOT NULL,
            UNIQUE(project,provider,source_run,attempt,test_id))''')
        jira_delivery.initialize(conn)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    def contains_secret(raw):
        return any(token and token.encode() in raw for token in (*tokens.values(), os.environ.get('QA_JIRA_TOKEN', '')))

    def authorize(request, project):
        expected = tokens.get(project)
        auth = request.headers.get('authorization', '')
        if not expected or not auth.startswith('Bearer ') or not hmac.compare_digest(auth[7:].encode(), expected.encode()):
            raise HTTPException(401, 'project authentication required', headers={'WWW-Authenticate': 'Bearer'})

    async def bounded(request, maximum):
        async def read():
            output = bytearray()
            async for block in request.stream():
                output.extend(block)
                if len(output) > maximum: raise HTTPException(413, 'request exceeds byte limit')
            return bytes(output)
        try:
            raw = await asyncio.wait_for(read(), timeout=10)
        except (asyncio.TimeoutError, ClientDisconnect):
            raise HTTPException(408, 'request incomplete')
        if contains_secret(raw):
            raise HTTPException(422, 'authentication credential found in data')
        return raw

    def row_for(conn, project, result_id):
        row = conn.execute('SELECT * FROM results WHERE project=? AND id=?', (project, result_id)).fetchone()
        if row is None: raise HTTPException(404, 'result not found')
        return row

    def file_path(result_id, name):
        return safe_path(root, 'attachments/' + result_id + '/' + digest(name.encode())[:24])

    def detail(row, *, include_evidence=True):
        env = json.loads(row['envelope'])
        available = []
        for item in env['files']:
            try:
                path = file_path(row['id'], item['path'])
                present = (item['sha256'] is not None and path.is_file() and
                    (item['size'] is None or path.stat().st_size == item['size']) and digest(path.read_bytes()) == item['sha256'])
            except (OSError, ValueError): present = False
            available.append({**item, 'available': present})
        if not include_evidence: env.pop('evidence_b64')
        return {'id': row['id'], 'cursor': row['seq'], 'envelope': env, 'files': available,
                'transfer_status': 'complete' if all(item['available'] for item in available) else 'partial'}

    @app.get('/health')
    async def health(): return {'status': 'ready'}

    @app.post('/projects/{project}/results')
    async def receive(project: str, request: Request):
        authorize(request, project)
        raw = await bounded(request, max_envelope_bytes)
        try:
            env = json.loads(raw)
            evidence, _ = validate_envelope(env, max_attachment_bytes=max_attachment_bytes, max_total_bytes=max_total_bytes)
            if env['project_id'] != project: raise ValueError('project mismatch')
            if contains_secret(evidence): raise ValueError('credential in original evidence')
            canonical = json.dumps(env, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
            raise HTTPException(422, 'invalid result metadata')
        key = (project, env['provider'], env['source_run_id'], env['attempt'], env['test_id'])
        with connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            previous = conn.execute('SELECT * FROM results WHERE project=? AND provider=? AND source_run=? AND attempt=? AND test_id=?', key).fetchone()
            if previous:
                if previous['fingerprint'] != digest(canonical.encode()): raise HTTPException(409, 'source key conflicts with immutable original')
                return Response(json.dumps(detail(previous)), media_type='application/json', status_code=200)
            result_id = uuid4().hex
            conn.execute('INSERT INTO results(id,project,provider,source_run,attempt,test_id,fingerprint,envelope,evidence) VALUES(?,?,?,?,?,?,?,?,?)',
                         (result_id, *key, digest(canonical.encode()), canonical, evidence))
            row = row_for(conn, project, result_id)
            jira_delivery.register_result(conn, row, jira_delivery.configuration(project))
        return Response(json.dumps(detail(row)), media_type='application/json', status_code=201)

    @app.get('/projects/{project}/results')
    async def listing(project: str, request: Request, limit: int = 20, cursor: int | None = None):
        authorize(request, project)
        if not 1 <= limit <= 100 or (cursor is not None and cursor < 1): raise HTTPException(422, 'invalid pagination')
        with connect() as conn:
            rows = conn.execute('SELECT * FROM results WHERE project=? AND seq<? ORDER BY seq DESC LIMIT ?',
                (project, cursor if cursor is not None else 2**63-1, limit+1)).fetchall()
        return {'items': [detail(row, include_evidence=False) for row in rows[:limit]],
                'next_cursor': rows[limit-1]['seq'] if len(rows) > limit else None}

    @app.get('/projects/{project}/results/{result_id}')
    async def get_result(project: str, result_id: str, request: Request):
        authorize(request, project)
        with connect() as conn: return detail(row_for(conn, project, result_id))

    @app.put('/projects/{project}/results/{result_id}/files')
    async def upload(project: str, result_id: str, request: Request, path: str):
        authorize(request, project)
        with connect() as conn: row = row_for(conn, project, result_id)
        env = json.loads(row['envelope'])
        try:
            relative_path(path)
            item = next(item for item in env['files'] if item['path'] == path)
        except (ValueError, StopIteration): raise HTTPException(422, 'attachment not declared')
        raw = await bounded(request, max_attachment_bytes)
        if item['sha256'] is None or digest(raw) != item['sha256'] or (item['size'] is not None and len(raw) != item['size']):
            raise HTTPException(422, 'attachment size/hash mismatch')
        try:
            if path == 'archive/manifest.json': validate_manifest(raw, json.loads(row['evidence']), env['files'])
            elif path == 'timeline.json':
                from signup031.timeline import validate_bytes
                original=json.loads(row['evidence']);validate_bytes(raw,original['execution']['id'],original['timeline'])
            elif path == 'test-context.json':
                from signup031.test_context import validate_bytes
                original=json.loads(row['evidence']);validate_bytes(raw,original['execution']['id'],original['test_context'])
            elif path.endswith('.png') and not raw.startswith(b'\x89PNG\r\n\x1a\n'): raise ValueError('invalid PNG')
            elif path.endswith('.har'):
                har = json.loads(raw)
                def check(value):
                    if isinstance(value, dict):
                        if '_file' in value: raise ValueError('external HAR body')
                        if value.get('encoding') == 'base64' and isinstance(value.get('text'), str):
                            body = base64.b64decode(value['text'], validate=True)
                            if contains_secret(body):
                                raise ValueError('credential in encoded HAR content')
                        for sub in value.values(): check(sub)
                    elif isinstance(value, list):
                        for sub in value: check(sub)
                check(har)
                if not isinstance(har['log']['entries'], list): raise ValueError('invalid HAR')
            destination = file_path(result_id, path)
            with connect() as conn:
                conn.execute('BEGIN IMMEDIATE')
                occupied = sum(file_path(result_id, declared['path']).stat().st_size for declared in env['files']
                    if declared['path'] != path and file_path(result_id, declared['path']).is_file())
                if occupied + len(raw) > max_total_bytes:
                    raise HTTPException(413, 'total attachment byte limit')
                if destination.exists():
                    if destination.read_bytes() != raw: raise HTTPException(409, 'stored attachment conflict')
                else:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    file_path(result_id, path)
                    temporary = safe_path(root, destination.relative_to(root).as_posix() + '.' + uuid4().hex[:8] + '.tmp')
                    try:
                        temporary.write_bytes(raw); temporary.replace(destination)
                    finally:
                        if temporary.exists(): temporary.unlink()
        except (ValueError, KeyError, TypeError, AttributeError, OSError, RecursionError):
            raise HTTPException(422, 'invalid attachment or storage path')
        return {'status': 'saved'}

    @app.get('/projects/{project}/results/{result_id}/files')
    async def download(project: str, result_id: str, request: Request, path: str):
        authorize(request, project)
        with connect() as conn: row = row_for(conn, project, result_id)
        env = json.loads(row['envelope'])
        try:
            relative_path(path)
            item = next(item for item in env['files'] if item['path'] == path)
            owned = file_path(result_id, path)
            if not owned.is_file(): raise HTTPException(404, 'attachment not uploaded')
            raw = owned.read_bytes()
            if digest(raw) != item['sha256']: raise ValueError('stored attachment corrupt')
        except StopIteration: raise HTTPException(404, 'attachment not declared')
        except (ValueError, OSError): raise HTTPException(422, 'attachment unavailable')
        return Response(raw, media_type='application/octet-stream')

    @app.get('/projects/{project}/results/{result_id}/jira')
    async def jira_status(project: str, result_id: str, request: Request):
        authorize(request, project)
        with connect() as conn: return jira_delivery.status_for(conn, row_for(conn, project, result_id))

    @app.post('/projects/{project}/results/{result_id}/jira/recover')
    async def jira_recover(project: str, result_id: str, request: Request):
        authorize(request, project)
        raw = await bounded(request, 10000)
        def reconcile():
            with connect() as conn:
                row = row_for(conn, project, result_id)
                try: return jira_delivery.recover(conn, row, json.loads(raw))
                except (ValueError, TypeError, KeyError, AttributeError, jira_delivery.JiraFailure):
                    raise HTTPException(422, 'Jira correlation or configuration could not be verified')
        return await asyncio.to_thread(reconcile)

    @app.post('/projects/{project}/results/{result_id}/jira/retry')
    async def jira_retry(project: str, result_id: str, request: Request):
        authorize(request, project)
        raw = await bounded(request, 10000)
        with connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = row_for(conn, project, result_id)
            try: return jira_delivery.retry_job(conn, row, json.loads(raw))
            except (ValueError, TypeError, KeyError, AttributeError):
                raise HTTPException(422, 'only configuration errors with valid settings can be retried; uncertain jobs require reconciliation')

    @app.put('/projects/{project}/results/{result_id}/qa')
    async def qa_update(project: str, result_id: str, request: Request):
        authorize(request, project)
        raw = await bounded(request, 2_000_000)
        with connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = row_for(conn, project, result_id)
            try: return jira_delivery.save_qa(conn, row, json.loads(raw))
            except jira_delivery.QAConflict:
                raise HTTPException(409, 'QA revision/history conflict; local edits were not replaced')
            except (ValueError, TypeError, KeyError, AttributeError):
                raise HTTPException(422, 'invalid QA source, history or retest reference')

    return app


def main(argv=None):
    parser = argparse.ArgumentParser(description='Local authenticated CI result receiver')
    parser.add_argument('--storage', type=Path, required=True)
    parser.add_argument('--host', default='127.0.0.1', choices=('127.0.0.1', '::1'))
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args(argv)
    try:
        import uvicorn
        tokens = json.loads(os.environ.get('QA_PROJECT_TOKENS', '{}'))
        limits = {}
        for name, default in [('max_envelope_bytes', MAX_ENVELOPE_BYTES), ('max_attachment_bytes', MAX_ATTACHMENT_BYTES), ('max_total_bytes', MAX_TOTAL_BYTES)]:
            value = int(os.environ.get('QA_' + name.upper(), default))
            if value <= 0: raise ValueError('invalid byte limit')
            limits[name] = value
        app = create_app(args.storage, tokens, **limits)
    except (ImportError, ValueError, OSError):
        print('Server configuration failed: install .[server], set project tokens and writable storage.')
        return 2
    uvicorn.run(app, host=args.host, port=args.port, access_log=False, log_level='warning')
    return 0


if __name__ == '__main__': raise SystemExit(main())
