"""Controlled Google HTTP contract; never claims real Google account acceptance."""
import hashlib
import importlib.util
import json
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from threading import Thread
import time
from urllib.parse import parse_qs,urlsplit
from urllib.request import urlopen

import pytest

DOC='controlled-document-123456789'
LINK=f'https://docs.google.com/spreadsheets/d/{DOC}/edit?gid=7#gid=7'
KEY='SYNTHETIC-SHEETS-KEY-18934'
TOKEN='SYNTHETIC-SHEETS-TOKEN-74218'


def api():
    assert importlib.util.find_spec('signup031.sheets_import') is not None,'Sheets import is not implemented'
    from signup031.sheets_import import parse_link,SheetsClient
    return parse_link,SheetsClient


class GoogleDouble:
    def __init__(self):
        self.mode='ok';self.calls=[];self.tab_title='수동 TC';self.rows=[['ID','제목','절차','기대 결과'],['001','로그인','첫째\n둘째','완료'],['002','=literal()']]
        self.token_forms=[];self.grid_override=None
        owner=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*_):pass
            def reply(self,status,data,content_type='application/json'):
                raw=json.dumps(data,ensure_ascii=False).encode() if not isinstance(data,bytes) else data
                self.send_response(status);self.send_header('Content-Type',content_type);self.send_header('Content-Length',str(len(raw)));self.end_headers()
                try:self.wfile.write(raw)
                except OSError:pass
            def do_GET(self):
                owner.calls.append((self.path,dict(self.headers)))
                if owner.mode.isdigit():return self.reply(int(owner.mode),{'error':TOKEN})
                if owner.mode=='html':return self.reply(200,b'<html>Google login</html>','text/html')
                if owner.mode=='broken':return self.reply(200,b'{broken')
                if owner.mode=='redirect':
                    self.send_response(302);self.send_header('Location',owner.url+'/forbidden');self.end_headers();return
                if owner.mode=='delay':time.sleep(3)
                if owner.mode=='large':return self.reply(200,b'x'*(10*1024*1024+1))
                if '/values/' in self.path:
                    return self.reply(200,{'range':"'"+owner.tab_title+"'!A1:D3",'majorDimension':'ROWS','values':[] if owner.mode=='empty' else owner.rows})
                query=parse_qs(urlsplit(self.path).query)
                if 'ranges' in query:
                    gid=9 if query['ranges']==["'Other'"] else 7
                    grid={'spreadsheetId':DOC,'sheets':[{'properties':{'sheetId':gid,'title':'Other' if gid==9 else owner.tab_title},
                        'data':[{'rowData':[{'values':[{'formattedValue':cell} if cell else {} for cell in row]} for row in ([] if owner.mode=='empty' else owner.rows)]}]}]}
                    return self.reply(200,owner.grid_override or grid)
                self.reply(200,{'spreadsheetId':DOC,'sheets':[{'properties':{'sheetId':7,'title':owner.tab_title}}, {'properties':{'sheetId':9,'title':'Other'}}]})
            def do_POST(self):
                form=parse_qs(self.rfile.read(int(self.headers.get('Content-Length',0))).decode())
                owner.token_forms.append(form)
                self.reply(200,{'access_token':TOKEN,'token_type':'Bearer','expires_in':3600,'scope':'https://www.googleapis.com/auth/spreadsheets.readonly'})
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.url='http://127.0.0.1:'+str(self.server.server_port)
        self.thread=Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
    def close(self):self.server.shutdown();self.server.server_close();self.thread.join()


@pytest.fixture
def google():
    server=GoogleDouble()
    try:yield server
    finally:server.close()


def test_link_gids_conflict_invalid_host_and_missing_tab(google):
    parse,Client=api()
    assert parse('  '+LINK+'  ')['gid']==7
    assert parse(LINK.split('?')[0]+'#gid=9')['gid']==9
    conflict=parse(LINK.replace('#gid=7','#gid=9'))
    assert conflict['gid'] is None and conflict['gid_choices']==[7,9]
    for bad in [LINK.replace('docs.google.com','evil.example'),LINK.replace('https:','http:'),LINK.replace('/d/','/d/user:secret@'),LINK+'&access_token=secret']:
        with pytest.raises(ValueError):parse(bad)
    client=Client(api_key=KEY,test_endpoint=google.url)
    with pytest.raises(ValueError,match='탭'):client.read_batch(DOC,99)
    assert all(KEY not in path and TOKEN not in path for path,_ in google.calls)


