"""Explicit Sheets/OAuth operations in a cancellable child; no persistent credentials."""
import json
from pathlib import Path
import sqlite3
import sys
import webbrowser

from signup031.sheets_import import DesktopOAuth,SheetsClient,parse_link
from signup031.tc_import import TCStore


def main():
    try:
        request=json.loads(sys.stdin.readline());action=request['action']
        if action=='oauth':
            with Path(request['client_file']).open('rb') as stream:raw=stream.read(65537)
            if len(raw)>65536:raise ValueError('OAuth 설정 파일 상한 초과')
            with DesktopOAuth(json.loads(raw),test_token_endpoint=request.get('test_token_endpoint')) as flow:
                # The UI only submits this action after an explicit Connect click.
                if request.get('open_browser') is not True:raise ValueError('명시적 브라우저 연결 조작이 필요합니다')
                if not webbrowser.open(flow.authorization_url):raise ValueError('시스템 브라우저를 열 수 없습니다')
                result=flow.wait(timeout=120)
        else:
            link=parse_link(request['link'])
            client=SheetsClient(api_key=request.get('api_key',''),access_token=request.get('access_token',''),test_endpoint=request.get('test_endpoint'))
            if action=='metadata':result={'link':link,'tabs':client.metadata(link['document_id'])}
            else:
                batch=client.read_batch(link['document_id'],request['sheet_id'],header_row=request['header_row'])
                store=TCStore(request['root'])
                if action=='preview':
                    result={key:value for key,value in batch.items() if key not in ('raw_bytes','rows')}
                    result.update(preview=batch['rows'][:20],total_rows=len(batch['rows']),mapping=store.get_mapping(request['project'],batch['headers']))
                elif action=='import':
                    if request['sha256']!=batch['sha256'] or request['source_id']!=batch['source_id'] or request['headers']!=batch['headers']:
                        raise ValueError('미리보기 후 시트 내용/선택 탭이 바뀌었습니다. 다시 읽어주세요')
                    result=store.register(request['project'],batch['source_id'],batch,request['mapping'])
                else:raise ValueError('지원하지 않는 작업')
        print(json.dumps({'status':'complete','result':result}),flush=True);return 0
    except (OSError,ValueError,KeyError,TypeError,AttributeError,sqlite3.Error) as exc:
        # Only our bounded messages are returned; never print remote response bodies or request objects.
        reason=str(exc)[:500]
        for value in (locals().get('request') or {}).values():
            if isinstance(value,str) and value and (value==locals().get('request',{}).get('api_key') or value==locals().get('request',{}).get('access_token')):
                reason=reason.replace(value,'[REDACTED]')
        if isinstance(exc,(KeyError,TypeError,AttributeError)):reason='연결 설정/응답 형식 오류'
        print(json.dumps({'status':'error','reason':reason}),flush=True);return 2


if __name__=='__main__':raise SystemExit(main())
