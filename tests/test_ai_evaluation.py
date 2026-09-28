"""AI evaluation snapshots, deterministic criteria, review history and counted states."""
from copy import deepcopy
import importlib.util
import json

import pytest
from test_ai_assistant import Provider,KEY


def api():
    assert importlib.util.find_spec('signup031.ai_evaluation') is not None,'AI evaluation is not implemented'
    from signup031 import ai_evaluation
    return ai_evaluation


def dataset():
    return {'format_version':1,'id':'eval-demo','version':'1','title':'Controlled output checks','requested_model':'synthetic-model',
        'prompt':'Answer the selected input.','settings':{'max_output_tokens':1000},'normalization':'literal',
        'cases':[{'id':'case-1','input':'Say hello','criteria':[{'id':'exact','kind':'exact','expected':'Hello'}],'rubric':'Is the answer relevant? Cite the response.'}]}


def reply(text='Hello',model='synthetic-model'):
    return {'status':'completed','model':model,'output':[{'type':'message','role':'assistant','status':'completed','content':[{'type':'output_text','text':text}]}]}


@pytest.mark.parametrize('kind,expected,text,passed',[
    ('exact','Hello','Hello',True),('exact','Hello','hello',False),('exact','Hello',' Hello ',False),
    ('includes','ell','Hello',True),('excludes','bad','Hello',True),('excludes','bad','bad result',False),
    ('json_contract',{'type':'integer'},'true',False),('json_contract',{'type':'integer'},'1',True),
    ('json_contract',{'type':'number'},'false',False),('json_contract',{'type':'boolean'},'1',False),
    ('json_contract',{'type':'null'},'null',True),('json_contract',{'type':'object','properties':{'x':{'type':'string'}},'required':['x'],'additionalProperties':False},'{"x":"ok","y":1}',False),
    ('json_contract',{'type':'string'},'invalid json',False)])
def test_deterministic_criteria(kind,expected,text,passed):
    m=api();d=dataset();d['cases'][0]['criteria']=[{'id':'rule','kind':kind,'expected':expected}]
    m.validate_dataset(d);result=m.evaluate(text,d['cases'][0]['criteria'],d['normalization'])
    assert result[0]['passed'] is passed


def test_normalization_invalid_empty_and_type_boundaries():
    m=api();d=dataset();d['normalization']='trim_casefold';m.validate_dataset(d)
    assert m.evaluate('  HELLO\n',d['cases'][0]['criteria'],'trim_casefold')[0]['passed']
    for change in ('empty','bad_schema','empty_case','bool_setting','duplicate'):
        bad=dataset()
        if change=='empty':bad['cases'][0]['criteria'][0]['expected']=' '
        elif change=='bad_schema':bad['cases'][0]['criteria'][0].update(kind='json_contract',expected={'type':'any'})
        elif change=='empty_case':bad['cases'][0].update(criteria=[],rubric='')
        elif change=='bool_setting':bad['settings']['max_output_tokens']=True
        else:bad['cases'].append(deepcopy(bad['cases'][0]))
        with pytest.raises(ValueError):m.validate_dataset(bad)


def test_versions_human_history_counts_comparison_and_export(tmp_path):
    m=api();store=m.EvaluationStore(tmp_path);d=dataset();store.save_dataset(d)
    changed=deepcopy(d);changed['cases'][0]['input']='changed'
    with pytest.raises(ValueError):store.save_dataset(changed)
    changed['version']='2';store.save_dataset(changed)
    first=store.create_run(d,'local-http-test');store.start(first['run_id']);store.start_case(first['run_id'],'case-1')
    store.complete_case(first['run_id'],'case-1','Hello','snapshot-A');store.finish(first['run_id'])
    before=store.load_run(first['run_id']);assert m.summarize(before,[])['human_unreviewed']==1
    store.review(first['run_id'],'case-1','failed','Relevant but too brief')
    store.review(first['run_id'],'case-1','passed','Reviewed context; greeting is sufficient')
    assert len(store.reviews(first['run_id']))==2 and store.load_run(first['run_id'])==before
    stats=m.summarize(before,store.reviews(first['run_id']));assert stats['automatic']['passed']==1 and stats['human']['passed']==1 and stats['ai_judge_not_run']==1
    second=store.create_run(d,'local-http-test');store.start(second['run_id']);store.start_case(second['run_id'],'case-1')
    store.complete_case(second['run_id'],'case-1','Different','snapshot-A');store.finish(second['run_id'])
    assert m.compare_runs(before,store.load_run(second['run_id']))[0]['change']=='response_changed'
    third=store.create_run(changed,'local-http-test');store.start(third['run_id']);store.start_case(third['run_id'],'case-1')
    store.complete_case(third['run_id'],'case-1','Hello','snapshot-B');store.finish(third['run_id'])
    assert m.compare_runs(before,store.load_run(third['run_id']))[0]['change']=='conditions_changed'
    exported=tmp_path/'export.json';store.export(first['run_id'],exported)
    data=json.loads(exported.read_bytes());assert data['run']==before and len(data['reviews'])==2 and data['summary']==stats
    restarted=m.EvaluationStore(tmp_path);assert restarted.load_run(first['run_id'])==before


