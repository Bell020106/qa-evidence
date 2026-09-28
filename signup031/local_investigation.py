"""Local manual investigation notes, independent of immutable evidence and server QA."""
from copy import deepcopy
import hashlib,json,os
from pathlib import Path
from signup031.contract import utc_now
from signup031.investigation import InvestigationStore

VERDICTS={'undetermined':'판단 못 함','same_issue':'같은 문제가 보임','not_seen':'같은 문제가 안 보임'}
STATES={'starting':'복원 중','ready':'수집된 범위 복원 완료 · 직접 조사 가능','partial':'일부만 복원 · 열린 화면에서 조사 가능','unavailable':'화면을 열 수 없음'}


def session_text(session):
    if not session:return '아직 로컬 조사 세션을 열지 않았습니다. 저장 자료만 보고 판단하지 마세요.'
    text=STATES[session['state']]+f"\n복원한 동작 {session['completed_actions']}/{session['total_actions']}개"
    if session['stopped_action'] is not None:text+=f" · {session['stopped_action']}번 동작에서 중단"
    if session['reason']:text+='\n중단 이유: '+session['reason']
    if session['limitations']:text+='\n수집·복원 제한: '+'; '.join(session['limitations'])
    if session['blocked']:text+=f"\n미수집 요청 {len(session['blocked'])}건 차단 · 원격 서버에 연결하지 않았습니다."
    return text+'\n복원 상태는 원본 테스트 판정이나 같은 문제의 재현 여부를 뜻하지 않습니다.'


def validate_session(session):
    if session is None:return
    fields={'session_id','state','phase','completed_actions','total_actions','stopped_action','reason','limitations','blocked'}
    if not isinstance(session,dict) or set(session)!=fields:raise ValueError('조사 세션 형식 오류')
    if not isinstance(session['session_id'],str) or len(session['session_id'])!=32 or any(c not in '0123456789abcdef' for c in session['session_id']):raise ValueError('조사 세션 ID 오류')
    if session['state'] not in STATES or session['phase'] not in ('navigation','action','observations','complete'):raise ValueError('조사 상태 오류')
    if any(type(session[k]) is not int or not 0<=session[k]<=10000 for k in ('completed_actions','total_actions')) or session['completed_actions']>session['total_actions']:raise ValueError('조사 동작 수 오류')
    stopped=session['stopped_action']
    if stopped is not None and (type(stopped) is not int or not 1<=stopped<=session['total_actions']):raise ValueError('조사 중단 위치 오류')
    if not isinstance(session['reason'],str) or len(session['reason'])>4000:raise ValueError('조사 이유 오류')
    if not isinstance(session['limitations'],list) or len(session['limitations'])>1000 or not all(isinstance(v,str) and len(v)<=10000 for v in session['limitations']):raise ValueError('조사 제한 오류')
    if not isinstance(session['blocked'],list) or len(session['blocked'])>10000:raise ValueError('조사 요청 오류')
    for row in session['blocked']:
        if not isinstance(row,dict) or set(row)!={'kind','reason','resource'} or row['kind'] not in ('http','websocket') or not all(isinstance(v,str) and len(v)<=10000 for v in row.values()):raise ValueError('차단 요청 형식 오류')


class LocalInvestigationStore:
    def __init__(self,root):self.qa=InvestigationStore(root)
    def path_for(self,eid):
        self.qa.path_for(eid)
        return self.qa._safe(Path('local-investigations')/(hashlib.sha256(eid.encode()).hexdigest()+'.json'))
    def _validate(self,doc,eid):
        if not isinstance(doc,dict) or set(doc)!={'version','source','revision','session','verdict','notes','history'}:raise ValueError('로컬 조사 형식 오류')
        if doc['version']!=1 or type(doc['revision']) is not int or doc['revision']<0:raise ValueError('로컬 조사 버전 오류')
        if doc['source']!=self.qa._reference(self.qa.record(eid)):raise ValueError('원본 ID/해시 불일치')
        validate_session(doc['session'])
        if doc['verdict'] not in VERDICTS or not isinstance(doc['notes'],str) or len(doc['notes'])>100000:raise ValueError('수동 판단·메모 오류')
        if not isinstance(doc['history'],list):raise ValueError('조사 이력 오류')
        for entry in doc['history']:
            if not isinstance(entry,dict) or set(entry)!={'at','session','verdict','notes'} or not isinstance(entry['at'],str):raise ValueError('조사 이력 오류')
            validate_session(entry['session'])
            if entry['verdict'] not in VERDICTS or not isinstance(entry['notes'],str) or len(entry['notes'])>100000:raise ValueError('조사 이력 내용 오류')
        return doc
    def load(self,eid):
        source=self.qa._reference(self.qa.record(eid));path=self.path_for(eid)
        if path.exists():return self._validate(json.loads(path.read_text(encoding='utf-8')),eid)
        return {'version':1,'source':source,'revision':0,'session':None,'verdict':'undetermined','notes':'','history':[]}
    def save(self,doc):
        eid=doc['source']['execution_id'];self._validate(doc,eid)
        path=self.path_for(eid);path.parent.mkdir(parents=True,exist_ok=True)
        lock=self.qa._safe(path.relative_to(self.qa.root/'.qa').with_suffix('.lock'))
        try:fd=os.open(lock,os.O_WRONLY|os.O_CREAT|os.O_EXCL)
        except FileExistsError:raise ValueError('다른 창에서 저장 중입니다. 잠시 후 다시 저장하세요.') from None
        try:
            previous=self.load(eid)
            self._validate(doc,eid)
            if doc['revision']!=previous['revision']:raise ValueError('다른 창에서 변경되었습니다. 닫고 다시 열어주세요.')
            if doc['history']!=previous['history']:raise ValueError('저장된 조사 이력은 직접 바꿀 수 없습니다.')
            saved=deepcopy(doc);saved['revision']+=1
            saved['history'].append({'at':utc_now(),**{k:deepcopy(saved[k]) for k in ('session','verdict','notes')}})
            self.qa._write(path.relative_to(self.qa.root/'.qa'),json.dumps(saved,ensure_ascii=False,indent=2)+'\n')
            return saved
        finally:os.close(fd);lock.unlink()
