"""Independent bounded worker for the receiver's durable Jira outbox."""
import argparse
import json
from pathlib import Path
import sqlite3
import time
from uuid import uuid4

from signup031.ingestion_contract import digest, safe_path
from signup031.jira_delivery import (JiraClient, JiraFailure, PROPERTY, configuration,
    initialize, enqueue, report)


def process_one(root, *, timeout=10, lease_seconds=30):
    if not 0 < timeout <= 60 or not max(1, 2 * timeout) <= lease_seconds <= 300:
        raise ValueError('lease must exceed bounded request timeout')
    root = Path(root).absolute()
    db = safe_path(root, 'results.sqlite3')
    conn = sqlite3.connect(db, timeout=15); conn.row_factory = sqlite3.Row
    owner, now = uuid4().hex, time.time()
    try:
        initialize(conn)
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute("UPDATE jira_jobs SET status='uncertain',last_error='worker_lease_expired' WHERE status='sending' AND lease_until<?", (now,))
            candidates = conn.execute('''SELECT j.* FROM jira_jobs j JOIN jira_groups g ON g.id=j.group_id
                WHERE j.status IN ('pending','retryable') AND j.next_at<=? AND j.attempts<j.attempt_limit
                AND (j.kind='create' OR g.issue_key IS NOT NULL) ORDER BY j.seq''', (now,)).fetchall()
            job = next((candidate for candidate in candidates if configuration(candidate['project']).get('enabled') is True), None)
            if job is None: return False
            config = configuration(job['project'])
            if config.get('enabled') is not True: return False
            conn.execute("UPDATE jira_jobs SET status='sending',owner=?,lease_until=?,attempts=attempts+1 WHERE id=?",
                         (owner, now + lease_seconds, job['id']))
        row = conn.execute('SELECT * FROM results WHERE id=? AND project=?', (job['result_id'], job['project'])).fetchone()
        group = conn.execute('SELECT * FROM jira_groups WHERE id=?', (job['group_id'],)).fetchone()
        remote_id, key = None, group['issue_key']
        try:
            client = JiraClient(config, timeout)
            properties = [{'key': PROPERTY, 'value': {'event_id': job['id'], 'project': job['project'], 'result_id': job['result_id']}}]
            if job['kind'] == 'create':
                fields = {**config.get('fields', {}), 'project': {'key': config['project_key']},
                    'issuetype': {'id': config['issue_type_id']}, 'summary': ('QA failure: ' + row['test_id'])[:250],
                    'description': report(row, job)}
                result = client.request('POST', '/rest/api/3/issue', {'fields': fields, 'properties': properties})
                if not isinstance(result, dict): raise JiraFailure('uncertain', 'invalid_issue_response')
                import re
                key = result.get('key')
                if not isinstance(key, str) or not re.fullmatch(re.escape(config['project_key']) + r'-[1-9][0-9]*', key):
                    raise JiraFailure('uncertain', 'invalid_issue_response')
                remote_id = key
            elif job['kind'] == 'comment':
                result = client.request('POST', '/rest/api/3/issue/' + key + '/comment', {'body': report(row, job), 'properties': properties})
                if not isinstance(result, dict): raise JiraFailure('uncertain', 'invalid_comment_response')
                remote_id = result.get('id')
                if not isinstance(remote_id, str) or not remote_id.isdigit(): raise JiraFailure('uncertain', 'invalid_comment_response')
            else:
                data = json.loads(job['payload']); name = data['path']
                attachment = safe_path(root, 'attachments/' + row['id'] + '/' + digest(name.encode())[:24])
                if not attachment.is_file(): raise JiraFailure('retryable', 'receiver_attachment_missing', 30)
                raw = attachment.read_bytes()
                if digest(raw) != data['sha256']: raise JiraFailure('configuration_error', 'attachment_integrity')
                boundary = 'qa' + uuid4().hex
                filename = job['id'] + '-' + Path(name).name
                body = ('--' + boundary + '\r\nContent-Disposition: form-data; name="file"; filename="' + filename +
                    '"\r\nContent-Type: application/octet-stream\r\n\r\n').encode() + raw + ('\r\n--' + boundary + '--\r\n').encode()
                result = client.request('POST', '/rest/api/3/issue/' + key + '/attachments', body, 'multipart/form-data; boundary=' + boundary)
                if not isinstance(result, list) or not result or not isinstance(result[0], dict): raise JiraFailure('uncertain', 'invalid_attachment_response')
                remote_id = str(result[0].get('id', ''))
            state, error, next_at = 'sent', None, 0
        except JiraFailure as exc:
            state, error, next_at = exc.status, exc.reason, time.time() + exc.delay
            if state == 'retryable' and job['attempts'] + 1 >= job['attempt_limit']:
                state, error = 'configuration_error', 'retry_limit_reached'
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            changed = conn.execute('''UPDATE jira_jobs SET status=?,last_error=?,next_at=?,remote_id=?,lease_until=NULL
                WHERE id=? AND owner=? AND status='sending' ''', (state, error, next_at, remote_id, job['id'], owner)).rowcount
            if changed and state == 'sent':
                conn.execute('UPDATE jira_groups SET issue_key=? WHERE id=?', (key, job['group_id']))
                if job['kind'] in ('create', 'comment'):
                    from signup031.jira_delivery import enqueue_attachments
                    enqueue_attachments(conn, row, job, config)
        return True
    finally: conn.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description='Independent Jira outbox worker; credentials are server environment only')
    parser.add_argument('--storage', type=Path, required=True)
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--timeout', type=float, default=10)
    parser.add_argument('--lease-seconds', type=float, default=30)
    args = parser.parse_args(argv)
    try:
        while True:
            processed = process_one(args.storage, timeout=args.timeout, lease_seconds=args.lease_seconds)
            if args.once: return 0
            if not processed: time.sleep(1)
    except KeyboardInterrupt: return 0
    except (ValueError, OSError, sqlite3.Error):
        print('Jira worker configuration/storage error; no remote response body logged')
        return 2


if __name__ == '__main__': raise SystemExit(main())
