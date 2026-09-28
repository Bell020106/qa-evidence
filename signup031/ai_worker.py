"""One stdin-supplied API call. Credentials never enter argv, files or output."""
import json
import sys
from signup031.ai_assistant import OpenAIAdapter


def main():
    try:
        raw=sys.stdin.buffer.readline(250001)
        if len(raw)>250000:raise ValueError('요청 상한 초과')
        request=json.loads(raw)
        client=OpenAIAdapter(request['key'],test_endpoint=request.get('test_endpoint'))
        result=client.generate(request['request'])
        print(json.dumps({'request_id':request['request_id'],'status':'complete','proposal':result}),flush=True);return 0
    except Exception as exc:
        reason=str(exc)[:250] if isinstance(exc,ValueError) else 'AI 연결 작업 실패'
        key=locals().get('request',{}).get('key','')
        if key:reason=reason.replace(key,'[REDACTED]')
        print(json.dumps({'request_id':locals().get('request',{}).get('request_id'),'status':'error','reason':reason}),flush=True);return 2


if __name__=='__main__':raise SystemExit(main())
