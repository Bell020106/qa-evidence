"""Capture original local Chromium WebDriver sessions before the first navigation.

No test is re-executed. CDP response bodies remain in memory until capture; only
sanitized GET/HEAD entries are written. The caller continues to own driver.quit().
"""
import base64
import hashlib
import json
from pathlib import Path
import platform
import re
from urllib.parse import urlsplit, quote

from signup031.contract import utc_now
from signup031.storage import create_run_directory, write_evidence
from signup031.manual_recording import validate_manual_record


def recording_options(*, headless=False, options=None):
    try:
        from selenium import webdriver
    except ImportError as exc:
        raise RuntimeError('Selenium 선택 의존성을 설치하세요: pip install -e ".[gui,selenium]"') from exc
    options = options if options is not None else webdriver.ChromeOptions()
    if headless:
        options.add_argument('--headless=new')
    options.set_capability('goog:loggingPrefs', {**options.capabilities.get('goog:loggingPrefs',{}),'performance': 'ALL','browser':'ALL'})
    return options


class SeleniumRecorder:
    def __init__(self, driver, artifacts_root, nodeid, *, max_body_bytes=10000000, max_total_bytes=50000000, test_context=None):
        from signup031.test_context import validate_context
        self.test_context=validate_context(test_context) if test_context is not None else None
        if any(type(value) is not int or value <= 0 for value in (max_body_bytes, max_total_bytes)):
            raise ValueError('본문 수집 상한은 양의 정수여야 합니다')
        self.max_body_bytes, self.max_total_bytes = max_body_bytes, max_total_bytes
        self.driver, self.nodeid = driver, nodeid
        self.execution_id, self.root = create_run_directory(Path(artifacts_root))
        self.archive = self.root / 'archive'
        self.archive.mkdir()
        self.started = utc_now()
        self.original_execute = driver.execute
        self.busy = False
        self.captured = False
        self.closed = False
        self.finished = False
        self.limits, self.errors, self.secrets = [], [], set()
        from signup031.timeline import Timeline
        self.timeline=Timeline(self.execution_id,'selenium-cdp');self.timeline_browser_logs=True
        self.requests, self.responses, self.bodies = {}, {}, {}
        self.snapshot = None
        self.url = None
        self.document_id = None
        self.total_body_bytes = 0
        self.environment = {'browser': driver.capabilities.get('browserName', '미수집'),
                            'browser_version': driver.capabilities.get('browserVersion', '미수집'),
                            'driver_version': driver.capabilities.get('chrome', {}).get('chromedriverVersion', '미수집'),
                            'python': platform.python_version(), 'platform': platform.platform()}
        try:
            if self.environment['browser'] not in ('chrome', 'chromium'):
                raise ValueError('로컬 Chrome/Chromium만 지원합니다')
            if driver.current_url not in ('about:blank', 'data:,'):
                raise ValueError('첫 탐색 전에 selenium_record(driver)를 연결하세요')
            driver.get_log('performance')  # Fails explicitly if logging wasn't enabled at driver creation.
            driver.execute_cdp_cmd('Network.enable', {'maxTotalBufferSize': 50000000,
                'maxResourceBufferSize': 10000000, 'enableDurableMessages': True})
            script = 'window.__qaManualLimit=()=>Promise.resolve();\n' + Path(__file__).with_name('manual_capture.js').read_text(encoding='utf-8-sig')
            driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument', {'source': script})
            driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument', {'source':Path(__file__).with_name('timeline_capture.js').read_text(encoding='utf-8')})
            self.timeline.availability.update(network='collected',action='collected')
            driver.execute = self.execute
        except Exception as exc:
            self.errors.append('녹화 준비 실패: ' + str(exc)[:1200] + ' · recording_options()와 맞는 Chrome/ChromeDriver 설치를 확인하세요')

    def limit(self, reason):
        if reason not in self.limits:
            self.limits.append(reason)

    def redact(self, value):
        if isinstance(value, str):
            variants = {variant for secret in self.secrets if secret
                        for variant in (secret, quote(secret, safe=''))}
            if not variants:
                return value
            pattern = r'\[REDACTED\]|' + '|'.join(re.escape(item) for item in sorted(variants, key=len, reverse=True))
            return re.sub(pattern, lambda match: '[REDACTED]', value)
        if isinstance(value, dict):
            return {key: self.redact(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.redact(item) for item in value]
        return value

    def execute(self, command, params=None):
        if self.busy or self.captured:
            return self.original_execute(command, params)
        self.busy = True
        try:
            # Persist earlier document restrictions before a navigation/quit destroys it.
            self.observe()
            if command in ('goBack', 'goForward', 'refresh', 'switchToFrame', 'switchToParentFrame',
                           'newWindow', 'switchToWindow', 'actions', 'clearActionState',
                           'w3cExecuteScript', 'w3cExecuteScriptAsync', 'addCookie', 'deleteCookie', 'deleteAllCookies',
                           'submitElement', 'clearElement', 'acceptAlert', 'dismissAlert', 'executeCdpCommand',
                           'setWindowRect', 'maximizeWindow', 'minimizeWindow', 'fullscreenWindow'):
                self.limit('unsupported WebDriver command: ' + command)
            result = self.original_execute(command, params)
            if command not in ('quit', 'close'):
                self.observe()
            else:
                self.closed = True
                self.limit('driver closed before failure capture')
                self.driver.execute = self.original_execute
            return result
        finally:
            self.busy = False

    def observe(self):
        # Instrumentation must not change a WebDriver command's exception/result.
        try:
            self.drain_network()
            if len(self.driver.window_handles) != 1:
                self.limit('new tab / multiple tabs are not recorded')
            state = self.driver.execute_script('''
                const sensitive=e=>e.matches('input[type=password]')||e.closest('[data-sensitive],[data-private]')||/password|cc-|one-time-code/.test(e.autocomplete||'');
                const secrets=[...document.querySelectorAll('input,textarea')].filter(sensitive).map(e=>e.value).filter(Boolean);
                return {snapshot:window.__qaManual ? window.__qaManual.snapshot(false):null,secrets};''')
            self.secrets.update(state['secrets'])
            self.timeline.learn(state['secrets'])
            timeline_state=self.driver.execute_script('return window.__qaTimeline ? window.__qaTimeline.drain():null')
            if timeline_state:
                if timeline_state.get('secret_limit_exceeded') is True:self.timeline.secret_limit()
                if timeline_state.get('unsupported_frames') is True:self.timeline.unsupported_frame()
                self.timeline.learn(timeline_state['secrets'])
                for event in timeline_state['events']:self.timeline.js_event(event)
                self.timeline.dropped+=timeline_state['dropped']
            if self.timeline_browser_logs:
                try:
                    logs=self.driver.get_log('browser');self.timeline.availability.update(console='collected',pageerror='collected')
                    for log in logs:
                        self.timeline.add('pageerror' if log.get('source')=='javascript' else 'console',
                            {'level':log.get('level',''),'text':log.get('message','')},source_clock='browser-log-wall:'+self.execution_id,
                            source_time=log.get('timestamp'),source_unit='ms')
                except Exception:self.timeline_browser_logs=False
            if state['secrets']:
                self.limit('sensitive input omitted; screenshot excluded')
            snapshot = state['snapshot']
            if snapshot is not None:
                if self.document_id and snapshot['document_id'] != self.document_id:
                    self.limit('document navigation: earlier document actions unavailable')
                self.document_id = snapshot['document_id']
                if self.url is None:
                    self.url = snapshot['final_url']
                self.snapshot = snapshot
                for reason in snapshot['limitations']:
                    self.limit(reason)
        except Exception as exc:
            self.limit('observation unavailable: ' + type(exc).__name__)

    def _sensitive_headers(self, headers):
        for key, value in headers.items():
            if key.lower() in ('authorization', 'proxy-authorization', 'cookie', 'set-cookie'):
                self.limit('authentication/cookie headers removed')
                if key.lower() in ('authorization', 'proxy-authorization'):
                    self.secrets.add(str(value))
                    self.secrets.add(str(value).split(' ', 1)[-1])
                else:
                    for part in str(value).split(';'):
                        if '=' in part:
                            name, cookie = part.strip().split('=', 1)
                            if cookie and name.lower() not in ('path', 'domain', 'expires', 'max-age', 'samesite'):
                                self.secrets.add(cookie)

    def drain_network(self):
        for row in self.driver.get_log('performance'):
            event = json.loads(row['message'])['message']
            self.timeline.cdp_event(event)
            method, params = event['method'], event['params']
            request_id = params.get('requestId')
            if method == 'Network.requestWillBeSent':
                req = params['request']
                self._sensitive_headers(req.get('headers', {}))
                self.requests[request_id] = {'url': req['url'], 'method': req['method']}
                if req['method'] not in ('GET', 'HEAD'):
                    self.limit('unsupported HTTP method: ' + req['method'])
                if params.get('redirectResponse'):
                    self.limit('redirect chain not fully captured')
            elif method == 'Network.responseReceived':
                response = params['response']
                self._sensitive_headers(response.get('headers', {}))
                self.responses[request_id] = response
                if response.get('fromServiceWorker'):
                    self.limit('service worker response: worker state not restored')
            elif method in ('Network.responseReceivedExtraInfo', 'Network.requestWillBeSentExtraInfo'):
                self._sensitive_headers(params.get('headers', {}))
            elif method == 'Network.loadingFinished' and request_id in self.responses:
                req = self.requests.get(request_id, {})
                if req.get('method') not in ('GET', 'HEAD'):
                    continue
                try:
                    body = self.driver.execute_cdp_cmd('Network.getResponseBody', {'requestId': request_id})
                    data = base64.b64decode(body['body']) if body['base64Encoded'] else body['body'].encode('utf-8')
                    if len(data) > self.max_body_bytes:
                        self.limit('per-response capture byte limit exceeded; body omitted')
                        continue
                    if self.total_body_bytes + len(data) > self.max_total_bytes:
                        self.limit('total-response capture byte limit exceeded; body omitted')
                        continue
                    self.total_body_bytes += len(data)
                    self.bodies[request_id] = data
                except Exception as exc:
                    self.limit('HTTP response body missing: ' + type(exc).__name__)
            elif method == 'Network.loadingFailed':
                self.limit('HTTP request failed: ' + params.get('errorText', 'unknown'))
            elif method.startswith('Network.webSocket'):
                self.limit('WebSocket state not supported')

    def har(self):
        entries = []
        for request_id, response in self.responses.items():
            req = self.requests.get(request_id, {})
            if req.get('method') not in ('GET', 'HEAD') or urlsplit(req.get('url', '')).scheme not in ('http', 'https'):
                continue
            if request_id not in self.bodies:
                self.limit('HTTP response body missing: ' + self.redact(req['url']))
                continue
            body = self.bodies[request_id]
            if any(secret.encode('utf-8') in body for secret in self.secrets):
                self.limit('response containing sensitive data excluded')
                continue
            headers = [{'name': key, 'value': str(value)} for key, value in response.get('headers', {}).items()
                       if key.lower() not in ('authorization', 'proxy-authorization', 'cookie', 'set-cookie',
                                              'content-encoding', 'content-length', 'transfer-encoding')]
            entries.append({'startedDateTime': utc_now(), 'time': 0,
                'request': {'method': req['method'], 'url': self.redact(req['url']), 'httpVersion': 'HTTP/1.1',
                            'headers': [], 'queryString': [], 'cookies': [], 'headersSize': -1, 'bodySize': 0},
                'response': {'status': int(response['status']), 'statusText': response.get('statusText', ''),
                    'httpVersion': 'HTTP/1.1', 'headers': self.redact(headers), 'cookies': [],
                    'content': {'size': len(body), 'mimeType': response.get('mimeType', 'application/octet-stream'),
                                'text': base64.b64encode(body).decode('ascii'), 'encoding': 'base64'},
                    'redirectURL': '', 'headersSize': -1, 'bodySize': len(body)},
                'cache': {}, 'timings': {'send': 0, 'wait': 0, 'receive': 0}})
        return {'log': {'version': '1.2', 'creator': {'name': 'Selenium CDP original session', 'version': '1'}, 'entries': entries}}

    def capture(self, report):
        if self.captured:
            return
        self.busy = True
        self.test_identity = {'nodeid': self.nodeid, 'outcome': report.outcome, 'phase': report.when}
        self.screenshot = {'status': 'not_collected', 'path': 'archive/page.png', 'reason': None}
        try:
            if self.closed:
                raise ValueError('driver already closed before failure capture')
            self.observe()
            if self.snapshot is None or self.url is None:
                raise ValueError('첫 탐색 이후 DOM/동작 자료를 수집하지 못했습니다')
            state = self.driver.execute_script('return window.__qaManual.snapshot(true)')
            self.snapshot = state
            dom = self.driver.execute_script('''const clone=document.documentElement.cloneNode(true);
                clone.querySelectorAll('input[type=password],[data-sensitive],[data-private]').forEach(e=>{e.removeAttribute('value');e.textContent='';});
                return clone.outerHTML;''')
            (self.archive / 'page.html').write_text(self.redact(dom), encoding='utf-8')
            if self.secrets:
                self.screenshot['reason'] = 'sensitive data: screenshot excluded'
                self.limit('sensitive data: screenshot excluded')
            else:
                if not self.driver.save_screenshot(str(self.archive / 'page.png')):
                    raise ValueError('screenshot capture failed')
                self.screenshot['status'] = 'collected'
            self.drain_network()
        except Exception as exc:
            self.errors.append('failure-time capture failed: ' + type(exc).__name__ + ': ' + str(exc)[:800])
            self.screenshot['reason'] = self.redact(self.errors[-1])
        finally:
            self.captured = True
            self.busy = False
            self.driver.execute = self.original_execute

    def finish(self, reports, session_exitcode):
        if self.finished:
            return self.root / 'evidence.json'
        if not self.captured:
            raise ValueError('failure-time capture hook did not run')
        har = self.har()
        state = self.snapshot or {'actions': [], 'observed': {'inputs': [], 'text': ''}, 'final_url': self.url}
        if self.url is None:
            self.limit('start URL unavailable')
        record = self.redact({'title': self.nodeid, 'start_url': self.url,
                             'final_url': state['final_url'], 'actions': state['actions'],
                             'observed': state['observed'], 'limitations': self.limits})
        if not har['log']['entries']:
            self.errors.append('no captured GET/HEAD response bodies')
        capture_status = 'failed' if self.errors else ('limited' if self.limits else 'saved')
        nodeid = self.redact(self.nodeid)
        if nodeid != self.nodeid:
            # Preserve distinct parameterized cases after masking their recognized secrets.
            nodeid += ' [sha256:' + hashlib.sha256(self.nodeid.encode('utf-8')).hexdigest() + ']'
        self.test_identity = {**self.test_identity, 'nodeid': nodeid}
        write_evidence(self.archive / 'resources.har', har)
        resources = [{'path': 'resources.har', 'sha256': hashlib.sha256((self.archive / 'resources.har').read_bytes()).hexdigest()}]
        attachments = [{'path': name, 'sha256': hashlib.sha256((self.archive / name).read_bytes()).hexdigest()}
                       for name in ('page.html', 'page.png') if (self.archive / name).is_file()]
        manifest = {'archive_version': 4, 'execution_id': self.execution_id, 'navigation_url': record['start_url'],
                    'captured_at': utc_now(), 'status': 'failed' if self.errors else 'recorded',
                    'reason': self.redact('; '.join(self.errors)) or None, 'replay_verification': 'not_run',
                    'selenium_record': record, 'test_identity': self.test_identity,
                    'resources': resources, 'attachments': attachments}
        write_evidence(self.archive / 'manifest.json', manifest)
        original = next((row for row in reports if row['outcome'] == 'failed'),
                        next((row for row in reports if row['when'] == self.test_identity['phase']), {}))
        outcome = original.get('outcome', self.test_identity['outcome'])
        phase = original.get('when', self.test_identity['phase'])
        if phase=='teardown' and self.test_identity['phase']!='teardown':
            record['limitations'].append('teardown 실패 이전의 마지막 관측 자료 · 실패 순간 화면 아님')
            if not self.errors:capture_status='limited'
            manifest['selenium_record']=record
            write_evidence(self.archive/'manifest.json',manifest)
        status = 'preparation_failed' if outcome == 'failed' and phase == 'setup' else outcome
        message = self.redact(original.get('message', 'pytest ' + outcome))
        payload = {'contract_version': '4', 'tc_id': nodeid,
                   'execution': {'id': self.execution_id, 'started_at': self.started},
                   'environment': self.environment, 'test_identity': self.test_identity, 'selenium_record': record,
                   'result': {'business': {'status': status, 'phase': phase, 'message': message},
                              'pytest': {'status': outcome, 'phase': phase, 'message': message,
                                         'reports': [{**row, 'message': self.redact(row.get('message', '')),
                                                     'wasxfail': self.redact(row.get('wasxfail'))} for row in reports],
                                         'session_exitcode': int(session_exitcode)}},
                   'capture': {'status': capture_status, 'errors': self.redact(self.errors),
                               'limits': {'per_response_bytes': self.max_body_bytes, 'total_response_bytes': self.max_total_bytes},
                               'source': 'original Selenium WebDriver session; CDP response bodies; no rerun'},
                   'evidence': {'screenshot': self.screenshot},
                   'replay': {'status': manifest['status'], 'manifest': 'archive/manifest.json', 'reason': manifest['reason']},
                   'post_run': {'cleanup': {'status': 'recorder_buffers_cleared', 'errors': [],
                                           'driver': 'caller_owned_not_verified'}}}
        self.timeline.learn(self.secrets)
        from signup031.timeline import save_optional
        save_optional(payload,self.timeline,self.root)
        if self.test_context is not None:
            from signup031.test_context import save_context
            try:payload['test_context']=save_context(self.root,self.execution_id,self.test_context,self.redact)
            except Exception:payload['test_context_capture']={'status':'collection_failed','reason':'명시적으로 제공한 테스트 자료 저장 실패'}
            self.test_context=None
        self.bodies.clear()
        self.responses.clear()
        self.requests.clear()
        self.secrets.clear()
        write_evidence(self.root / 'evidence.json', payload)
        self.finished = True
        return self.root / 'evidence.json'


def validate_selenium_record(record):
    return validate_manual_record(record, allow_missing_urls=True)
