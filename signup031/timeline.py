"""Bounded observer-receipt timeline; never invent cross-clock occurrence times."""
import hashlib
import json
import math
from pathlib import Path
import re
import time
from urllib.parse import parse_qsl,quote,urlsplit,urlunsplit

MAX_EVENTS=3000
MAX_STRING=512
MAX_BYTES=2_000_000
KINDS={'action','console','pageerror','request','response','request_finished','request_failed','redirect'}
CLOCK_NOTE='기록기가 받은 상대 시각(perf_counter)으로 정렬합니다. 원천 시계는 변환하지 않으며 배치 수신 순서는 발생 순서와 다를 수 있습니다. 시간 인접은 원인 확정이 아닙니다.'


def validate_declaration(value):
    if not isinstance(value,dict) or set(value)!={'version','path','size','sha256'} or type(value['version']) is not int or value['version']!=1 or value['path']!='timeline.json':raise ValueError('타임라인 선언 형식 오류')
    if type(value['size']) is not int or not 0<value['size']<=MAX_BYTES or not isinstance(value['sha256'],str) or not re.fullmatch('[0-9a-f]{64}',value['sha256']):raise ValueError('타임라인 크기/해시 오류')
    return value


def validate_bytes(raw,execution_id,declaration=None):
    if len(raw)>MAX_BYTES:raise ValueError('타임라인 파일 상한 초과')
    if declaration:
        validate_declaration(declaration)
        if len(raw)!=declaration['size'] or hashlib.sha256(raw).hexdigest()!=declaration['sha256']:raise ValueError('타임라인 파일 크기/해시 불일치')
    data=json.loads(raw)
    if not isinstance(data,dict) or data.get('timeline_version')!=1 or data.get('execution_id')!=execution_id or data.get('axis')!='observer_received_ms':raise ValueError('타임라인 버전/실행 ID/시간축 오류')
    events=data.get('events');previous=-1
    if not isinstance(events,list) or len(events)>MAX_EVENTS or type(data.get('dropped')) is not int or data['dropped']<0:raise ValueError('타임라인 이벤트 상한 오류')
    for index,event in enumerate(events,1):
        if not isinstance(event,dict) or set(event)!={'seq','kind','received_ms','occurred_ms','source_clock','source_unit','source_time','request_id','details'}:raise ValueError('타임라인 이벤트 형식 오류')
        if event['seq']!=index or event['kind'] not in KINDS or event['occurred_ms'] is not None:raise ValueError('타임라인 이벤트 종류/발생시각 오류')
        value=event['received_ms']
        if type(value) not in (int,float) or not math.isfinite(value) or value<previous:raise ValueError('타임라인 수신시각 오류')
        previous=value
        if event['source_time'] is not None and (type(event['source_time']) not in (int,float) or not math.isfinite(event['source_time'])):raise ValueError('원천 시각 오류')
        if event['source_unit'] not in (None,'ms','s'):raise ValueError('원천 시각 단위 오류')
        for key in ('source_clock','request_id'):
            if event[key] is not None and (not isinstance(event[key],str) or len(event[key])>MAX_STRING):raise ValueError('원천 시계/요청 ID 오류')
        if not isinstance(event['details'],dict) or len(event['details'])>16:raise ValueError('상세 형식 오류')
        if any(not isinstance(v,(str,int,float,bool,type(None))) or (isinstance(v,str) and len(v)>MAX_STRING) for v in event['details'].values()):raise ValueError('상세 값 상한 오류')
    if not isinstance(data.get('availability'),dict) or any(v not in ('collected','not_collected') for v in data['availability'].values()):raise ValueError('수집 상태 오류')
    return data


def load_timeline(evidence_path):
    from signup031.ingestion_contract import safe_path
    path=Path(evidence_path);payload=json.loads(path.read_bytes());declaration=payload.get('timeline')
    if declaration is None:return None
    validate_declaration(declaration);timeline=safe_path(path.parent,'timeline.json')
    with timeline.open('rb') as stream:raw=stream.read(MAX_BYTES+1)
    return validate_bytes(raw,payload['execution']['id'],declaration)


def save_optional(payload,collector,root):
    try:payload['timeline']=collector.save(root)
    except Exception as exc:
        payload['timeline_capture']={'status':'collection_failed','reason':type(exc).__name__+' · 타임라인 저장 실패. 핵심 실행 판정과 별개입니다.'}


