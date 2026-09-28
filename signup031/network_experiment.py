"""One bounded response rule layered over an already verified offline replay."""
import base64
import hashlib
import json
import time
from signup031.timeline import Timeline


def recorded_response(response,method):
    if type(response.get('status')) is not int or not 200<=response['status']<=599:raise ValueError('실제 응답 상태가 미수집된 항목입니다')
    content=response.get('content',{})
    if not isinstance(content,dict):raise ValueError('기록 본문 형식 오류')
    if 'text' not in content:
        if method=='HEAD' or (content.get('size')==0 and response['status'] in (204,304)):body=b''
        else:raise ValueError('응답 본문 미수집: 빈 본문으로 추정하지 않습니다')
    elif not isinstance(content['text'],str):raise ValueError('기록 본문 형식 오류')
    elif content.get('encoding')=='base64':body=base64.b64decode(content['text'],validate=True)
    elif content.get('encoding') is None:body=content['text'].encode()
    else:raise ValueError('기록 본문 인코딩을 지원하지 않습니다')
    if len(body)>10_000_000:raise ValueError('실험 응답 본문 10 MB 상한 초과')
    headers={};seen=set()
    for header in response.get('headers',[]):
        name,value=header['name'],header['value'];key=name.lower()
        if key in ('content-length','content-encoding','transfer-encoding','connection','keep-alive','te','trailer','upgrade'):continue
        if key in seen:raise ValueError('중복 응답 헤더는 동일하게 복원할 수 없어 선택하지 않습니다')
        headers[name]=value;seen.add(key)
    return body,headers


class NetworkExperiment:
    def __init__(self,replay):
        if not replay.restored or replay.status()['status']!='ready':raise ValueError('원래 기록의 정상 복원을 먼저 확인하세요. 제한/불일치 상태에서는 실험하지 않습니다')
        self.replay=replay;self.rule=None;self.pending=[];self.applied=[];self.rejected=0
        with replay.har_path.open('rb') as stream:raw=stream.read(80_000_001)
        if len(raw)>80_000_000:raise ValueError('실험 HAR 80 MB 상한 초과')
        self.raw_entries=json.loads(raw)['log']['entries']
        if len(self.raw_entries)>10000:raise ValueError('실험 HAR 항목 상한 10000 초과')
        self.entries=[];groups={};clean=Timeline('display','experiment')
        # Learn across the archive before rendering any earlier URL. Raw replay
        # requests and responses remain untouched; only display metadata is cleaned.
        for entry in self.raw_entries:
            for side in ('request','response'):
                for header in entry[side].get('headers',[]):
                    clean.learn_headers({header['name']:header['value']})
            clean.url(entry['request']['url'])
        for index,entry in enumerate(self.raw_entries):
            request,response=entry['request'],entry['response']
            if request['method'] not in ('GET','HEAD'):continue
            digest=hashlib.sha256(json.dumps(response,sort_keys=True,separators=(',',':')).encode()).hexdigest()
            key=(request['method'],request['url']);groups.setdefault(key,set()).add(digest)
            self.entries.append({'index':index,'method':key[0],'display_url':clean.url(key[1]),'response_sha256':digest,'status':response['status']})
        for item in self.entries:
            request=self.raw_entries[item['index']]['request'];item['ambiguous']=len(groups[(request['method'],request['url'])])>1
            try:recorded_response(self.raw_entries[item['index']]['response'],request['method']);item['unavailable_reason']=None
            except (ValueError,KeyError,TypeError) as exc:item['unavailable_reason']=str(exc)

    def arm(self,index,mode,delay_ms=0):
        if self.rule is not None:raise ValueError('한 세션에는 실험 규칙 하나만 적용합니다. 새 세션을 여세요')
        item=next((r for r in self.entries if type(index) is int and r['index']==index),None)
        if item is None or item['ambiguous']:raise ValueError('선택 응답이 없거나 같은 URL/method의 서로 다른 응답으로 모호합니다')
        if item['unavailable_reason']:raise ValueError(item['unavailable_reason'])
        if mode not in ('http500','delay') or type(delay_ms) is not int or (mode=='delay' and not 1<=delay_ms<=3000) or (mode=='http500' and delay_ms!=0):raise ValueError('500 또는 1~3000ms 지연을 선택하세요')
        entry=self.raw_entries[index];response=entry['response'];body,headers=recorded_response(response,entry['request']['method'])
        self.rule={**item,'mode':mode,'delay_ms':delay_ms};self.target=entry['request'];self.body=body
        self.headers=headers
        self.replay.context.route('**/*',self._route)

    def _route(self,route):
        request=route.request
        if request.method!=self.target['method'] or request.url!=self.target['url']:
            route.fallback();return # Only the existing HAR, then abort; never live network.
        if len(self.pending)>=64 or len(self.applied)>=1000:
            self.rejected+=1;route.abort('blockedbyclient');return
        observed=time.perf_counter()
        if self.rule['mode']=='http500':
            route.fulfill(status=500,headers={'Content-Type':'text/plain; charset=utf-8'},body=b'Local experiment HTTP 500')
            self.applied.append({'mode':'http500','elapsed_ms':round((time.perf_counter()-observed)*1000,3)})
        else:self.pending.append((route,observed,observed+self.rule['delay_ms']/1000))

    def tick(self):
        # Keep routes pending without blocking Playwright callbacks or the child command loop.
        for pending in list(self.pending):
            route,started,deadline=pending
            if time.perf_counter()<deadline:continue
            self.pending.remove(pending)
            route.fulfill(status=int(self.rule['status']),headers=self.headers,body=self.body)
            self.applied.append({'mode':'delay','elapsed_ms':round((time.perf_counter()-started)*1000,3)})

    def status(self):
        return {'normal_restore':'ready','experiment':'armed' if self.rule else 'not_armed','rule':self.rule,
            'applied':len(self.applied),'pending':len(self.pending),'rejected':self.rejected,'observations':list(self.applied)}
