from dataclasses import dataclass, replace
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


STATUS_LABELS = {
    'running': 'Android 실행 중',
    'execution_error': '실행 오류',
    'cancelled': '취소',
    'interrupted': '중단',
    'skipped': '건너뜀',
    'unjudged': '수동 기록 · 판정 미입력',
    "passed": "통과",
    "failed": "실패",
    "preparation_failed": "준비 실패",
    "read_error": "읽기 오류",
    "incomplete": "불완전 기록",
    "legacy": "구형 형식",
}

TARGET_LABELS = {
    "local_demo": "로컬 데모",
    "live": "실제 사이트",
}


@dataclass(frozen=True)
class ScreenshotReference:
    status: str
    path: Path | None
    reason: str | None


@dataclass(frozen=True)
class EvidenceRecord:
    source_path: Path
    execution_id: str
    started_at: str | None
    tc_id: str | None
    business_status: str
    business_phase: str | None
    pytest_status: str | None
    pytest_phase: str | None
    expected_maximum: int | None
    initial_length: int | None
    after_extra_length: int | None
    target_kind: str | None
    target_label: str
    target_url: str | None
    cleanup_status: str | None
    cleanup_errors: tuple[str, ...]
    message: str
    screenshot: ScreenshotReference
    archive_root: Path | None = None
    archive_message: str = '재현 자료 없음 · 기록을 켜고 다시 실행'
    title: str = '비밀번호 최대 길이 실행 상세'
    checks: tuple = ()
    scenario_snapshot: dict | None = None
    manual_record: dict | None = None
    selenium_record: dict | None = None
    selenium_details: str = ''
    remote_details: str = ''
    android_scenario: dict | None = None
    android_details: str = ''
    android_log: str | None = None

    @property
    def status_label(self) -> str:
        return STATUS_LABELS[self.business_status]


class IncompleteEvidence(ValueError):
    pass


def _nested(payload: dict[str, Any], *keys: str) -> Any:
    value: Any = payload
    for key in keys:
        if not isinstance(value, dict) or key not in value:
            raise IncompleteEvidence(".".join(keys))
        value = value[key]
    return value


