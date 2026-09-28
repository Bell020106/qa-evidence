"""Offline browser session and a JSON-lines child process protocol."""
import argparse
import json
from pathlib import Path
from queue import Queue, Empty
import sys
from threading import Thread
from urllib.parse import urlsplit
from uuid import uuid4

from signup031.archive import load_archive


class ReplaySession:
    def __init__(self, root, *, headless=False, notify=None):
        self.manifest, self.har_path = load_archive(root)
        self.headless = headless
        self.notify = notify or (lambda event: None)
        self.blocked = []
        self.playwright = self.browser = self.context = self.page = None
        self.restored = False
        self.recording_limitations = []
        self.investigation = None

    def __enter__(self):
        from playwright.sync_api import sync_playwright
        try:
            self.playwright = sync_playwright().start()
            self.browser = self.playwright.chromium.launch(headless=self.headless)
            self.context = self.browser.new_context(service_workers='block', offline=True,
                                                    accept_downloads=False)
            # HAR fallback never reaches network; registration order is intentional.
            self.context.route('**/*', self._unrecorded)
            self.context.route_from_har(self.har_path, not_found='fallback', update=False)
            self.context.route_web_socket('**/*', self._websocket)
            self.page = self.context.new_page()
            return self
        except Exception:
            self.__exit__(None, None, None)
            raise

    def _block(self, kind, url=''):
        parsed = urlsplit(url)
        self.blocked.append({'kind': kind, 'reason': '기록되지 않은 요청',
                             'resource': parsed.netloc + parsed.path})
        self.notify(self.status())

    def _unrecorded(self, route):
        self._block('http', route.request.url)
        route.abort('blockedbyclient')

    def _websocket(self, socket):
        self._block('websocket', socket.url)
        # Do not connect_to_server: the intercepted socket has no network peer.
        # Context disposal closes it; synchronous close here deadlocks in 1.62.

    def restore(self):
        if self.manifest['archive_version'] == 1:
            from signup031.legacy_replay import restore_legacy
            restore_legacy(self.page, self.manifest)
        elif self.manifest['archive_version'] == 4:
            from signup031.selenium_replay import restore_selenium
            self.recording_limitations = restore_selenium(self.page, self.manifest['selenium_record'])
        elif self.manifest['archive_version'] == 3:
            from signup031.manual_replay import restore_manual
            self.recording_limitations = restore_manual(self.page, self.manifest['manual_record'])
        else:
            from signup031.web_scenario import execute_scenario
            actual = execute_scenario(self.page, self.manifest['scenario_snapshot'])
            if actual != self.manifest['observed_checks']:
                raise ValueError('restored checks differ from recorded observations')
        self.restored = True
        return self.status()

    def status(self):
        if self.investigation is not None:
            state=dict(self.investigation)
            if self.blocked and state['state']=='ready':state['state']='partial'
            state['blocked']=list(self.blocked)
            return {'status':'investigation','restored':state['state']=='ready',
                    'investigation':state,'blocked_count':len(self.blocked),'blocked':list(self.blocked)}
        return {'status': 'limited' if self.blocked or self.recording_limitations else ('ready' if self.restored else 'starting'),
                'reason': '; '.join(self.recording_limitations) if self.recording_limitations else ('기록되지 않은 요청 차단' if self.blocked else None),
                'restored': self.restored, 'blocked_count': len(self.blocked),
                'blocked': list(self.blocked),
                'observed_length': self.manifest.get('observed', {}).get('after_extra_length') if self.restored else None}

    def investigate(self):
        """Preserve usable offline pages after action failures, without changing strict restore."""
        from playwright.sync_api import Error
        version=self.manifest['archive_version']
        record=self.manifest.get('selenium_record') or self.manifest.get('manual_record') or self.manifest.get('scenario_snapshot') or {}
        actions=record.get('actions',record.get('steps',[]))
        self.investigation={'session_id':uuid4().hex,'state':'starting','phase':'navigation',
            'completed_actions':0,'total_actions':len(actions),'stopped_action':None,'reason':'',
            'limitations':list(record.get('limitations',[])),'blocked':[]}
        def progress(**event):self.investigation.update(event)
        try:
            if version==4:
                from signup031.selenium_replay import restore_selenium
                limits=restore_selenium(self.page,record,progress=progress,raise_errors=True)
            elif version==3:
                from signup031.manual_replay import restore_manual
                limits=restore_manual(self.page,record,progress=progress)
            elif version==2:
                from signup031.web_scenario import execute_scenario
                observed=execute_scenario(self.page,record,progress=progress)
                limits=[] if observed==self.manifest['observed_checks'] else ['저장 당시 관측 결과와 다릅니다.']
            else:
                from signup031.legacy_replay import restore_legacy
                restore_legacy(self.page,self.manifest);limits=[]
            self.investigation.update(state='partial' if limits or self.blocked else 'ready',phase='complete',limitations=limits)
        except (Error,ValueError) as exc:
            self.investigation.update(state='partial',reason=f'{type(exc).__name__}: {str(exc)[:1500]}')
        # An allocated page, blank response, browser error page, or closed browser is not a usable investigation.
        if (self.page.is_closed() or not self.browser.is_connected() or
            urlsplit(self.page.url).scheme not in ('http','https') or
            not self.page.locator('body').count() or not self.page.locator('body').inner_html(timeout=1000).strip()):
            self.investigation.update(state='unavailable',reason='저장된 문서가 없거나 브라우저가 닫혔습니다. '+self.investigation['reason'])
            self.restored=False
            self.notify(self.status())
            raise ValueError('조사 화면을 열 수 없습니다. 저장된 문서가 없거나 브라우저가 닫혔습니다. '+self.investigation['reason'])
        self.restored=self.investigation['state']=='ready'
        return self.status()

    def __exit__(self, *args):
        if self.context:
            try:
                # Detach pending handlers before disposing HAR and the context.
                # Waiting here can deadlock a sync route callback during shutdown.
                # offline=True remains active after removal, so no network fallback.
                self.context.unroute_all(behavior='ignoreErrors')
            except Exception:
                pass
        for resource, method in ((self.context, 'close'), (self.browser, 'close'),
                                 (self.playwright, 'stop')):
            if resource:
                try:
                    getattr(resource, method)()
                except Exception:
                    pass


