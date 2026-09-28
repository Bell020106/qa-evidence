"""Stdin-handshaken, owned desktop Android worker."""
import json
import sys
from signup031.android_adapter import ADBCommand, parse_devices, run_scenario


def main():
    try:
        raw=sys.stdin.buffer.readline(150001)
        if len(raw)>150000: raise ValueError('Android request too large')
        request=json.loads(raw)
        command=ADBCommand(request['adb'])
        if request['mode']=='devices':
            result={'devices':parse_devices(command(['devices','-l']).decode('utf-8','replace'))}
        elif request['mode']=='run':
            result=run_scenario(request['config'],request['output'],command)
            result={'status':result['result']['status'],'path':request['output']+'/evidence.json'}
        else: raise ValueError('Unknown Android operation')
        print(json.dumps(result),flush=True)
        return 0
    except Exception as exc:
        print(json.dumps({'error':str(exc)[:500]}),flush=True)
        return 2


if __name__=='__main__':raise SystemExit(main())