def test_cancel_and_generation_error_are_not_quality_failures(tmp_path):
    m=api();store=m.EvaluationStore(tmp_path);d=dataset()
    for i in range(2):case=deepcopy(d['cases'][0]);case['id']=str(i);d['cases'].append(case)
    state=store.create_run(d,'local-http-test');rid=state['run_id'];store.start(rid);store.start_case(rid,'case-1');store.fail_case(rid,'case-1','HTTP 429')
    store.start_case(rid,'0');store.cancel(rid)
    assert store.complete_case(rid,'0','late','model') is False
    final=store.load_run(rid);stats=m.summarize(final,[])
    assert stats['generation']['generation_error']==1 and stats['generation']['cancelled']==1 and stats['generation']['not_run']==1
    assert stats['automatic']['rate'] is None and stats['human']['rate'] is None and stats['human_unavailable']==3


def test_large_json_integer_and_missing_model_human_comparison(tmp_path):
    m=api();assert m.matches(10**400,{'type':'number'}) is True
    store=m.EvaluationStore(tmp_path);d=dataset();a=store.create_run(d,'local-http-test');store.start(a['run_id']);store.start_case(a['run_id'],'case-1')
    store.complete_case(a['run_id'],'case-1','Hello','same-model');store.finish(a['run_id']);store.review(a['run_id'],'case-1','passed','Relevant greeting')
    b=store.create_run(d,'local-http-test');store.start(b['run_id']);store.start_case(b['run_id'],'case-1');store.fail_case(b['run_id'],'case-1','HTTP 429');store.finish(b['run_id'])
    rows=m.compare_runs(store.load_run(a['run_id']),store.load_run(b['run_id']),store.reviews(a['run_id']),[])
    assert rows[0]['change']=='not_comparable' and 'returned_model' not in rows[0]['differences']
    assert rows[0]['before_human']['status']=='passed' and rows[0]['before_human']['reason']=='Relevant greeting'
    assert rows[0]['after_human']['status']=='unavailable'


