"""A real failing Selenium test remains inspectable after the origin shuts down."""
import hashlib,json,os,subprocess,sys
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from threading import Thread
import pytest
from test_manual_recording import manual_site


@pytest.fixture(scope='module')
def failure_runs(tmp_path_factory):
    root=tmp_path_factory.mktemp('local-failure')
    class Handler(BaseHTTPRequestHandler):
        hits=0
        def do_GET(self):
            Handler.hits+=1
            body=b'''<label>Query<input id="query"></label><button id="apply">Apply</button><p id="result">Initial</p>
            <script>document.querySelector('#apply').onclick=()=>document.querySelector('#result').textContent=document.querySelector('#query').value;</script>'''
            if self.path=='/blank':body=b'<html><body></body></html>'
            self.send_response(200);self.send_header('Content-Type','text/html');self.end_headers();self.wfile.write(body)
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=Thread(target=server.serve_forever,daemon=True);thread.start()
    test=root/'test_original_failure.py'
    test.write_text('''import os,pytest
from selenium import webdriver
from selenium.webdriver.common.by import By
from signup031.selenium_recording import recording_options
@pytest.fixture
def driver(selenium_record):
    d=webdriver.Chrome(options=recording_options(headless=True));selenium_record(d)
    yield d
    d.quit()
def test_assertion(driver):
    driver.get(os.environ['QA_FAILURE_URL'])
    driver.find_element(By.ID,'query').send_keys('bug')
    driver.find_element(By.ID,'apply').click()
    assert driver.find_element(By.ID,'result').text=='healthy'
def test_dom_action(driver):
    driver.get(os.environ['QA_FAILURE_URL'])
    driver.find_element(By.ID,'query').send_keys('bug')
    driver.execute_script("const b=document.createElement('button');b.id='transient';b.textContent='Temporary';document.body.append(b)")
    driver.find_element(By.ID,'transient').click()
    driver.find_element(By.ID,'missing-original-control').click()
def test_blank(driver):
    driver.get(os.environ['QA_FAILURE_URL']+'blank')
    assert False, 'blank document is not a usable investigation'
''',encoding='utf-8')
    try:
        env={**os.environ,'QA_FAILURE_URL':f'http://127.0.0.1:{server.server_port}/'}
        child=subprocess.run([sys.executable,'-m','pytest','-p','signup031.selenium_plugin',str(test),'--rootdir',str(root),'--selenium-artifacts',str(root/'runs'),'-q'],env=env,capture_output=True,timeout=60)
        (root/'capture.log').write_bytes(child.stdout+child.stderr)
        assert child.returncode==1 and b'3 failed' in child.stdout,child.stdout+child.stderr
    finally:server.shutdown();server.server_close();thread.join()
    records={json.loads(p.read_text(encoding='utf-8'))['tc_id'].rsplit('::',1)[-1]:p for p in (root/'runs').rglob('evidence.json')}
    return root,records,Handler


@pytest.mark.parametrize('name,wanted,completed',[('test_assertion','ready',2),('test_dom_action','partial',1)])
def test_offline_failure_keeps_page_for_manual_input(failure_runs,name,wanted,completed):
    from signup031.replay import ReplaySession
    root,records,handler=failure_runs;source=records[name]
    before={p:p.read_bytes() for p in source.parent.rglob('*') if p.is_file()}
    payload=json.loads(source.read_text(encoding='utf-8'));assert payload['result']['business']['status']=='failed'
    assert ('AssertionError' if name=='test_assertion' else 'NoSuchElementException') in json.dumps(payload['result'])
    with ReplaySession(source.parent/'archive',headless=True) as session:
        state=session.investigate()
        assert state['investigation']['state']==wanted
        assert state['investigation']['completed_actions']==completed
        assert state['restored'] is (wanted=='ready')
        assert not session.page.is_closed()
        session.page.locator('#query').fill('manual investigation');session.page.locator('#apply').click()
        assert session.page.locator('#result').inner_text()=='manual investigation'
        session.page.evaluate("fetch('/uncollected').catch(()=>null);new WebSocket('ws://127.0.0.1:9/socket')")
        session.page.wait_for_timeout(100)
        assert {row['kind'] for row in session.blocked}>={'http','websocket'}
    assert all(p.read_bytes()==data for p,data in before.items())


