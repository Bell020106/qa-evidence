"""Bounded Android UI tests on one explicitly selected ADB device and package."""
import copy
import json
import math
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import platform
import shlex
import socket
import subprocess
import tempfile
import time
import uuid


SERIAL = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}\Z')
PACKAGE = re.compile(r'[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+\Z')
INPUT = re.compile(r'[A-Za-z0-9 _.,@+\-]{0,200}\Z')


def _fields(value, allowed, required):
    if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
        raise ValueError('Missing or unsupported Android fields')


def _text(value, maximum=200):
    if not isinstance(value, str) or not value or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise ValueError('Invalid Android text field')


def validate_scenario(value):
    fields = ('android_scenario_version', 'id', 'title', 'serial', 'package', 'activity', 'steps', 'checks')
    _fields(value, fields, fields)
    if type(value['android_scenario_version']) is not int or value['android_scenario_version'] != 1:
        raise ValueError('Unsupported Android scenario version')
    if len(json.dumps(value, ensure_ascii=True).encode()) > 128 * 1024:
        raise ValueError('Android scenario too large')
    for field in ('id', 'title', 'serial', 'package', 'activity'):
        _text(value[field])
    if not SERIAL.fullmatch(value['serial']) or not PACKAGE.fullmatch(value['package']):
        raise ValueError('Invalid device serial or package')
    activity = value['activity']
    if not re.fullmatch(r'\.?[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*', activity):
        raise ValueError('Invalid activity')
    if not activity.startswith('.') and not activity.startswith(value['package'] + '.'):
        raise ValueError('Activity must belong to selected package')
    for group, limit in (('steps', 50), ('checks', 20)):
        if not isinstance(value[group], list) or not 1 <= len(value[group]) <= limit:
            raise ValueError('Android steps/checks count out of range')
        for item in value[group]:
            keys = ('action', 'locator', 'target', 'value') if group == 'steps' else ('kind', 'locator', 'target', 'expected')
            _fields(item, keys, keys[:3])
            if item['locator'] not in ('content_desc', 'resource_id'):
                raise ValueError('Unsupported Android locator')
            _text(item['target'])
            if item['locator'] == 'resource_id' and not re.fullmatch(re.escape(value['package']) + r':id/[A-Za-z_][A-Za-z0-9_]*', item['target']):
                raise ValueError('Resource ID must belong to selected app')
            if group == 'steps':
                if item['action'] not in ('wait', 'fill', 'tap'):
                    raise ValueError('Unsupported Android action')
                if item['action'] == 'fill':
                    if not isinstance(item.get('value'), str) or not INPUT.fullmatch(item['value']):
                        raise ValueError('Input supports at most 200 ASCII letters, digits, spaces and _.,@+-; percent/Unicode/shell syntax unsupported')
                elif 'value' in item:
                    raise ValueError('Only fill accepts a value')
            else:
                kind = item['kind']; expected = item.get('expected')
                if kind == 'text':
                    if not isinstance(expected, str) or len(expected) > 200:
                        raise ValueError('Text check requires expected text')
                elif kind == 'visible':
                    if type(expected) is not bool:
                        raise ValueError('Visible check requires boolean expected')
                elif kind == 'text_length':
                    if type(expected) is not int or not 0 <= expected <= 10000:
                        raise ValueError('Length check requires expected integer')
                else:
                    raise ValueError('Unsupported Android check')
    return copy.deepcopy(value)


def parse_devices(text):
    devices = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and SERIAL.fullmatch(parts[0]) and parts[1] in ('device', 'unauthorized', 'offline', 'no'):
            devices.append({'serial': parts[0], 'state': parts[1] if parts[1] != 'no' else 'no_permissions',
                            'model': next((p[6:] for p in parts[2:] if p.startswith('model:')), '')})
    return devices


def hierarchy_nodes(xml, package):
    if len(xml.encode()) > 2 * 1024 * 1024 or '<!DOCTYPE' in xml.upper() or '<!ENTITY' in xml.upper():
        raise ValueError('Unsafe or oversized UI hierarchy')
    start = xml.find('<hierarchy'); end = xml.find('</hierarchy>')
    if start < 0 or end < start:
        raise ValueError('UI hierarchy unavailable')
    try:
        nodes = list(ET.fromstring(xml[start:end + len('</hierarchy>')]).iter('node'))
    except ET.ParseError as exc:
        raise ValueError('Malformed UI hierarchy') from exc
    if len(nodes) > 5000:
        raise ValueError('UI hierarchy has too many nodes')
    return [dict(node.attrib) for node in nodes if node.get('package') == package]


def find_node(xml, package, locator, target):
    attribute = {'content_desc': 'content-desc', 'resource_id': 'resource-id'}[locator]
    nodes = [node for node in hierarchy_nodes(xml, package) if node.get(attribute) == target]
    if len(nodes) > 1:
        raise ValueError('Ambiguous Android element')
    return nodes[0] if nodes else None


