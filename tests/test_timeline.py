"""Observed timeline capture and isolated offline response experiments."""
import hashlib
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
import socket
from threading import Thread
import time

import pytest

TOKEN='SYNTHETIC-TIMELINE-TOKEN-12345'
PASSWORD='SYNTHETIC-PRIVATE-INPUT-12345'


class TimelineSite:
    def __init__(self):
        self.hits=[];owner=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*_):pass
            def do_GET(self):
                owner.hits.append(self.path)
                if self.path=='/disconnect':self.connection.shutdown(socket.SHUT_RDWR);self.connection.close();return
                if self.path=='/redirect':self.send_response(302);self.send_header('Location','/api');self.end_headers();return
                status=500 if self.path=='/http500' else 200
                if self.path=='/':
                    body='''<input id="private" data-sensitive><input id="query"><button id="go">Fetch</button><button id="batch">Batch</button><p id="result">idle</p><script>
document.querySelector('#go').onclick=async()=>{let p=document.querySelector('#result');p.textContent='loading';try{let r=await fetch('/api');p.textContent=r.ok?await r.text():'HTTP '+r.status;}catch(e){p.textContent='transport error';}p.dataset.done='true';console.error('after fetch');setTimeout(()=>{throw new Error('controlled page error')},0);};
document.querySelector('#batch').onclick=()=>{setTimeout(()=>fetch('/api'),50);setTimeout(()=>fetch('/api'),250);};
</script>'''
                else:body='original response'
                raw=body.encode();self.send_response(status);self.send_header('Content-Type','text/html' if self.path=='/' else 'text/plain')
                self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
        self.handler=Handler;self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler);self.port=self.server.server_port
        self.url=f'http://127.0.0.1:{self.port}';self.thread=Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
    def stop(self):self.server.shutdown();self.server.server_close();self.thread.join()
    def restart(self):
        self.server=ThreadingHTTPServer(('127.0.0.1',self.port),self.handler);self.thread=Thread(target=self.server.serve_forever,daemon=True);self.thread.start()


@pytest.fixture
def site():
    server=TimelineSite()
    try:yield server
    finally:
        if server.thread.is_alive():server.stop()


def timeline_api():
    assert importlib.util.find_spec('signup031.timeline') is not None,'Timeline is not implemented'
    from signup031 import timeline
    return timeline


def config(base):
    return {'version':1,'id':'ASYNC-13','title':'비동기 요청','url':base+'/',
        'steps':[{'action':'click','locator':'css','target':'#go','value':''},{'action':'wait','locator':'css','target':'#result[data-done]','value':''}],
        'checks':[{'kind':'text','locator':'css','target':'#result','expected':'original response'}]}


def test_manual_observed_sequence_http_failures_and_secrets(site,tmp_path):
    m=timeline_api();from signup031.manual_recording import ManualRecorder
    with ManualRecorder(site.url+'/','timeline',tmp_path,headless=True) as recorder:
        recorder.page.locator('#private').fill(PASSWORD)
        recorder.page.evaluate('(token)=>fetch("/api?token="+token,{headers:{Authorization:"Bearer "+token}})',TOKEN)
        recorder.page.evaluate('(values)=>console.error(values.join(" "))',[TOKEN,PASSWORD])
        recorder.page.locator('#go').click();recorder.page.wait_for_selector('#result[data-done]')
        recorder.page.evaluate('async()=>{await fetch("/http500");await fetch("/redirect");await fetch("/disconnect").catch(()=>{});}');recorder.page.wait_for_timeout(100)
        path=recorder.save()
    timeline=m.load_timeline(path);events=timeline['events']
    assert [e['received_ms'] for e in events]==sorted(e['received_ms'] for e in events)
    assert any(e['kind']=='action' for e in events) and any(e['kind']=='pageerror' for e in events)
    assert any(e['kind']=='response' and e['details']['status']==500 for e in events)
    assert any(e['kind']=='request_failed' for e in events) and any(e['kind']=='redirect' for e in events)
    starts=[e for e in events if e['kind']=='request'];assert len({e['request_id'] for e in starts})==len(starts)
    raw=(path.parent/'timeline.json').read_bytes();assert TOKEN.encode() not in raw and PASSWORD.encode() not in raw
    assert all(e['occurred_ms'] is None for e in events)


def test_timeline_limits_and_original_absence_are_explicit(tmp_path):
    m=timeline_api();collector=m.Timeline('bounded','test',max_events=3,sensitive_values=[TOKEN])
    for i in range(10):collector.add('console',{'text':TOKEN+'x'*10000})
    declaration=collector.save(tmp_path);assert declaration['size']<=m.MAX_BYTES
    data=json.loads((tmp_path/'timeline.json').read_bytes());assert len(data['events'])==3 and data['dropped']==7
    assert TOKEN not in json.dumps(data) and all(len(e['details']['text'])<=m.MAX_STRING for e in data['events'])
    original=tmp_path/'evidence.json';original.write_text(json.dumps({'execution':{'id':'old'}}))
    assert m.load_timeline(original) is None


