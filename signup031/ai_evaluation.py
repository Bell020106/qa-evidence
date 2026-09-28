"""Versioned AI evaluation data, deterministic checks and independent human history."""
from contextlib import contextmanager
from copy import deepcopy
import json
import math
import os
from pathlib import Path
import re
import sqlite3
from uuid import uuid4

from signup031.ai_assistant import OpenAIAdapter,canonical,digest,response_text,strict_json
from signup031.contract import utc_now
from signup031.ingestion_contract import safe_path
from signup031.storage import write_evidence

MAX_CASES=50
MAX_DATASET_BYTES=1024*1024
MAX_TEXT=20000
GENERATIONS=('not_run','running','completed','generation_error','cancelled','interrupted')


def text(value,limit,label,empty=False):
    if not isinstance(value,str) or len(value)>limit or (not empty and not value.strip()):raise ValueError(label+' 형식/길이 오류')


def exact_fields(value,keys,label):
    if not isinstance(value,dict) or set(value)!=set(keys):raise ValueError(label+' 필드 오류')


def validate_contract(schema,depth=0,budget=None):
    budget=[0] if budget is None else budget;budget[0]+=1
    if depth>6 or budget[0]>100 or not isinstance(schema,dict):raise ValueError('JSON 계약 깊이/노드 상한 오류')
    kind=schema.get('type')
    if kind=='object':
        exact_fields(schema,('type','properties','required','additionalProperties'),'JSON object 계약')
        props=schema['properties'];required=schema['required']
        if not isinstance(props,dict) or len(props)>30 or any(not isinstance(k,str) or not k or len(k)>100 for k in props):raise ValueError('JSON 속성 오류')
        if not isinstance(required,list) or any(not isinstance(k,str) for k in required) or len(required)!=len(set(required)) or not set(required)<=set(props) or type(schema['additionalProperties']) is not bool:raise ValueError('JSON 필수/추가 속성 오류')
        for child in props.values():validate_contract(child,depth+1,budget)
    elif kind=='array':
        exact_fields(schema,('type','items'),'JSON array 계약');validate_contract(schema['items'],depth+1,budget)
    elif kind in ('string','integer','number','boolean','null'):exact_fields(schema,('type',),'JSON 값 계약')
    else:raise ValueError('지원하지 않는 JSON 계약 유형')


def validate_dataset(value):
    exact_fields(value,('format_version','id','version','title','requested_model','prompt','settings','normalization','cases'),'평가 묶음')
    if type(value['format_version']) is not int or value['format_version']!=1:raise ValueError('평가 묶음 버전 오류')
    for key,limit in (('id',64),('version',64),('title',200),('prompt',8000)):text(value[key],limit,key)
    if not isinstance(value['requested_model'],str) or not re.fullmatch('[A-Za-z0-9_.:-]{1,100}',value['requested_model']):raise ValueError('요청 모델 ID 오류')
    exact_fields(value['settings'],('max_output_tokens',),'생성 설정');tokens=value['settings']['max_output_tokens']
    if type(tokens) is not int or not 1<=tokens<=6000:raise ValueError('출력 token 상한은 1~6000 정수입니다')
    if value['normalization'] not in ('literal','trim_casefold'):raise ValueError('문자열 정규화 정책 오류')
    cases=value['cases']
    if not isinstance(cases,list) or not 1<=len(cases)<=MAX_CASES:raise ValueError('평가 사례는 1~50개입니다')
    seen=set()
    for case in cases:
        exact_fields(case,('id','input','criteria','rubric'),'사례');text(case['id'],64,'case ID');text(case['input'],8000,'입력');text(case['rubric'],2000,'사람 rubric',True)
        if case['id'].strip() in seen:raise ValueError('중복 case ID')
        seen.add(case['id'].strip());criteria=case['criteria']
        if not isinstance(criteria,list) or len(criteria)>20 or (not criteria and not case['rubric'].strip()):raise ValueError('자동 기준 또는 사람 rubric이 필요합니다')
        ids=set()
        for rule in criteria:
            exact_fields(rule,('id','kind','expected'),'기준');text(rule['id'],64,'기준 ID')
            if rule['id'].strip() in ids:raise ValueError('중복 기준 ID')
            ids.add(rule['id'].strip())
            if rule['kind']=='json_contract':validate_contract(rule['expected'])
            elif rule['kind'] in ('exact','includes','excludes'):text(rule['expected'],8000,'문자열 기준')
            else:raise ValueError('지원하지 않는 평가 기준')
    if len(canonical(value))>MAX_DATASET_BYTES:raise ValueError('평가 묶음 1 MiB 상한 초과')
    return deepcopy(value)