def scoped_logs(raw, pid, start, end):
    if not all(math.isfinite(v) for v in (start, end)) or end < start:
        raise ValueError('Invalid Android log time window')
    lines = []
    for line in raw.splitlines():
        match = re.match(r'^\s*(\d+\.\d+)\s+(\d+)\s+\d+\s', line)
        if match and int(match[2]) == pid and start <= float(match[1]) <= end:
            lines.append(line)
    return '\n'.join(lines) + ('\n' if lines else '')


class Cancelled(ValueError):
    pass


class ADBCommand:
    """Bound output and lifetime of owned clients; never start/stop an ADB server."""
    def __init__(self, executable, cancelled=lambda: False):
        self.executable = str(Path(executable).resolve())
        self.cancelled = cancelled

    def __call__(self, args, timeout=12, maximum=2*1024*1024):
        # Explicit host disables adb's local server auto-start path. Require the
        # separately prepared server; its lifecycle never belongs to this worker.
        try:
            with socket.create_connection(('127.0.0.1', 5037), timeout=1):
                pass
        except OSError as exc:
            raise ValueError('ADB server unavailable; start adb start-server during device preparation') from exc
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as error:
            process = subprocess.Popen([self.executable, '-H', '127.0.0.1', '-P', '5037', *args],
                                       stdin=subprocess.DEVNULL, stdout=output, stderr=error,
                                       shell=False, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            started = time.monotonic()
            try:
                while process.poll() is None:
                    if self.cancelled():
                        raise Cancelled('Android execution cancelled')
                    if time.monotonic() - started > timeout:
                        raise ValueError('ADB command timeout')
                    if output.tell() > maximum or error.tell() > 16384:
                        raise ValueError('ADB output limit exceeded')
                    time.sleep(.04)
                output.seek(0); data = output.read(maximum + 1)
                error.seek(0); reason = error.read(16385)
                if len(data) > maximum or len(reason) > 16384:
                    raise ValueError('ADB output limit exceeded')
                if process.returncode:
                    raise ValueError('ADB command failed: ' + reason.decode('utf-8', 'replace')[:300])
                return data
            finally:
                if process.poll() is None:
                    process.terminate()
                    try: process.wait(timeout=2)
                    except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=2)


