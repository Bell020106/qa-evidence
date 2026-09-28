"""PC-owned synchronization process; Jira credentials never reach this client."""
import json
from pathlib import Path
import sys

from signup031.ingestion_client import IngestionClient
from signup031.investigation import InvestigationStore


def main():
    try:
        request = json.loads(sys.stdin.readline())
        store = InvestigationStore(request['root'])
        record = store.record(request['execution_id'])
        source = json.loads((record.source_path.parent / 'remote-source.json').read_text(encoding='utf-8'))
        client = IngestionClient(source['server_origin'], source['project_id'], request['token'])
        rid = source['result_id']
        relative = (record.source_path.parent / 'remote-jira.json').relative_to(store.root).as_posix()
        action = request['action']
        if action == 'publish':
            # Only the last observed revision is used. Never refresh behind the user's edits.
            snapshot = json.loads((store.root / relative).read_text(encoding='utf-8'))
            state = client.publish_qa(rid, store.load(record.execution_id), snapshot['qa_revision'])
        elif action == 'recover':
            state = client.recover_jira(rid, request['job_id'], request['issue_key'], request.get('comment_id'))
        elif action == 'retry':
            state = client.retry_jira(rid, request['job_id'])
        else:
            state = client.jira_status(rid)
            if action == 'import_qa':
                if state['qa'] is None: raise ValueError('서버에 QA 메모가 없습니다')
                store.import_remote(state['qa'])
            elif action != 'refresh': raise ValueError('unknown sync action')
        if state['result_id'] != rid: raise ValueError('remote result mismatch')
        client._atomic_write(store.root, relative, json.dumps(state, ensure_ascii=False).encode())
        print(json.dumps({'status': 'complete', 'state': state}, ensure_ascii=False), flush=True)
        return 0
    except (ValueError, OSError, KeyError, TypeError):
        print(json.dumps({'status': 'error', 'reason': '동기화 실패: 인증·연결·서버 수정 충돌·원본 참조를 확인하세요. 로컬 QA는 보존됩니다.'}), flush=True)
        return 2


if __name__ == '__main__': raise SystemExit(main())