def _integer_or_none(value: Any, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise IncompleteEvidence(field)
    return value


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _resolve_screenshot(payload: dict[str, Any], evidence_path: Path, root: Path) -> ScreenshotReference:
    screenshot = _nested(payload, "evidence", "screenshot")
    if not isinstance(screenshot, dict):
        raise IncompleteEvidence("evidence.screenshot")
    status = screenshot.get("status")
    reason = screenshot.get("reason")
    if status != "collected":
        return ScreenshotReference(str(status or "unknown"), None, str(reason) if reason else None)

    declared = screenshot.get("path")
    if not isinstance(declared, str) or not declared:
        return ScreenshotReference("missing", None, "스크린샷 경로가 없습니다")
    parsed = urlparse(declared)
    if parsed.scheme.lower() in {"http", "https"}:
        return ScreenshotReference("remote_not_allowed", None, "원격 이미지는 자동 요청하지 않습니다")

    declared_path = Path(declared)
    candidate = declared_path if declared_path.is_absolute() else evidence_path.parent / declared_path
    candidate = candidate.resolve()
    fallback = (evidence_path.parent / declared_path.name).resolve()
    if _inside(candidate, root) and candidate.is_file():
        return ScreenshotReference("available", candidate, None)
    if fallback.is_file() and _inside(fallback, root):
        return ScreenshotReference("available", fallback, None)
    if not _inside(candidate, root):
        return ScreenshotReference("outside_root", None, "선택한 결과 폴더 밖의 파일은 읽지 않습니다")
    return ScreenshotReference("missing", None, "스크린샷 파일이 없습니다")


def _error_record(path: Path, status: str, message: str, execution_id: str | None = None) -> EvidenceRecord:
    return EvidenceRecord(
        source_path=path,
        execution_id=execution_id or path.parent.name,
        started_at=None,
        tc_id=None,
        business_status=status,
        business_phase=None,
        pytest_status=None,
        pytest_phase=None,
        expected_maximum=None,
        initial_length=None,
        after_extra_length=None,
        target_kind=None,
        target_label="알 수 없음",
        target_url=None,
        cleanup_status=None,
        cleanup_errors=(),
        message=message,
        screenshot=ScreenshotReference("not_available", None, message),
    )


def _parse_current(payload: dict[str, Any], path: Path, root: Path) -> EvidenceRecord:
    business_status = _nested(payload, "result", "business", "status")
    if not isinstance(business_status, str) or business_status not in {
        "passed",
        "failed",
        "preparation_failed",
    }:
        raise IncompleteEvidence("result.business.status")
    tc_id = _nested(payload, "tc_id")
    if not isinstance(tc_id, str) or not tc_id.strip():
        raise IncompleteEvidence("tc_id")
    execution_id = _nested(payload, "execution", "id")
    started_at = _nested(payload, "execution", "started_at")
    business_phase = _nested(payload, "result", "business", "phase")
    message = _nested(payload, "result", "business", "message")
    pytest_status = _nested(payload, "result", "pytest", "status")
    pytest_phase = _nested(payload, "result", "pytest", "phase")
    expected = _integer_or_none(
        _nested(payload, "expected_result_snapshot", "maximum_length"),
        "expected_result_snapshot.maximum_length",
    )
    initial = _integer_or_none(
        _nested(payload, "measurements", "initial_length"),
        "measurements.initial_length",
    )
    after_extra = _integer_or_none(
        _nested(payload, "measurements", "after_extra_length"),
        "measurements.after_extra_length",
    )
    if expected is None:
        raise IncompleteEvidence("expected_result_snapshot.maximum_length")
    if business_status in {"passed", "failed"}:
        if initial is None:
            raise IncompleteEvidence("measurements.initial_length")
        if after_extra is None:
            raise IncompleteEvidence("measurements.after_extra_length")
    target_kind = _nested(payload, "target", "kind")
    target_url = _nested(payload, "target", "url")
    cleanup_status = _nested(payload, "post_run", "cleanup", "status")
    cleanup_errors = _nested(payload, "post_run", "cleanup", "errors")
    if not all(isinstance(value, str) for value in (
        execution_id,
        started_at,
        business_phase,
        message,
        pytest_status,
        pytest_phase,
        target_kind,
        target_url,
        cleanup_status,
    )) or not isinstance(cleanup_errors, list):
        raise IncompleteEvidence("field type")
    if not all(isinstance(value, str) for value in cleanup_errors):
        raise IncompleteEvidence("post_run.cleanup.errors")
    archive_root, archive_message = replay_reference(payload, path)
    return EvidenceRecord(
        source_path=path,
        execution_id=execution_id,
        started_at=started_at,
        tc_id=tc_id,
        business_status=business_status,
        business_phase=business_phase,
        pytest_status=pytest_status,
        pytest_phase=pytest_phase,
        expected_maximum=expected,
        initial_length=initial,
        after_extra_length=after_extra,
        target_kind=target_kind,
        target_label=TARGET_LABELS.get(target_kind, "알 수 없음"),
        target_url=target_url,
        cleanup_status=cleanup_status,
        cleanup_errors=tuple(cleanup_errors),
        message=message,
        screenshot=_resolve_screenshot(payload, path, root),
        archive_root=archive_root,
        archive_message=archive_message,
    )


def replay_reference(payload, evidence_path):
    metadata = payload.get('replay')
    if not isinstance(metadata, dict):
        return None, '재현 자료 없음 · 기록을 켜고 다시 실행'
    if metadata.get('status') != 'recorded':
        return None, '기록 실패: ' + str(metadata.get('reason'))
    try:
        from signup031.archive import load_archive
        relative = Path(metadata['manifest'])
        parent = evidence_path.parent.resolve()
        manifest_path = (parent / relative).resolve()
        if relative.is_absolute() or not manifest_path.is_relative_to(parent):
            raise ValueError('archive path escapes result folder')
        manifest, _ = load_archive(manifest_path.parent)
        if manifest['execution_id'] != payload['execution']['id']:
            raise ValueError('archive execution does not match result')
        if payload.get('contract_version') == '2':
            if manifest.get('archive_version') != 2 or manifest.get('scenario_snapshot') != payload.get('scenario_snapshot'):
                raise ValueError('archive scenario does not match result')
            if manifest.get('observed_checks') != payload.get('checks'):
                raise ValueError('archive observations do not match result')
        if payload.get('contract_version') == '3':
            if manifest.get('archive_version') != 3 or manifest.get('manual_record') != payload.get('manual_record'):
                raise ValueError('archive manual recording does not match result')
        if payload.get('contract_version') == '4':
            if (manifest.get('archive_version') != 4 or manifest.get('selenium_record') != payload.get('selenium_record') or
                manifest.get('test_identity') != payload.get('test_identity')):
                raise ValueError('archive Selenium identity does not match result')
        return manifest_path.parent, '기록 완료 · 복원 확인 전'
    except Exception as exc:
        return None, f'재현 자료 오류: {type(exc).__name__}: {exc}'


def _with_transfer_state(record: EvidenceRecord, path: Path) -> EvidenceRecord:
    if '_server_results' not in path.parts:
        return record
    try:
        source = json.loads((path.parent / 'remote-source.json').read_text(encoding='utf-8'))
        details = ('서버 출처 · ' + str(source['server_origin']) + ' · 프로젝트 ' + str(source['project_id']) +
                   '\n제공자/실행/시도: ' + str(source['provider']) + ' / ' + str(source['source_run_id']) +
                   ' / ' + str(source['attempt']))
        provenance = source.get('provenance', {})
        if isinstance(provenance, dict):
            details += ''.join('\n' + str(key) + ': ' + str(value) for key, value in provenance.items())
        if source['transfer_status'] == 'complete':
            return replace(record, remote_details=details)
        missing = ', '.join(source.get('missing_files', []))
        reason = '서버 첨부 다운로드 미완료' + (' · ' + missing if missing else '')
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError):
        reason = '서버 전송 상태 자료 없음/손상'
        details = ''
    return replace(record, archive_root=None, archive_message=reason,
                   message=record.message + '\n' + reason, remote_details=details)