def _atomic_json(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def run_scenario(config, output, command, cancelled=lambda: False):
    config = validate_scenario(config)
    output = Path(output); output.mkdir(parents=True, exist_ok=True)
    path = output / 'evidence.json'
    if path.exists():
        raise ValueError('Android output already contains evidence; use a new execution directory')
    package = config['package']; serial = config['serial']
    started = time.monotonic(); pid = None; epoch_start = None; ready = False
    result = {'contract_version': 'android-1', 'execution_id': str(uuid.uuid4()),
              'started_at': datetime.now(timezone.utc).isoformat(), 'tc_id': config['id'],
              'android_scenario': config, 'target': {'kind': 'android', 'serial': serial, 'package': package, 'activity': config['activity']},
              'environment': {'adapter': 'adb-ui-1', 'host_platform': platform.platform()},
              'result': {'status': 'running', 'phase': 'preparation', 'message': ''},
              'steps': [{**step, 'status': 'not_run'} for step in config['steps']],
              'checks': [{**check, 'status': 'not_run', 'actual': None} for check in config['checks']],
              'evidence': {name: {'status': 'not_collected', 'reason': 'Not attempted'} for name in ('screenshot', 'hierarchy', 'log')},
              'cleanup': {'status': 'not_needed', 'errors': [], 'app_state': 'preserved; app remains on device'}}
    def save(): _atomic_json(path, result)
    def run(*args, maximum=2*1024*1024):
        if cancelled(): raise Cancelled('Android execution cancelled')
        if time.monotonic()-started > 180: raise ValueError('Android execution timeout')
        return command(['-s', serial, *args], maximum=maximum)
    def shell(*tokens):
        return run('shell', ' '.join(shlex.quote(str(t)) for t in tokens)).decode('utf-8', 'replace')
    def foreground():
        text = shell('dumpsys', 'window')
        if not re.search(r'mCurrentFocus=[^\r\n]*\s' + re.escape(package) + r'/', text):
            raise ValueError('Selected app is not foreground; Android input/capture stopped')
        if pid is not None and shell('pidof', package).strip() != str(pid):
            raise ValueError('Selected app process changed during execution')
    def hierarchy():
        foreground()
        xml = run('exec-out', 'uiautomator', 'dump', '/dev/tty').decode('utf-8', 'replace')
        foreground()
        hierarchy_nodes(xml, package)
        return xml
    def locate(item, wait=False):
        until = time.monotonic()+15
        while True:
            node = find_node(hierarchy(), package, item['locator'], item['target'])
            if node is not None or not wait or time.monotonic() >= until: return node
            time.sleep(.2)
    def tap(node):
        if node.get('enabled') != 'true': raise ValueError('Android element disabled')
        bounds = re.fullmatch(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', node.get('bounds',''))
        if not bounds: raise ValueError('Invalid Android element bounds')
        x1,y1,x2,y2 = map(int,bounds.groups())
        if not (0<=x1<x2<=10000 and 0<=y1<y2<=10000): raise ValueError('Android bounds out of range')
        foreground(); shell('input','tap',(x1+x2)//2,(y1+y2)//2)
    def attach(name, data, filename):
        (output/filename).write_bytes(data)
        result['evidence'][name] = {'status':'collected','path':filename,'size':len(data),'sha256':hashlib.sha256(data).hexdigest()}
    save()
    try:
        if run('get-state').decode().strip() != 'device': raise ValueError('Selected device not ready')
        if not shell('pm','path',package).strip().startswith('package:'): raise ValueError('Selected app not installed')
        details = shell('dumpsys','package',package)
        for key, pattern in [('app_version_name',r'versionName=([^\s]+)'),('app_version_code',r'versionCode=(\d+)')]:
            match=re.search(pattern,details)
            if not match: raise ValueError('App version unavailable')
            result['environment'][key]=match[1]
        for key,prop in [('device_model','ro.product.model'),('android_release','ro.build.version.release'),('android_sdk','ro.build.version.sdk')]:
            result['environment'][key]=shell('getprop',prop).strip()
        epoch_start=float(shell('date','+%s.%N').strip())
        if not math.isfinite(epoch_start): raise ValueError('Device clock unavailable')
        launch=shell('am','start','-W','-n',package+'/'+config['activity'])
        if 'Error' in launch or 'Status: ok' not in launch: raise ValueError('Android activity launch failed')
        raw_pid=shell('pidof',package).strip()
        if not re.fullmatch(r'[1-9]\d*',raw_pid): raise ValueError('App PID unavailable or multiple processes unsupported')
        pid=int(raw_pid);result['environment']['app_pid']=pid
        result['environment'].update({'serial':serial,'package':package,'activity':config['activity']})
        ready=True;result['result']['phase']='execution';save()
        for step in result['steps']:
            step['status']='running';save()
            node=locate(step,wait=True)
            if node is None: raise ValueError('Android element not found: '+step['target'])
            if step['action']=='tap': tap(node)
            if step['action']=='fill':
                if node.get('password')=='true' or not INPUT.fullmatch(node.get('text','')):
                    raise ValueError('Existing input cannot be safely cleared')
                tap(node)
                if node.get('text'):
                    foreground();shell('input','keyevent','123',*(['67']*len(node['text'])))
                cleared=locate(step)
                if cleared is None or cleared.get('text')!='':
                    raise ValueError('Input clearing could not be verified; UI hint text is indistinguishable from existing text with this adapter')
                foreground()
                if step['value']: shell('input','text',step['value'].replace(' ','%s'))
            step['status']='completed';save()
        for check in result['checks']:
            node=locate(check)
            if check['kind']=='visible': actual=node is not None
            elif node is None: raise ValueError('Android check element not found: '+check['target'])
            elif check['kind']=='text': actual=node.get('text','')
            else: actual=len(node.get('text',''))
            check.update(actual=actual,status='passed' if actual==check['expected'] else 'failed');save()
        result['result'].update(status='passed' if all(c['status']=='passed' for c in result['checks']) else 'failed',phase='assertion')
    except Cancelled as exc:
        result['result'].update(status='cancelled',message=str(exc))
    except (ValueError,OSError,subprocess.SubprocessError) as exc:
        result['result'].update(status='execution_error' if ready else 'preparation_failed',message=str(exc)[:500])
    for step in result['steps']:
        if step['status']=='running':step['status']='cancelled' if result['result']['status']=='cancelled' else 'error'
    if ready and not cancelled():
        for name in ('screenshot','hierarchy','log'):
            try:
                if name=='screenshot':
                    foreground();data=run('exec-out','screencap','-p',maximum=10*1024*1024);foreground()
                    if not data.startswith(b'\x89PNG\r\n\x1a\n'): raise ValueError('Invalid screenshot PNG')
                    attach(name,data,'screenshot.png')
                elif name=='hierarchy':
                    nodes=hierarchy_nodes(hierarchy(),package)
                    root=ET.Element('hierarchy')
                    for node in nodes: ET.SubElement(root,'node',node)
                    attach(name,ET.tostring(root,encoding='utf-8'),'hierarchy.xml')
                else:
                    epoch_end=float(shell('date','+%s.%N').strip())
                    raw=shell('logcat','-d','-v','epoch','--pid='+str(pid),'-t','2000')
                    data=scoped_logs(raw,pid,epoch_start,epoch_end).encode()
                    if len(data)>1024*1024: raise ValueError('App log exceeds evidence limit')
                    attach(name,data,'app.log')
                    result['evidence'][name].update(pid=pid,start_epoch=epoch_start,end_epoch=epoch_end,scope='selected app PID; execution window; latest 2000 log records only')
            except (ValueError,OSError,subprocess.SubprocessError) as exc:
                result['evidence'][name]={'status':'not_collected','reason':str(exc)[:300]}
    result['finished_at']=datetime.now(timezone.utc).isoformat();save()
    return result