def matches(value,schema):
    kind=schema['type']
    if kind=='object':
        return type(value) is dict and set(schema['required'])<=set(value) and (schema['additionalProperties'] or set(value)<=set(schema['properties'])) and all(matches(value[k],s) for k,s in schema['properties'].items() if k in value)
    if kind=='array':return type(value) is list and all(matches(v,schema['items']) for v in value)
    if kind=='integer':return type(value) is int
    if kind=='number':return type(value) is int or (type(value) is float and math.isfinite(value))
    return type(value) is {'string':str,'boolean':bool,'null':type(None)}[kind]


def evaluate(response,criteria,normalization):
    def norm(value):return value if normalization=='literal' else value.strip().casefold()
    rows=[]
    for rule in criteria:
        kind=rule['kind'];reason='deterministic comparison'
        if kind=='json_contract':
            try:passed=matches(strict_json(response),rule['expected']);reason='JSON type/field contract'
            except (ValueError,TypeError,RecursionError):passed=False;reason='invalid JSON'
        else:
            actual,expected=norm(response),norm(rule['expected'])
            passed=actual==expected if kind=='exact' else expected in actual if kind=='includes' else expected not in actual
        rows.append({'id':rule['id'],'kind':kind,'passed':passed,'reason':reason})
    return rows


def request_for(snapshot,case):
    return {'model':snapshot['requested_model'],'instructions':snapshot['prompt'],'input':[{'role':'user','content':case['input']}],
        'max_output_tokens':snapshot['settings']['max_output_tokens'],'store':False,'stream':False,'tools':[],'text':{'format':{'type':'text'}}}


def ensure_no_key(value,key):
    if key and key in canonical(value).decode():raise ValueError('선택한 데이터에 인증 키가 포함되어 처리하지 않습니다')


def generate_case(key,snapshot,case,*,test_endpoint=None,timeout=20):
    client=OpenAIAdapter(key,test_endpoint=test_endpoint,timeout=timeout);request=request_for(snapshot,case)
    value,model=response_text(client.request_response(request),key)
    if len(value)>MAX_TEXT:raise ValueError('평가 응답 20000자 상한 초과')
    return value,model


