"""Durable Jira outbox. Unknown POST outcomes are never automatically repeated."""
import base64
import hashlib
import json
import os
import re
import sqlite3
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, build_opener
from uuid import uuid4
from datetime import datetime

from signup031.ingestion_client import _NoRedirect, TransferError, validated_origin

PROPERTY = 'qa-evidence-event'


def configurations():
    try:
        value = json.loads(os.environ.get('QA_JIRA_CONFIG', '{}'))
        if not isinstance(value, dict): raise ValueError()
        return value
    except ValueError:
        raise ValueError('invalid QA_JIRA_CONFIG') from None


def configuration(project):
    try:
        config = configurations().get(project, {})
        if not isinstance(config, dict): raise ValueError()
        if 'enabled' in config and type(config['enabled']) is not bool: raise ValueError()
        for key in ('allowed_statuses', 'allowed_classifications'):
            if key in config and (not isinstance(config[key], list) or not all(isinstance(v, str) for v in config[key])):
                raise ValueError()
        return config
    except (ValueError, TypeError):
        return {'enabled': True, 'configuration_error': 'invalid_configuration'}


def validate_config(config):
    try:
        origin = validated_origin(config['base_url'])
        if urlsplit(origin).scheme != 'https' and config.get('allow_loopback') is not True:
            raise ValueError()
        if not re.fullmatch(r'[A-Z][A-Z0-9_]{1,63}', config['project_key']): raise ValueError()
        if not re.fullmatch(r'[0-9]{1,32}', config['issue_type_id']): raise ValueError()
        fields = config.get('fields', {})
        if not isinstance(fields, dict) or any(k in fields for k in ('project', 'issuetype', 'summary', 'description')):
            raise ValueError()
        if len(json.dumps(fields)) > 30000: raise ValueError()
        if not os.environ.get('QA_JIRA_EMAIL') or not os.environ.get('QA_JIRA_TOKEN'): raise ValueError()
        for key in ('allowed_statuses', 'allowed_classifications'):
            if not isinstance(config.get(key), list) or not all(isinstance(v, str) for v in config[key]): raise ValueError()
        return origin
    except (ValueError, KeyError, TypeError, AttributeError):
        raise ValueError('Jira settings or server credentials are incomplete') from None