def test_formatted_rows_tab_rename_duplicates_and_distinct_tabs(google,tmp_path):
    _,Client=api();from signup031.tc_import import TCStore
    client=Client(api_key=KEY,test_endpoint=google.url);store=TCStore(tmp_path/'results')
    mapping={'original_id':0,'title':1,'steps':2,'expected':3}
    batch=client.read_batch(DOC,7)
    assert batch['rows'][0]['cells'][0]=='001' and batch['rows'][0]['cells'][2]=='첫째\n둘째'
    assert batch['rows'][1]['cells']==['002','=literal()','','']
    result=store.register('demo',batch['source_id'],batch,mapping)
    assert result['counts']['registered']==2 and result['counts']['review_needed']==1
    google.tab_title='Renamed'
    renamed=client.read_batch(DOC,7)
    assert renamed['source_id']==batch['source_id']
    assert store.register('demo',renamed['source_id'],renamed,mapping)['counts']['duplicate']==2
    other=client.read_batch(DOC,9)
    assert store.register('demo',other['source_id'],other,mapping)['counts']['registered']==2
    google.rows[1][1]='Changed'
    changed=client.read_batch(DOC,7)
    assert store.register('demo',changed['source_id'],changed,mapping)['counts']['candidate']==1
    value_calls=[parse_qs(urlsplit(p).query) for p,_ in google.calls if 'ranges=' in p]
    assert value_calls and all('formattedValue' in q['fields'][0] and 'sheetId' in q['fields'][0] for q in value_calls)
    for f in (tmp_path/'results').rglob('*'):
        if f.is_file():assert TOKEN.encode() not in f.read_bytes() and KEY.encode() not in f.read_bytes()


@pytest.mark.parametrize('mode',['401','403','404','429','500','html','broken','redirect','large'])
def test_http_failures_are_not_empty_sheet_success(google,mode):
    _,Client=api();client=Client(access_token=TOKEN,test_endpoint=google.url)
    google.mode=mode
    with pytest.raises(ValueError) as failure:client.read_batch(DOC,7)
    assert TOKEN not in str(failure.value) and KEY not in str(failure.value)
    assert all('/forbidden' not in p for p,_ in google.calls)


def test_empty_sheet_and_request_timeout_are_distinct(google):
    _,Client=api();client=Client(api_key=KEY,test_endpoint=google.url,timeout=.2)
    google.mode='empty'
    with pytest.raises(ValueError,match='빈 시트'):client.read_batch(DOC,7)
    google.mode='delay';started=time.monotonic()
    with pytest.raises(ValueError,match='연결|시간'):client.read_batch(DOC,7)
    assert time.monotonic()-started<1


def test_desktop_oauth_pkce_state_and_token_exchange_on_loopback(google):
    api();from signup031.sheets_import import DesktopOAuth
    config={'installed':{'client_id':'123-test.apps.googleusercontent.com','client_secret':'synthetic-client-secret',
        'auth_uri':'https://accounts.google.com/o/oauth2/auth','token_uri':'https://oauth2.googleapis.com/token','redirect_uris':['http://localhost']}}
    flow=DesktopOAuth(config,test_token_endpoint=google.url+'/token')
    with flow:
        auth=parse_qs(urlsplit(flow.authorization_url).query)
        assert auth['code_challenge_method']==['S256']
        assert auth['scope']==['https://www.googleapis.com/auth/spreadsheets.readonly']
        callback=auth['redirect_uri'][0]
        thread=Thread(target=lambda:urlopen(callback+'?code=controlled-code&state='+auth['state'][0]).read())
        thread.start();token=flow.wait(timeout=2);thread.join()
        assert token['access_token']==TOKEN
        form=google.token_forms[0]
        import base64
        assert base64.urlsafe_b64encode(hashlib.sha256(form['code_verifier'][0].encode()).digest()).decode().rstrip('=')==auth['code_challenge'][0]
    with DesktopOAuth(config,test_token_endpoint=google.url+'/token') as flow:
        def wrong():
            try:urlopen(parse_qs(urlsplit(flow.authorization_url).query)['redirect_uri'][0]+'?code=x&state=wrong').read()
            except OSError:pass
        thread=Thread(target=wrong);thread.start()
        with pytest.raises(ValueError,match='state'):flow.wait(timeout=2)
        thread.join()
    bad=json.loads(json.dumps(config));bad['installed']['token_uri']='https://evil.example/token'
    with pytest.raises(ValueError):DesktopOAuth(bad)
    assert len(google.token_forms)==1


