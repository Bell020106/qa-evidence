"""Read-only Sheets v4 and desktop OAuth. Credentials live only in session memory."""
import base64
from collections import Counter
import hashlib
from http.server import BaseHTTPRequestHandler,HTTPServer
import json
import re
import secrets
import time
from urllib.error import HTTPError,URLError
from urllib.parse import parse_qs,urlencode,urlsplit
from urllib.request import Request,build_opener

from signup031.ingestion_client import _NoRedirect,TransferError
from signup031.tc_import import MAX_BYTES,MAX_ROWS,MAX_COLUMNS,MAX_CELL,canonical

SCOPE='https://www.googleapis.com/auth/spreadsheets.readonly'
API_ORIGIN='https://sheets.googleapis.com'
AUTH_URL='https://accounts.google.com/o/oauth2/v2/auth'
TOKEN_URL='https://oauth2.googleapis.com/token'


def parse_link(link):
    try:
        parsed=urlsplit(link.strip())
        match=re.fullmatch(r'/spreadsheets/d/([A-Za-z0-9_-]{10,160})(?:/(?:edit|view|preview))?/?',parsed.path)
        if parsed.scheme!='https' or parsed.netloc!='docs.google.com' or not match:raise ValueError()
        query=parse_qs(parsed.query,keep_blank_values=True);fragment=parse_qs(parsed.fragment,keep_blank_values=True)
        if any(key.lower() in ('access_token','token','key','api_key','code') for key in [*query,*fragment]):raise ValueError()
        raw=[*query.get('gid',[]),*fragment.get('gid',[])]
        if any(not re.fullmatch(r'[0-9]{1,10}',v) or int(v)>2147483647 for v in raw):raise ValueError()
        choices=list(dict.fromkeys(int(v) for v in raw))
        return {'document_id':match[1],'gid':choices[0] if len(choices)==1 else None,
                'gid_choices':choices,'gid_conflict':len(choices)>1}
    except (ValueError,TypeError,AttributeError):raise ValueError('Google Sheets HTTPS 문서 링크와 숫자 gid를 확인하세요. 인증 값은 URL에 넣지 마세요') from None


def test_origin(url,*,allow_path=False):
    parsed=urlsplit(url)
    if parsed.scheme!='http' or parsed.hostname not in ('127.0.0.1','::1') or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('대역은 명시적 loopback HTTP만 허용합니다')
    if not allow_path and parsed.path not in ('','/'):raise ValueError('대역 origin 오류')
    return url.rstrip('/')


def json_request(url,*,headers=None,body=None,timeout=10,secrets_to_hide=()):
    if not 0<timeout<=60:raise ValueError('요청 시간 상한 오류')
    request=Request(url,data=body,headers=headers or {},method='POST' if body is not None else 'GET')
    try:
        start=time.monotonic()
        with build_opener(_NoRedirect).open(request,timeout=timeout) as response:
            if response.status!=200:raise ValueError('예상하지 않은 HTTP 응답')
            if 'application/json' not in response.headers.get('Content-Type','').lower():raise ValueError('JSON이 아닙니다. 로그인 페이지는 등록할 수 없습니다')
            chunks=[];length=0
            while True:
                block=response.read1(min(65536,MAX_BYTES+1-length))
                if not block:break
                length+=len(block)
                if length>MAX_BYTES:raise ValueError('응답 상한 10 MiB 초과')
                if time.monotonic()-start>timeout:raise ValueError('요청 시간 상한 초과')
                chunks.append(block)
            try:data=json.loads(b''.join(chunks))
            except (ValueError,UnicodeError):raise ValueError('JSON 응답 손상') from None
            if not isinstance(data,dict):raise ValueError('응답 객체 형식 오류')
            encoded=canonical(data)
            if any(value and value in encoded for value in secrets_to_hide):raise ValueError('응답에 인증 값이 포함되어 저장하지 않았습니다')
            return data
    except HTTPError as exc:
        messages={401:'인증 필요 또는 세션 만료',403:'읽기 권한/키 설정 필요',404:'문서 없음',429:'요청 한도 초과'}
        raise ValueError(messages.get(exc.code,'Google HTTP 오류')+f' (HTTP {exc.code})') from None
    except TransferError:raise ValueError('리디렉션 거절: 인증 정보를 다른 주소로 전달하지 않습니다') from None
    except (URLError,OSError,TimeoutError):raise ValueError('연결 실패 또는 요청 시간 초과') from None


