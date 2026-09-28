"""Local QA annotations, separate from immutable execution evidence."""
from copy import deepcopy
from datetime import datetime
import hashlib
import json
from pathlib import Path
import tempfile
from uuid import uuid4
from urllib.parse import quote

from signup031.contract import utc_now
from signup031.viewer_model import _load_one

CLASSIFICATIONS = ('미분류', '제품 결함', '자동화 코드', '환경', '테스트 데이터')
REPORT_FIELDS = {'title': '제목', 'environment': '환경', 'steps': '재현 단계',
                 'expected': '기대 결과', 'actual': '실제 결과', 'evidence': '증거 참조', 'notes': 'QA 메모'}


def collected_environment(record):
    payload = json.loads(record.source_path.read_text(encoding='utf-8'))
    environment = payload.get('environment', {})
    if not isinstance(environment, dict):
        environment = {}
    if record.android_scenario is not None:
        return {key: str(environment[key]) if isinstance(environment.get(key),(str,int)) and environment[key] != '' else '미수집'
                for key in ('serial','package','activity','app_version_name','app_version_code','app_pid','device_model','android_release','android_sdk','adapter','host_platform')}
    return {key: environment[key] if isinstance(environment.get(key), str) and environment[key] else '미수집'
            for key in ('browser', 'browser_version', 'driver_version', 'platform', 'python', 'server')}


def investigation_candidates(record):
    """Candidates always name the evidence and uncertainty, never assign a verdict."""
    evidence = []
    for index, check in enumerate(record.checks):
        if not check['passed'] and check.get('status') != 'not_run':
            evidence.append(f'evidence.json#/checks/{index}: 기대 {check["expected"]!r}, 실제 {check["actual"]!r}')
    if record.expected_maximum is not None and record.after_extra_length is not None and record.expected_maximum != record.after_extra_length:
        evidence.append(f'evidence.json#/measurements/after_extra_length: 기대 {record.expected_maximum}, 실제 {record.after_extra_length}')
    if evidence:
        return [{'status': '미확인', 'candidate': '제품 동작·검증 기준·테스트 데이터 차이 가능성',
                 'evidence': '\n'.join(evidence), 'missing': '승인된 요구사항, 동일 데이터의 독립 재현',
                 'next': '기대값의 근거를 확인하고 동일 단계·데이터로 직접 재현하세요.'}]
    if record.android_scenario is not None and record.business_status in ('preparation_failed','execution_error','cancelled','interrupted'):
        return [{'status':'미확인','candidate':'Android 기기·앱·실행 단계 확인 필요',
                 'evidence':f'evidence.json#/result: {record.business_status} · {record.message}',
                 'missing':'기기 연결·권한·설치 앱·전면 앱 상태 및 동일 조건 재실행',
                 'next':'실행 오류와 제품 기대값 불일치를 구분하고 미실행 항목을 확인하세요.'}]
    if record.business_status == 'preparation_failed':
        return [{'status': '미확인', 'candidate': '실행 준비·자동화 대상·환경 확인 필요',
                 'evidence': f'evidence.json#/result/business: 단계 {record.business_phase}, 메시지 {record.message}',
                 'missing': '서버 상태, 요소 변경 내역, 동일 조건의 재실행 결과',
                 'next': '실패 단계와 대상 요소·접속 상태를 확인하세요. 오류 이름만으로 원인을 확정할 수 없습니다.'}]
    if record.selenium_record is not None:
        return [{'status': '미확인', 'candidate': 'Selenium assertion·동작·자료 제한 대조 필요',
                 'evidence': 'evidence.json#/result/pytest: ' + record.message,
                 'missing': '승인된 요구사항과 동일 환경 재검증',
                 'next': 'pytest 오류와 수집된 동작·관측을 대조하세요. 수집 제한만으로 원인을 확정하지 않습니다.'}]
    if record.manual_record is not None:
        limits = record.manual_record['limitations']
        return [{'status': '미확인', 'candidate': '수동 관측의 요구사항 대조 필요',
                 'evidence': 'evidence.json#/manual_record/observed: ' + record.manual_record['observed']['text'] +
                             ('\n기록 제한: ' + '; '.join(limits) if limits else ''),
                 'missing': 'QA 기대 결과와 승인된 요구사항',
                 'next': '저장된 관측과 요구사항을 비교하고 기록 제한을 고려해 QA 근거를 작성하세요.'}]
    return [{'status': '미확인', 'candidate': '원인 판단 근거 부족',
             'evidence': f'evidence.json#/result/business: {record.business_status} · {record.message}',
             'missing': '원인 판단에 필요한 재현 및 비교 근거', 'next': '문제 시점과 기대 동작을 확인하세요.'}]


