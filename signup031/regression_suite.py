"""Validated suite definitions, frozen execution criteria and persistent run records."""
from contextlib import contextmanager
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

from signup031.contract import utc_now
from signup031.ingestion_contract import safe_path
from signup031.storage import write_evidence
from signup031.tc_import import fingerprint
from signup031.web_scenario import validate_scenario

MAX_ITEMS=100
MAX_BYTES=10*1024*1024
STATUSES=('passed','failed','preparation_failed','cancelled','not_run','interrupted','skipped','running')


def read_json(path,limit=MAX_BYTES):
    with Path(path).open('rb') as stream:raw=stream.read(limit+1)
    if len(raw)>limit:raise ValueError('설정/기록 크기 상한 초과')
    return json.loads(raw)


def criteria_hash(config):
    return fingerprint({k:config[k] for k in ('version','url','steps','checks')})


def freeze_suite(name,paths):
    if not isinstance(name,str) or not name.strip() or len(name)>200:raise ValueError('묶음 이름은 1~200자입니다')
    if not isinstance(paths,(list,tuple)) or not 1<=len(paths)<=MAX_ITEMS:raise ValueError('웹 TC 설정 파일 1~100개를 선택하세요')
    items=[];errors=[];seen={}
    for index,path in enumerate(paths,1):
        try:
            path=Path(path).resolve();config=validate_scenario(read_json(path,1024*1024))
            tc_id=config['id']
            if tc_id.strip() in seen:raise ValueError(f'TC ID 충돌: {seen[tc_id.strip()]}행과 {index}행 ({tc_id})')
            seen[tc_id.strip()]=index
            items.append({'position':index,'tc_id':tc_id,'source_path':str(path),'config':config,
                'config_sha256':fingerprint(config),'criteria_sha256':criteria_hash(config)})
        except (OSError,ValueError,TypeError) as exc:errors.append(f'{index}행: {exc}')
    if errors:raise ValueError('\n'.join(errors))
    snapshot={'version':1,'name':name.strip(),'items':items}
    return validate_snapshot(snapshot)


def validate_snapshot(snapshot):
    if not isinstance(snapshot,dict) or snapshot.get('version')!=1 or not isinstance(snapshot.get('name'),str) or not snapshot['name'].strip() or len(snapshot['name'])>200:
        raise ValueError('묶음 snapshot 형식 오류')
    items=snapshot.get('items')
    if not isinstance(items,list) or not 1<=len(items)<=MAX_ITEMS:raise ValueError('묶음 항목 상한 오류')
    seen=set()
    for index,item in enumerate(items,1):
        config=validate_scenario(item['config']);tc_id=config['id']
        if item.get('position')!=index or item.get('tc_id')!=tc_id or tc_id.strip() in seen:raise ValueError('묶음 순서/TC ID 충돌')
        if item.get('config_sha256')!=fingerprint(config) or item.get('criteria_sha256')!=criteria_hash(config):raise ValueError('묶음 설정 해시 오류')
        if not isinstance(item.get('source_path'),str):raise ValueError('설정 출처 오류')
        seen.add(tc_id.strip())
    if len(json.dumps(snapshot,ensure_ascii=False).encode())>MAX_BYTES:raise ValueError('묶음 10 MiB 상한 초과')
    return deepcopy(snapshot)


def save_definition(path,name,paths):
    snapshot=freeze_suite(name,paths);path=Path(path).resolve()
    if any((parent/'evidence.json').exists() for parent in path.parents):raise ValueError('원본 실행 폴더에 묶음을 저장할 수 없습니다')
    write_evidence(path,{'suite_version':1,'name':snapshot['name'],'paths':[i['source_path'] for i in snapshot['items']]})


def load_definition(path):
    definition=read_json(path)
    if not isinstance(definition,dict) or set(definition)!={'suite_version','name','paths'} or definition['suite_version']!=1:raise ValueError('묶음 정의 형식 오류')
    # Revalidate all current source files; missing files are never silently excluded.
    freeze_suite(definition['name'],definition['paths'])
    return definition


def summarize(state):
    result={s:0 for s in STATUSES}
    for item in state['items']:
        status=item['status']
        if status not in result:raise ValueError('알 수 없는 실행 상태')
        result[status]+=1
    result['total']=len(state['items']);result['pass_rate']=result['passed']/result['total'] if result['total'] else 0
    return result


