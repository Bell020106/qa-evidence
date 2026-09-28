"""Explicit frozen stdio worker entry point; never launches the desktop GUI."""
import json
import runpy
import sys
from signup031.desktop_runtime import configure_runtime

WORKER_MODULES=(
    'signup031.web_runner','signup031.manual_recording','signup031.replay',
    'signup031.ingestion_worker','signup031.jira_sync_worker','signup031.tc_import_worker',
    'signup031.sheets_worker','signup031.suite_worker','signup031.experiment_worker',
    'signup031.ai_worker','signup031.evaluation_worker','signup031.android_worker',
    'signup031.browser_install','signup031.failure_analysis_worker',
)


def main(argv=None):
    args=list(sys.argv[1:] if argv is None else argv)
    configure_runtime()
    for stream in (sys.stdin,sys.stdout,sys.stderr):
        if hasattr(stream,'reconfigure'):stream.reconfigure(encoding='utf-8')
    gate=bool(args and args[0]=='--startup-gate')
    if gate:args.pop(0)
    if not args or args[0] not in WORKER_MODULES:
        print(json.dumps({'status':'error','reason':'Unsupported desktop worker'}),flush=True);return 2
    if gate and sys.stdin.buffer.readline(32)!=b'start\n':
        print(json.dumps({'status':'error','reason':'Worker ownership handshake missing'}),flush=True);return 2
    module=args.pop(0);sys.argv=[module,*args]
    try:runpy.run_module(module,run_name='__main__',alter_sys=True)
    except SystemExit as exc:return exc.code if isinstance(exc.code,int) else (0 if exc.code is None else 1)
    return 0


if __name__=='__main__':raise SystemExit(main())