class EvaluationStore:
    def __init__(self,root):
        self._lease=None
        self.root=Path(root).resolve()
        if any((p/'evidence.json').exists() for p in (self.root,*self.root.parents)):raise ValueError('원본 증거 내부에 평가 저장소를 만들 수 없습니다')
        self.root.mkdir(parents=True,exist_ok=True);folder=safe_path(self.root,'.ai-evaluations');folder.mkdir(exist_ok=True)
        self.path=safe_path(self.root,'.ai-evaluations/evaluations.sqlite3')
        with self.connection() as con:
            con.executescript('CREATE TABLE IF NOT EXISTS datasets(id TEXT,version TEXT,sha TEXT,data TEXT,PRIMARY KEY(id,version));CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,status TEXT,data TEXT);CREATE TABLE IF NOT EXISTS reviews(id INTEGER PRIMARY KEY AUTOINCREMENT,run_id TEXT,case_id TEXT,data TEXT);')
            for table,columns in (('datasets',{'title':'$.title'}),('runs',{'dataset_id':'$.snapshot.id','dataset_version':'$.snapshot.version','title':'$.snapshot.title'})):
                existing={r[1] for r in con.execute('PRAGMA table_info('+table+')')}
                for column,json_path in columns.items():
                    if column not in existing:
                        con.execute('ALTER TABLE '+table+' ADD COLUMN '+column+' TEXT')
                        con.execute('UPDATE '+table+' SET '+column+'=json_extract(data,?)',(json_path,))

    def acquire_run(self):
        if self._lease is not None:raise ValueError('이 창에서 진행 중 평가가 있습니다')
        stream=safe_path(self.root,'.ai-evaluations/active.lock').open('a+b')
        try:
            if stream.seek(0,2)==0:stream.write(b'0');stream.flush()
            stream.seek(0)
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            stream.close();raise ValueError('다른 창/프로세스의 평가가 진행 중입니다') from None
        self._lease=stream

    def release_run(self):
        if self._lease is not None:
            stream,self._lease=self._lease,None
            try:
                stream.seek(0)
                if os.name=='nt':
                    import msvcrt
                    msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
                else:
                    import fcntl
                    fcntl.flock(stream,fcntl.LOCK_UN)
            finally:stream.close()

    def recover(self):
        try:self.acquire_run()
        except ValueError:return {'active_owner':True,'recovered':0}
        try:
            with self.connection() as con:ids=[r[0] for r in con.execute("SELECT id FROM runs WHERE status IN ('queued','running')")]
            for run_id in ids:
                def change(s):
                    s['status']='interrupted';s['finished_at']=utc_now()
                    for item in s['items']:
                        if item['generation']=='running':item['generation']='interrupted'
                self.mutate(run_id,change)
            return {'active_owner':False,'recovered':len(ids)}
        finally:self.release_run()

    @contextmanager
    def connection(self):
        con=sqlite3.connect(self.path,timeout=.25)
        try:
            with con:yield con
        finally:con.close()

    def save_dataset(self,value,secret=''):
        value=validate_dataset(value);ensure_no_key(value,secret)
        with self.connection() as con:
            row=con.execute('SELECT sha FROM datasets WHERE id=? AND version=?',(value['id'],value['version'])).fetchone()
            if row:
                if row[0]!=digest(value):raise ValueError('기존 데이터셋 버전은 변경할 수 없습니다. 새 버전을 지정하세요')
                return
            if con.execute('SELECT count(*) FROM datasets').fetchone()[0]>=1000:raise ValueError('데이터셋 버전 1000개 상한')
            con.execute('INSERT INTO datasets(id,version,sha,data,title) VALUES(?,?,?,?,?)',(value['id'],value['version'],digest(value),canonical(value).decode(),value['title']))

    def datasets(self):
        with self.connection() as con:return [dict(zip(('id','version','title','sha256'),r)) for r in con.execute('SELECT id,version,title,sha FROM datasets ORDER BY rowid DESC')]

    def load_dataset(self,dataset_id,version):
        with self.connection() as con:row=con.execute('SELECT data FROM datasets WHERE id=? AND version=?',(dataset_id,version)).fetchone()
        if not row:raise ValueError('저장한 데이터셋 버전을 찾을 수 없습니다')
        return validate_dataset(json.loads(row[0]))

    def create_run(self,snapshot,provider,secret=''):
        snapshot=validate_dataset(snapshot);ensure_no_key(snapshot,secret)
        if provider not in ('local-http-test','openai-responses'):raise ValueError('평가 공급자 오류')
        state={'evaluation_version':1,'run_id':uuid4().hex,'created_at':utc_now(),'provider':provider,'snapshot':snapshot,'snapshot_sha256':digest(snapshot),'status':'queued',
            'items':[{'case_id':c['id'],'generation':'not_run','response':None,'returned_model':None,'automatic':[],'auto_status':'not_evaluated','reason':None} for c in snapshot['cases']],
            'ai_judge':{'status':'not_run','reason':'AI 평가자 미설정'}}
        self.acquire_run()
        try:
            with self.connection() as con:
                con.execute('BEGIN IMMEDIATE')
                if con.execute("SELECT 1 FROM runs WHERE status IN ('queued','running')").fetchone():raise ValueError('앱을 재시작하여 중단된 평가를 회수한 후 실행하세요')
                if con.execute('SELECT count(*) FROM runs').fetchone()[0]>=1000:raise ValueError('평가 실행 1000개 상한')
                con.execute('INSERT INTO runs(id,status,data,dataset_id,dataset_version,title) VALUES(?,?,?,?,?,?)',(state['run_id'],state['status'],canonical(state).decode(),snapshot['id'],snapshot['version'],snapshot['title']))
        except Exception:self.release_run();raise
        return state

    def load_run(self,run_id):
        with self.connection() as con:row=con.execute('SELECT data FROM runs WHERE id=?',(run_id,)).fetchone()
        if not row:raise ValueError('평가 실행을 찾을 수 없습니다')
        return json.loads(row[0])

    def runs(self):
        with self.connection() as con:return [dict(zip(('run_id','status','dataset_id','dataset_version','title'),r)) for r in con.execute('SELECT id,status,dataset_id,dataset_version,title FROM runs ORDER BY rowid DESC')]

    def mutate(self,run_id,callback):
        with self.connection() as con:
            con.execute('BEGIN IMMEDIATE');row=con.execute('SELECT data FROM runs WHERE id=?',(run_id,)).fetchone()
            if not row:raise ValueError('평가 실행을 찾을 수 없습니다')
            state=json.loads(row[0]);result=callback(state)
            if result is False:return False
            con.execute('UPDATE runs SET status=?,data=? WHERE id=?',(state['status'],canonical(state).decode(),run_id))
            return True

    def start(self,run_id):
        def change(s):
            if s['status']!='queued':return False
            s['status']='running'
        return self.mutate(run_id,change)

    def start_case(self,run_id,case_id):
        def change(s):
            item=next(i for i in s['items'] if i['case_id']==case_id)
            if s['status']!='running' or item['generation']!='not_run':return False
            item['generation']='running'
        return self.mutate(run_id,change)

    def complete_case(self,run_id,case_id,response,model):
        def change(s):
            item=next(i for i in s['items'] if i['case_id']==case_id)
            if s['status']!='running' or item['generation']!='running':return False
            case=next(c for c in s['snapshot']['cases'] if c['id']==case_id)
            automatic=evaluate(response,case['criteria'],s['snapshot']['normalization'])
            item.update(generation='completed',response=response,returned_model=model,automatic=automatic,
                auto_status=('passed' if all(r['passed'] for r in automatic) else 'failed') if automatic else 'not_configured')
        return self.mutate(run_id,change)

    def fail_case(self,run_id,case_id,reason):
        def change(s):
            item=next(i for i in s['items'] if i['case_id']==case_id)
            if s['status']!='running' or item['generation']!='running':return False
            item.update(generation='generation_error',reason=reason[:250])
        return self.mutate(run_id,change)

    def finish(self,run_id):
        def change(s):
            if s['status']!='running':return False
            if any(i['generation'] in ('not_run','running') for i in s['items']):raise ValueError('아직 처리하지 않은 항목이 있습니다')
            s['status']='completed';s['finished_at']=utc_now()
        result=self.mutate(run_id,change)
        if result:self.release_run()
        return result

    def cancel(self,run_id,*,interrupted=False):
        def change(s):
            if s['status'] not in ('queued','running'):return False
            s['status']='interrupted' if interrupted else 'cancelled';s['finished_at']=utc_now()
            for item in s['items']:
                if item['generation']=='running':item['generation']=s['status']
        result=self.mutate(run_id,change)
        if result:self.release_run()
        return result

    def review(self,run_id,case_id,verdict,reason,secret=''):
        if verdict not in ('passed','failed'):raise ValueError('사람 판단은 통과/실패를 명시하세요')
        text(reason,2000,'사람 판단 근거');ensure_no_key(reason,secret)
        state=self.load_run(run_id);item=next(i for i in state['items'] if i['case_id']==case_id);case=next(c for c in state['snapshot']['cases'] if c['id']==case_id)
        if item['generation']!='completed' or not case['rubric'].strip():raise ValueError('생성 완료와 사람 rubric이 있어야 판단할 수 있습니다')
        with self.connection() as con:
            con.execute('BEGIN IMMEDIATE');count=con.execute('SELECT count(*) FROM reviews WHERE run_id=? AND case_id=?',(run_id,case_id)).fetchone()[0]
            if count>=100:raise ValueError('사례별 사람 판단 이력 100개 상한')
            row={'case_id':case_id,'revision':count+1,'verdict':verdict,'reason':reason,'at':utc_now(),'rubric_sha256':digest(case['rubric']),'response_sha256':digest(item['response'])}
            con.execute('INSERT INTO reviews(run_id,case_id,data) VALUES(?,?,?)',(run_id,case_id,canonical(row).decode()))

    def reviews(self,run_id):
        with self.connection() as con:return [json.loads(r[0]) for r in con.execute('SELECT data FROM reviews WHERE run_id=? ORDER BY id',(run_id,))]

    def export(self,run_id,path,baseline_id=None):
        path=Path(path).resolve()
        if path.suffix.lower()!='.json' or path.is_relative_to(self.path.parent) or any((p/'evidence.json').exists() for p in path.parents):raise ValueError('원본/평가 저장소 바깥 JSON으로 내보내세요')
        run=self.load_run(run_id);reviews=self.reviews(run_id);value={'export_version':1,'run':run,'reviews':reviews,'summary':summarize(run,reviews)}
        if baseline_id:
            baseline=self.load_run(baseline_id);before_reviews=self.reviews(baseline_id)
            value['comparison']={'baseline':baseline,'baseline_reviews':before_reviews,'cases':compare_runs(baseline,run,before_reviews,reviews)}
        if len(canonical(value))>50_000_000:raise ValueError('내보내기 50 MB 상한 초과')
        write_evidence(path,value)