def compare_runs(old,new):
    before={x['tc_id']:x for x in old['items']};after={x['tc_id']:x for x in new['items']};rows=[]
    for tc_id in [*after,*(k for k in before if k not in after)]:
        a,b=before.get(tc_id),after.get(tc_id)
        if a is None:change='added'
        elif b is None:change='removed'
        elif a['criteria_sha256']!=b['criteria_sha256']:change='criteria_changed'
        elif a['status'] not in ('passed','failed') or b['status'] not in ('passed','failed'):change='not_comparable'
        elif a['status']=='failed' and b['status']=='passed':change='recovered'
        elif a['status']=='passed' and b['status']=='failed':change='regressed'
        else:change='unchanged'
        rows.append({'tc_id':tc_id,'before':a['status'] if a else None,'after':b['status'] if b else None,'change':change,
            'config_changed':bool(a and b and a['config_sha256']!=b['config_sha256'])})
    return rows


class SuiteStore:
    def __init__(self,root):
        self.root=Path(root).absolute()
        if any((p/'evidence.json').exists() for p in (self.root,*self.root.parents)):raise ValueError('원본 실행 폴더 밖의 결과 루트를 선택하세요')
        self.directory=safe_path(self.root,'.suites');self.directory.mkdir(parents=True,exist_ok=True)
        self.runs=safe_path(self.root,'.suites/runs');self.runs.mkdir(exist_ok=True)

    @contextmanager
    def lease(self):
        path=safe_path(self.root,'.suites/active.lock')
        stream=path.open('a+b');stream.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            stream.close();raise ValueError('이 결과 폴더에 실행 중인 묶음이 있습니다') from None
        try:yield
        finally:
            stream.seek(0)
            if os.name=='nt':msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
            else:fcntl.flock(stream,fcntl.LOCK_UN)
            stream.close()

    def path(self,suite_id):
        if not isinstance(suite_id,str) or len(suite_id)!=32 or any(c not in '0123456789abcdef' for c in suite_id):raise ValueError('묶음 실행 ID 오류')
        return safe_path(self.root,'.suites/runs/'+suite_id)

    def read(self,suite_id):
        state=read_json(self.path(suite_id)/'state.json',2*MAX_BYTES)
        if state.get('suite_id')!=suite_id:raise ValueError('묶음 실행 ID 불일치')
        return state

    def save(self,state):
        state['summary']=summarize(state)
        deadline=time.monotonic()+1
        while True:
            try:
                write_evidence(self.path(state['suite_id'])/'state.json',state);break
            except PermissionError:
                # Windows readers briefly deny replace; durable denial still fails.
                if time.monotonic()>deadline:raise
                time.sleep(.02)

    def create(self,snapshot):
        snapshot=validate_snapshot(snapshot);suite_id=uuid4().hex;path=self.path(suite_id);path.mkdir()
        write_evidence(path/'snapshot.json',snapshot)
        state={'version':1,'suite_id':suite_id,'name':snapshot['name'],'snapshot_sha256':fingerprint(snapshot),
            'status':'running','started_at':utc_now(),'finished_at':None,'message':'','cleanup_errors':[],
            'items':[dict(item,status='not_run',reason='아직 실행하지 않음',execution_id=None,evidence=None,cleanup=None) for item in snapshot['items']]}
        self.save(state);return state

    def _recover_unlocked(self):
        for path in self.runs.glob('*/state.json'):
            state=self.read(path.parent.name)
            if state['status']!='running':continue
            state.update(status='interrupted',finished_at=utc_now(),message='이전 실행자가 종료됐습니다. 남은 항목은 자동 재실행하지 않습니다')
            for item in state['items']:
                if item['status']=='running':item.update(status='interrupted',reason='실행자 비정상 종료',cleanup={'status':'unknown','errors':['종료 전 정리 결과를 확인하지 못했습니다']})
                elif item['status']=='not_run':item['reason']='이전 실행자 중단으로 미실행'
            self.save(state)

    def recover(self):
        with self.lease():self._recover_unlocked()

    def list_runs(self):
        return sorted((self.read(p.parent.name) for p in self.runs.glob('*/state.json')),key=lambda s:s['started_at'],reverse=True)


