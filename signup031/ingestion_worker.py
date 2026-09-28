"""Qt-owned child process: credentials arrive on stdin and are never arguments."""
import json
import os
from pathlib import Path
import sys

from signup031.ingestion_client import IngestionClient, TransferError


def main():
    token = ''
    try:
        request = json.loads(sys.stdin.readline())
        token = request['token']
        print(json.dumps({'status': 'started', 'pid': os.getpid()}), flush=True)
        client = IngestionClient(request['url'], request['project'], token, timeout=10)
        result = client.download(Path(request['root']))
        print(json.dumps({'status': 'complete' if not result['partial'] else 'partial',
            'complete': len(result['complete']), 'partial': len(result['partial'])}), flush=True)
        return 0 if not result['partial'] else 3
    except (TransferError, OSError, ValueError, KeyError, TypeError) as exc:
        reason = str(exc).replace(token, '[REDACTED]') if token else str(exc)
        print(json.dumps({'status': 'error', 'reason': reason[:300]}), flush=True)
        return 2


if __name__ == '__main__': raise SystemExit(main())