def test_qt_link_tab_mapping_registration_and_cancel(google,tmp_path):
    import os
    os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
    from PySide6.QtWidgets import QApplication
    from PySide6.QtTest import QTest
    from PySide6.QtCore import QTimer
    from signup031.viewer import EvidenceViewerWindow
    from test_scenario_editor import until
    api();app=QApplication.instance() or QApplication([]);viewer=EvidenceViewerWindow(tmp_path/'results')
    viewer.open_tc_catalog();catalog=viewer.tc_catalog_dialog;catalog.project_edit.setText('demo')
    try:
        assert hasattr(catalog,'sheets_button'),'catalog needs an explicit Sheets entrypoint'
        from signup031.sheets_dialog import SheetsImportDialog
        dialog=SheetsImportDialog(catalog,test_endpoint=google.url);catalog.sheets_dialog=dialog
        assert dialog.process is None and not google.calls
        dialog.path_edit.setText(LINK);dialog.api_key_edit.setText(KEY)
        dialog.start_metadata();until(lambda:dialog.process is None)
        assert dialog.tabs.currentData()==7
        dialog.start_preview();until(lambda:dialog.process is None)
        dialog.start_import();dialog.start_import();until(lambda:dialog.process is None)
        assert '등록 2' in dialog.status_label.text() and catalog.store.count_cases('demo')==2
        google.mode='delay';ticks=[];timer=QTimer();timer.timeout.connect(lambda:ticks.append(1));timer.start(20)
        dialog.start_metadata();QTest.qWait(250);assert len(ticks)>=3
        dialog.cancel();until(lambda:dialog.process is None,3);timer.stop()
        assert catalog.store.count_cases('demo')==2
        dialog.close();assert not dialog.api_key_edit.text()
        catalog.close();viewer.close()
        reopened=EvidenceViewerWindow(tmp_path/'results');reopened.open_tc_catalog()
        reopened.tc_catalog_dialog.project_edit.setText('demo');reopened.tc_catalog_dialog.refresh()
        assert reopened.tc_catalog_dialog.table.rowCount()==2
        reopened.tc_catalog_dialog.close();reopened.close()
    finally:
        if getattr(catalog,'sheets_dialog',None):catalog.sheets_dialog.close()
        catalog.close();viewer.close();app.processEvents()


def test_anonymous_rows_survive_tab_rename_without_new_candidates(google,tmp_path):
    _,Client=api();from signup031.tc_import import TCStore
    google.rows=[['제목','절차','기대 결과'],['manual','do it','done']]
    client=Client(api_key=KEY,test_endpoint=google.url);store=TCStore(tmp_path/'results')
    batch=client.read_batch(DOC,7);mapping={'title':0,'steps':1,'expected':2}
    store.register('demo',batch['source_id'],batch,mapping)
    google.tab_title='Name only changed';renamed=client.read_batch(DOC,7)
    assert renamed['sha256']==batch['sha256']
    outcome=store.register('demo',renamed['source_id'],renamed,mapping)
    assert outcome['counts']['duplicate']==1 and outcome['counts']['candidate']==0


def test_oauth_denial_cancellation_and_incomplete_http_are_bounded(google):
    api();from signup031.sheets_import import DesktopOAuth
    import socket
    config={'installed':{'client_id':'123-test.apps.googleusercontent.com','client_secret':'synthetic-client-secret',
        'auth_uri':'https://accounts.google.com/o/oauth2/auth','token_uri':'https://oauth2.googleapis.com/token','redirect_uris':['http://localhost']}}
    with DesktopOAuth(config,test_token_endpoint=google.url+'/token') as flow:
        with pytest.raises(ValueError,match='취소'):flow.wait(timeout=1,cancel=lambda:True)
    with DesktopOAuth(config,test_token_endpoint=google.url+'/token') as flow:
        callback=parse_qs(urlsplit(flow.authorization_url).query)
        def denied():
            try:urlopen(callback['redirect_uri'][0]+'?error=access_denied&state='+callback['state'][0]).read()
            except OSError:pass
        thread=Thread(target=denied);thread.start()
        with pytest.raises(ValueError,match='거절'):flow.wait(timeout=2)
        thread.join()
    with DesktopOAuth(config,test_token_endpoint=google.url+'/token') as flow:
        sock=socket.create_connection(('127.0.0.1',flow.server.server_port));sock.sendall(b'GET / HTTP/1.1\r\n')
        started=time.monotonic()
        # A separate closer prevents a broken implementation from hanging the whole test.
        def release():time.sleep(2);sock.close()
        closer=Thread(target=release);closer.start()
        with pytest.raises(ValueError,match='시간'):flow.wait(timeout=.1)
        elapsed=time.monotonic()-started;closer.join()
        assert elapsed<1
    assert google.token_forms==[]