def run_suite(root,snapshot,*,cancel=None,on_update=None,case_timeout=120,suite_timeout=3600):
    from signup031.owned_job import OwnedJob,python_command,worker_environment
    snapshot=validate_snapshot(snapshot)
    if not .1<=case_timeout<=600 or not .1<=suite_timeout<=3600:raise ValueError('묶음 실행 시간 상한 오류')
    store=SuiteStore(root);start=time.monotonic()
    with store.lease():
        store._recover_unlocked();state=store.create(snapshot);run_dir=store.path(state['suite_id'])
        def publish():
            store.save(state)
            if on_update:on_update(deepcopy(state))
        def cancelled():return (cancel and cancel()) or (run_dir/'cancel.request').exists()
        publish()
        try:
            for item in state['items']:
                if cancelled() or time.monotonic()-start>suite_timeout:
                    state.update(status='cancelled',message='사용자 취소 또는 묶음 시간 상한');break
                item.update(status='running',reason='',started_at=utc_now());publish()
                execution_root=run_dir/'executions'/str(item['position']);execution_root.mkdir(parents=True)
                process=None;job=None;cleanup=[];forced=False
                try:
                    job=OwnedJob()
                    with (execution_root/'worker.log').open('wb') as log:
                        process=subprocess.Popen(python_command('signup031.web_runner','--stdin-config','--artifacts-dir',str(execution_root)),
                            stdin=subprocess.PIPE,stdout=log,stderr=log,env=worker_environment(),cwd=Path(__file__).resolve().parent.parent,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                        # web_runner blocks on stdin until membership is confirmed.
                        job.attach(process.pid)
                        process.stdin.write(json.dumps(item['config'],ensure_ascii=True).encode());process.stdin.close()
                        deadline=time.monotonic()+case_timeout
                        while process.poll() is None:
                            if item['execution_id'] is None:
                                configs=list(execution_root.glob('*/scenario.json'))
                                if len(configs)==1:
                                    item['execution_id']=configs[0].parent.name;publish()
                            if cancelled() or time.monotonic()-start>suite_timeout:
                                item.update(status='cancelled',reason='사용자 취소 또는 묶음 시간 상한');state.update(status='cancelled',message=item['reason']);forced=True;break
                            if time.monotonic()>deadline:
                                item.update(status='preparation_failed',reason='개별 실행 시간 상한 초과');forced=True;break
                            time.sleep(.05)
                        if not forced:
                            paths=list(execution_root.glob('*/evidence.json'))
                            if len(paths)!=1:raise ValueError('개별 실행 증거가 없거나 여러 개입니다')
                            path=paths[0];payload=read_json(path)
                            if payload.get('tc_id')!=item['tc_id'] or payload.get('scenario_snapshot')!=item['config'] or payload['execution']['id']!=path.parent.name:
                                raise ValueError('개별 증거의 ID/실행 기준 불일치')
                            status=payload['result']['business']['status']
                            if status not in ('passed','failed','preparation_failed'):raise ValueError('개별 실행 판정 오류')
                            if process.returncode!=(0 if status=='passed' else 1):raise ValueError('개별 실행 종료 코드/판정 불일치')
                            item.update(status=status,reason=payload['result']['business']['message'],execution_id=payload['execution']['id'],
                                evidence=path.relative_to(store.root).as_posix())
                            cleanup.extend(payload.get('post_run',{}).get('cleanup',{}).get('errors',[]))
                except (OSError,ValueError,KeyError,TypeError) as exc:
                    item.update(status='preparation_failed',reason=str(exc)[:2000])
                finally:
                    if job:
                        try:job.stop()
                        except OSError as exc:cleanup.append(str(exc))
                        try:job.close()
                        except OSError as exc:cleanup.append(str(exc))
                    if process:
                        if process.poll() is None:
                            # On attachment failure stdin never permitted descendants to start.
                            process.kill()
                        try:process.wait(timeout=5)
                        except subprocess.TimeoutExpired:cleanup.append('작업자 종료 확인 실패')
                        if process.stdin and not process.stdin.closed:process.stdin.close()
                    item.update(cleanup={'status':'failed' if cleanup else 'completed','errors':cleanup},finished_at=utc_now())
                    state['cleanup_errors'].extend(cleanup)
                publish()
                if state['status']=='cancelled':break
            if state['status']=='running':state['status']='completed'
            for item in state['items']:
                if item['status']=='not_run':item['reason']='묶음 취소/중단으로 미실행'
            state['finished_at']=utc_now();publish();return state
        except BaseException:
            state.update(status='interrupted',finished_at=utc_now(),message='실행 상태 저장/작업자 오류. 기록을 다시 확인하세요')
            for item in state['items']:
                if item['status']=='running':item.update(status='interrupted',reason=state['message'])
            try:store.save(state)
            except OSError:pass
            raise