def _parse_android(payload, path, root):
    import hashlib
    from signup031.android_adapter import validate_scenario
    config=validate_scenario(payload['android_scenario'])
    status=payload['result']['status']
    if status not in ('running','passed','failed','preparation_failed','execution_error','cancelled','interrupted'):
        raise ValueError('Android result status')
    for key in ('execution_id','started_at','tc_id'):
        if not isinstance(payload.get(key),str) or not payload[key] or len(payload[key])>200:
            raise ValueError('Android identity')
    if payload['tc_id']!=config['id'] or payload['target']!={'kind':'android',**{key:config[key] for key in ('serial','package','activity')}}:
        raise ValueError('Android target identity mismatch')
    checks=payload['checks'];steps=payload['steps']
    if not isinstance(checks,list) or len(checks)!=len(config['checks']) or not isinstance(steps,list) or len(steps)!=len(config['steps']):
        raise ValueError('Android measured rows mismatch')
    for source,step in zip(config['steps'],steps):
        if {k:v for k,v in step.items() if k!='status'}!=source or step['status'] not in ('not_run','running','completed','error','cancelled','interrupted'):
            raise ValueError('Android step snapshot mismatch')
    for source,check in zip(config['checks'],checks):
        if {k:v for k,v in check.items() if k not in ('actual','status')}!=source or check['status'] not in ('not_run','passed','failed'):
            raise ValueError('Android check snapshot mismatch')
        if check['status']=='not_run':
            if check['actual'] is not None: raise ValueError('Unperformed Android measurement')
        else:
            if type(check['actual']) is not type(check['expected']): raise ValueError('Android measurement type')
            if (check['actual']==check['expected']) != (check['status']=='passed'): raise ValueError('Android assertion mismatch')
    if status in ('passed','failed'):
        if any(s['status']!='completed' for s in steps) or any(c['status']=='not_run' for c in checks): raise ValueError('Android incomplete verdict')
        if (status=='passed')!=all(c['status']=='passed' for c in checks): raise ValueError('Android verdict mismatch')
    environment=payload['environment']
    if not isinstance(environment,dict) or len(environment)>30 or any(not isinstance(v,(str,int)) for v in environment.values()):
        raise ValueError('Android environment')
    details=['Android 환경']+[f'{key}: {value}' for key,value in environment.items()]
    screenshot=ScreenshotReference('not_available',None,'미수집');log_text=None
    for name in ('screenshot','hierarchy','log'):
        attachment=payload['evidence'][name]
        if attachment['status']=='not_collected':
            details.append(name+': 미수집 · '+str(attachment.get('reason','사유 없음')))
            continue
        if attachment['status']!='collected': raise ValueError('Android attachment status')
        try:
            relative=Path(attachment['path']);candidate=(path.parent/relative).resolve()
            if relative.is_absolute() or not candidate.is_relative_to(path.parent.resolve()) or not candidate.is_relative_to(root.resolve()):
                raise ValueError('첨부 경로 이탈')
            maximum=10*1024*1024 if name=='screenshot' else 2*1024*1024
            if type(attachment['size']) is not int or not 0<=attachment['size']<=maximum or candidate.stat().st_size!=attachment['size']:
                raise ValueError('첨부 크기 불일치')
            if hashlib.sha256(candidate.read_bytes()).hexdigest()!=attachment['sha256']: raise ValueError('첨부 해시 불일치')
            details.append(f'{name}: 수집 확인 · {relative}')
            if name=='screenshot': screenshot=ScreenshotReference('available',candidate,None)
            if name=='log':
                details.append(f"로그 범위: PID {attachment['pid']} · {attachment['start_epoch']} ~ {attachment['end_epoch']} · {attachment['scope']}")
                data=candidate.read_bytes()
                if hashlib.sha256(data).hexdigest()!=attachment['sha256']:raise ValueError('첨부 해시 변경')
                text=data.decode('utf-8','replace');log_text=text[:100000]+('\n[표시 상한 100,000자 · 나머지 생략]' if len(text)>100000 else '')
        except (OSError,ValueError,KeyError) as exc:
            details.append(name+': 첨부 사용 불가 · '+str(exc))
            if name=='screenshot': screenshot=ScreenshotReference('missing',None,str(exc))
    record=_error_record(path,status,payload['result']['message'],payload['execution_id'])
    return replace(record,started_at=payload['started_at'],tc_id=config['id'],business_phase=payload['result']['phase'],
                   target_kind='android',target_label='Android · '+config['package'],title=config['title'],
                   cleanup_status=payload['cleanup']['status'],cleanup_errors=tuple(payload['cleanup']['errors']),
                   screenshot=screenshot,checks=tuple({**check,'passed':check['status']=='passed'} for check in checks),
                   android_scenario=config,android_details='\n'.join(details),android_log=log_text,
                   archive_message='Android 증거 · 같은 앱·기기에서 재검증하세요. HAR 재현/앱 전체 복제는 지원하지 않습니다.')