def test_configured_runner_creates_bound_timeline(site,tmp_path):
    m=timeline_api();from signup031.web_runner import run_scenario
    path=run_scenario(config(site.url),tmp_path)
    timeline=m.load_timeline(path)
    assert timeline is not None and timeline['source']=='configured-playwright'
    kinds=[e['kind'] for e in timeline['events']]
    assert 'action' in kinds and 'request' in kinds and 'console' in kinds
    assert timeline['execution_id']==json.loads(path.read_bytes())['execution']['id']


def test_selenium_delayed_batch_preserves_source_clock_without_fake_occurrence(site,tmp_path):
    m=timeline_api();from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from signup031.selenium_recording import SeleniumRecorder,recording_options
    from types import SimpleNamespace
    driver=webdriver.Chrome(options=recording_options(headless=True));recorder=SeleniumRecorder(driver,tmp_path,'controlled::test_fetch')
    try:
        driver.get(site.url+'/');driver.find_element(By.ID,'go').click()
        driver.find_element(By.ID,'batch').click()
        # Delay the next WebDriver command: events remain in browser/CDP buffers.
        time.sleep(.4)
        WebDriverWait(driver,5).until(lambda d:d.find_element(By.ID,'result').text=='original response')
        recorder.capture(SimpleNamespace(outcome='failed',when='call'))
        path=recorder.finish([{'when':'call','outcome':'failed','message':'controlled failure'}],1)
    finally:driver.quit()
    timeline=m.load_timeline(path)
    assert timeline is not None
    events=timeline['events'];cdp=[e for e in events if (e['source_clock'] or '').startswith('cdp:')]
    assert cdp and all(e['source_unit']=='s' and e['occurred_ms'] is None for e in cdp)
    assert any((e['source_clock'] or '').startswith('document-performance:') for e in events)
    assert timeline['availability']['console'] in ('collected','not_collected')
    batch=[e for e in cdp if e['kind']=='request' and e['details']['url'].endswith('/api')][-2:]
    assert len(batch)==2 and batch[1]['source_time']-batch[0]['source_time']>.1
    assert batch[1]['received_ms']-batch[0]['received_ms']<100
    from test_ingestion import ServerProcess,TOKEN as INGEST_TOKEN
    from signup031.ingestion_client import IngestionClient
    server=ServerProcess(tmp_path/'server').start()
    try:
        client=IngestionClient(server.url,'demo',INGEST_TOKEN);client.send_folder(path.parent,provider='selenium',run='timeline-transfer')
        received=client.download(tmp_path/'downloaded');assert len(received['complete'])==1
        assert m.load_timeline(Path(received['complete'][0]))==timeline
    finally:server.stop()


def test_offline_selected_response_error_delay_and_normal_reset(site,tmp_path):
    timeline_api();from signup031.web_runner import run_scenario
    from signup031.replay import ReplaySession
    path=run_scenario(config(site.url),tmp_path/'source');archive=path.parent/'archive'
    before={str(p.relative_to(path.parent)):p.read_bytes() for p in path.parent.rglob('*') if p.is_file()}
    site.stop();hits=len(site.hits)
    assert importlib.util.find_spec('signup031.network_experiment') is not None,'Offline experiment is not implemented'
    from signup031.network_experiment import NetworkExperiment
    with ReplaySession(archive,headless=True) as replay:
        with pytest.raises(ValueError):NetworkExperiment(replay)
        assert replay.restore()['status']=='ready';experiment=NetworkExperiment(replay)
        entry=next(r for r in experiment.entries if r['display_url'].endswith('/api'))
        experiment.arm(entry['index'],'http500',0);replay.page.locator('#go').click();replay.page.wait_for_function("document.querySelector('#result').textContent==='HTTP 500'")
        assert experiment.status()['applied']==1
    site.restart()
    with ReplaySession(archive,headless=True) as replay:
        assert replay.restore()['status']=='ready';experiment=NetworkExperiment(replay)
        entry=next(r for r in experiment.entries if r['display_url'].endswith('/api'))
        experiment.arm(entry['index'],'delay',300);started=time.monotonic();replay.page.locator('#go').click()
        assert replay.page.inner_text('#result')=='loading'
        while experiment.status()['applied']==0:
            experiment.tick();replay.page.wait_for_timeout(20)
        assert time.monotonic()-started>=.3
        replay.page.wait_for_function("document.querySelector('#result').textContent==='original response'")
        replay.page.evaluate("fetch('/unrecorded').catch(()=>{});new WebSocket('ws://127.0.0.1:9/blocked')");replay.page.wait_for_timeout(100)
        assert replay.status()['blocked_count']>=2
    with ReplaySession(archive,headless=True) as replay:
        assert replay.restore()['status']=='ready' and replay.page.inner_text('#result')=='original response'
    assert len(site.hits)==hits and before=={str(p.relative_to(path.parent)):p.read_bytes() for p in path.parent.rglob('*') if p.is_file()}