def test_local_notes_bind_source_and_reject_stale_edits(failure_runs):
    from signup031.local_investigation import LocalInvestigationStore
    from signup031.replay import ReplaySession
    root,records,_=failure_runs;source=records['test_assertion'];eid=json.loads(source.read_text(encoding='utf-8'))['execution']['id']
    original=source.read_bytes();store=LocalInvestigationStore(root/'runs')
    with ReplaySession(source.parent/'archive',headless=True) as session:
        status=session.investigate()['investigation']
        first=store.load(eid);stale=store.load(eid)
        first.update(session=status,verdict='same_issue',notes='수동 입력 후 같은 문제를 관측함')
        saved=store.save(first)
        assert store.load(eid)==saved and saved['source']['sha256']==hashlib.sha256(original).hexdigest()
        with pytest.raises(ValueError,match='다른 창'):store.save(stale)
    assert source.read_bytes()==original


def test_viewer_opens_notes_during_pinned_investigation(failure_runs):
    os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import Qt
    from signup031.viewer import EvidenceViewerWindow
    from test_scenario_editor import until
    root,records,_=failure_runs;app=QApplication.instance() or QApplication([])
    viewer=EvidenceViewerWindow(root/'runs',replay_headless=True);viewer.show();app.processEvents()
    for row in range(viewer.run_list.count()):
        if viewer.run_list.item(row).data(Qt.ItemDataRole.UserRole).source_path==records['test_dom_action']:viewer.run_list.setCurrentRow(row);break
    try:
        viewer.start_replay(headless=True,investigate=True)
        until(lambda:viewer.local_session and viewer.local_session['state']=='partial',30)
        assert viewer.replay_process is not None and not viewer.run_list.isEnabled()
        assert viewer.investigation_button.isEnabled()
        assert not viewer.analysis_button.isEnabled()
        viewer.open_investigation();app.processEvents()
        dialog=viewer.investigation_dialog
        assert dialog.execution_id==json.loads(records['test_dom_action'].read_text(encoding='utf-8'))['execution']['id']
        dialog.local_notes.setPlainText('조사 중 저장한 메모');dialog.local_verdict.setCurrentIndex(dialog.local_verdict.findData('undetermined'))
        dialog.save_local();dialog.close();viewer.open_investigation();app.processEvents()
        assert viewer.investigation_dialog.local_notes.toPlainText()=='조사 중 저장한 메모'
        viewer.investigation_dialog.local_notes.setPlainText('저장하지 않고 닫은 편집')
        viewer.investigation_dialog.close()
        viewer.open_investigation();assert viewer.investigation_dialog.local_notes.toPlainText()=='조사 중 저장한 메모'
        viewer.investigation_dialog.close()
    finally:
        viewer.stop_replay();until(lambda:viewer.replay_process is None);viewer.close()


def test_unusable_and_corrupt_archive_never_ready(failure_runs,tmp_path):
    import shutil
    from signup031.replay import ReplaySession
    _,records,_=failure_runs
    with ReplaySession(records['test_blank'].parent/'archive',headless=True) as session:
        with pytest.raises(ValueError,match='조사 화면'):session.investigate()
        assert session.status()['investigation']['state']=='unavailable'
    with ReplaySession(records['test_assertion'].parent/'archive',headless=True) as session:
        session.page.close()
        with pytest.raises(ValueError):session.investigate()
    archive=tmp_path/'corrupt';shutil.copytree(records['test_assertion'].parent/'archive',archive)
    (archive/'resources.har').write_bytes(b'broken')
    with pytest.raises(ValueError):ReplaySession(archive,headless=True)