def _load_one(path: Path, root: Path) -> EvidenceRecord:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return _error_record(path, "read_error", f"JSON 읽기 오류: {type(exc).__name__}: {exc}")
    if not isinstance(payload, dict):
        return _error_record(path, "incomplete", "불완전 기록: JSON 최상위 객체가 아닙니다")
    if payload.get('contract_version') == 'android-1':
        try:
            return _parse_android(payload, path, root)
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            return _error_record(path, 'incomplete', f'불완전 Android 기록: {exc}')
    if payload.get('contract_version') == '4':
        try:
            return _with_transfer_state(_parse_selenium(payload, path, root), path)
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            return _error_record(path, 'incomplete', f'불완전 Selenium 기록: {exc}')
    if payload.get('contract_version') == '3':
        try:
            return _with_transfer_state(_parse_manual(payload, path, root), path)
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            return _error_record(path, 'incomplete', f'불완전 수동 기록: {exc}')
    if payload.get('contract_version') == '2':
        try:
            return _with_transfer_state(_parse_web(payload, path, root), path)
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            return _error_record(path, 'incomplete', f'불완전 기록: {exc}')
    if payload.get("contract_version") != "1":
        return _error_record(
            path,
            "incomplete",
            "불완전 기록: 지원하지 않는 contract_version입니다",
        )
    result = payload.get("result")
    execution = payload.get("execution")
    execution_id = execution.get("id") if isinstance(execution, dict) else None
    if isinstance(result, dict) and ("verdict" in result or "raw_status" in result):
        return _error_record(
            path,
            "legacy",
            "구형 형식: 현재 필수 구조 result.business/result.pytest가 없습니다",
            str(execution_id) if execution_id is not None else None,
        )
    try:
        return _with_transfer_state(_parse_current(payload, path, root), path)
    except IncompleteEvidence as exc:
        return _error_record(
            path,
            "incomplete",
            f"불완전 기록: 필수 필드가 없거나 잘못되었습니다 ({exc})",
            str(execution_id) if execution_id is not None else None,
        )