def test_untrusted_js_clock_is_dropped_and_origins_keep_ports(tmp_path):
    m=timeline_api();collector=m.Timeline('bad-js','manual-playwright')
    assert collector.url('http://example.test:8765/path?token=synthetic')=='http://example.test:8765/path'
    assert collector.url('http://[::1]:8765/path#fragment')=='http://[::1]:8765/path'
    collector.js_event({'kind':'action','document':'doc','time':'invalid-clock','details':{'action':'click'},'secrets':[]})
    collector.js_event({'kind':'action','document':'doc','time':float('nan'),'details':{'action':'click'},'secrets':[]})
    declaration=collector.save(tmp_path);data=json.loads((tmp_path/'timeline.json').read_bytes())
    assert data['events']==[] and data['dropped']==2 and data['limits']


def test_timeline_collection_error_does_not_erase_core_verdict(site,tmp_path,monkeypatch):
    m=timeline_api();from signup031.web_runner import run_scenario
    monkeypatch.setattr(m.Timeline,'save',lambda *_:(_ for _ in ()).throw(ValueError('controlled timeline failure')))
    path=run_scenario(config(site.url),tmp_path);payload=json.loads(path.read_bytes())
    assert payload['result']['business']['status']=='passed'
    assert payload['timeline_capture']['status']=='collection_failed' and 'timeline' not in payload


def test_timeline_transport_is_exact_named_versioned_attachment(site,tmp_path):
    m=timeline_api();from signup031.web_runner import run_scenario
    from signup031.ingestion_contract import build_bundle,relative_path,validate_envelope
    path=run_scenario(config(site.url),tmp_path/'source');envelope,contents=build_bundle(path.parent,'demo','local','timeline-run')
    assert 'timeline.json' in contents
    original=contents['timeline.json'];payload=json.loads(path.read_bytes())
    assert m.validate_bytes(original,payload['execution']['id'],payload['timeline'])['events']
    with pytest.raises(ValueError):relative_path('arbitrary.json')
    for entry in envelope['files']:
        if entry['path']=='timeline.json':entry['sha256']='0'*64
    with pytest.raises(ValueError):validate_envelope(envelope)


@pytest.mark.parametrize('interact',[True,False])
def test_sensitive_field_overflow_masks_unlisted_values(site,tmp_path,interact):
    m=timeline_api();from signup031.manual_recording import ManualRecorder
    values=[hashlib.sha256(('synthetic-private-'+str(i)).encode()).hexdigest() for i in range(1001)];last=values[-1]
    with ManualRecorder(site.url+'/','overflow',tmp_path,headless=True) as recorder:
        recorder.page.evaluate('''values=>{for(const value of values){let e=document.createElement('input');e.dataset.sensitive='';e.value=value;document.body.append(e);}}''',values)
        if interact:recorder.page.locator('#go').click();recorder.page.wait_for_selector('#result[data-done]')
        recorder.page.evaluate('(s)=>console.error(s)',last)
        path=recorder.save()
    raw=(path.parent/'timeline.json').read_bytes();assert last.encode() not in raw
    assert 'sensitive value limit' in m.load_timeline(path)['limits']


@pytest.mark.parametrize('provider',['manual','selenium'])
def test_iframe_sensitive_console_is_excluded_from_new_timeline(site,tmp_path,provider):
    m=timeline_api();secret='SYNTHETIC-IFRAME-PRIVATE-29014'
    script='''const f=document.createElement('iframe');document.body.append(f);f.contentDocument.body.innerHTML='<input type="password">';f.contentDocument.querySelector('input').value=SECRET;f.contentWindow.console.error(SECRET);'''
    if provider=='manual':
        from signup031.manual_recording import ManualRecorder
        with ManualRecorder(site.url+'/','frame',tmp_path,headless=True) as recorder:
            recorder.page.evaluate('(SECRET)=>{'+script+'}',secret);path=recorder.save()
    else:
        from selenium import webdriver
        from signup031.selenium_recording import recording_options,SeleniumRecorder
        from types import SimpleNamespace
        driver=webdriver.Chrome(options=recording_options(headless=True));recorder=SeleniumRecorder(driver,tmp_path,'frame::test')
        try:
            driver.get(site.url+'/');driver.execute_script('const SECRET=arguments[0];'+script,secret)
            recorder.capture(SimpleNamespace(outcome='failed',when='call'));path=recorder.finish([{'when':'call','outcome':'failed'}],1)
        finally:driver.quit()
    assert secret.encode() not in (path.parent/'timeline.json').read_bytes()
    assert 'iframe console attribution unavailable; text masked' in m.load_timeline(path)['limits']