def summarize(state,reviews):
    latest={r['case_id']:r for r in reviews};result={'total':len(state['items']),'generation':{k:0 for k in GENERATIONS},
        'automatic':{'passed':0,'failed':0,'not_evaluated':0,'not_configured':0},'human':{'passed':0,'failed':0},
        'human_unreviewed':0,'human_unavailable':0,'human_not_configured':0,'ai_judge_not_run':len(state['items']),'criteria':{}}
    cases={c['id']:c for c in state['snapshot']['cases']}
    for item in state['items']:
        case=cases[item['case_id']];result['generation'][item['generation']]+=1;result['automatic'][item['auto_status']]+=1
        if not case['rubric'].strip():result['human_not_configured']+=1
        elif item['generation']!='completed':result['human_unavailable']+=1
        elif item['case_id'] not in latest:result['human_unreviewed']+=1
        else:result['human'][latest[item['case_id']]['verdict']]+=1
        actual={r['id']:r for r in item['automatic']}
        for rule in case['criteria']:
            count=result['criteria'].setdefault(rule['kind'],{'passed':0,'failed':0,'not_evaluated':0})
            count['not_evaluated' if rule['id'] not in actual else 'passed' if actual[rule['id']]['passed'] else 'failed']+=1
    for count in (result['automatic'],result['human'],*result['criteria'].values()):
        count['evaluated']=count['passed']+count['failed'];count['rate']=count['passed']/count['evaluated'] if count['evaluated'] else None
    return result