def _parse_web(payload, path, root):
    from signup031.web_scenario import validate_scenario, validate_observed_checks
    config = validate_scenario(payload['scenario_snapshot'])
    business = payload['result']['business']
    status = business['status']
    if not isinstance(status, str) or status not in ('passed', 'failed', 'preparation_failed'):
        raise ValueError('invalid business status')
    if payload['tc_id'] != config['id'] or payload['target']['url'] != config['url']:
        raise ValueError('scenario identity mismatch')
    checks = payload['checks']
    if status != 'preparation_failed':
        validate_observed_checks(config, checks)
        if (status == 'passed') != all(c['passed'] for c in checks):
            raise ValueError('business verdict does not match checks')
    elif checks != []:
        raise ValueError('unexpected incomplete checks')
    for value in (payload['execution']['id'], payload['execution']['started_at'], business['phase'], business['message']):
        if not isinstance(value, str) or not value:
            raise ValueError('invalid execution or result metadata')
    cleanup = payload['post_run']['cleanup']
    if not isinstance(cleanup['errors'], list) or not all(isinstance(e, str) for e in cleanup['errors']):
        raise ValueError('invalid cleanup errors')
    archive_root, archive_message = replay_reference(payload, path)
    return replace(_error_record(path, status, business['message']),
                   execution_id=payload['execution']['id'], started_at=payload['execution']['started_at'],
                   tc_id=config['id'], title=config['title'], business_phase=business['phase'],
                   target_kind='configured_web', target_label='설정한 웹 대상', target_url=config['url'],
                   cleanup_status=cleanup['status'], cleanup_errors=tuple(cleanup['errors']),
                   screenshot=_resolve_screenshot(payload, path, root), archive_root=archive_root,
                   archive_message=archive_message, checks=tuple(checks), scenario_snapshot=config)


