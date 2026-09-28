"""Install only the browser revision required by the bundled Playwright."""
import json
import sys
from signup031.desktop_runtime import configure_runtime


def main():
    configure_runtime()
    print(json.dumps({'status':'installing','message':'Playwright Chromium 설치 중 · 인터넷 연결 필요'}),flush=True)
    from playwright.__main__ import main as playwright_main
    sys.argv=['playwright','install','chromium']
    try:playwright_main()
    except SystemExit as exc:
        code=exc.code or 0
        print(json.dumps({'status':'ready' if code==0 else 'failed','exit_code':code}),flush=True)
        return code
    return 0


if __name__=='__main__':raise SystemExit(main())
