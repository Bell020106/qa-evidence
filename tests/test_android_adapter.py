import copy
import pytest
import shlex

from signup031 import android_adapter as android


def scenario():
    return {'android_scenario_version': 1, 'id': 'ANDROID-1', 'title': 'Measured input',
            'serial': 'emulator-5580', 'package': 'org.qaevidence.demo', 'activity': '.MainActivity',
            'steps': [{'action': 'fill', 'locator': 'content_desc', 'target': 'qa-input', 'value': 'hello world'},
                      {'action': 'tap', 'locator': 'content_desc', 'target': 'qa-submit'}],
            'checks': [{'kind': 'text', 'locator': 'content_desc', 'target': 'qa-result', 'expected': 'length:11'}]}


@pytest.mark.parametrize('value', ['한글', '50%', 'a;reboot', '$(reboot)', 'a\nb', '"x"'])
def test_unsupported_input_rejected_before_any_device_command(value):
    config = scenario()
    config['steps'][0]['value'] = value
    with pytest.raises(ValueError):
        android.validate_scenario(config)


@pytest.mark.parametrize('field,value', [('serial', '-d'), ('package', 'org.demo;reboot'), ('activity', 'other.app.Main')])
def test_target_cannot_escape_selected_device_or_app(field, value):
    config = scenario()
    config[field] = value
    with pytest.raises(ValueError):
        android.validate_scenario(config)


def test_valid_snapshot_is_independent_and_unknown_fields_rejected():
    config = scenario()
    snapshot = android.validate_scenario(config)
    config['steps'][0]['value'] = 'changed'
    assert snapshot['steps'][0]['value'] == 'hello world'
    config['shell'] = 'reboot'
    with pytest.raises(ValueError):
        android.validate_scenario(config)


def test_device_list_distinguishes_states_and_never_infers_absent_ready():
    assert android.parse_devices('List of devices attached\none\tdevice product:x model:Pixel transport_id:1\ntwo\tunauthorized\nthree\toffline\n') == [
        {'serial': 'one', 'state': 'device', 'model': 'Pixel'},
        {'serial': 'two', 'state': 'unauthorized', 'model': ''},
        {'serial': 'three', 'state': 'offline', 'model': ''}]
    assert android.parse_devices('List of devices attached\n') == []


def test_hierarchy_rejects_other_package_ambiguous_elements_and_entities():
    xml = '<hierarchy><node package="org.qaevidence.demo" content-desc="qa-result" text="length:11" enabled="true" bounds="[1,2][11,22]"/><node package="other.app" content-desc="qa-result" text="secret"/></hierarchy>'
    node = android.find_node(xml, 'org.qaevidence.demo', 'content_desc', 'qa-result')
    assert node['text'] == 'length:11'
    with pytest.raises(ValueError):
        android.find_node(xml.replace('other.app', 'org.qaevidence.demo'), 'org.qaevidence.demo', 'content_desc', 'qa-result')
    with pytest.raises(ValueError):
        android.find_node('<!DOCTYPE x [<!ENTITY x "unsafe">]>'+xml, 'org.qaevidence.demo', 'content_desc', 'qa-result')


def test_logs_require_app_pid_and_execution_time_not_just_tag():
    raw = '99.000 42 42 I QA: old\n100.250 42 43 I QA: inside\n100.300 99 99 I QA: other\n101.250 42 42 I QA: late\n continuation secret\n'
    assert android.scoped_logs(raw, 42, 100.0, 101.0) == '100.250 42 43 I QA: inside\n'


