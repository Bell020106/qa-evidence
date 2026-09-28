"""Stdin handshake starts a frozen suite only after its parent establishes ownership."""
import json
import sys
from signup031.regression_suite import MAX_BYTES,run_suite,validate_snapshot

# JSON ASCII escaping can expand four-byte Unicode into twelve-byte surrogates.
MAX_WIRE_BYTES=3*MAX_BYTES+65536


def parse_request(raw):
    if len(raw)>MAX_WIRE_BYTES:raise ValueError('묶음 전송 입력 상한 초과')
    request=json.loads(raw)
    if not isinstance(request,dict) or set(request)!={'root','snapshot'} or not isinstance(request['root'],str) or len(request['root'])>4096:raise ValueError('묶음 요청 형식 오류')
    request['snapshot']=validate_snapshot(request['snapshot'])
    return request


def main():
    try:
        raw=sys.stdin.buffer.read(MAX_WIRE_BYTES+1)
        request=parse_request(raw)
        def update(state):print(json.dumps({'suite_id':state['suite_id'],'status':state['status']}),flush=True)
        state=run_suite(request['root'],request['snapshot'],on_update=update)
        return 0 if state['status']=='completed' else 1
    except (OSError,ValueError,KeyError,TypeError) as exc:
        print(json.dumps({'status':'error','reason':str(exc)[:3000]}),flush=True);return 2


if __name__=='__main__':raise SystemExit(main())
