"""One bounded, fixed-origin analysis request; stdin credentials only."""
import json,sys
from signup031.ai_assistant import OpenAIAdapter,strict_json
from signup031.failure_analysis import build_request,parse_analysis,reject_secret


def main():
    request={}
    try:
        raw=sys.stdin.buffer.readline(350001)
        if len(raw)>350000:raise ValueError('분석 요청 크기 상한 초과')
        request=strict_json(raw);payload=request['request'];snapshot=strict_json(payload['input'][0]['content'])
        if payload!=build_request(payload['model'],snapshot):raise ValueError('분석 전송 계약 불일치')
        reject_secret(snapshot,request['key'])
        client=OpenAIAdapter(request['key'],test_endpoint=request.get('test_endpoint'),timeout=request.get('timeout',45))
        result=parse_analysis(client.request_response(payload),payload,client.provider,client.key)
        print(json.dumps({'request_id':request['request_id'],'status':'complete','analysis':result},ensure_ascii=False),flush=True);return 0
    except Exception as exc:
        reason=str(exc)[:250] if isinstance(exc,ValueError) else 'AI 분석 연결 작업 실패'
        key=request.get('key','') if isinstance(request,dict) else ''
        if key:reason=reason.replace(key,'[REDACTED]')
        print(json.dumps({'request_id':request.get('request_id') if isinstance(request,dict) else None,'status':'error','reason':reason}),flush=True);return 2


if __name__=='__main__':raise SystemExit(main())