class Device:
    def __init__(self, state='device', app=True, foreground='org.qaevidence.demo', missing=()):
        self.state, self.app, self.foreground, self.missing = state, app, foreground, missing
        self.calls = []
        self.value = ''
        self.result = 'ready'

    def __call__(self, args, **kwargs):
        self.calls.append(args)
        assert args[:2] == ['-s', 'emulator-5580']
        command = args[2:]
        if command == ['get-state']:
            if self.state != 'device': raise ValueError(self.state)
            return b'device\n'
        if command == ['exec-out', 'screencap', '-p']:
            if 'screenshot' in self.missing: raise ValueError('Screenshot missing')
            return b'\x89PNG\r\n\x1a\nimage'
        if command == ['exec-out', 'uiautomator', 'dump', '/dev/tty']:
            return ('<hierarchy>'+''.join(f'<node package="org.qaevidence.demo" content-desc="{key}" text="{text}" enabled="true" password="false" bounds="{bounds}"/>' for key,text,bounds in [
                ('qa-input',self.value,'[0,0][100,100]'),('qa-submit','Measure','[0,100][100,200]'),('qa-result',self.result,'[0,200][100,300]')])+'</hierarchy>').encode()
        tokens = shlex.split(command[1]) if command[0] == 'shell' else []
        if tokens[:2] == ['pm','path']: return b'package:/data/app/demo/base.apk' if self.app else b''
        if tokens[:2] == ['dumpsys','package']: return b'versionCode=1 minSdk=23\nversionName=1.0\n'
        if tokens[0] == 'getprop': return b'35\n'
        if tokens[:2] == ['am','start']: return b'Status: ok\n'
        if tokens[0] == 'pidof': return b'42\n'
        if tokens[0] == 'date': return b'100.500\n'
        if tokens[:2] == ['dumpsys','window']: return f'mCurrentFocus=Window{{abc u0 {self.foreground}/.MainActivity}}'.encode()
        if tokens[:2] == ['input','text']: self.value += tokens[2].replace('%s',' ');return b''
        if tokens[:2] == ['input','keyevent']:
            self.value = self.value[:max(0,len(self.value)-tokens.count('67'))]; return b''
        if tokens[:2] == ['input','tap']:
            if tokens[3] == '150': self.result=f'length:{len(self.value)}'
            return b''
        if tokens[0] == 'logcat':
            if 'log' in self.missing: raise ValueError('Log missing')
            return b'100.500 42 42 I QA: measured\n'
        raise AssertionError(command)


def test_real_adapter_records_measured_checks_and_scoped_evidence(tmp_path):
    device = Device()
    result = android.run_scenario(scenario(), tmp_path, device)
    assert result['result']['status'] == 'passed'
    assert result['checks'][0]['actual'] == 'length:11'
    assert result['environment']['app_version_name'] == '1.0'
    assert result['evidence']['log']['status'] == 'collected'
    assert result['contract_version'] == 'android-1'


@pytest.mark.parametrize('state,app', [('unauthorized',True),('offline',True),('absent',True),('device',False)])
def test_preparation_failure_never_sends_input(tmp_path,state,app):
    device=Device(state=state,app=app)
    result=android.run_scenario(scenario(),tmp_path,device)
    assert result['result']['status']=='preparation_failed'
    assert not any('input ' in ' '.join(args) for args in device.calls)
    assert all(check['status']=='not_run' for check in result['checks'])


def test_foreground_switch_stops_input_and_capture(tmp_path):
    device=Device(foreground='other.app')
    result=android.run_scenario(scenario(),tmp_path,device)
    assert result['result']['status']=='execution_error'
    assert not any('input ' in ' '.join(args) for args in device.calls)
    assert result['evidence']['screenshot']['status']=='not_collected'


def test_missing_attachments_do_not_change_measured_failure(tmp_path):
    device=Device(missing=('screenshot','log'))
    config=scenario();config['checks'][0]['expected']='length:99'
    result=android.run_scenario(config,tmp_path,device)
    assert result['result']['status']=='failed'
    assert result['checks'][0]['actual']=='length:11'
    assert result['evidence']['screenshot']['status']=='not_collected'
    assert result['evidence']['log']['status']=='not_collected'


def test_cancelled_execution_has_no_false_pass(tmp_path):
    result=android.run_scenario(scenario(),tmp_path,Device(),cancelled=lambda:True)
    assert result['result']['status']=='cancelled'
    assert all(check['status']=='not_run' for check in result['checks'])