def test_manual_investigation_blocks_live_receiver(failure_runs):
    from signup031.replay import ReplaySession
    _,records,_=failure_runs
    class Receiver(BaseHTTPRequestHandler):
        hits=0
        def do_GET(self):Receiver.hits+=1;self.send_response(200);self.end_headers()
        def do_POST(self):Receiver.hits+=1;self.send_response(200);self.end_headers()
        def log_message(self,*args):pass
    receiver=ThreadingHTTPServer(('127.0.0.1',0),Receiver);thread=Thread(target=receiver.serve_forever,daemon=True);thread.start()
    try:
        with ReplaySession(records['test_assertion'].parent/'archive',headless=True) as session:
            session.investigate();port=receiver.server_port
            session.page.evaluate(f"fetch('http://127.0.0.1:{port}/get').catch(()=>null);fetch('http://127.0.0.1:{port}/post',{{method:'POST',body:'manual'}}).catch(()=>null);new WebSocket('ws://127.0.0.1:{port}/socket')")
            session.page.wait_for_timeout(150)
            assert Receiver.hits==0 and len(session.blocked)>=3
            assert session.status()['investigation']['state']=='partial'
    finally:receiver.shutdown();receiver.server_close();thread.join()


def test_local_notes_reject_source_change_duplicate_and_busy_writer(failure_runs,tmp_path):
    import shutil
    from signup031.local_investigation import LocalInvestigationStore
    _,records,_=failure_runs;source=records['test_assertion']
    root=tmp_path/'isolated';shutil.copytree(source.parent,root/'one')
    path=root/'one/evidence.json';eid=json.loads(path.read_text(encoding='utf-8'))['execution']['id']
    store=LocalInvestigationStore(root);doc=store.save(store.load(eid))
    original=path.read_bytes();path.write_bytes(original+b'\n')
    with pytest.raises(ValueError,match='해시'):store.save(doc)
    path.write_bytes(original)
    lock=store.path_for(eid).with_suffix('.lock');lock.write_text('owned test writer')
    try:
        with pytest.raises(ValueError,match='다른 창'):store.save(doc)
    finally:lock.unlink()
    shutil.copytree(root/'one',root/'duplicate')
    with pytest.raises(ValueError,match='중복'):store.load(eid)


@pytest.mark.parametrize('version',[2,3])
def test_web_and_manual_archives_support_independent_investigation(manual_site,tmp_path,version):
    from signup031.manual_recording import ManualRecorder
    from signup031.web_runner import run_scenario
    from signup031.replay import ReplaySession
    if version==2:
        source=run_scenario({'version':1,'id':'WEB','title':'Web failure','url':manual_site[0],
            'steps':[{'action':'fill','locator':'css','target':'#query','value':'bug'}],
            'checks':[{'kind':'input_length','locator':'css','target':'#query','expected':7}]},tmp_path/'runs')
    else:
        with ManualRecorder(manual_site[0]+'/search','Manual',tmp_path/'runs',headless=True) as recorder:
            recorder.page.locator('#query').fill('bug');source=recorder.save()
    original={p:p.read_bytes() for p in source.parent.rglob('*') if p.is_file()}
    with ReplaySession(source.parent/'archive',headless=True) as session:
        result=session.investigate()['investigation']
        assert result['state']=='ready' and result['completed_actions']==1
        session.page.locator('#query').fill('edited');session.page.locator('#apply').click()
        assert session.page.locator('#result').inner_text()=='edited'
    assert all(p.read_bytes()==data for p,data in original.items())


def test_observation_difference_is_partial_but_strict_still_fails(manual_site,tmp_path):
    from signup031.manual_recording import ManualRecorder
    from signup031.replay import ReplaySession
    with ManualRecorder(manual_site[0]+'/search','Unjournaled DOM state',tmp_path/'runs',headless=True) as recorder:
        recorder.page.evaluate("document.querySelector('#result').textContent='volatile state'")
        source=recorder.save()
    with ReplaySession(source.parent/'archive',headless=True) as strict:
        with pytest.raises(ValueError,match='saved visible text differs'):strict.restore()
    with ReplaySession(source.parent/'archive',headless=True) as session:
        state=session.investigate()['investigation']
        assert state['state']=='partial' and state['phase']=='observations'
        assert 'saved visible text differs' in state['reason']
        session.page.locator('#query').fill('manual');session.page.locator('#apply').click()
        assert session.page.locator('#result').inner_text()=='manual'
