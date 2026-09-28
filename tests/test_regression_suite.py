"""Suite snapshot, real browser execution, owned cancellation and Qt integration."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import time

import pytest
from test_web_scenarios import web_site,scenario_for


def api():
    assert importlib.util.find_spec('signup031.regression_suite') is not None,'Regression suite is not implemented'
    from signup031 import regression_suite
    return regression_suite


def files(tmp_path,base):
    paths=[]
    for name in ('password','search'):
        path=tmp_path/(name+'.json');path.write_text(json.dumps(scenario_for(base,name)),encoding='utf-8');paths.append(path)
    return paths


def test_all_files_validated_and_snapshots_frozen_before_any_run(tmp_path):
    m=api();paths=files(tmp_path,'http://127.0.0.1:1')
    snap=m.freeze_suite('suite',paths);before=deepcopy(snap)
    paths[0].write_text('{}');paths[1].unlink()
    assert snap==before and snap['items'][1]['config']['id']=='FILTER-007'
    with pytest.raises(ValueError,match='(?s)1행:.*2행:'):m.freeze_suite('suite',paths)
    paths=files(tmp_path,'http://127.0.0.1:1')
    with pytest.raises(ValueError,match='ID'):m.freeze_suite('suite',[paths[0],paths[0]])
    paths[0].write_text(json.dumps({'automation':None,'title':'manual TC'}))
    with pytest.raises(ValueError):m.freeze_suite('suite',paths)
    assert not (tmp_path/'.suites').exists()


def test_comparison_has_fixed_criteria_and_explicit_denominator(tmp_path):
    m=api();snap=m.freeze_suite('suite',files(tmp_path,'http://127.0.0.1:1'))
    old={'items':[dict(x,status='failed') for x in snap['items']]}
    new=deepcopy(old);new['items'][0]['status']='passed'
    new['items'][1]['criteria_sha256']='changed';new['items'][1]['status']='passed'
    new['items'].append(dict(new['items'][0],tc_id='new'))
    old['items'].append(dict(old['items'][0],tc_id='removed'))
    comparison=m.compare_runs(old,new)
    assert [x['change'] for x in comparison]==['recovered','criteria_changed','added','removed']
    statuses=['passed','failed','preparation_failed','cancelled','not_run','interrupted','skipped']
    summary=m.summarize({'items':[{'status':s} for s in statuses]})
    assert summary['total']==7 and summary['passed']==1 and summary['not_run']==1
    assert summary['pass_rate']==1/7
    for status in statuses[2:]:
        assert m.compare_runs({'items':[old['items'][0]]},{'items':[dict(new['items'][0],status=status)]})[0]['change']=='not_comparable'


def test_sequential_real_browser_failure_continues_and_files_can_disappear(web_site,tmp_path):
    m=api();paths=files(tmp_path,web_site[0]);snap=m.freeze_suite('browser suite',paths)
    def started(state):
        if not state.get('test_deleted'):
            for p in paths:p.unlink(missing_ok=True)
    assert hasattr(m,'run_suite'),'Suite executor is not implemented'
    state=m.run_suite(tmp_path/'results',snap,on_update=started)
    assert state['status']=='completed'
    assert [r['status'] for r in state['items']]==['failed','passed']
    assert len({i['execution_id'] for i in state['items']})==2
    for item in state['items']:
        payload=json.loads((tmp_path/'results'/item['evidence']).read_text(encoding='utf-8'))
        assert payload['execution']['id']==item['execution_id'] and payload['scenario_snapshot']==item['config']
        assert payload['replay']['status']=='recorded'
    assert m.SuiteStore(tmp_path/'results').list_runs()[0]['summary']['passed']==1


def test_running_lease_recovery_and_unwritable_root(tmp_path):
    m=api();snap=m.freeze_suite('suite',files(tmp_path,'http://127.0.0.1:1'));store=m.SuiteStore(tmp_path/'results')
    with store.lease():
        state=store.create(snap);state['items'][0]['status']='running';store.save(state)
        with pytest.raises(ValueError,match='실행 중'):store.recover()
    snapshot=(store.path(state['suite_id'])/'snapshot.json').read_bytes()
    store.recover();recovered=store.read(state['suite_id'])
    assert recovered['status']=='interrupted' and [i['status'] for i in recovered['items']]==['interrupted','not_run']
    assert snapshot==(store.path(state['suite_id'])/'snapshot.json').read_bytes()
    blocked=tmp_path/'blocked';blocked.mkdir();(blocked/'.suites').write_text('occupied')
    with pytest.raises((ValueError,OSError)):m.SuiteStore(blocked)


def test_cancel_owned_browser_before_remaining_site_request(web_site,tmp_path):
    m=api();paths=files(tmp_path,web_site[0]);config=scenario_for(web_site[0],'search');config['steps'][0]={'action':'wait','locator':'css','target':'#never','value':''}
    paths[0].write_text(json.dumps(config));config=scenario_for(web_site[0],'password');paths[1].write_text(json.dumps(config))
    snap=m.freeze_suite('cancel',paths);baseline=web_site[2].requests
    canary=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])
    try:
        assert hasattr(m,'run_suite'),'Suite executor is not implemented'
        state=m.run_suite(tmp_path/'results',snap,cancel=lambda:web_site[2].requests>baseline)
        assert state['status']=='cancelled' and [i['status'] for i in state['items']]==['cancelled','not_run']
        assert state['items'][0]['cleanup']['status']=='completed'
        after=web_site[2].requests;time.sleep(.3);assert web_site[2].requests==after
        assert canary.poll() is None
    finally:canary.terminate();canary.wait(timeout=5)


def test_abrupt_worker_exit_kills_owned_descendants_and_recovers(web_site,tmp_path):
    m=api();from signup031.owned_job import OwnedJob,python_command,worker_environment
    paths=files(tmp_path,web_site[0]);config=json.loads(paths[0].read_text());config['steps'][0]['target']='#never';config['steps'][0]['locator']='css';paths[0].write_text(json.dumps(config))
    snap=m.freeze_suite('crash',paths);root=tmp_path/'results';canary=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])
    job=OwnedJob();process=subprocess.Popen(python_command('signup031.suite_worker'),stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,env=worker_environment())
    try:
        job.attach(process.pid);process.stdin.write(json.dumps({'root':str(root),'snapshot':snap}).encode());process.stdin.close()
        deadline=time.monotonic()+15
        while web_site[2].requests==0 and time.monotonic()<deadline:time.sleep(.05)
        assert web_site[2].requests>0 and len(job.pids())>2
        process.kill();process.wait(timeout=5)
        deadline=time.monotonic()+5
        while job.pids() and time.monotonic()<deadline:time.sleep(.05)
        assert not job.pids() and canary.poll() is None
        store=m.SuiteStore(root);store.recover();state=store.list_runs()[0]
        assert state['status']=='interrupted' and state['items'][1]['status']=='not_run'
        assert state['summary']['passed']==0
    finally:
        job.close()
        if process.poll() is None:process.kill()
        process.wait(timeout=5);canary.terminate();canary.wait(timeout=5)


def test_qt_suite_save_load_run_compare_details_and_close(web_site,tmp_path,monkeypatch):
    import os
    os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QTimer
    from signup031.viewer import EvidenceViewerWindow
    from test_scenario_editor import until
    app=QApplication.instance() or QApplication([]);window=EvidenceViewerWindow(tmp_path/'results');window.show()
    assert hasattr(window,'open_suite'),'Suite UI entrypoint is not implemented'
    window.open_suite();dialog=window.suite_dialog
    timer=QTimer();ticks=[];timer.timeout.connect(lambda:ticks.append(1));timer.start(20)
    try:
        paths=files(tmp_path,web_site[0]);dialog.name_edit.setText('Qt 회귀');dialog.set_paths(paths)
        definition=tmp_path/'suite.json';dialog.save_to(definition);dialog.set_paths([]);dialog.load_from(definition)
        dialog.start_run();dialog.start_run();assert not dialog.edit_area.isEnabled()
        until(lambda:dialog.process is None,30)
        assert len(ticks)>3 and dialog.current_state['summary']['passed']==1 and len(dialog.store.list_runs())==1,dialog.status_label.text()
        original=deepcopy(dialog.current_state);dialog.start_run();until(lambda:dialog.process is None,30)
        assert dialog.current_state['suite_id']!=original['suite_id']
        dialog.compare_with(original['suite_id']);assert '변화 없음' in dialog.comparison.toPlainText()
        assert dialog.baseline.currentData()==original['suite_id']
        with monkeypatch.context() as scoped:
            scoped.setattr('signup031.suite_dialog.python_command',lambda *_:[sys._base_executable,'-c','import sys; sys.stdin.read(); sys.exit(9)'])
            dialog.start_run();until(lambda:dialog.process is None)
            assert '실행 실패' in dialog.status_label.text() and '실행 종료 · completed' not in dialog.status_label.text()
        from signup031.viewer_model import _load_one
        item=dialog.current_state['items'][1];detail_path=(dialog.store.root/item['evidence']).resolve()
        detail=_load_one(detail_path,dialog.store.root)
        assert detail.tc_id=='FILTER-007' and detail.business_status=='passed'
        dialog.results.selectRow(1);dialog.open_detail()
        assert window.records and all(record.business_status in ('failed','preparation_failed','execution_error') for record in window.records)
        assert 'FILTER-007' not in [record.tc_id for record in window.records]
        dialog.show();config=json.loads(paths[0].read_text());config['steps'][0].update(locator='css',target='#never');paths[0].write_text(json.dumps(config))
        baseline=web_site[2].requests;dialog.start_run();until(lambda:web_site[2].requests>baseline)
        window.close();until(lambda:dialog.process is None and not window.isVisible(),15)
        assert dialog.current_state['status']=='cancelled' and dialog.current_state['items'][1]['status']=='not_run'
        reopened=EvidenceViewerWindow(tmp_path/'results');reopened.open_suite()
        assert len(reopened.suite_dialog.store.list_runs())==3
        reopened.suite_dialog.close();reopened.close()
    finally:
        timer.stop()
        if dialog.process is not None:dialog.cancel_run();until(lambda:dialog.process is None,15)
        dialog.close();window.close();app.processEvents()


def test_valid_unicode_snapshot_fits_worker_envelope(tmp_path):
    m=api();from signup031 import suite_worker
    config=scenario_for('http://127.0.0.1:1','search')
    config['checks']=[{'kind':'text','locator':'css','target':'#result','expected':'한'*10000} for _ in range(30)]
    paths=[]
    for i in range(11):
        path=tmp_path/f'{i}.json';config['id']=str(i);path.write_text(json.dumps(config,ensure_ascii=False),encoding='utf-8');paths.append(path)
    snapshot=m.freeze_suite('큰 설정',paths)
    raw=json.dumps({'root':str(tmp_path/'results'),'snapshot':snapshot}).encode()
    assert len(raw)>m.MAX_BYTES
    assert hasattr(suite_worker,'parse_request'),'Worker envelope budget must be separate from snapshot budget'
    assert suite_worker.parse_request(raw)['snapshot']==snapshot


def test_attachment_failure_prevents_site_requests_and_timeout_is_error(web_site,tmp_path,monkeypatch):
    m=api();from signup031.owned_job import OwnedJob
    paths=files(tmp_path,web_site[0]);snap=m.freeze_suite('suite',paths)
    with monkeypatch.context() as scoped:
        scoped.setattr(OwnedJob,'attach',lambda *_:(_ for _ in ()).throw(OSError('controlled attachment failure')))
        state=m.run_suite(tmp_path/'failed-ownership',snap)
    assert web_site[2].requests==0 and state['summary']['preparation_failed']==2
    config=json.loads(paths[0].read_text());config['steps'][0].update(locator='css',target='#never');paths[0].write_text(json.dumps(config))
    state=m.run_suite(tmp_path/'timeout',m.freeze_suite('timeout',paths),case_timeout=.5)
    assert state['items'][0]['status']=='preparation_failed' and '시간 상한' in state['items'][0]['reason']
    assert state['summary']['total']==2 and state['items'][1]['status'] in ('passed','preparation_failed')
    assert all(i['cleanup']['status']=='completed' for i in state['items'])


def test_state_write_tolerates_brief_windows_reader_lock(tmp_path):
    from threading import Thread,Event
    m=api();store=m.SuiteStore(tmp_path/'results');snapshot=m.freeze_suite('reader',files(tmp_path,'http://127.0.0.1:1'))
    with store.lease():
        state=store.create(snapshot);ready=Event()
        def reader():
            with (store.path(state['suite_id'])/'state.json').open('rb') as stream:
                ready.set();time.sleep(.15);assert stream.read()
        thread=Thread(target=reader);thread.start();assert ready.wait(2)
        try:state['message']='new state';store.save(state)
        finally:thread.join()
        assert store.read(state['suite_id'])['message']=='new state'


def test_gui_abrupt_exit_closes_nested_jobs_but_not_canary(web_site,tmp_path):
    m=api();from signup031.owned_job import OwnedJob,worker_environment
    paths=files(tmp_path,web_site[0]);config=json.loads(paths[0].read_text());config['steps'][0].update(locator='css',target='#never');paths[0].write_text(json.dumps(config))
    host_code='''import json,sys,os
os.environ['QT_QPA_PLATFORM']='offscreen'
request=json.load(sys.stdin)
from PySide6.QtWidgets import QApplication
from signup031.viewer import EvidenceViewerWindow
app=QApplication([]);window=EvidenceViewerWindow(request['root']);window.show();window.open_suite()
d=window.suite_dialog;d.name_edit.setText('abrupt GUI');d.set_paths(request['paths']);d.start_run()
app.exec()
'''
    root=tmp_path/'results';guard=OwnedJob();canary=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])
    host=subprocess.Popen([sys._base_executable,'-c',host_code],stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,env=worker_environment())
    try:
        guard.attach(host.pid);host.stdin.write(json.dumps({'root':str(root),'paths':[str(p) for p in paths]}).encode());host.stdin.close()
        deadline=time.monotonic()+15
        while web_site[2].requests==0 and time.monotonic()<deadline:time.sleep(.05)
        assert web_site[2].requests>0 and len(guard.pids())>3
        host.kill();host.wait(timeout=5);deadline=time.monotonic()+5
        while guard.pids() and time.monotonic()<deadline:time.sleep(.05)
        assert not guard.pids() and canary.poll() is None
        store=m.SuiteStore(root);store.recover();state=store.list_runs()[0]
        assert state['status']=='interrupted' and state['summary']['not_run']==1 and state['summary']['passed']==0
    finally:
        guard.close()
        if host.poll() is None:host.kill()
        host.wait(timeout=5);canary.terminate();canary.wait(timeout=5)


def test_persistent_state_write_failure_stops_and_keeps_original_evidence(web_site,tmp_path,monkeypatch):
    m=api();snapshot=m.freeze_suite('storage',files(tmp_path,web_site[0]));root=tmp_path/'results';original=m.SuiteStore.save
    def failure(store,state):
        if state['items'][0]['status']=='failed':raise PermissionError('controlled persistent storage denial')
        return original(store,state)
    with monkeypatch.context() as scoped:
        scoped.setattr(m.SuiteStore,'save',failure)
        with pytest.raises(PermissionError):m.run_suite(root,snapshot)
    evidence=list(root.rglob('evidence.json'));assert len(evidence)==1
    before=evidence[0].read_bytes();store=m.SuiteStore(root);store.recover();state=store.list_runs()[0]
    assert state['status']=='interrupted' and state['items'][1]['status']=='not_run'
    assert evidence[0].read_bytes()==before


def test_preparation_failure_continues_and_cleanup_error_is_separate(web_site,tmp_path,monkeypatch):
    m=api();from signup031.owned_job import OwnedJob
    paths=files(tmp_path,web_site[0]);config=json.loads(paths[0].read_text());config['steps'][0].update(locator='css',target='[');paths[0].write_text(json.dumps(config))
    stop=OwnedJob.stop;calls=[]
    def cleanup_failure(job):
        stop(job);calls.append(1)
        if len(calls)==1:raise OSError('controlled cleanup status failure')
    monkeypatch.setattr(OwnedJob,'stop',cleanup_failure)
    state=m.run_suite(tmp_path/'results',m.freeze_suite('prep',paths))
    assert [i['status'] for i in state['items']]==['preparation_failed','passed']
    assert state['items'][0]['cleanup']['status']=='failed' and state['cleanup_errors']
    assert state['summary']['passed']==1