class SheetsClient:
    def __init__(self,*,api_key='',access_token='',test_endpoint=None,timeout=10):
        if not api_key and not access_token:raise ValueError('연결 설정 필요: 공개 API key 또는 읽기 전용 OAuth 연결을 선택하세요. CSV 가져오기도 사용할 수 있습니다')
        for value in (api_key,access_token):
            if not isinstance(value,str) or len(value)>10000 or any(ord(c)<33 or ord(c)>126 for c in value):raise ValueError('인증 값 형식 오류')
        self.origin=test_origin(test_endpoint) if test_endpoint else API_ORIGIN
        self.api_key,self.token,self.timeout=api_key,access_token,timeout

    def _get(self,path):
        headers={'Accept':'application/json'}
        if self.token:headers['Authorization']='Bearer '+self.token
        elif self.api_key:headers['X-Goog-Api-Key']=self.api_key
        return json_request(self.origin+path,headers=headers,timeout=self.timeout,secrets_to_hide=(self.token,self.api_key))

    @staticmethod
    def _doc(document_id):
        if not isinstance(document_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{10,160}',document_id):raise ValueError('문서 ID 오류')
        return document_id

    def metadata(self,document_id):
        self._doc(document_id)
        data=self._get('/v4/spreadsheets/'+document_id+'?'+urlencode({'fields':'spreadsheetId,sheets.properties(sheetId,title)'}))
        if data.get('spreadsheetId')!=document_id or not isinstance(data.get('sheets'),list):raise ValueError('문서 metadata 형식 오류')
        tabs=[];seen=set()
        for sheet in data['sheets']:
            prop=sheet.get('properties',{}) if isinstance(sheet,dict) else {}
            gid,title=prop.get('sheetId'),prop.get('title')
            if type(gid) is not int or gid<0 or gid in seen or not isinstance(title,str) or not title or len(title)>1000:raise ValueError('탭 metadata 형식 오류')
            tabs.append({'sheet_id':gid,'title':title});seen.add(gid)
        if not tabs:raise ValueError('문서에 읽을 수 있는 탭이 없습니다')
        return tabs

    def read_batch(self,document_id,sheet_id,*,header_row=1):
        if type(header_row) is not int or not 1<=header_row<=100:raise ValueError('헤더 행은 1~100입니다')
        tabs=self.metadata(document_id)
        tab=next((tab for tab in tabs if tab['sheet_id']==sheet_id),None)
        if tab is None:raise ValueError('선택한 탭이 문서에 없습니다. 첫 탭으로 대체하지 않습니다')
        a1="'"+tab['title'].replace("'","''")+"'"
        # GridData binds the returned sheet ID and displayed values in one readonly response.
        fields='spreadsheetId,sheets(properties(sheetId,title),data(startRow,startColumn,rowData(values(formattedValue))))'
        data=self._get('/v4/spreadsheets/'+document_id+'?'+urlencode({'ranges':a1,'fields':fields}))
        sheets=data.get('sheets')
        if data.get('spreadsheetId')!=document_id or not isinstance(sheets,list) or len(sheets)!=1 or not isinstance(sheets[0],dict):
            raise ValueError('출처 문서/탭이 달라졌습니다. 탭을 다시 조회하세요')
        sheet=sheets[0];prop=sheet.get('properties',{})
        if not isinstance(prop,dict) or type(prop.get('sheetId')) is not int or prop['sheetId']!=sheet_id or prop.get('title')!=tab['title']:
            raise ValueError('출처 탭이 달라졌습니다. 탭을 다시 조회하세요')
        blocks=sheet.get('data',[])
        if not isinstance(blocks,list) or len(blocks)>1:raise ValueError('단일 탭 범위 응답이 아닙니다')
        block=blocks[0] if blocks else {}
        if not isinstance(block,dict) or any(type(block.get(k,0)) is not int or block.get(k,0)!=0 for k in ('startRow','startColumn')):
            raise ValueError('A1 시작 범위가 아닙니다. 탭을 다시 조회하세요')
        row_data=block.get('rowData',[])
        if not isinstance(row_data,list):raise ValueError('행 응답 형식 오류')
        if len(row_data)>header_row+MAX_ROWS:raise ValueError('데이터 행 상한 10000 초과')
        values=[]
        for row in row_data:
            cells=row.get('values',[]) if isinstance(row,dict) else None
            if not isinstance(cells,list) or len(cells)>MAX_COLUMNS:raise ValueError('열 응답 형식/상한 오류')
            if any(not isinstance(cell,dict) or not isinstance(cell.get('formattedValue',''),str) for cell in cells):raise ValueError('표시 문자열 형식 오류')
            values.append([cell.get('formattedValue','') for cell in cells])
        if not values or all(not row for row in values):raise ValueError('빈 시트: 등록할 행이 없습니다')
        if len(values)>header_row+MAX_ROWS:raise ValueError('데이터 행 상한 10000 초과')
        for row in values:
            if not isinstance(row,list) or len(row)>MAX_COLUMNS or any(not isinstance(v,str) or len(v)>MAX_CELL for v in row):
                raise ValueError('표시 문자열/열/셀 상한 오류. 형식을 추측해 변환하지 않습니다')
        if header_row>len(values):raise ValueError('헤더 행이 없습니다')
        width=max(map(len,values),default=0)
        if not width:raise ValueError('빈 시트')
        padded=[row+['']*(width-len(row)) for row in values]
        headers=padded[header_row-1]
        if not any(headers):raise ValueError('헤더가 비어 있습니다')
        # Hash displayed content only. Tab names, fetch time and credentials cannot change identity.
        raw=canonical(padded).encode()
        if len(raw)>MAX_BYTES:raise ValueError('정규화 응답 상한 10 MiB 초과')
        rows=[{'logical_row':i+1,'physical_start':i+1,'physical_end':i+1,'cells':row}
              for i,row in enumerate(padded) if i>=header_row]
        return {'format_version':1,'kind':'google-sheets','source_id':f'google-sheets:{document_id}:{sheet_id}',
            'source_location':f'https://docs.google.com/spreadsheets/d/{document_id}/edit#gid={sheet_id}',
            'sha256':hashlib.sha256(raw).hexdigest(),'raw_bytes':raw,'encoding':'formatted-json-utf8',
            'header_row':header_row,'headers':headers,'duplicate_headers':sorted(k for k,v in Counter(headers).items() if v>1),
            'provider_metadata':{'document_id':document_id,'sheet_id':sheet_id,'sheet_title':tab['title']},'rows':rows}


class DesktopOAuth:
    def __init__(self,config,*,test_token_endpoint=None):
        try:
            installed=config['installed']
            if not isinstance(installed,dict) or 'web' in config:raise ValueError()
            self.client_id=installed['client_id'];self.client_secret=installed['client_secret']
            if not re.fullmatch(r'[A-Za-z0-9._-]+\.apps\.googleusercontent\.com',self.client_id):raise ValueError()
            if not isinstance(self.client_secret,str) or not self.client_secret or len(self.client_secret)>1000:raise ValueError()
            if installed['auth_uri'] not in (AUTH_URL,'https://accounts.google.com/o/oauth2/auth') or installed['token_uri']!=TOKEN_URL:raise ValueError()
            redirects=installed.get('redirect_uris',[])
            if not isinstance(redirects,list) or not redirects:raise ValueError()
            for uri in redirects:
                parsed=urlsplit(uri)
                if parsed.scheme!='http' or parsed.hostname not in ('localhost','127.0.0.1','::1') or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('','/'):
                    raise ValueError()
        except (ValueError,KeyError,TypeError,AttributeError):raise ValueError('공식 Google Desktop installed OAuth 클라이언트 설정이 필요합니다') from None
        self.token_url=test_origin(test_token_endpoint,allow_path=True) if test_token_endpoint else TOKEN_URL
        self.verifier=secrets.token_urlsafe(64);self.state=secrets.token_urlsafe(32)
        self.server=None;self.code=None;self.error=None

    def __enter__(self):
        owner=self
        class Callback(BaseHTTPRequestHandler):
            def log_message(self,*_):pass
            def do_GET(self):
                parsed=urlsplit(self.path);query=parse_qs(parsed.query,keep_blank_values=True)
                if parsed.path!='/':
                    self.send_response(404);self.end_headers();return
                state=query.get('state',[])
                if len(state)!=1 or not secrets.compare_digest(state[0],owner.state):owner.error='OAuth state 불일치'
                elif 'error' in query:owner.error='OAuth 인증이 거절되었습니다'
                elif len(query.get('code',[]))!=1 or not query['code'][0] or len(query['code'][0])>10000:owner.error='OAuth callback code 오류'
                else:owner.code=query['code'][0]
                self.send_response(400 if owner.error else 200);self.send_header('Content-Type','text/plain; charset=utf-8');self.end_headers()
                try:self.wfile.write('이 창을 닫고 TC 등록 화면으로 돌아가세요.'.encode())
                except OSError:pass
        class BoundedLoopback(HTTPServer):
            def get_request(self):
                connection,address=super().get_request();connection.settimeout(.25)
                return connection,address
            def handle_error(self,request,client_address):pass
        self.server=BoundedLoopback(('127.0.0.1',0),Callback);self.server.timeout=.2
        self.redirect_uri='http://127.0.0.1:'+str(self.server.server_port)+'/'
        challenge=base64.urlsafe_b64encode(hashlib.sha256(self.verifier.encode()).digest()).decode().rstrip('=')
        self.authorization_url=AUTH_URL+'?'+urlencode({'client_id':self.client_id,'redirect_uri':self.redirect_uri,
            'response_type':'code','scope':SCOPE,'state':self.state,'code_challenge':challenge,'code_challenge_method':'S256','access_type':'online'})
        return self

    def wait(self,*,timeout=120,cancel=None):
        if self.server is None or not 0<timeout<=180:raise ValueError('OAuth 대기 설정 오류')
        deadline=time.monotonic()+timeout
        while not self.code and not self.error:
            if cancel and cancel():raise ValueError('OAuth 취소')
            if time.monotonic()>deadline:raise ValueError('OAuth 인증 시간 초과')
            self.server.handle_request()
        if self.error:raise ValueError(self.error)
        body=urlencode({'client_id':self.client_id,'client_secret':self.client_secret,'code':self.code,
            'code_verifier':self.verifier,'grant_type':'authorization_code','redirect_uri':self.redirect_uri}).encode()
        token=json_request(self.token_url,headers={'Content-Type':'application/x-www-form-urlencoded','Accept':'application/json'},body=body)
        access=token.get('access_token');expires=token.get('expires_in')
        if not isinstance(access,str) or not access or any(ord(c)<33 or ord(c)>126 for c in access) or token.get('token_type','').lower()!='bearer' or type(expires) is not int or expires<=0:
            raise ValueError('OAuth token 응답 오류')
        if set(token.get('scope',SCOPE).split())!={SCOPE}:raise ValueError('읽기 전용 범위가 확인되지 않았습니다')
        return {'access_token':access,'expires_at':time.time()+expires}

    def __exit__(self,*_):
        if self.server:self.server.server_close();self.server=None
        self.code=None;self.verifier='';self.state='';self.client_secret=''