def test_failed_clear_never_appends_to_existing_or_hint_text(tmp_path):
    device=Device();device.value='unchanged'
    original=device.__call__
    def command(args,**kwargs):
        if args[2]=='shell' and shlex.split(args[3])[:2]==['input','keyevent']:
            return b''  # The device accepted a command but did not clear its field.
        return original(args,**kwargs)
    result=android.run_scenario(scenario(),tmp_path,command)
    assert result['result']['status']=='execution_error'
    assert device.value=='unchanged'
    assert not any('input text' in ' '.join(args) for args in device.calls)
    assert result['steps'][0]['status']=='error'
    assert result['steps'][1]['status']=='not_run'


def test_mobile_reader_preserves_environment_and_rejects_false_pass(tmp_path):
    import json
    from signup031.viewer_model import _load_one
    from signup031.investigation import collected_environment, default_report
    android.run_scenario(scenario(),tmp_path/'run',Device())
    path=tmp_path/'run/evidence.json'
    record=_load_one(path,tmp_path)
    assert record.business_status=='passed'
    assert record.android_scenario['serial']=='emulator-5580'
    assert collected_environment(record)['app_version_name']=='1.0'
    assert 'qa-submit' in default_report(record,tmp_path)['steps']
    payload=json.loads(path.read_bytes());payload['checks'][0]['actual']='wrong'
    path.write_text(json.dumps(payload))
    assert _load_one(path,tmp_path).business_status=='incomplete'


def test_mobile_missing_attachment_is_visible_without_rewriting_business_result(tmp_path):
    from signup031.viewer_model import _load_one
    android.run_scenario(scenario(),tmp_path/'run',Device())
    (tmp_path/'run/screenshot.png').write_bytes(b'tampered')
    record=_load_one(tmp_path/'run/evidence.json',tmp_path)
    assert record.business_status=='passed'
    assert record.screenshot.status!='available'
    assert 'screenshot' in record.android_details


def test_disconnect_after_first_action_stops_further_input(tmp_path):
    device=Device()
    def command(args,**kwargs):
        if device.value:raise ValueError('device offline')
        return device(args,**kwargs)
    result=android.run_scenario(scenario(),tmp_path,command)
    assert result['result']['status']=='execution_error'
    assert result['steps'][1]['status']=='error'
    assert all(check['status']=='not_run' for check in result['checks'])
    assert device.result=='ready'


def test_adb_timeout_reaps_only_owned_client(tmp_path,monkeypatch):
    import contextlib
    import subprocess
    import sys
    import time
    original=subprocess.Popen;owned=[]
    canary=original([sys.executable,'-c','import time; time.sleep(60)'])
    def launch(*args,**kwargs):
        process=original([sys.executable,'-c','import time; time.sleep(60)'],**kwargs)
        owned.append(process);return process
    monkeypatch.setattr(android.subprocess,'Popen',launch)
    monkeypatch.setattr(android.socket,'create_connection',lambda *a,**k:contextlib.nullcontext())
    try:
        started=time.monotonic()
        with pytest.raises(ValueError,match='timeout'):
            android.ADBCommand(sys.executable)(['-s','one','get-state'],timeout=.1)
        assert time.monotonic()-started<3
        assert owned[0].poll() is not None
        assert canary.poll() is None
    finally:canary.terminate();canary.wait(timeout=5)


def test_adb_output_limit_reaps_owned_client(monkeypatch):
    import contextlib
    import subprocess
    import sys
    original=subprocess.Popen;owned=[]
    def launch(*args,**kwargs):
        process=original([sys.executable,'-c','import sys,time;sys.stdout.write("x"*100000);sys.stdout.flush();time.sleep(60)'],**kwargs)
        owned.append(process);return process
    monkeypatch.setattr(android.subprocess,'Popen',launch)
    monkeypatch.setattr(android.socket,'create_connection',lambda *a,**k:contextlib.nullcontext())
    with pytest.raises(ValueError,match='limit'):
        android.ADBCommand(sys.executable)(['devices','-l'],maximum=1000)
    assert owned[0].poll() is not None