def _parse_manual(payload, path, root):
    from signup031.manual_recording import validate_manual_record
    record = validate_manual_record(payload['manual_record'])
    if payload['result']['business']['status'] != 'unjudged':
        raise ValueError('manual recordings cannot have an automatic verdict')
    if payload['recording']['status'] not in ('saved', 'limited', 'failed'):
        raise ValueError('invalid recording status')
    archive_root, archive_message = replay_reference(payload, path)
    status = payload['recording']['status']
    message = {'saved':'저장 완료', 'limited':'부분 기록 · 제한 있음', 'failed':'저장 실패'}[status]
    reasons = record['limitations'] + ([payload['recording']['reason']] if payload['recording'].get('reason') else [])
    return replace(_error_record(path, 'unjudged', message + (' · ' + '; '.join(reasons) if reasons else '')),
                   execution_id=payload['execution']['id'], started_at=payload['execution']['started_at'],
                   tc_id='수동 기록', title=record['title'], business_phase='manual',
                   target_kind='manual_web', target_label='수동 웹 기록', target_url=record['final_url'],
                   cleanup_status=payload['post_run']['cleanup']['status'],
                   cleanup_errors=tuple(payload['post_run']['cleanup']['errors']),
                   screenshot=_resolve_screenshot(payload, path, root), archive_root=archive_root,
                   archive_message=archive_message, manual_record=record)


def load_evidence_root(root: Path) -> list[EvidenceRecord]:
    root = Path(root).resolve()
    if root.name.startswith('.qa-capture-') or not root.is_dir():
        return []
    records = [_load_one(path, root) for path in root.rglob("evidence.json") if path.is_file() and not any(part.startswith('.qa-capture-') for part in path.relative_to(root).parts)]
    records.sort(key=lambda record: (record.started_at or "", record.execution_id), reverse=True)
    return records


def _parse_selenium(payload, path, root):
    from signup031.selenium_recording import validate_selenium_record
    record = validate_selenium_record(payload['selenium_record'])
    business, result = payload['result']['business'], payload['result']['pytest']
    identity = payload['test_identity']
    if identity.get('nodeid') != payload['tc_id'] or not any(
        row['when'] == identity.get('phase') and row['outcome'] == identity.get('outcome') for row in result['reports']):
        raise ValueError('pytest identity mismatch')
    final = next((row for row in result['reports'] if row['outcome'] == 'failed'),
                 next((row for row in result['reports'] if row['when'] == identity['phase']), None))
    if final is None or final['when'] != result['phase'] or final['outcome'] != result['status']:
        raise ValueError('final pytest phase mismatch')
    expected_status = 'preparation_failed' if result['status'] == 'failed' and result['phase'] == 'setup' else result['status']
    if business['status'] != expected_status or business['status'] not in ('passed', 'failed', 'preparation_failed', 'skipped'):
        raise ValueError('pytest verdict mismatch')
    if type(result['session_exitcode']) is not int or not isinstance(result['reports'], list):
        raise ValueError('missing original pytest reports')
    archive_root, archive_message = replay_reference(payload, path)
    message = business['message'] + '\n수집: ' + payload['capture']['status']
    if record['limitations']:
        message += '\n제한: ' + '; '.join(record['limitations'])
    if payload['capture']['errors']:
        message += '\n수집 오류: ' + '; '.join(payload['capture']['errors'])
    return replace(_error_record(path, business['status'], message),
        execution_id=payload['execution']['id'], started_at=payload['execution']['started_at'],
        tc_id=payload['tc_id'], title=record['title'], business_phase=business['phase'],
        pytest_status=f'pytest {result["status"]} · session exit {result["session_exitcode"]}', pytest_phase=result['phase'],
        target_kind='selenium_web', target_label='원본 Selenium 세션', target_url=record['final_url'],
        cleanup_status=payload['post_run']['cleanup']['status'], cleanup_errors=tuple(payload['post_run']['cleanup']['errors']),
        screenshot=_resolve_screenshot(payload, path, root), archive_root=archive_root, archive_message=archive_message,
        selenium_record=record, selenium_details=(
            f'수집 시점: {identity["phase"]} · {identity["outcome"]}\n최종 pytest: {result["phase"]} · {result["status"]}\n' +
            '단계 결과: ' + ', '.join(row['when'] + '=' + row['outcome'] +
                (' · xfail: ' + str(row['wasxfail']) if row.get('wasxfail') else '') for row in result['reports']) +
            '\n브라우저/드라이버: ' + str(payload['environment'].get('browser_version', '미수집')) + ' / ' +
            str(payload['environment'].get('driver_version', '미수집'))))
