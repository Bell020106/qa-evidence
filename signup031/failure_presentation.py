"""Human labels for evidence; IDs and original text remain available separately."""
import json

EVIDENCE_NAMES={
    'original.failure':'원본 실패','original.environment':'실행 환경','original.observations':'수집된 화면 관측',
    'original.actions':'기록된 자동화 동작','original.limitations':'수집 제한','original.timeline':'최근 동작 기록',
    'collected.tc_id':'수집된 테스트 ID','collected.expected':'저장된 기대 결과','collected.automation_code':'자동화 코드',
    'local.restore':'로컬 복원 상태','local.verdict':'직접 확인한 결과','local.notes':'수동 조사 메모',
    'qa.expected':'QA 보고서 기대 결과','qa.steps':'QA 보고서 재현 순서','qa.notes':'QA 보고서 메모',
    'qa.investigation_notes':'QA 조사 메모','user.expected':'추가한 기대 결과','user.automation_code':'추가한 자동화 코드'}
SOURCES={'original':'원본 기록','collected_test_context':'자동화에서 명시적으로 수집',
         'local_investigation':'저장된 수동 조사','qa_report':'저장된 QA 보고서','user_added':'사용자 추가'}

def failure_summary(message):
    if 'AssertionError' in message or 'assert ' in message:return '자동화가 확인한 결과와 기대값이 일치하지 않았습니다. 정확한 비교 내용은 원문에서 확인하세요.'
    if 'Timeout' in message or 'timeout' in message:return '자동화가 제한 시간 안에 작업을 마치지 못했습니다. 어떤 작업인지 원문을 확인하세요.'
    if 'NoSuchElement' in message:return '자동화가 지정한 화면 요소를 찾지 못했습니다. 원문에서 대상과 실행 조건을 확인하세요.'
    return '원본 자동화가 남긴 결과입니다. 오류의 원인은 아직 확인되지 않았습니다. 정확한 내용은 원문에서 확인하세요.'

def restore_summary(session):
    if not session:return '로컬 조사 미실행 · 저장된 화면을 열어 직접 확인할 수 있습니다.'
    states={'starting':'로컬 화면 준비 중','ready':'수집된 범위의 화면 준비 완료','partial':'일부만 복원됨 · 제한을 확인하고 직접 조사하세요',
            'unavailable':'조사할 화면을 준비하지 못했습니다'}
    text=states.get(session['state'],'복원 상태 확인 필요')+f"\n복원한 동작 {session['completed_actions']}/{session['total_actions']}개"
    if session['stopped_action'] is not None:text+=f" · {session['stopped_action']}번 동작에서 중단"
    return text+'\n복원은 원본 자동화의 통과를 의미하지 않습니다. 중단 이유는 상세에서 확인하세요.'

def readable_evidence(snapshot):
    lines=[]
    for row in snapshot['evidence']:
        key=row['id'];content=row['content']
        if key=='original.failure':
            try:content=failure_summary(json.loads(content).get('message',''))
            except (ValueError,AttributeError):content='원본 실패 원문은 전송 내용에서 확인하세요.'
        elif key=='local.restore':
            if content=='조사 미실행':content='로컬 조사 미실행'
            elif row['truncated']:content='복원 상태 상세 일부 생략 · 전송 상한으로 잘렸으므로 원본 조사에서 확인하세요.'
            else:
                try:content=restore_summary(json.loads(content))
                except (ValueError,TypeError,KeyError):content='복원 상태 확인 필요 · 정확한 원문은 전송 상세에서 확인하세요.'
        elif key=='local.verdict':
            from signup031.local_investigation import VERDICTS
            content=VERDICTS.get(content,content)
        elif key in ('original.environment','original.observations','original.actions','original.limitations','original.timeline'):
            content='수집된 원문이 전송 상세에 포함됩니다.' if content not in ('미수집','[]','{}') else '수집된 항목 없음'
        lines.append(EVIDENCE_NAMES.get(key,'추가 근거')+' · '+SOURCES[row['source']]+'\n'+content+('\n[길이 상한으로 잘림]' if row['truncated'] else ''))
    return '\n\n'.join(lines)
