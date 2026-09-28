"""Explicitly unjudged TC drafts and portable, verified manual-record references."""
import hashlib
from pathlib import Path

from signup031.viewer_model import load_evidence_root
from signup031.web_scenario import validate_scenario


def draft_from_manual(path):
    path = Path(path).resolve()
    records = [record for record in load_evidence_root(path.parent) if record.source_path == path]
    if len(records) != 1 or records[0].manual_record is None or records[0].archive_root is None:
        raise ValueError('정상적으로 저장되고 검증된 수동 기록이 필요합니다')
    original = records[0]
    if not isinstance(original.execution_id, str) or not original.execution_id.strip():
        raise ValueError('원본 기록 ID가 없습니다')
    record = original.manual_record
    draft = {'version': 1, 'id': 'TC-' + original.execution_id, 'title': record['title'] + ' · TC 초안',
             'url': record['start_url'], 'draft': True,
             'steps': [{'action': action['action'], 'locator': 'css', 'target': action['selector'],
                        'value': action.get('value', '')} for action in record['actions']],
             'checks': [],
             'source_manual': {'execution_id': original.execution_id,
                               'evidence_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                               'limitations': list(record['limitations'])}}
    return validate_scenario(draft, allow_draft=True)


def resolve_manual_source(records, source):
    candidates = [record for record in records if record.execution_id == source['execution_id']]
    if len(candidates) != 1:
        return None, '원본 출처 없음 또는 동일 ID 중복 · 원본이 포함된 결과 폴더를 선택하세요'
    record = candidates[0]
    try:
        if record.manual_record is None or record.archive_root is None:
            raise ValueError('원본 수동 기록 또는 아카이브 검증 실패')
        if hashlib.sha256(record.source_path.read_bytes()).hexdigest() != source['evidence_sha256']:
            raise ValueError('원본 evidence 해시 불일치')
        if record.manual_record['limitations'] != source['limitations']:
            raise ValueError('원본 제한 사유 불일치')
    except (OSError, ValueError) as exc:
        return None, str(exc)
    return record, '원본 수동 기록 · ' + record.execution_id
