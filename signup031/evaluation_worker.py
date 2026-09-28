"""Frozen evaluation batch in one owned process; each failure continues the batch."""
import json
import re
import sys
from signup031.ai_assistant import OpenAIAdapter
from signup031.ai_evaluation import MAX_DATASET_BYTES,EvaluationStore,ensure_no_key,generate_case,validate_dataset,digest

MAX_WIRE_BYTES=3*MAX_DATASET_BYTES+65536


def encode_request(root,snapshot,key,run_id,test_endpoint):
    raw=json.dumps({'root':root,'snapshot':snapshot,'key':key,'run_id':run_id,'test_endpoint':test_endpoint},ensure_ascii=True).encode()
    parse_request(raw);return raw


def parse_request(raw):
    if len(raw)>MAX_WIRE_BYTES:raise ValueError('평가 요청 포장 상한 초과')
    value=json.loads(raw)
    if not isinstance(value,dict) or set(value)!={'root','snapshot','key','run_id','test_endpoint'}:raise ValueError('평가 요청 형식 오류')
    if not isinstance(value['root'],str) or len(value['root'])>4096 or not isinstance(value['run_id'],str) or not re.fullmatch('[0-9a-f]{32}',value['run_id']):raise ValueError('평가 실행 경로/ID 오류')
    value['snapshot']=validate_dataset(value['snapshot']);ensure_no_key(value['snapshot'],value['key'])
    OpenAIAdapter(value['key'],test_endpoint=value['test_endpoint'])
    return value


def run(request,notify=lambda _:None):
    store=EvaluationStore(request['root']);run_id=request['run_id'];state=store.load_run(run_id)
    if state['snapshot_sha256']!=digest(request['snapshot']):raise ValueError('실행 스냅샷 불일치')
    if not store.start(run_id):return
    for index,case in enumerate(request['snapshot']['cases']):
        if not store.start_case(run_id,case['id']):return
        notify({'run_id':run_id,'case_index':index,'status':'running'})
        try:
            response,model=generate_case(request['key'],request['snapshot'],case,test_endpoint=request['test_endpoint'])
            store.complete_case(run_id,case['id'],response,model)
        except Exception as exc:
            reason=str(exc)[:250] if isinstance(exc,ValueError) else '대상 생성 연결/처리 오류'
            if request['key']:reason=reason.replace(request['key'],'[REDACTED]')
            store.fail_case(run_id,case['id'],reason)
        notify({'run_id':run_id,'case_index':index,'status':'case_finished'})
    store.finish(run_id);notify({'run_id':run_id,'status':'completed'})


def main():
    try:
        request=parse_request(sys.stdin.buffer.read(MAX_WIRE_BYTES+1))
        run(request,lambda event:print(json.dumps(event),flush=True));return 0
    except Exception:
        print(json.dumps({'status':'error','reason':'평가 작업 설정/저장 실패'}),flush=True);return 2


if __name__=='__main__':raise SystemExit(main())