class Timeline:
    def __init__(self,execution_id,source,*,max_events=MAX_EVENTS,sensitive_values=()):
        self.execution_id=execution_id;self.source=source;self.started=time.perf_counter();self.events=[];self.dropped=0;self.limits=set()
        self.max_events=min(MAX_EVENTS,max_events);self.secrets=set();self.hide_text=False;self.availability={k:'not_collected' for k in ('action','console','pageerror','network')}
        self.ids={};self.cdp_hops={};self.learn(sensitive_values)

    def learn(self,values):
        for value in values:
            if isinstance(value,str) and value:
                if (len(self.secrets)>=1000 and value not in self.secrets) or len(value)>10000:self.secret_limit()
                else:self.secrets.add(value)

    def secret_limit(self):
        self.hide_text=True;self.limits.add('sensitive value limit')

    def unsupported_frame(self):
        self.hide_text=True;self.limits.add('iframe console attribution unavailable; text masked')

    def learn_headers(self,headers):
        for name,value in headers.items():
            if name.lower() in ('authorization','proxy-authorization'):
                self.learn([str(value),str(value).split(' ',1)[-1]])
            elif name.lower() in ('cookie','set-cookie'):
                self.learn([piece.split('=',1)[1] for piece in str(value).split(';') if '=' in piece and piece.strip().split('=',1)[0].lower() not in ('path','domain','expires','max-age','samesite')])

    def clean(self,value):
        if not isinstance(value,str):return value
        if self.hide_text:return '[REDACTED: secret limit]'
        variants={v for s in self.secrets for v in (s,quote(s,safe=''))}
        if variants:value=re.sub('|'.join(re.escape(s) for s in sorted(variants,key=len,reverse=True)),'[REDACTED]',value)
        value=re.sub(r'(?i)\bBearer\s+[^\s,;]+','Bearer [REDACTED]',value)
        value=re.sub(r'(?i)(token|password|secret|api_key|cookie|authorization)(\s*[:=]\s*)[^\s,;&]+',r'\1\2[REDACTED]',value)
        return value if len(value)<=MAX_STRING else '[omitted: string limit]'

    def url(self,value):
        parsed=urlsplit(value)
        self.learn(v for k,v in parse_qsl(parsed.query) if re.search('(?i)token|key|password|secret|auth',k))
        host=parsed.hostname or ''
        if ':' in host:host='['+host+']'
        if parsed.port:host+=':'+str(parsed.port)
        return self.clean(urlunsplit((parsed.scheme,host,parsed.path,'','')))

    def add(self,kind,details,*,request_id=None,source_clock=None,source_time=None,source_unit=None):
        if len(self.events)>=self.max_events:self.dropped+=1;return
        if kind not in KINDS:raise ValueError('이벤트 종류 오류')
        self.events.append({'seq':len(self.events)+1,'kind':kind,'received_ms':round((time.perf_counter()-self.started)*1000,3),
            'occurred_ms':None,'source_clock':source_clock,'source_unit':source_unit,'source_time':source_time,
            'request_id':request_id,'details':{str(k)[:64]:self.clean(v) for k,v in list(details.items())[:16]}})

    def js_event(self,payload):
        try:
            if not isinstance(payload,dict) or payload.get('kind')!='action':raise ValueError()
            if payload.get('secret_limit_exceeded') is True:self.secret_limit()
            clock=payload['document'];stamp=payload['time'];details=payload['details'];secrets=payload.get('secrets',[])
            if not isinstance(clock,str) or not re.fullmatch('[A-Za-z0-9_-]{1,64}',clock):raise ValueError()
            if type(stamp) not in (int,float) or not math.isfinite(stamp) or not 0<=stamp<=1e12:raise ValueError()
            if not isinstance(details,dict) or not 1<=len(details)<=16 or any(not isinstance(k,str) or len(k)>64 or not isinstance(v,str) or len(v)>10000 for k,v in details.items()):raise ValueError()
            if not isinstance(secrets,list) or len(secrets)>1000 or any(not isinstance(v,str) or len(v)>10000 for v in secrets):raise ValueError()
            self.learn(secrets)
            self.add('action',details,source_clock='document-performance:'+clock,source_time=stamp,source_unit='ms')
        except (KeyError,TypeError,ValueError):self.dropped+=1;self.limits.add('invalid DOM event omitted')

    def cdp_event(self,event):
        method,p=event['method'],event['params'];rid=p.get('requestId')
        self.learn_headers(p.get('headers',{}))
        for key in ('request','response','redirectResponse'):self.learn_headers(p.get(key,{}).get('headers',{}))
        if rid is None:return
        if rid not in self.cdp_hops and len(self.cdp_hops)<MAX_EVENTS:self.cdp_hops[rid]=0
        hop=self.cdp_hops.get(rid,0)
        if method=='Network.requestWillBeSent' and p.get('redirectResponse'):
            self.add('redirect',{'status':p['redirectResponse']['status'],'url':self.url(p['request']['url'])},request_id=f'{rid}:{hop}',source_clock='cdp:'+self.execution_id,source_time=p.get('timestamp'),source_unit='s')
            hop+=1
            if rid in self.cdp_hops:self.cdp_hops[rid]=hop
        common={'request_id':f'{rid}:{hop}','source_clock':'cdp:'+self.execution_id,'source_time':p.get('timestamp'),'source_unit':'s'}
        if method=='Network.requestWillBeSent':self.add('request',{'method':p['request']['method'],'url':self.url(p['request']['url'])},**common)
        elif method=='Network.responseReceived':self.add('response',{'status':p['response']['status'],'url':self.url(p['response']['url'])},**common)
        elif method=='Network.loadingFinished':self.add('request_finished',{},**common)
        elif method=='Network.loadingFailed':self.add('request_failed',{'error':p.get('errorText','unknown')},**common)

    def observe_dom(self,page):
        try:
            state=page.evaluate('() => window.__qaTimeline ? window.__qaTimeline.drain() : null')
            if not isinstance(state,dict) or not isinstance(state.get('secrets'),list):raise ValueError()
            if state.get('secret_limit_exceeded') is True:self.secret_limit()
            if state.get('unsupported_frames') is True:self.unsupported_frame()
            self.learn(state['secrets'])
        except Exception:
            self.hide_text=True;self.limits.add('sensitive fields unavailable; text masked')

    def attach_playwright(self,context,page):
        self.availability={k:'collected' for k in self.availability}
        page.on('frameattached',lambda frame:self.unsupported_frame())
        context.expose_binding('__qaTimelineSend',lambda source,payload:self.js_event(payload) if source['page']==page and source['frame']==page.main_frame else None)
        context.add_init_script(path=str(Path(__file__).with_name('timeline_capture.js')))
        def request_id(request):
            if request not in self.ids and len(self.ids)<MAX_EVENTS:self.ids[request]='pw-'+str(len(self.ids)+1)
            return self.ids.get(request,'over-limit')
        def requested(request):
            self.learn_headers(request.all_headers())
            rid=request_id(request)
            self.add('request',{'method':request.method,'url':self.url(request.url)},request_id=rid)
            if request.redirected_from:self.add('redirect',{'from_request':request_id(request.redirected_from),'url':self.url(request.url)},request_id=rid)
        def responded(response):
            self.learn_headers(response.all_headers())
            self.add('response',{'status':response.status,'url':self.url(response.url)},request_id=request_id(response.request))
        page.on('request',requested);page.on('response',responded)
        page.on('requestfinished',lambda request:self.add('request_finished',{},request_id=request_id(request)))
        page.on('requestfailed',lambda request:self.add('request_failed',{'error':request.failure},request_id=request_id(request)))
        page.on('console',lambda message:self.add('console',{'level':message.type,'text':message.text}))
        page.on('pageerror',lambda error:self.add('pageerror',{'text':str(error)}))

    def save(self,root):
        events=[dict(e,details={k:self.clean(v) for k,v in e['details'].items()}) for e in self.events]
        data={'timeline_version':1,'execution_id':self.execution_id,'source':self.source,'axis':'observer_received_ms',
            'clock_note':CLOCK_NOTE,'availability':self.availability,'events':events,'dropped':self.dropped,'limits':sorted(self.limits)}
        raw=json.dumps(data,ensure_ascii=False,separators=(',',':')).encode()
        while len(raw)>MAX_BYTES:
            events.pop();data['dropped']+=1;raw=json.dumps(data,ensure_ascii=False,separators=(',',':')).encode()
        validate_bytes(raw,self.execution_id)
        path=Path(root)/'timeline.json';path.write_bytes(raw)
        return {'version':1,'path':'timeline.json','size':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}