def test_maximum_dataset_wire_and_shared_transport(tmp_path):
    m=api();from signup031.evaluation_worker import parse_request,encode_request
    d=dataset();d['cases']=[]
    for i in range(m.MAX_CASES):
        case={'id':str(i),'input':'😀'*4000,'criteria':[],'rubric':'Human rubric'};d['cases'].append(case)
    remaining=m.MAX_DATASET_BYTES-len(m.canonical(d))
    for case in d['cases']:
        extra=min(8000-len(case['input']),remaining//4);case['input']+='😀'*extra;remaining-=extra*4
        if remaining<4 and len(case['input'])+remaining<=8000:case['input']+='x'*remaining;remaining=0
    assert len(m.canonical(d))==m.MAX_DATASET_BYTES
    m.validate_dataset(d)
    raw=encode_request(str(tmp_path),d,KEY,'a'*32,None)
    assert parse_request(raw)['snapshot']==d
    server=Provider();server.body=reply('plain evaluation response','returned-snapshot')
    try:
        value=m.generate_case(KEY,d,d['cases'][0],test_endpoint=server.url)
        request=server.requests[0][2]
        assert 'web_tc_proposal' not in json.dumps(request) and request['tools']==[] and request['store'] is False
        assert request['input'][0]['content']==d['cases'][0]['input'] and 'criteria' not in request and 'rubric' not in request
        assert value==('plain evaluation response','returned-snapshot')
    finally:server.stop()


@pytest.mark.parametrize('failure',['401','429','500','refusal','incomplete','malformed','secret','oversize'])
def test_actual_http_generation_errors_continue_and_never_become_quality_failures(tmp_path,failure):
    m=api();from signup031.evaluation_worker import run
    server=Provider();server.body=reply();d=dataset();second=deepcopy(d['cases'][0]);second['id']='second';d['cases'].append(second)
    if failure.isdigit():server.status=int(failure)
    elif failure=='refusal':server.body['output'][0]['content']=[{'type':'refusal','refusal':'cannot comply'}]
    elif failure=='incomplete':server.body['status']='incomplete'
    elif failure=='malformed':server.body=b'not JSON'
    elif failure=='secret':server.body=reply(KEY)
    elif failure=='oversize':server.body=reply('x'*20001)
    store=m.EvaluationStore(tmp_path);state=store.create_run(d,'local-http-test',KEY)
    try:
        run({'root':str(tmp_path),'run_id':state['run_id'],'snapshot':d,'key':KEY,'test_endpoint':server.url})
        state=store.load_run(state['run_id']);stats=m.summarize(state,[])
        assert len(server.requests)==2 and state['status']=='completed'
        assert stats['generation']['generation_error']==2 and stats['automatic']['rate'] is None and stats['human_unavailable']==2
        target=tmp_path/'export.json';store.export(state['run_id'],target)
        assert KEY.encode() not in target.read_bytes()
        with pytest.raises(ValueError):store.review(state['run_id'],'case-1','passed','No actual response')
    finally:server.stop()


def test_http_timeout_and_credentials_never_persist(tmp_path):
    m=api();server=Provider();server.body=reply();server.delay=.2;d=dataset()
    try:
        with pytest.raises(ValueError,match='시간'):m.generate_case(KEY,d,d['cases'][0],test_endpoint=server.url,timeout=.05)
        store=m.EvaluationStore(tmp_path);d['prompt']=KEY
        with pytest.raises(ValueError):store.save_dataset(d,secret=KEY)
        with pytest.raises(ValueError):store.create_run(d,'local-http-test',secret=KEY)
        assert store.datasets()==[] and store.runs()==[]
        for path in tmp_path.rglob('*'):
            if path.is_file():assert KEY.encode() not in path.read_bytes()
    finally:server.stop()


def test_owned_lease_preserves_live_owner_and_recovers_after_process_death(tmp_path):
    import subprocess,sys,os
    from signup031.owned_job import worker_environment
    m=api();script='''import sys,json,time
from signup031.ai_evaluation import EvaluationStore
sys.path.insert(0,'tests')
from test_ai_evaluation import dataset
s=EvaluationStore(sys.argv[1]);r=s.create_run(dataset(),'local-http-test');s.start(r['run_id']);s.start_case(r['run_id'],'case-1')
print(r['run_id'],flush=True)
time.sleep(30)
'''
    process=subprocess.Popen([getattr(sys,'_base_executable',sys.executable),'-c',script,str(tmp_path)],env=worker_environment(),stdout=subprocess.PIPE,text=True)
    try:
        rid=process.stdout.readline().strip();assert rid
        second=m.EvaluationStore(tmp_path);assert second.recover()['active_owner'] is True
        with pytest.raises(ValueError):second.create_run(dataset(),'local-http-test')
        assert second.load_run(rid)['status']=='running'
        process.kill();process.wait(timeout=5)
        assert second.recover()['recovered']==1
        assert second.load_run(rid)['items'][0]['generation']=='interrupted'
        new=second.create_run(dataset(),'local-http-test');second.cancel(new['run_id'])
    finally:
        if process.poll() is None:process.kill();process.wait(timeout=5)


def test_history_lists_do_not_parse_large_snapshot_bodies(tmp_path,monkeypatch):
    m=api();store=m.EvaluationStore(tmp_path);d=dataset();d['prompt']='x'*8000
    for i in range(4):
        d['version']=str(i);store.save_dataset(d);r=store.create_run(d,'local-http-test');store.cancel(r['run_id'])
    monkeypatch.setattr(m.json,'loads',lambda *_:(_ for _ in ()).throw(AssertionError('history must not parse snapshots')))
    assert len(store.datasets())==4 and len(store.runs())==4


def test_qt_evaluation_run_human_restart_export_and_cancel(tmp_path):
    import os
    os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
    from PySide6.QtWidgets import QApplication
    from PySide6.QtTest import QTest
    from signup031.viewer import EvidenceViewerWindow
    from test_scenario_editor import until
    m=api();server=Provider();server.body=reply();app=QApplication.instance() or QApplication([])
    viewer=EvidenceViewerWindow(tmp_path/'results');viewer.show()
    assert hasattr(viewer,'open_evaluation'),'Evaluation UI is not implemented'
    viewer.open_evaluation();dialog=viewer.evaluation_dialog;dialog.test_endpoint=server.url
    try:
        dialog.set_dataset(dataset());dialog.key.setText(KEY);dialog.save_version();dialog.start_run();assert dialog.process is None
        dialog.preview_requests();dialog.start_run();dialog.start_run();until(lambda:dialog.process is None,15)
        assert len(server.requests)==1;first=dialog.current_run;assert first['status']=='completed'
        dialog.results.selectRow(0);dialog.verdict.setCurrentIndex(dialog.verdict.findData('passed'));dialog.reason.setPlainText('Greeting is directly relevant.');dialog.save_review()
        assert dialog.store.reviews(first['run_id'])[0]['verdict']=='passed'
        target=tmp_path/'evaluation-export.json';dialog.export_to(target);assert KEY.encode() not in target.read_bytes()
        server.delay=.5;dialog.preview_requests();dialog.start_run();until(lambda:len(server.requests)==2)
        rid=dialog.run_id;dialog.cancel();until(lambda:dialog.process is None);QTest.qWait(600)
        state=dialog.store.load_run(rid);assert state['status']=='cancelled' and state['items'][0]['generation']=='cancelled'
        assert dialog.store.load_run(first['run_id'])==first
        dialog.close();viewer.close();viewer=EvidenceViewerWindow(tmp_path/'results');viewer.open_evaluation();dialog=viewer.evaluation_dialog
        assert len(dialog.store.runs())==2 and len(dialog.store.reviews(first['run_id']))==1
        dialog.history.setCurrentIndex(dialog.history.findData(first['run_id']))
        assert dialog.current_run['run_id']==first['run_id']
    finally:
        if dialog.process is not None:dialog.cancel();until(lambda:dialog.process is None)
        dialog.close();viewer.close();app.processEvents();server.stop()


def test_qt_database_write_failure_is_explicit_and_bounded(tmp_path):
    import os,sqlite3,time
    os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
    from PySide6.QtWidgets import QApplication
    from signup031.viewer import EvidenceViewerWindow
    app=QApplication.instance() or QApplication([]);viewer=EvidenceViewerWindow(tmp_path);viewer.open_evaluation();dialog=viewer.evaluation_dialog
    dialog.set_dataset(dataset());lock=sqlite3.connect(dialog.store.path);lock.execute('BEGIN EXCLUSIVE')
    try:
        started=time.monotonic();dialog.save_version()
        assert time.monotonic()-started<1 and '실패' in dialog.status_label.text()
    finally:lock.rollback();lock.close();dialog.close();viewer.close();app.processEvents()


def test_real_gui_death_releases_job_and_preserves_other_live_viewer(tmp_path):
    import os,subprocess,sys,ctypes
    os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
    from PySide6.QtWidgets import QApplication
    from signup031.viewer import EvidenceViewerWindow
    from signup031.owned_job import worker_environment
    from test_scenario_editor import until
    server=Provider();server.body=reply();server.delay=3
    script='''import sys,json
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest
from signup031.viewer import EvidenceViewerWindow
sys.path.insert(0,'tests')
from test_ai_evaluation import dataset
from test_ai_assistant import KEY
from test_scenario_editor import until
app=QApplication([]);v=EvidenceViewerWindow(sys.argv[1]);v.open_evaluation();d=v.evaluation_dialog;d.test_endpoint=sys.argv[2]
d.set_dataset(dataset());d.key.setText(KEY);d.save_version();d.preview_requests();d.start_run()
until(lambda:d.store.load_run(d.run_id)['items'][0]['generation']=='running')
print(json.dumps({'run_id':d.run_id,'worker_pid':d.process.processId()}),flush=True)
while True:QTest.qWait(50)
'''
    process=subprocess.Popen([getattr(sys,'_base_executable',sys.executable),'-c',script,str(tmp_path),server.url],env=worker_environment(),stdout=subprocess.PIPE,text=True)
    app=QApplication.instance() or QApplication([]);viewer=None;handle=None
    kernel=ctypes.WinDLL('kernel32',use_last_error=True);kernel.OpenProcess.argtypes=[ctypes.c_ulong,ctypes.c_int,ctypes.c_ulong];kernel.OpenProcess.restype=ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes=[ctypes.c_void_p,ctypes.c_ulong];kernel.CloseHandle.argtypes=[ctypes.c_void_p]
    try:
        info=json.loads(process.stdout.readline());until(lambda:len(server.requests)==1)
        handle=kernel.OpenProcess(0x100000,False,info['worker_pid']);assert handle
        viewer=EvidenceViewerWindow(tmp_path);viewer.open_evaluation();dialog=viewer.evaluation_dialog
        assert dialog.store.load_run(info['run_id'])['status']=='running' and '살아 있는' in dialog.status_label.text()
        process.kill();process.wait(timeout=5);assert kernel.WaitForSingleObject(handle,5000)==0
        dialog.close();viewer.close();viewer=EvidenceViewerWindow(tmp_path);viewer.open_evaluation();dialog=viewer.evaluation_dialog
        assert dialog.store.load_run(info['run_id'])['status']=='interrupted'
        server.delay=0;dialog.test_endpoint=server.url;dialog.set_dataset(dataset());dialog.key.setText(KEY);dialog.preview_requests();dialog.start_run()
        until(lambda:dialog.process is None,10);assert dialog.current_run['status']=='completed' and dialog.current_run['run_id']!=info['run_id']
    finally:
        if process.poll() is None:process.kill();process.wait(timeout=5)
        if handle:kernel.CloseHandle(handle)
        if viewer:viewer.close();app.processEvents()
        server.stop()
