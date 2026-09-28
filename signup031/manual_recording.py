"""Capture the user's original single-tab browser session; never re-run on save."""
import argparse
import json
from pathlib import Path
from queue import Empty, Queue
import sys
from threading import Thread
from urllib.parse import urlsplit

from signup031.contract import utc_now
from signup031.storage import create_run_directory, write_evidence


def validate_manual_url(url):
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('URL must be HTTP/HTTPS without credentials')
    _ = parsed.port


def validate_manual_record(record, *, allow_missing_urls=False):
    if not isinstance(record, dict):
        raise ValueError('missing manual record')
    for key in ('start_url', 'final_url'):
        if allow_missing_urls and record.get(key) is None:
            continue
        validate_manual_url(record[key])
    if not isinstance(record['title'], str) or not record['title'].strip():
        raise ValueError('missing recording title')
    if not isinstance(record['limitations'], list) or not all(isinstance(s, str) for s in record['limitations']):
        raise ValueError('invalid limitations')
    if not isinstance(record['actions'], list) or len(record['actions']) > 10000:
        raise ValueError('invalid actions')
    for row in [*record['actions'], *record['observed']['inputs']]:
        if not isinstance(row, dict) or not isinstance(row.get('selector'), str) or not row['selector']:
            raise ValueError('invalid recorded selector')
        signature = row.get('signature')
        if not isinstance(signature, dict) or set(signature) != {'tag','type','name'} or not all(isinstance(v, str) for v in signature.values()):
            raise ValueError('invalid element signature')
        if row.get('action', 'fill') not in ('fill', 'click'):
            raise ValueError('unsupported recorded action')
        if row.get('action', 'fill') == 'fill' and not isinstance(row.get('value'), str):
            raise ValueError('invalid recorded value')
    if not isinstance(record['observed']['text'], str):
        raise ValueError('invalid text observation')
    return record


