"""Bounded CSV parsing/registration in a cancellable PC child process."""
import json
import sqlite3
import sys

from signup031.tc_import import TCStore, parse_csv


def main():
    try:
        request = json.loads(sys.stdin.readline())
        batch = parse_csv(request['path'], encoding=request['encoding'], header_row=request['header_row'])
        store = TCStore(request['root'])
        if request['action'] == 'preview':
            result = {key: value for key,value in batch.items() if key not in ('raw_bytes','rows')}
            result['preview'] = batch['rows'][:20]
            result['total_rows'] = len(batch['rows'])
            result['mapping'] = store.get_mapping(request['project'],batch['headers'])
        else:
            if request['sha256'] != batch['sha256'] or request['headers'] != batch['headers']:
                raise ValueError('미리보기 후 파일이 바뀌었습니다. 다시 읽어주세요')
            result = store.register(request['project'],request['source_id'],batch,request['mapping'])
        print(json.dumps({'status':'complete','result':result}),flush=True)
        return 0
    except (OSError,ValueError,KeyError,TypeError,sqlite3.Error) as exc:
        print(json.dumps({'status':'error','reason':str(exc)[:500]}),flush=True)
        return 2


if __name__ == '__main__': raise SystemExit(main())