def test_credentials_are_rejected_in_data_and_untrusted_endpoints(google):
    _,Client=api()
    with pytest.raises(ValueError):Client(api_key=KEY,test_endpoint='https://evil.example')
    google.rows[1][1]=TOKEN
    with pytest.raises(ValueError,match='인증 값'):Client(access_token=TOKEN,test_endpoint=google.url).read_batch(DOC,7)


def test_qt_no_credentials_conflicting_gid_expired_session_do_not_register(google,tmp_path):
    import os
    os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
    from PySide6.QtWidgets import QApplication
    from signup031.viewer import EvidenceViewerWindow
    from signup031.sheets_dialog import SheetsImportDialog
    from test_scenario_editor import until
    app=QApplication.instance() or QApplication([]);viewer=EvidenceViewerWindow(tmp_path/'results');viewer.open_tc_catalog();catalog=viewer.tc_catalog_dialog
    dialog=SheetsImportDialog(catalog,test_endpoint=google.url);catalog.sheets_dialog=dialog
    try:
        dialog.path_edit.setText(LINK);dialog.start_metadata()
        assert '연결 설정 필요' in dialog.status_label.text() and not google.calls
        dialog.api_key_edit.setText(KEY);dialog.path_edit.setText(LINK.replace('#gid=7','#gid=9'))
        dialog.start_metadata();until(lambda:dialog.process is None)
        assert dialog.tabs.currentIndex()==-1 and '다릅니다' in dialog.status_label.text()
        dialog.start_preview();assert dialog.process is None
        dialog.tabs.setCurrentIndex(dialog.tabs.findData(9));dialog.start_preview();until(lambda:dialog.process is None)
        assert dialog.snapshot['provider_metadata']['sheet_id']==9
        calls=len(google.calls);dialog.session={'access_token':TOKEN,'expires_at':time.time()-1}
        dialog.start_metadata();assert '만료' in dialog.status_label.text() and len(google.calls)==calls
        assert catalog.store.count_cases('default')==0
        dialog.session={'access_token':TOKEN,'expires_at':time.time()+300}
        catalog.close()
        assert dialog.session is None and not dialog.api_key_edit.text()
    finally:dialog.close();catalog.close();viewer.close();app.processEvents()


@pytest.mark.parametrize('fault',['renamed_id','document','offset_row','offset_column','multiple_blocks'])
def test_grid_response_identity_and_offsets_rejected_before_registration(google,tmp_path,fault):
    _,Client=api();from signup031.tc_import import TCStore
    grid={'spreadsheetId':DOC,'sheets':[{'properties':{'sheetId':7,'title':google.tab_title},
        'data':[{'rowData':[{'values':[{'formattedValue':v} for v in row]} for row in google.rows]}]}]}
    if fault=='renamed_id':grid['sheets'][0]['properties']['sheetId']=9
    elif fault=='document':grid['spreadsheetId']='different-document'
    elif fault=='offset_row':grid['sheets'][0]['data'][0]['startRow']=1
    elif fault=='offset_column':grid['sheets'][0]['data'][0]['startColumn']=1
    else:grid['sheets'][0]['data']*=2
    google.grid_override=grid;store=TCStore(tmp_path/'results')
    with pytest.raises(ValueError,match='출처|범위'):
        batch=Client(api_key=KEY,test_endpoint=google.url).read_batch(DOC,7)
        store.register('demo',batch['source_id'],batch,{'original_id':0,'title':1,'steps':2,'expected':3})
    assert store.count_cases('demo')==0
