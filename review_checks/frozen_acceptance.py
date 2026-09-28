"""Acceptance-only frozen Qt entry. Never included in the product installer."""
import argparse
import hashlib
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
import traceback
from PySide6.QtCore import QProcess,Qt
from PySide6.QtWidgets import QApplication
from signup031.desktop_runtime import configure_runtime
from signup031.viewer import EvidenceViewerWindow,render_window_to_png
from signup031.owned_job import configure_worker


def guided_acceptance(data,output,app,viewer):
    from signup031.scenario_editor import ScenarioEditor
    from signup031.ingestion_dialog import ServerImportDialog
    from signup031.ai_dialog import AIAssistantDialog
    from signup031.evaluation_dialog import EvaluationDialog
    from signup031.jira_dialog import JiraSyncDialog
    from PySide6.QtWidgets import QWidget,QLineEdit
    from types import SimpleNamespace
    editor=ScenarioEditor(data);editor.show();app.processEvents()
    editor.set_config({'version':1,'id':'GUIDED','title':'기록한 입력과 결과 확인','url':'https://example.test',
        'steps':[{'action':'fill','locator':'label','target':'검색어','value':'사과'},
                 {'action':'click','locator':'css','target':'#search','value':''}],
        'checks':[],'draft':True,'source_manual':{'execution_id':'synthetic-guided','evidence_sha256':'a'*64,
        'limitations':['URL changed during recording','unknown future limitation']}})
    editor.start_run();assert editor.process is None and '결과 추가' in editor.status_label.text()
    editor.save_to(output/'draft.json');assert '초안 저장됨' in editor.status_label.text()
    for row,(kind,expected) in enumerate((('input_length','2'),('visible','false'),('text','완료'))):
        editor.add_check_button.click();combo=editor.checks_table.cellWidget(row,0);combo.setCurrentIndex(combo.findData(kind))
        editor.checks_table.cellWidget(row,2).choice.setCurrentIndex(1)
        editor.checks_table.cellWidget(row,3).setText(expected)
    editor.save_to(output/'guided.json');editor.load_from(output/'guided.json')
    assert [c['expected'] for c in editor.config()['checks']]==[2,False,'완료']
    assert all(c['target']=='검색어' and c['locator']=='label' for c in editor.config()['checks'])
    for width,height in ((900,620),(1180,760)):
        editor.resize(width,height)
        for advanced in (False,True):
            editor.advanced_check.setChecked(advanced);app.processEvents()
            editor.help_scroll.verticalScrollBar().setValue(0)
            assert render_window_to_png(editor,output/f'guided-{"advanced" if advanced else "basic"}-{width}.png')
            editor.help_scroll.ensureWidgetVisible(editor.checks_table);app.processEvents()
            assert render_window_to_png(editor,output/f'guided-results-{"advanced" if advanced else "basic"}-{width}.png')
        editor.help_button.click();app.processEvents();editor.help_dialog.resize(width,height)
        assert editor.help_dialog.parentWidget() is editor and editor.help_dialog.current_topic=='scenario'
        assert render_window_to_png(editor.help_dialog,output/f'guided-help-{width}.png');editor.help_dialog.close()
    editor.close()
    parent=QWidget();parent.execution_id='synthetic'
    def missing(*args):raise OSError('synthetic local record absent')
    parent.store=SimpleNamespace(record=missing)
    for name,factory in (('server',lambda:ServerImportDialog(viewer)),('jira',lambda:JiraSyncDialog(parent)),('ai',lambda:AIAssistantDialog(viewer)),('evaluation',lambda:EvaluationDialog(viewer))):
        dialog=factory();dialog.show();app.processEvents()
        key=dialog.token_edit if hasattr(dialog,'token_edit') else dialog.key
        assert key.echoMode()==QLineEdit.EchoMode.Password and dialog.process is None
        for width,height in ((900,620),(1180,760)):
            dialog.resize(width,height);app.processEvents();assert render_window_to_png(dialog,output/f'connection-{name}-{width}.png')
            if name=='evaluation':
                dialog.help_scroll.ensureWidgetVisible(key);app.processEvents();assert render_window_to_png(dialog,output/f'connection-{name}-key-{width}.png')
        dialog.close();app.processEvents()
    (output/'guided-summary.json').write_text(json.dumps({'typed_values':[2,False,'완료'],'targets_preserved':True,'empty_checks_blocked':True,'draft_saved':True,'masked_keys':True,'network_or_worker_started':False,'sizes':[[900,620],[1180,760]],'user_data_used':False},ensure_ascii=False,indent=2),encoding='utf-8')