class ManualRecorder:
    def __init__(self, url, title, artifacts_root, *, headless=False):
        validate_manual_url(url)
        if not isinstance(title, str) or not title.strip():
            raise ValueError('recording title is required')
        self.url, self.title, self.headless = url, title, headless
        self.execution_id, self.root = create_run_directory(Path(artifacts_root))
        self._owned_root = self.root.resolve()
        self.started = utc_now()
        from signup031.timeline import Timeline
        self.timeline=Timeline(self.execution_id,'manual-playwright')
        self.archive = self.root / 'archive'
        self.archive.mkdir()
        self.playwright = self.browser = self.context = self.page = None
        self.saved_path = None
        self.document_id = None
        self.extra_limitations = []
        self.last_snapshot = {'actions': [], 'limitations': [], 'final_url': url,
                              'observed': {'inputs': [], 'text': ''}}

    def __enter__(self):
        from playwright.sync_api import sync_playwright
        try:
            self.playwright = sync_playwright().start()
            self.browser = self.playwright.chromium.launch(headless=self.headless)
            self.context = self.browser.new_context(service_workers='block', accept_downloads=False,
                record_har_path=self.archive / 'resources.har', record_har_content='embed', record_har_mode='full')
            self.context.expose_binding('__qaManualLimit', self._receive_limitation)
            self.context.add_init_script(path=str(Path(__file__).with_name('manual_capture.js')))
            self.page = self.context.new_page()
            self.timeline.attach_playwright(self.context,self.page)
            self.page.on('frameattached', lambda frame: self.extra_limitations.append('iframe content not recorded'))
            self.context.on('page', lambda page: self.extra_limitations.append('new tab not recorded'))
            self.page.on('download', lambda download: self.extra_limitations.append('download not recorded'))
            self.page.goto(self.url, wait_until='domcontentloaded', timeout=30000)
            self.snapshot()
            return self
        except Exception:
            self.close()
            raise

    def _receive_limitation(self, source, reason):
        if source['page'] == self.page and source['frame'] == self.page.main_frame and isinstance(reason, str):
            if reason not in self.extra_limitations:
                self.extra_limitations.append(reason)

    def snapshot(self, stop=False):
        snapshot = self.page.evaluate('(stop) => window.__qaManual.snapshot(stop)', stop)
        if self.document_id is None:
            self.document_id = snapshot['document_id']
        elif self.document_id != snapshot['document_id']:
            self.extra_limitations.append('unexpected document navigation; earlier actions unavailable')
        if snapshot['final_url'] != self.url:
            self.extra_limitations.append('URL changed during recording')
        self.last_snapshot = snapshot
        return snapshot

    def close(self, *, discard=True):
        errors = []
        for attribute, method in (('context','close'), ('browser','close'), ('playwright','stop')):
            resource = getattr(self, attribute)
            if resource is not None:
                try:
                    getattr(resource, method)()
                except Exception as exc:
                    errors.append(str(exc)[:500])
                setattr(self, attribute, None)
        if discard and self.saved_path is None:
            # Only files owned by this unique execution, after HAR flushing ends.
            for relative in ('archive/resources.har', 'archive/resources.har.tmp',
                             'archive/manifest.json', 'archive/manifest.json.tmp',
                             'page.png', 'evidence.json.tmp'):
                path = self._owned_root / relative
                try:
                    if not path.resolve().is_relative_to(self._owned_root):
                        raise ValueError('cleanup path escaped execution directory')
                    if path.is_file() or path.is_symlink():
                        path.unlink()
                except Exception as exc:
                    errors.append(f'cleanup {relative}: {exc}')
        return errors

    def __exit__(self, *args):
        errors = self.close()
        if errors and args[0] is None:
            raise RuntimeError('; '.join(errors))

    def save(self):
        if self.saved_path is not None:
            return self.saved_path
        failure = None
        screenshot = {'status':'not_collected', 'path':'page.png', 'reason':None}
        try:
            self.timeline.observe_dom(self.page)
            snapshot = self.snapshot(stop=True)
            self.page.screenshot(path=str(self.root / 'page.png'), full_page=True, timeout=5000)
            screenshot['status'] = 'collected'
        except Exception as exc:
            snapshot = self.last_snapshot
            failure = f'save failed: {type(exc).__name__}: {str(exc)[:700]}'
            screenshot['reason'] = failure
        errors = self.close(discard=False)
        record = {'title':self.title, 'start_url':self.url, 'final_url':snapshot['final_url'],
                  'actions':snapshot['actions'], 'observed':snapshot['observed'],
                  'limitations':sorted(set(snapshot['limitations'] + self.extra_limitations))}
        from signup031.archive import finish_manual_archive
        replay = finish_manual_archive(self.archive, execution_id=self.execution_id, record=record,
                                       completed=failure is None, close_errors=errors)
        status = 'failed' if failure or errors or replay['status'] != 'recorded' else ('limited' if record['limitations'] else 'saved')
        payload = {'contract_version':'3', 'execution':{'id':self.execution_id,'started_at':self.started},
                   'manual_record':record, 'recording':{'status':status,'reason':failure or replay['reason']},
                   'result':{'business':{'status':'unjudged','phase':'manual','message':'수동 기록 · 판정 미입력'}},
                   'evidence':{'screenshot':screenshot}, 'replay':replay,
                   'post_run':{'cleanup':{'status':'failed' if errors else 'completed','errors':errors}}}
        path = self.root / 'evidence.json'
        from signup031.timeline import save_optional
        save_optional(payload,self.timeline,self.root)
        write_evidence(path, payload)
        self.saved_path = path
        return path


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', required=True)
    parser.add_argument('--title', required=True)
    parser.add_argument('--artifacts-dir', type=Path, required=True)
    parser.add_argument('--headless', action='store_true')
    args = parser.parse_args(argv)
    commands = Queue()
    def read_commands():
        for line in sys.stdin:
            commands.put(line.strip())
        commands.put('cancel')
    def emit(**event):
        print(json.dumps(event, ensure_ascii=True), flush=True)
    Thread(target=read_commands, daemon=True).start()
    try:
        with ManualRecorder(args.url, args.title, args.artifacts_dir, headless=args.headless) as recorder:
            emit(status='recording', count=0)
            while True:
                try:
                    command = commands.get_nowait()
                except Empty:
                    command = None
                if command == 'cancel':
                    errors = recorder.close()
                    if errors:
                        raise RuntimeError('; '.join(errors))
                    emit(status='cancelled', reason='미저장 종료')
                    return 0
                if command == 'save' or recorder.page.is_closed():
                    path = recorder.save()
                    payload = json.loads(path.read_text(encoding='utf-8'))
                    emit(status=payload['recording']['status'], evidence=str(path), reason=payload['recording']['reason'])
                    return 0 if payload['recording']['status'] != 'failed' else 1
                snapshot = recorder.snapshot()
                emit(status='recording', count=len(snapshot['actions']),
                     limitations=snapshot['limitations'] + recorder.extra_limitations)
                try:
                    recorder.page.wait_for_timeout(200)
                except Exception:
                    if not recorder.page.is_closed():
                        raise
    except Exception as exc:
        emit(status='failed', reason=f'{type(exc).__name__}: {str(exc)[:900]}')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