def default_report(record, root):
    expected = actual = steps = '미수집'
    if record.android_scenario:
        steps='\n'.join(f'{i}. {row["action"]} {row["locator"]} {row["target"]} {row.get("value", "")}' for i,row in enumerate(record.android_scenario['steps'],1))
        expected='\n'.join(f'{row["target"]}: {row["expected"]!r}' for row in record.android_scenario['checks'])
        actual='\n'.join(f'{row["target"]}: {row["status"]} · {row["actual"]!r}' for row in record.checks)
    elif record.scenario_snapshot:
        steps = '\n'.join(f'{i}. {row["action"]} {row["locator"]} {row["target"]} {row["value"]}'
                          for i, row in enumerate(record.scenario_snapshot['steps'], 1))
        expected = '\n'.join(f'{row["target"]}: {row["expected"]!r}' for row in record.scenario_snapshot['checks'])
        actual = '\n'.join(f'{row["target"]}: {row["actual"]!r}' for row in record.checks) or '미수집'
    elif record.selenium_record:
        steps = '\n'.join(f'{i}. {row["action"]} {row["selector"]} {row.get("value", "")}'
                          for i, row in enumerate(record.selenium_record['actions'], 1)) or '미수집'
        actual = record.selenium_record['observed']['text'] + '\n원본 pytest: ' + record.message
    elif record.manual_record:
        steps = '\n'.join(f'{i}. {row["action"]} {row["selector"]} {row.get("value", "")}'
                          for i, row in enumerate(record.manual_record['actions'], 1)) or '미수집'
        actual = record.manual_record['observed']['text'] or '미수집'
    elif record.expected_maximum is not None:
        expected = f'최대 입력 길이: {record.expected_maximum}'
        actual = f'입력 길이: {record.after_extra_length}' if record.after_extra_length is not None else '미수집'
    environment = '\n'.join(f'{key}: {value}' for key, value in collected_environment(record).items())
    target = f'Android 앱: {record.android_scenario["package"]}' if record.android_scenario else f'대상 URL: {record.target_url or "미수집"}'
    return {'title': record.title, 'environment': f'{target}\n{environment}',
            'steps': steps, 'expected': expected, 'actual': actual,
            'evidence': record.source_path.relative_to(root).as_posix(), 'notes': ''}


def execution_differences(first, second):
    differences = []
    for label, a, b in (('TC', first.tc_id, second.tc_id), ('URL', first.target_url, second.target_url),
                        ('대상 종류', first.target_kind, second.target_kind)):
        if a != b:
            differences.append(f'{label}: {a} → {b}')
    if first.scenario_snapshot != second.scenario_snapshot or first.android_scenario != second.android_scenario:
        differences.append('TC 설정/단계/기대 결과 또는 출처가 다릅니다')
    env_first, env_second = collected_environment(first), collected_environment(second)
    for key in dict.fromkeys([*env_first,*env_second]):
        if env_first.get(key) != env_second.get(key):
            differences.append(f'환경 {key}: {env_first.get(key,"미수집")} → {env_second.get(key,"미수집")}')
    return differences


class InvestigationStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        if any((p / 'evidence.json').exists() for p in (self.root, *self.root.parents)):
            raise ValueError('원본 실행 폴더 밖의 결과 루트를 선택하세요')

    def _safe(self, relative):
        path = self.root / '.qa' / relative
        boundary = self.root / '.qa'
        for parent in (path, *path.parents):
            if parent == self.root:
                break
            if parent.resolve() != parent:
                raise ValueError('QA 경로 연결/외부 경로는 허용하지 않습니다')
            if parent != path and (parent / 'evidence.json').exists():
                raise ValueError('원본 기록 경로에는 저장할 수 없습니다')
        if not path.resolve().is_relative_to(boundary):
            raise ValueError('QA 경로 이탈')
        return path

    def path_for(self, execution_id):
        if not isinstance(execution_id, str) or not execution_id or len(execution_id) > 10000:
            raise ValueError('실행 ID 오류')
        key = hashlib.sha256(execution_id.encode()).hexdigest()
        return self._safe(Path('investigations') / (key + '.json'))

    def records(self):
        records = []
        for path in self.root.rglob('evidence.json'):
            if '.qa' in path.relative_to(self.root).parts or path.resolve() != path or not path.is_file():
                continue
            records.append(_load_one(path, self.root))
        return records

    def record(self, execution_id):
        matches = [r for r in self.records() if r.execution_id == execution_id]
        if len(matches) != 1:
            raise ValueError('실행 없음 또는 실행 ID 중복')
        record = matches[0]
        if record.business_status not in ('passed', 'failed', 'preparation_failed', 'unjudged','execution_error','cancelled','interrupted'):
            raise ValueError('원본 자료 읽기/형식 오류')
        return record

    def _reference(self, record):
        return {'execution_id': record.execution_id,
                'sha256': hashlib.sha256(record.source_path.read_bytes()).hexdigest()}

    def _retest_record(self, execution_id):
        record = self.record(execution_id)
        payload = json.loads(record.source_path.read_text(encoding='utf-8'))
        if payload.get('replay', {}).get('status') == 'recorded' and record.archive_root is None:
            raise ValueError('재검증 자료 검증 실패 · ' + record.archive_message)
        return record

    def _validate(self, doc, execution_id):
        try:
            if set(doc) != {'version', 'source', 'revision', 'classification', 'grounds', 'notes', 'report', 'retests', 'history'}:
                raise ValueError('필드 오류')
            if doc['version'] != 1 or type(doc['revision']) is not int or doc['revision'] < 0:
                raise ValueError('버전/수정 번호 오류')
            if doc['source'] != self._reference(self.record(execution_id)):
                raise ValueError('원본 ID/해시 불일치')
            if doc['classification'] not in CLASSIFICATIONS or set(doc['report']) != set(REPORT_FIELDS):
                raise ValueError('QA 분류/보고서 오류')
            for value in (doc['grounds'], doc['notes'], *doc['report'].values()):
                if not isinstance(value, str) or len(value) > 100000:
                    raise ValueError('QA 텍스트 오류')
            if not isinstance(doc['history'], list) or not isinstance(doc['retests'], list):
                raise ValueError('QA 이력 오류')
            seen = set()
            for link in doc['retests']:
                if set(link) != {'target', 'reason', 'differences', 'source_status', 'target_status', 'linked_at'}:
                    raise ValueError('재검증 참조 오류')
                ref = link['target']
                if set(ref) != {'execution_id', 'sha256'} or not all(isinstance(v, str) for v in ref.values()):
                    raise ValueError('재검증 대상 오류')
                if ref['execution_id'] == execution_id or ref['execution_id'] in seen:
                    raise ValueError('자기 자신 또는 중복 재검증')
                seen.add(ref['execution_id'])
                if len(ref['sha256']) != 64 or any(c not in '0123456789abcdef' for c in ref['sha256']):
                    raise ValueError('재검증 해시 오류')
                if not all(isinstance(link[k], str) for k in ('reason', 'linked_at', 'source_status', 'target_status')):
                    raise ValueError('재검증 메타데이터 오류')
                if not isinstance(link['differences'], list) or not all(isinstance(d, str) for d in link['differences']):
                    raise ValueError('재검증 차이 오류')
            for entry in doc['history']:
                if not isinstance(entry, dict) or set(entry) != {'at', 'classification', 'grounds', 'notes', 'report'}:
                    raise ValueError('QA 변경 이력 오류')
                if entry['classification'] not in CLASSIFICATIONS or set(entry['report']) != set(REPORT_FIELDS):
                    raise ValueError('QA 변경 이력 오류')
                if not all(isinstance(v, str) for v in (entry['at'], entry['grounds'], entry['notes'], *entry['report'].values())):
                    raise ValueError('QA 변경 이력 오류')
        except (KeyError, TypeError, AttributeError) as exc:
            raise ValueError('QA 자료 손상') from exc
        return doc

    def load(self, execution_id):
        record = self.record(execution_id)
        path = self.path_for(execution_id)
        if path.exists():
            try:
                doc = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, UnicodeError, ValueError) as exc:
                raise ValueError(f'QA 자료 읽기 실패: {exc}') from exc
            return self._validate(doc, execution_id)
        return {'version': 1, 'source': self._reference(record), 'revision': 0,
                'classification': '미분류', 'grounds': '', 'notes': '',
                'report': default_report(record, self.root), 'retests': [], 'history': []}

    def _write(self, relative, contents):
        path = self._safe(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path = self._safe(relative)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, suffix='.tmp', delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(contents)
            self._safe(relative)
            temporary.replace(path)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()
        return path

    def save(self, doc, *, _new_link=None):
        execution_id = doc['source']['execution_id']
        self._validate(doc, execution_id)
        previous = self.load(execution_id)
        if doc['revision'] != previous['revision']:
            raise ValueError('다른 창에서 변경되었습니다. 닫고 다시 열어주세요')
        if doc['history'] != previous['history'] or doc['retests'] != previous['retests']:
            raise ValueError('기존 이력은 직접 변경할 수 없습니다')
        updated = deepcopy(doc)
        updated['revision'] += 1
        updated['history'].append({'at': utc_now(), **{key: deepcopy(doc[key]) for key in ('classification', 'grounds', 'notes', 'report')}})
        if _new_link is not None:
            updated['retests'].append(_new_link)
        self._validate(updated, execution_id)
        path = self.path_for(execution_id)
        self._write(path.relative_to(self.root / '.qa'), json.dumps(updated, ensure_ascii=False, indent=2) + '\n')
        return updated

    def import_remote(self, doc):
        """Explicit import into an unedited local investigation only."""
        execution_id = doc['source']['execution_id']
        self._validate(doc, execution_id)
        local = self.load(execution_id)
        if local == doc: return local
        if local['revision'] != 0:
            raise ValueError('로컬 QA가 있습니다. 내보내기로 비교하세요. 자동 덮어쓰기는 하지 않습니다')
        path = self.path_for(execution_id)
        self._write(path.relative_to(self.root / '.qa'), json.dumps(doc, ensure_ascii=False, indent=2) + '\n')
        return doc

    def link_retest(self, doc, target_id, reason):
        source = self.record(doc['source']['execution_id'])
        target = self._retest_record(target_id)
        if source.execution_id == target_id or any(link['target']['execution_id'] == target_id for link in doc['retests']):
            raise ValueError('자기 자신 또는 중복 재검증 연결')
        try:
            if datetime.fromisoformat(target.started_at.replace('Z', '+00:00')) <= datetime.fromisoformat(source.started_at.replace('Z', '+00:00')):
                raise ValueError('원본 이후의 후속 실행을 선택하세요')
        except (TypeError, AttributeError) as exc:
            raise ValueError('실행 시각 미수집: 후속 실행 여부 확인 불가') from exc
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 100000:
            raise ValueError('재검증 연결 이유를 입력하세요')
        link = {'target': self._reference(target), 'reason': reason, 'differences': execution_differences(source, target),
                'source_status': source.business_status, 'target_status': target.business_status, 'linked_at': utc_now()}
        return self.save(doc, _new_link=link)

    def retest_details(self, doc):
        details = []
        for link in doc['retests']:
            try:
                target = self._retest_record(link['target']['execution_id'])
                if self._reference(target) != link['target']:
                    raise ValueError('재검증 원본 해시 불일치')
                message = f'{link["source_status"]} → {target.business_status}'
                available = True
            except (ValueError, OSError) as exc:
                message, available = str(exc), False
            details.append({**deepcopy(link), 'available': available, 'message': message})
        return details

    def export(self, doc):
        # Export only the persisted revision; UI saves edits before invoking this.
        if self.load(doc['source']['execution_id']) != doc:
            raise ValueError('저장한 QA 자료와 다릅니다. 먼저 저장하세요')
        record = self.record(doc['source']['execution_id'])
        lines = [f'# {doc["report"]["title"]}', '', f'원본 상태: {record.status_label}',
                 f'QA 분류: {doc["classification"]}', f'실행 ID: {record.execution_id}', '',
                 '## QA 분류 근거', doc['grounds'] or '미수집', '', '## 조사 메모', doc['notes'] or '미수집']
        for key, caption in REPORT_FIELDS.items():
            if key != 'title':
                lines.extend(['', '## ' + caption, doc['report'][key] or '미수집'])
        link = '../../' + quote(record.source_path.relative_to(self.root).as_posix(), safe='/')
        lines.extend(['', f'[원본 evidence]({link})', '', '## 기계 후보 · 미확인'])
        for candidate in investigation_candidates(record):
            lines.extend([candidate['candidate'], '근거: ' + candidate['evidence'],
                          '부족한 정보: ' + candidate['missing'], '다음 확인: ' + candidate['next'], ''])
        lines.extend(['## 재검증 이력', '미수집 환경 항목은 비교할 수 없습니다. URL 일치만으로 동일 환경을 보장하지 않습니다.'])
        for detail in self.retest_details(doc):
            lines.extend([detail['target']['execution_id'] + ' · ' + detail['message'],
                          '연결 이유: ' + detail['reason'], '차이: ' + ('; '.join(detail['differences']) or '수집된 TC/URL 차이 없음')])
        name = hashlib.sha256(record.execution_id.encode()).hexdigest() + '-' + uuid4().hex + '.md'
        return self._write(Path('exports') / name, '\n\n'.join(lines) + '\n')