def failure_acceptance(data,output,app,viewer):
    """Real collected Selenium example, offline interaction and HTTP protocol double."""
    from signup031.replay import ReplaySession
    from signup031.investigation import InvestigationStore
    from signup031.test_context import load_test_context
    from signup031.ai_assistant import digest
    from urllib.parse import urlsplit
    import socket
    source=next(path for path in data.rglob('evidence.json') if json.loads(path.read_bytes())['tc_id'].endswith('::test_search_result'))
    original=source.read_bytes();payload=json.loads(original)
    original_hashes={path:hashlib.sha256(path.read_bytes()).hexdigest() for path in source.parent.rglob('*') if path.is_file()}
    viewer._scenario_recorded(str(source));app.processEvents()
    assert payload['result']['business']['status']=='failed'
    context=load_test_context(source)
    assert context['test_context']['tc_id']=='SEARCH-FAILURE-01' and 'def test_search_result' in context['test_context']['automation_code']
    target=urlsplit(InvestigationStore(data).record(payload['execution']['id']).target_url)
    with socket.socket() as check:
        check.settimeout(.5);assert check.connect_ex((target.hostname,target.port))!=0,'Original example server must be stopped'
    with ReplaySession(source.parent/'archive',headless=True) as session:
        manual_state=session.investigate()['investigation']
        assert manual_state['state'] in ('ready','partial')
        session.page.locator('#query').fill('배');session.page.locator('#search').click()
        observed=session.page.locator('#result').inner_text();assert observed=='검색 오류: 배'
    def until(predicate):
        start=time.monotonic()
        while not predicate():
            app.processEvents();time.sleep(.02)
            if time.monotonic()-start>30:raise AssertionError('Failure flow timed out: '+viewer.replay_label.text())
    for width,height in ((900,620),(1180,760)):
        viewer.resize(width,height);app.processEvents();assert viewer.width()==width and viewer.height()==height
        assert not viewer.other_tools.isVisible();assert render_window_to_png(viewer,output/f'failure-home-{width}.png')
        assert not viewer.other_tools_button.isVisible() and not viewer.server_import_button.isVisible() and not viewer.timeline_button.isVisible()
    viewer.start_replay(headless=True,investigate=True)
    until(lambda:viewer.local_session is not None and viewer.local_session['state'] in ('ready','partial','unavailable'))
    assert viewer.local_session['state'] in ('ready','partial');worker_state=viewer.local_session
    viewer.stop_replay();until(lambda:viewer.replay_process is None)
    viewer.open_investigation();dialog=viewer.investigation_dialog;app.processEvents()
    assert dialog.tabs.count()==1 and not dialog.jira_button.isVisible()
    dialog.set_local_session(worker_state,InvestigationStore(data)._reference(InvestigationStore(data).record(payload['execution']['id'])),active=False)
    dialog.local_notes.setPlainText('원본 서버 종료 후 복원 화면에서 검색어에 배를 입력하고 검색을 클릭했습니다. 검색 오류: 배를 확인했습니다.')
    dialog.local_verdict.setCurrentIndex(dialog.local_verdict.findData('same_issue'))
    for width,height in ((900,620),(1180,760)):
        dialog.resize(width,height);app.processEvents();assert dialog.size().width()==width and dialog.size().height()==height;assert render_window_to_png(dialog,output/f'failure-investigation-{width}.png')
    dialog.open_analysis();analysis=dialog.analysis_dialog;analysis.model.setText('synthetic-model');app.processEvents()
    assert '배를 입력' in analysis.collected.toPlainText()
    for width,height in ((900,620),(1180,760)):
        analysis.resize(width,height);app.processEvents();assert analysis.size().width()==width and analysis.size().height()==height;assert render_window_to_png(analysis,output/f'failure-analysis-{width}.png')
    analysis.preview_request();exported=analysis.export_request();assert exported and analysis.process is None
    analysis.help_button.click();app.processEvents()
    help_window=analysis.help_dialog
    assert set(help_window.ids)=={'start','selenium','replay','report','failure-analysis','browser','terms','troubleshooting'}
    help_window.select_topic('selenium')
    for width,height in ((900,620),(1180,760)):
        help_window.resize(width,height);app.processEvents();assert help_window.width()==width and help_window.height()==height
        assert render_window_to_png(help_window,output/f'failure-help-{width}.png')
    # The frozen product must expose the same path-specific, file-by-file guide.
    # These marker folders are acceptance-owned UI inputs; actual pip/pytest execution is
    # covered by verify_practical_setup.py against a copied real source package.
    guide_source=output/'도움말 검증 소스';guide_project=output/'도움말 검증 테스트 프로젝트'
    (guide_source/'signup031').mkdir(parents=True,exist_ok=True);guide_project.mkdir(parents=True,exist_ok=True)
    (guide_source/'pyproject.toml').write_text('[project]\nname="frozen-guide-marker"\n',encoding='utf-8')
    (guide_source/'signup031'/'__init__.py').write_text('',encoding='utf-8')
    help_window.copy_setup.click();app.processEvents();guide=help_window.setup_guide
    guide.source_path.setText(str(guide_source));guide.project_path.setText(str(guide_project));app.processEvents()
    assert guide.materials and all(button.isEnabled() for button in guide.copy_buttons.values())
    guide.copy_buttons['conftest_new'].click();assert 'selenium_record(browser' in QApplication.clipboard().text()
    assert '복사' in guide.copy_status.text()
    for width,height in ((900,620),(1180,760)):
        guide.resize(width,height)
        for index in range(guide.tabs.count()):
            guide.tabs.setCurrentIndex(index);app.processEvents()
            assert guide.width()==width and guide.height()==height
            assert render_window_to_png(guide,output/f'failure-setup-{index+1}-{width}.png')
    guide.close()
    help_window.close()
    value={'candidates':[{'category':'product','title':'검색 결과 문구 불일치 후보','evidence_ids':['original.failure','collected.expected','local.notes'],
        'reasoning':'자동화 실패와 수동 조사에서 오류 문구가 관측되었습니다. 실제 원인은 추가 검토가 필요합니다.',
        'missing':['제품의 실제 요구사항 검토'],'next_checks':['기대 문구와 이벤트 처리 코드를 비교하세요.']}],
        'limitations':['localhost HTTP 검증 대역의 합성 응답이며 실제 AI 분석이 아닙니다.']}
    response={'status':'completed','model':'synthetic-model','output':[{'type':'message','role':'assistant','status':'completed','content':[{'type':'output_text','text':json.dumps(value,ensure_ascii=False)}]}]}
    requests=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            requests.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            raw=json.dumps(response,ensure_ascii=False).encode();self.send_response(200);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        analysis.test_endpoint=f'http://127.0.0.1:{server.server_port}/v1/responses';analysis.key.setText('SYNTHETIC-FAILURE-PROTOCOL-KEY')
        analysis.preview_request();sent=analysis.previewed;analysis.generate();until(lambda:analysis.process is None)
        assert analysis.result is not None,analysis.status_label.text()
        assert requests==[sent] and digest(sent)==analysis.result['request_sha256']
        assert analysis.result['provider']=='local-http-test'
        assert '수동 조사 메모 (local.notes)' in analysis.output.toPlainText()
        result=analysis.result
        for width,height in ((900,620),(1180,760)):
            analysis.resize(width,height);app.processEvents();assert render_window_to_png(analysis,output/f'failure-result-{width}.png')
        analysis.close();app.processEvents();assert not analysis.isVisible() and not analysis.key.text()
        dialog.close();app.processEvents();viewer.open_investigation();dialog=viewer.investigation_dialog
        assert '배를 입력' in dialog.local_notes.toPlainText();dialog.open_analysis();analysis=dialog.analysis_dialog
        assert analysis.result['analysis_id']==result['analysis_id'];analysis.close();dialog.close()
    finally:
        server.shutdown();server.server_close();thread.join()
        if analysis.process is not None:analysis.cancel();until(lambda:analysis.process is None)
        analysis.close();dialog.close()
    assert source.read_bytes()==original
    assert all(hashlib.sha256(path.read_bytes()).hexdigest()==value for path,value in original_hashes.items())
    proof={'original_pytest_status':'failed','original_server_stopped':True,'manual_input':'배','manual_observation':observed,
           'manual_restore':manual_state,'gui_worker_restore':worker_state,'request_equals_preview':True,'request_sha256':digest(sent),
           'analysis_id':result['analysis_id'],'saved_and_reopened':True,'original_unchanged':True,'provider':'local-http-test','real_api_called':False,
           'sizes':[[900,620],[1180,760]],'setup_guide_tabs':guide.tabs.count(),'setup_copy_checked':True,
           'frozen':bool(getattr(sys,'frozen',False))}
    (output/'failure-proof.json').write_text(json.dumps(proof,ensure_ascii=False,indent=2),encoding='utf-8')


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--data',required=True);parser.add_argument('--output',required=True);parser.add_argument('--adb',required=True);parser.add_argument('--resume',action='store_true');parser.add_argument('--errors-only',action='store_true');parser.add_argument('--replay-only',action='store_true');parser.add_argument('--help-only',action='store_true');parser.add_argument('--guided-only',action='store_true');parser.add_argument("--failure-only",action="store_true");args=parser.parse_args()
    configure_runtime();data=Path(args.data).resolve();data.mkdir(parents=True,exist_ok=True);output=Path(args.output).resolve();output.mkdir(parents=True,exist_ok=True)
    app=QApplication.instance() or QApplication([]);viewer=EvidenceViewerWindow(data,replay_headless=True);viewer.show()
    if args.failure_only:
        try:failure_acceptance(data,output,app,viewer)
        finally:viewer.close();app.processEvents()
        return
    if args.guided_only:
        try:guided_acceptance(data,output,app,viewer)
        finally:viewer.close();app.processEvents()
        return
    if args.help_only:
        try:
            assert getattr(sys,'frozen',False)
            before={p.relative_to(data).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in data.rglob('*') if p.is_file()}
            for width,height in ((900,620),(1180,760)):
                viewer.resize(width,height);app.processEvents();assert viewer.width()==width and viewer.height()==height
                assert render_window_to_png(viewer,output/f'help-main-{width}.png')
            viewer.help_button.click();app.processEvents();help_window=viewer.help_dialog
            assert help_window.isVisible() and help_window.current_topic=='start'
            for topic in help_window.ids:
                help_window.select_topic(topic);app.processEvents();assert help_window.body.toPlainText().strip()
            help_window.select_topic('terms');assert render_window_to_png(help_window,output/'frozen-help-terms.png');help_window.close()
            viewer.open_scenario_editor();editor=viewer.scenario_editor;app.processEvents();editor.resize(900,620)
            editor.url_edit.setText('https://example.test/unchanged');editor.title_edit.setText('작성 중인 설정')
            editor.help_button.click();app.processEvents()
            assert editor.help_dialog.current_topic=='scenario' and editor.help_dialog.parentWidget() is editor
            assert editor.windowModality()==Qt.WindowModality.ApplicationModal
            assert render_window_to_png(editor.help_dialog,output/'frozen-modal-help.png')
            editor.help_dialog.close();app.processEvents();assert editor.url_edit.text()=='https://example.test/unchanged' and editor.title_edit.text()=='작성 중인 설정' and editor.process is None
            editor.close();app.processEvents()
            after={p.relative_to(data).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in data.rglob('*') if p.is_file()};assert before==after
            (output/'summary.json').write_text(json.dumps({'topics':len(help_window.ids),'first_topics':help_window.ids[:3],'modal_topic':'scenario','input_preserved':True,'data_unchanged':True,'network_or_worker_started':False,'sizes':[[900,620],[1180,760]],'scope':'separate frozen probe with same product modules; no user data'},ensure_ascii=False,indent=2),encoding='utf-8')
        finally:viewer.close();app.processEvents()
        return
    rows=[];config={};requests=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def answer(self,value,content_type='application/json'):
            body=value if isinstance(value,bytes) else json.dumps(value).encode();self.send_response(200);self.send_header('Content-Type',content_type);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        def do_GET(self):
            requests.append(self.path)
            if self.path.startswith('/page'):
                self.answer(b'<!doctype html><html><body><h1>Frozen web test</h1><label>Value<input id="value" maxlength="128"></label><p id="result">ready</p></body></html>','text/html')
            elif self.path.startswith('/v4/spreadsheets/'):
                self.answer({'spreadsheetId':'abcdefghij123','sheets':[{'properties':{'sheetId':0,'title':'Synthetic TC'}}]})
            elif self.path.endswith('/jira'):
                self.answer({'result_id':'a'*32,'qa_revision':0,'qa':None,'jobs':[]})
            elif self.path.startswith('/projects/probe/results'):
                self.answer({'items':[],'next_cursor':None})
            else:self.send_error(404)
        def do_POST(self):
            body=json.loads(self.rfile.read(int(self.headers['Content-Length'])));requests.append(self.path)
            text=json.dumps({'scenario':config,'review_notes':['Verify the synthetic expectation.']}) if body.get('text',{}).get('format',{}).get('type')=='json_schema' else 'OK'
            self.answer({'status':'completed','model':'probe-model','output':[{'type':'message','role':'assistant','status':'completed','content':[{'type':'output_text','text':text}]}]})
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();base=f'http://127.0.0.1:{server.server_port}'
    def until(predicate,seconds=45):
        started=time.monotonic()
        while not predicate():
            app.processEvents();time.sleep(.02)
            if time.monotonic()-started>seconds:raise AssertionError('Frozen Qt timeout')
        app.processEvents()
    def shot(name,window=viewer):
        assert render_window_to_png(window,output/(name+'.png'))
    def record(name,**values):
        rows.append({'name':name,'status':'passed',**values});(output/'progress.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf-8');print(name,flush=True)
    def rpc(module,request):
        process=QProcess(viewer);configure_worker(process,module);process.start();until(lambda:process.state()!=QProcess.ProcessState.Starting)
        assert process.state()==QProcess.ProcessState.Running
        process.write(json.dumps(request).encode()+b'\n');process.closeWriteChannel();until(lambda:process.state()==QProcess.ProcessState.NotRunning,60)
        raw=bytes(process.readAllStandardOutput());code=process.exitCode();process.deleteLater()
        assert code==0,(module,code,raw)
        return json.loads(raw.splitlines()[-1])
    try:
        assert getattr(sys,'frozen',False);assert 'PYTHONPATH' not in os.environ;shot('fresh-qt-probe')
        if args.replay_only:
            assert viewer.selected_archive is not None
            archive=viewer.selected_archive;har=archive/'resources.har';held=archive/'resources.har.held'
            assert har.exists() and not held.exists()
            original=hashlib.sha256(har.read_bytes()).hexdigest();har.rename(held)
            try:
                viewer.start_replay(headless=True);until(lambda:viewer.replay_process is None)
                message=viewer.replay_label.text()
                assert 'FileNotFoundError' in message and 'resources.har' in message,message
                assert viewer.run_list.isEnabled();shot('frozen-replay-failure');record('frozen_worker_failure_reason',message=message)
            finally:held.rename(har)
            assert hashlib.sha256(har.read_bytes()).hexdigest()==original
            (output/'summary.json').write_text(json.dumps({'rows':rows,'synthetic_archive_restored':True},ensure_ascii=False,indent=2),encoding='utf-8')
            return
        config.update(version=1,id='FROZEN-WEB',title='설치본 웹 기록',url=base+'/page',
                      steps=[{'action':'fill','locator':'css','target':'#value','value':'probe'}],
                      checks=[{'kind':'input_length','locator':'css','target':'#value','expected':5}])
        if args.errors_only:
            worker=Path(sys.executable).parent/'QA Evidence Worker.exe';disabled=worker.with_suffix('.disabled')
            assert not disabled.exists()
            viewer.open_scenario_editor();editor=viewer.scenario_editor;editor.set_config(config)
            worker.rename(disabled)
            try:
                editor.start_run();until(lambda:editor.process is None)
                assert editor.run_button.isEnabled() and '실패' in editor.status_label.text()
                shot('missing-worker',editor);record('missing_worker_explicit',message=editor.status_label.text())
            finally:disabled.rename(worker)
            cache=os.environ['PLAYWRIGHT_BROWSERS_PATH'];os.environ['PLAYWRIGHT_BROWSERS_PATH']=str(data.parent/'browser-not-installed')
            try:
                editor.start_run();until(lambda:editor.process is None)
                failure=json.loads(Path(editor.last_evidence).read_bytes())
                assert failure['result']['business']['status']=='preparation_failed'
                assert 'Executable' in failure['result']['business']['message']
                shot('missing-browser',editor);record('missing_browser_explicit',message=editor.status_label.text())
            finally:os.environ['PLAYWRIGHT_BROWSERS_PATH']=cache
            editor.close();viewer.load_root(data.parent/('x'*150))
            assert '140' in viewer.message_label.text();shot('long-data-path');record('long_path_explicit',message=viewer.message_label.text())
            viewer.load_root(data);assert viewer.android_button.isEnabled()
            (output/'summary.json').write_text(json.dumps({'rows':rows},ensure_ascii=False,indent=2),encoding='utf-8')
            return
        if not args.resume:
            viewer.open_scenario_editor();editor=viewer.scenario_editor;editor.set_config(config);saved=data/'frozen-web.json';editor.save_to(saved);editor.start_run();until(lambda:editor.process is None)
            assert editor.last_evidence is not None,editor.status_label.text();web=Path(editor.last_evidence);payload=json.loads(web.read_bytes());assert payload['result']['business']['status']=='passed',payload
            editor.close();viewer._scenario_recorded(web);shot('web-recorded');record('web_qt_worker',path=str(web),actual=payload['checks'][0]['actual'])
            viewer.start_replay(headless=True);until(lambda:'수동 테스트 가능' in viewer.replay_label.text() or viewer.replay_process is None)
            assert viewer.replay_process is not None,viewer.replay_label.text();shot('web-replay');viewer.stop_replay();until(lambda:viewer.replay_process is None);record('web_replay_qt')
            viewer.open_manual_recorder();manual=viewer.manual_dialog;manual.url_edit.setText(base+'/page');manual.title_edit.setText('Frozen manual snapshot');manual.start_recording();until(lambda:manual.save_button.isEnabled() or manual.process is None)
            assert manual.process is not None,manual.status_label.text();manual.save_recording();until(lambda:manual.process is None);assert manual.last_evidence is not None
            manual.close();record('manual_snapshot_qt',path=str(manual.last_evidence),scope='page snapshot; no human interaction claimed')
            viewer._scenario_recorded(web);viewer.open_investigation();investigation=viewer.investigation_dialog
            store=investigation.store;doc=store.load(payload['execution']['id']);doc['notes']='Frozen QA persistence';store.save(doc);investigation.close();record('qa_persistence')
            viewer.open_suite();suite=viewer.suite_dialog;suite.name_edit.setText('Frozen suite');suite.set_paths([saved]);suite.start_run();until(lambda:suite.process is None,120)
            assert suite.current_state['summary']['passed']==1,suite.status_label.text();shot('suite',suite);suite.close();record('suite_nested_frozen_worker')
            viewer._scenario_recorded(web);viewer.open_timeline();timeline=viewer.timeline_dialog;timeline.start_experiment();until(lambda:timeline.normal_ready or timeline.process is None)
            assert timeline.normal_ready,timeline.status_label.text();timeline.stop_experiment();until(lambda:timeline.process is None);timeline.close();record('timeline_experiment_worker')
        else:
            rows.extend(json.loads((output/'progress.json').read_text(encoding='utf-8')))
            web=Path(rows[0]['path']);payload=json.loads(web.read_bytes());viewer._scenario_recorded(web)
            viewer.open_investigation();store=viewer.investigation_dialog.store;viewer.investigation_dialog.close()
        if not any(row['name']=='server_empty_list_http_worker' for row in rows):
            csv=data/'frozen-tc.csv';csv.write_text('ID,Title,Steps,Expected\nTC-1,Example,Open page,Visible\n',encoding='utf-8')
            request={'action':'preview','root':str(data),'project':'probe','path':str(csv),'encoding':'utf-8-sig','header_row':1,'source_id':'frozen-csv','mapping':{}}
            reply=rpc('signup031.tc_import_worker',request);batch=reply['result'];assert batch['total_rows']==1
            request.update(action='import',sha256=batch['sha256'],headers=batch['headers'],mapping={'original_id':0,'title':1,'steps':2,'expected':3})
            registered=rpc('signup031.tc_import_worker',request);assert registered['result']['counts']['registered']==1,registered;record('csv_import_worker')
            reply=rpc('signup031.sheets_worker',{'action':'metadata','link':'https://docs.google.com/spreadsheets/d/abcdefghij123/edit#gid=0','api_key':'synthetic-sheet-key','test_endpoint':base})
            assert reply['result']['tabs'][0]['title']=='Synthetic TC';record('sheets_http_worker')
            reply=rpc('signup031.ingestion_worker',{'url':base,'project':'probe','token':'synthetic-token','root':str(data.parent/'server-client')})
            assert reply['status']=='complete' and reply['complete']==0;record('server_empty_list_http_worker')
        remote=data.parent/'remote-client-valid-id';remote_run=remote/'run';shutil.copytree(web.parent,remote_run)
        (remote_run/'remote-source.json').write_text(json.dumps({'server_origin':base,'project_id':'probe','result_id':'a'*32}))
        reply=rpc('signup031.jira_sync_worker',{'root':str(remote),'execution_id':payload['execution']['id'],'token':'synthetic-token','action':'refresh'})
        assert reply['state']['result_id']=='a'*32;record('jira_status_http_worker')
        viewer.open_ai_assistant();ai=viewer.ai_dialog;ai.test_endpoint=base+'/v1/responses';ai.model.setText('probe-model');ai.key.setText('synthetic-ai-key');ai.url.setText(config['url']);ai.goal.setPlainText('Verify the input');ai.preview_request();ai.generate();until(lambda:ai.process is None)
        assert ai.proposal is not None,ai.status_label.text();shot('ai-proposal',ai);ai.close();record('ai_http_qt_worker')
        viewer.open_evaluation();evaluation=viewer.evaluation_dialog;evaluation.test_endpoint=base+'/v1/responses';evaluation.key.setText('synthetic-ai-key')
        dataset={'format_version':1,'id':'FROZEN-EVAL','version':'1','title':'Frozen evaluation','requested_model':'probe-model','prompt':'Reply OK','settings':{'max_output_tokens':100},'normalization':'literal','cases':[{'id':'one','input':'hello','criteria':[{'id':'exact','kind':'exact','expected':'OK'}],'rubric':''}]}
        evaluation.set_dataset(dataset);evaluation.save_version();evaluation.preview_requests();evaluation.text_dialog.close();evaluation.start_run();until(lambda:evaluation.process is None)
        state=evaluation.store.load_run(evaluation.run_id);assert state['status']=='completed'
        item=state['items'][0];assert item['generation']=='completed' and item['response']=='OK' and item['auto_status']=='passed'
        shot('evaluation',evaluation);evaluation.close();record('evaluation_http_qt_worker',generation=item['generation'],response=item['response'],auto_status=item['auto_status'])
        reply=rpc('signup031.android_worker',{'mode':'devices','adb':args.adb});assert isinstance(reply['devices'],list);record('android_device_list_worker',scope='read-only ADB list; no emulator boot')
        viewer.close();app.processEvents();viewer=EvidenceViewerWindow(data,replay_headless=True);viewer.show();assert len(viewer.records)>=2
        assert store.load(payload['execution']['id'])['notes']=='Frozen QA persistence';shot('reopened-probe');record('reopened_qt_stores')
        result={'scope':'acceptance-only frozen Qt probe using installed worker and shared product _internal; not human UI/clean VM','rows':rows,'requests':requests,'python_executable':sys.executable,'data':str(data),'web_evidence':str(web)}
        (output/'summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    except Exception:
        (output/'failure.txt').write_text(traceback.format_exc(),encoding='utf-8');raise
    finally:
        viewer.close();app.processEvents();server.shutdown();server.server_close();thread.join(timeout=5)


if __name__=='__main__':main()