def initialize(conn):
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS jira_groups (
            id TEXT PRIMARY KEY, project TEXT NOT NULL, identity TEXT NOT NULL,
            issue_key TEXT, UNIQUE(project,identity));
        CREATE TABLE IF NOT EXISTS jira_results (
            result_id TEXT PRIMARY KEY, group_id TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS jira_jobs (
            seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
            project TEXT NOT NULL, result_id TEXT NOT NULL, group_id TEXT NOT NULL,
            kind TEXT NOT NULL, event_key TEXT NOT NULL UNIQUE, payload TEXT NOT NULL,
            status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
            next_at REAL NOT NULL DEFAULT 0, last_error TEXT,
            owner TEXT, lease_until REAL, remote_id TEXT);
        CREATE TABLE IF NOT EXISTS result_qa (
            result_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, document TEXT NOT NULL);
    ''')
    if 'attempt_limit' not in {row[1] for row in conn.execute('PRAGMA table_info(jira_jobs)')}:
        conn.execute('ALTER TABLE jira_jobs ADD COLUMN attempt_limit INTEGER NOT NULL DEFAULT 3')


def failure_identity(env, payload, result_id):
    environment = payload.get('environment', {})
    if not isinstance(environment, dict): environment = {}
    keys = ('browser', 'browser_version', 'driver_version', 'platform', 'python', 'server')
    provenance = env['provenance']
    environment = {**environment, 'server': environment.get('server') or provenance.get('environment_id')}
    expected = provenance.get('expected_criteria_version')
    snapshot = payload.get('scenario_snapshot')
    if not expected and isinstance(snapshot, dict) and snapshot.get('checks'):
        expected = hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()
    signature = provenance.get('error_signature')
    phase = payload['result']['business'].get('phase')
    values = [env['test_id'], phase, expected, signature, *(environment.get(k) for k in keys)]
    if not all(isinstance(v, str) and v.strip() and v not in ('미수집', 'unknown') for v in values):
        return 'individual:' + result_id
    return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode()).hexdigest()


def enqueue(conn, project, result_id, group_id, kind, event_key, payload):
    conn.execute('''INSERT OR IGNORE INTO jira_jobs
        (id,project,result_id,group_id,kind,event_key,payload,status) VALUES(?,?,?,?,?,?,?,'pending')''',
        (uuid4().hex, project, result_id, group_id, kind, event_key, json.dumps(payload, ensure_ascii=False)))


def enqueue_attachments(conn, row, job, config):
    if config.get('attachments') is True:
        for item in json.loads(row['envelope'])['files']:
            enqueue(conn, row['project'], row['id'], job['group_id'], 'attachment', 'attachment:' + row['id'] + ':' + item['path'], item)


def retry_job(conn, row, body):
    config = configuration(row['project'])
    if config.get('enabled') is not True: raise ValueError('Jira disabled')
    validate_config(config)
    updated = conn.execute("""UPDATE jira_jobs SET status='pending',last_error=NULL,next_at=0,
        attempt_limit=attempts+3 WHERE id=? AND project=? AND result_id=? AND status='configuration_error'""",
        (body.get('job_id'), row['project'], row['id'])).rowcount
    if not updated: raise ValueError('only definitively rejected configuration errors can be requeued')
    return status_for(conn, row)


def register_result(conn, row, config, qa=None):
    if config.get('enabled') is not True: return
    env, payload = json.loads(row['envelope']), json.loads(row['evidence'])
    if payload['result']['business']['status'] not in config.get('allowed_statuses', ['failed']): return
    if (qa or {}).get('classification', '미분류') not in config.get('allowed_classifications', ['미분류']): return
    if conn.execute('SELECT 1 FROM jira_results WHERE result_id=?', (row['id'],)).fetchone(): return
    identity = failure_identity(env, payload, row['id'])
    group = conn.execute('SELECT * FROM jira_groups WHERE project=? AND identity=?', (row['project'], identity)).fetchone()
    kind = 'comment' if group else 'create'
    gid = group['id'] if group else uuid4().hex
    if not group:
        conn.execute('INSERT INTO jira_groups(id,project,identity) VALUES(?,?,?)', (gid, row['project'], identity))
    conn.execute('INSERT INTO jira_results VALUES(?,?)', (row['id'], gid))
    enqueue(conn, row['project'], row['id'], gid, kind, 'result:' + row['id'], {'event': 'execution', 'qa': qa})
    try: validate_config(config)
    except ValueError:
        conn.execute("UPDATE jira_jobs SET status='configuration_error',last_error='invalid_configuration' WHERE event_key=?", ('result:' + row['id'],))


def status_for(conn, row):
    group = conn.execute('''SELECT g.* FROM jira_groups g JOIN jira_results r ON r.group_id=g.id
                            WHERE r.result_id=?''', (row['id'],)).fetchone()
    jobs = conn.execute('''SELECT id,kind,status,attempts,next_at,last_error,remote_id FROM jira_jobs
                          WHERE result_id=? ORDER BY seq''', (row['id'],)).fetchall()
    qa = conn.execute('SELECT * FROM result_qa WHERE result_id=?', (row['id'],)).fetchone()
    config = configuration(row['project'])
    mode = 'loopback-test' if config.get('allow_loopback') is True and str(config.get('base_url', '')).startswith('http:') else 'jira-cloud'
    return {'result_id': row['id'], 'enabled': config.get('enabled') is True, 'integration_mode': mode,
            'issue_key': group['issue_key'] if group else None, 'jobs': [dict(j) for j in jobs],
            'qa_revision': qa['revision'] if qa else 0, 'qa': json.loads(qa['document']) if qa else None}


class QAConflict(ValueError): pass


def save_qa(conn, row, body):
    from signup031.investigation import CLASSIFICATIONS, REPORT_FIELDS
    doc, expected = body['document'], body['expected_revision']
    if type(expected) is not int or expected < 0: raise ValueError('invalid revision')
    env, original = json.loads(row['envelope']), json.loads(row['evidence'])
    if not isinstance(doc, dict) or set(doc) != {'version', 'source', 'revision', 'classification', 'grounds', 'notes', 'report', 'retests', 'history'}:
        raise ValueError('invalid QA document')
    if doc['source'] != {'execution_id': env['execution_id'], 'sha256': hashlib.sha256(row['evidence']).hexdigest()}:
        raise ValueError('original reference mismatch')
    if doc['version'] != 1 or type(doc['revision']) is not int or doc['revision'] < 1:
        raise ValueError('persist QA before sending')
    if doc['classification'] not in CLASSIFICATIONS or set(doc['report']) != set(REPORT_FIELDS): raise ValueError('invalid classification/report')
    if not all(isinstance(v, str) and len(v) <= 100000 for v in (doc['grounds'], doc['notes'], *doc['report'].values())):
        raise ValueError('invalid QA text')
    if not isinstance(doc['history'], list) or len(doc['history']) != doc['revision'] or not isinstance(doc['retests'], list):
        raise ValueError('invalid QA history')
    for entry in doc['history']:
        if not isinstance(entry, dict) or set(entry) != {'at', 'classification', 'grounds', 'notes', 'report'}:
            raise ValueError('invalid history entry')
        if entry['classification'] not in CLASSIFICATIONS or set(entry['report']) != set(REPORT_FIELDS): raise ValueError('invalid history report')
        if not all(isinstance(v, str) for v in (entry['at'], entry['grounds'], entry['notes'], *entry['report'].values())): raise ValueError('invalid history text')
    if {k: doc['history'][-1][k] for k in ('classification', 'grounds', 'notes', 'report')} != {k: doc[k] for k in ('classification', 'grounds', 'notes', 'report')}:
        raise ValueError('QA current values must match saved history')
    seen = set()
    for link in doc['retests']:
        if set(link) != {'target', 'reason', 'differences', 'source_status', 'target_status', 'linked_at'}: raise ValueError('invalid retest')
        if not isinstance(link['reason'], str) or not link['reason'].strip(): raise ValueError('retest reason required')
        ref = link['target']
        if set(ref) != {'execution_id', 'sha256'} or ref['execution_id'] == env['execution_id'] or ref['execution_id'] in seen:
            raise ValueError('invalid retest reference')
        seen.add(ref['execution_id'])
        candidates = conn.execute('SELECT envelope,evidence FROM results WHERE project=?', (row['project'],)).fetchall()
        matches = [r for r in candidates if json.loads(r['envelope'])['execution_id'] == ref['execution_id'] and hashlib.sha256(r['evidence']).hexdigest() == ref['sha256']]
        if len(matches) != 1: raise ValueError('retest not uniquely received in this project')
        target = json.loads(matches[0]['evidence'])
        if datetime.fromisoformat(target['execution']['started_at'].replace('Z', '+00:00')) <= datetime.fromisoformat(original['execution']['started_at'].replace('Z', '+00:00')):
            raise ValueError('retest is not later')
        if link['source_status'] != original['result']['business']['status'] or link['target_status'] != target['result']['business']['status']:
            raise ValueError('retest original status mismatch')
        if not isinstance(link['differences'], list) or not all(isinstance(v, str) for v in link['differences']) or not isinstance(link['linked_at'], str):
            raise ValueError('invalid retest metadata')
    encoded = json.dumps(doc, ensure_ascii=False, sort_keys=True)
    previous = conn.execute('SELECT * FROM result_qa WHERE result_id=?', (row['id'],)).fetchone()
    revision = previous['revision'] if previous else 0
    if previous and previous['document'] == encoded: return status_for(conn, row)
    if expected != revision: raise QAConflict('server QA revision changed')
    if previous:
        old = json.loads(previous['document'])
        if doc['revision'] <= old['revision'] or doc['history'][:len(old['history'])] != old['history'] or doc['retests'][:len(old['retests'])] != old['retests']:
            raise QAConflict('QA histories diverged; resolve explicitly')
    revision += 1
    conn.execute('INSERT INTO result_qa VALUES(?,?,?) ON CONFLICT(result_id) DO UPDATE SET revision=excluded.revision,document=excluded.document', (row['id'], revision, encoded))
    config = configuration(row['project'])
    existed = conn.execute('SELECT group_id FROM jira_results WHERE result_id=?', (row['id'],)).fetchone()
    register_result(conn, row, config, doc)
    if existed:
        enqueue(conn, row['project'], row['id'], existed['group_id'], 'comment', f"qa:{row['id']}:{revision}", {'event': 'qa_revision', 'qa': doc, 'server_revision': revision})
    return status_for(conn, row)


def adf(text):
    return {'type': 'doc', 'version': 1, 'content': [
        {'type': 'paragraph', 'content': [{'type': 'text', 'text': line[:30000] or ' '}]} for line in text.splitlines()[:1000]]}


def report(row, job):
    env, evidence = json.loads(row['envelope']), json.loads(row['evidence'])
    data = json.loads(job['payload'])
    qa = data.get('qa') or {}
    report_fields = qa.get('report', {})
    expected = evidence.get('expected_result_snapshot') or (evidence.get('scenario_snapshot') or {}).get('checks')
    actual = evidence.get('checks') or evidence.get('measurements') or (evidence.get('selenium_record') or evidence.get('manual_record') or {}).get('observed')
    lines = [f"TC: {env['test_id']}", f"Execution: {env['execution_id']}",
        f"Source: {env['provider']} / {env['source_run_id']} / attempt {env['attempt']}",
        'Provenance: ' + json.dumps(env['provenance'], ensure_ascii=False),
        'Environment: ' + json.dumps(evidence.get('environment', {}), ensure_ascii=False),
        'Original result: ' + json.dumps(evidence['result'], ensure_ascii=False),
        'Steps: ' + str(report_fields.get('steps') or evidence.get('scenario_snapshot') or evidence.get('selenium_record', {}).get('actions') or 'not collected'),
        'Expected: ' + str(report_fields.get('expected') or (json.dumps(expected, ensure_ascii=False) if expected is not None else 'not collected')),
        'Actual: ' + str(report_fields.get('actual') or (json.dumps(actual, ensure_ascii=False) if actual is not None else evidence['result']['business'].get('message', 'not collected'))),
        'Evidence: authenticated receiver result ' + row['id'] + ' / evidence.json',
        'Attachments (receiver-relative): ' + ', '.join(f['path'] for f in env['files']),
        'QA: ' + json.dumps(qa, ensure_ascii=False), 'Event: ' + job['id']]
    return adf('\n'.join(lines))


class JiraFailure(Exception):
    def __init__(self, status, reason, delay=0):
        self.status, self.reason, self.delay = status, reason, delay


class JiraClient:
    def __init__(self, config, timeout=10):
        try: self.origin = validate_config(config)
        except ValueError: raise JiraFailure('configuration_error', 'invalid_configuration') from None
        self.config, self.timeout = config, timeout
        raw = (os.environ['QA_JIRA_EMAIL'] + ':' + os.environ['QA_JIRA_TOKEN']).encode()
        self.auth = 'Basic ' + base64.b64encode(raw).decode()
        self.opener = build_opener(_NoRedirect)

    def request(self, method, path, body=None, content_type='application/json'):
        raw = json.dumps(body, ensure_ascii=False).encode() if isinstance(body, dict) else body
        headers = {'Authorization': self.auth, 'Accept': 'application/json', 'Content-Type': content_type}
        if content_type.startswith('multipart/'): headers['X-Atlassian-Token'] = 'no-check'
        try:
            started = time.monotonic()
            with self.opener.open(Request(self.origin + path, raw, headers, method=method), timeout=self.timeout) as response:
                chunks, length = [], 0
                while True:
                    chunk = response.read1(65536)
                    if not chunk: break
                    length += len(chunk)
                    if length > 2_000_000 or time.monotonic() - started > self.timeout:
                        raise JiraFailure('uncertain', 'response_limit')
                    chunks.append(chunk)
                data = json.loads(b''.join(chunks))
                if response.status != (200 if method == 'GET' or content_type.startswith('multipart/') else 201):
                    raise JiraFailure('uncertain', 'unexpected_response')
                return data
        except HTTPError as exc:
            if exc.code == 429:
                try: delay = max(30, min(3600, int(exc.headers.get('Retry-After', '30'))))
                except ValueError: delay = 30
                raise JiraFailure('retryable', 'http_429', delay) from None
            if exc.code in (400, 401, 403, 404, 413, 422) or 300 <= exc.code < 400:
                raise JiraFailure('configuration_error', 'http_' + str(exc.code)) from None
            raise JiraFailure('uncertain', 'http_' + str(exc.code)) from None
        except TransferError:
            raise JiraFailure('configuration_error', 'redirect_refused') from None
        except (URLError, OSError, TimeoutError, ValueError):
            raise JiraFailure('uncertain', 'response_not_confirmed') from None

    def verify(self, key, event_id, comment_id=None):
        if not re.fullmatch(re.escape(self.config['project_key']) + r'-[1-9][0-9]*', key or ''):
            raise ValueError('issue project/key mismatch')
        issue = self.request('GET', '/rest/api/3/issue/' + key + '?fields=project')
        if issue.get('fields', {}).get('project', {}).get('key') != self.config['project_key'] or issue.get('key') != key:
            raise ValueError('issue project mismatch')
        if comment_id:
            if not re.fullmatch(r'[0-9]{1,32}', comment_id): raise ValueError('invalid comment ID')
            reply = self.request('GET', '/rest/api/3/issue/' + key + '/comment/' + comment_id + '?expand=properties')
            prop = next((p for p in reply.get('properties', []) if p['key'] == PROPERTY), {})
        else:
            prop = self.request('GET', '/rest/api/3/issue/' + key + '/properties/' + PROPERTY)
        if prop.get('value', {}).get('event_id') != event_id: raise ValueError('correlation does not match')


def recover(conn, row, body):
    job = conn.execute('SELECT * FROM jira_jobs WHERE id=? AND result_id=? AND project=?',
        (body.get('job_id'), row['id'], row['project'])).fetchone()
    if not job or job['status'] != 'uncertain' or job['kind'] == 'attachment':
        raise ValueError('only uncertain create/comment jobs can be reconciled')
    key = body.get('issue_key')
    client = JiraClient(configuration(row['project']))
    client.verify(key, job['id'], body.get('comment_id') if job['kind'] == 'comment' else None)
    if job['kind'] == 'comment' and not body.get('comment_id'): raise ValueError('comment ID required')
    if job['kind'] == 'comment':
        group = conn.execute('SELECT issue_key FROM jira_groups WHERE id=?', (job['group_id'],)).fetchone()
        if group['issue_key'] != key: raise ValueError('comment belongs to another issue')
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        updated = conn.execute("UPDATE jira_jobs SET status='sent',last_error=NULL,remote_id=? WHERE id=? AND status='uncertain'",
            (body.get('comment_id') or key, job['id'])).rowcount
        if not updated: raise ValueError('job changed; refresh status')
        conn.execute('UPDATE jira_groups SET issue_key=? WHERE id=? AND project=?', (key, job['group_id'], row['project']))
        enqueue_attachments(conn, row, job, configuration(row['project']))
    return status_for(conn, row)