def wait_for_manual_session(page, commands):
    from playwright.sync_api import Error
    while not page.is_closed():
        try:
            if commands.get_nowait() == 'stop':
                break
        except Empty:
            pass
        try:
            page.wait_for_timeout(100)
        except Error:
            if page.is_closed() or not page.context.browser.is_connected():
                break
            raise


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('archive', type=Path)
    parser.add_argument('--headless', action='store_true')
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--investigate', action='store_true')
    args = parser.parse_args(argv)
    if args.check and args.investigate:parser.error('--check and --investigate cannot be combined')
    def emit(event):
        print(json.dumps(event, ensure_ascii=True), flush=True)
    commands = Queue()
    def read_commands():
        for line in sys.stdin:
            commands.put(line.strip())
        commands.put('stop')
    try:
        emit({'status': 'starting'})
        with ReplaySession(args.archive, headless=args.headless, notify=emit) as replay:
            result = replay.investigate() if args.investigate else replay.restore()
            emit(result)
            if args.check:
                return 0 if result['status'] == 'ready' else 2
            Thread(target=read_commands, daemon=True).start()
            wait_for_manual_session(replay.page, commands)
        emit({'status': 'closed'})
        return 0
    except Exception as exc:
        # Playwright errors can include page URLs; never emit response bodies.
        emit({'status': 'failed', 'reason': f'{type(exc).__name__}: {str(exc)[:800]}'})
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