def compare_runs(old,new,old_reviews=(),new_reviews=()):
    before={i['case_id']:i for i in old['items']};after={i['case_id']:i for i in new['items']};cases_a={c['id']:c for c in old['snapshot']['cases']};cases_b={c['id']:c for c in new['snapshot']['cases']};rows=[]
    global_fields=('id','version','requested_model','prompt','settings','normalization')
    def human(item,cases,reviews):
        if item is None:return {'status':'absent','reason':None}
        if not cases[item['case_id']]['rubric'].strip():return {'status':'not_configured','reason':None}
        if item['generation']!='completed':return {'status':'unavailable','reason':None}
        latest=next((r for r in reversed(reviews) if r['case_id']==item['case_id']),None)
        return {'status':latest['verdict'],'reason':latest['reason'],'revision':latest['revision']} if latest else {'status':'unreviewed','reason':None}
    for case_id in [*after,*(k for k in before if k not in after)]:
        a,b=before.get(case_id),after.get(case_id);differences=[]
        if a is None:change='added'
        elif b is None:change='removed'
        else:
            differences=[key for key in global_fields if old['snapshot'][key]!=new['snapshot'][key]]
            differences += ['case_'+key for key in ('input','criteria','rubric') if cases_a[case_id][key]!=cases_b[case_id][key]]
            if old['provider']!=new['provider']:differences.append('provider')
            if a['returned_model'] is not None and b['returned_model'] is not None and a['returned_model']!=b['returned_model']:differences.append('returned_model')
            if differences:change='conditions_changed'
            elif a['generation']!='completed' or b['generation']!='completed':change='not_comparable'
            elif a['response']!=b['response']:change='response_changed'
            else:change='unchanged'
        rows.append({'case_id':case_id,'change':change,'differences':differences,'before_auto':a['auto_status'] if a else None,'after_auto':b['auto_status'] if b else None,
            'before_human':human(a,cases_a,old_reviews),'after_human':human(b,cases_b,new_reviews),'ai_judge':'not_run'})
    return rows