def test_qt_timeline_filters_and_pending_experiment_cancel(site,tmp_path):
    import os
    os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QTimer
    from signup031.web_runner import run_scenario
    from signup031.viewer import EvidenceViewerWindow
    from signup031.viewer_model import _load_one
    from signup031.timeline_dialog import TimelineDialog
    from test_scenario_editor import until
    path=run_scenario(config(site.url),tmp_path/'results');app=QApplication.instance() or QApplication([])
    window=EvidenceViewerWindow(tmp_path/'results',replay_headless=True);window.show()
    assert window.records==[] and not window.timeline_button.isVisible()
    record=_load_one(path,tmp_path/'results')
    dialog=TimelineDialog(window,record);dialog.show();ticks=[];timer=QTimer();timer.timeout.connect(lambda:ticks.append(1));timer.start(20)
    try:
        count=dialog.table.rowCount();assert count>0
        dialog.kind_filter.setCurrentIndex(dialog.kind_filter.findData('response'));assert 0<dialog.table.rowCount()<count
        dialog.table.selectRow(0);assert 'status' in dialog.details.toPlainText()
        dialog.end_ms.setValue(0);assert dialog.table.rowCount()==0
        dialog.end_ms.setValue(1e9);dialog.kind_filter.setCurrentIndex(0)
        site.stop();hits=len(site.hits);dialog.start_experiment();until(lambda:dialog.normal_ready,20)
        index=next(i for i in range(dialog.responses.count()) if dialog.responses.itemData(i)['display_url'].endswith('/api'))
        dialog.responses.setCurrentIndex(index);dialog.mode.setCurrentIndex(dialog.mode.findData('delay'));dialog.delay.setValue(3000)
        dialog.arm();until(lambda:dialog.last_status and dialog.last_status['rule'] is not None)
        dialog.repeat_click();until(lambda:dialog.last_status['pending']==1)
        started=time.monotonic();dialog.stop_experiment();until(lambda:dialog.process is None,8)
        assert time.monotonic()-started<5 and len(ticks)>5 and len(site.hits)==hits
    finally:
        timer.stop()
        if dialog.process is not None:dialog.stop_experiment();until(lambda:dialog.process is None,8)
        dialog.close();window.close();app.processEvents()


def test_experiment_rejects_ambiguous_or_missing_responses_and_keeps_headers(tmp_path):
    from types import SimpleNamespace
    from copy import deepcopy
    from signup031.network_experiment import NetworkExperiment
    entry={'request':{'url':'http://example.test/api','method':'GET'},'response':{'status':200,
        'headers':[{'name':'Content-Type','value':'text/plain'},{'name':'Set-Cookie','value':'synthetic=value'},{'name':'X-Meaning','value':'original'}],
        'content':{'text':'original','size':8}}}
    path=tmp_path/'resources.har';routes=[]
    def session(entries):
        path.write_text(json.dumps({'log':{'entries':entries}}));return SimpleNamespace(restored=True,status=lambda:{'status':'ready'},har_path=path,context=SimpleNamespace(route=lambda *args:routes.append(args)))
    experiment=NetworkExperiment(session([entry]));experiment.arm(0,'delay',10)
    assert experiment.headers['Set-Cookie']=='synthetic=value' and experiment.headers['X-Meaning']=='original'
    private=deepcopy(entry);private['request']['url']='http://example.test/synthetic-header-credential'
    later=deepcopy(entry);later['request']['headers']=[{'name':'Authorization','value':'Bearer synthetic-header-credential'}]
    experiment=NetworkExperiment(session([private,later]));experiment.arm(0,'delay',10)
    assert 'synthetic-header-credential' not in json.dumps(experiment.entries)
    assert 'synthetic-header-credential' not in json.dumps(experiment.status())
    assert experiment.target['url']==private['request']['url']
    changed=deepcopy(entry);changed['response']['content']['text']='different'
    experiment=NetworkExperiment(session([entry,changed]))
    with pytest.raises(ValueError,match='모호'):experiment.arm(0,'http500',0)
    for mutation in ('missing','invalid_status'):
        broken=deepcopy(entry)
        if mutation=='missing':broken['response']['content']={'size':8}
        else:broken['response']['status']=0
        experiment=NetworkExperiment(session([broken]))
        with pytest.raises(ValueError):experiment.arm(0,'delay',10)
