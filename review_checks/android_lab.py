"""Own only the explicitly prepared isolated AVD and verify original AVD hashes."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import time

ROOT=Path(__file__).resolve().parents[1]
OWNER=ROOT/'artifacts/ticket15-manager/isolated-a607b9b2/owner.json'


def file_hash(path):
    value=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):value.update(block)
    return value.hexdigest()


def stop_owned_lab(process, adb, serial, verified):
    report = {'stop_exit_code': None}
    try:
        if verified and process.poll() is None:
            name = subprocess.run([str(adb), '-s', serial, 'emu', 'avd', 'name'], capture_output=True, text=True, timeout=5)
            if name.returncode == 0 and name.stdout.splitlines()[0:1] == ['QA_Evidence_Test'] and process.poll() is None:
                stopped = subprocess.run([str(adb), '-s', serial, 'emu', 'kill'], capture_output=True, text=True, timeout=10)
                report['stop_exit_code'] = stopped.returncode
    except (OSError, subprocess.SubprocessError) as exc:
        report['stop_error'] = str(exc)
    finally:
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait(timeout=10)
        report['owned_launcher_stopped'] = process.poll() is not None
    return report


@contextmanager
def isolated_lab(output):
    output=Path(output);output.mkdir(parents=True,exist_ok=True);owner=json.loads(OWNER.read_bytes())
    original_ini=Path.home()/'.android/avd/Medium_Phone_API_35.ini'
    original=Path(next(line.split('=',1)[1] for line in original_ini.read_text().splitlines() if line.startswith('path=')))
    paths=[original_ini,*[original/name for name in ('config.ini','userdata-qemu.img','userdata-qemu.img.qcow2','encryptionkey.img','encryptionkey.img.qcow2')]]
    before={str(path):file_hash(path) for path in paths};sdk=Path(owner['args'][0]).parent.parent;adb=sdk/'platform-tools/adb.exe';serial=owner['serial']
    for port in (5580,5581):
        with socket.socket() as sock:sock.bind(('127.0.0.1',port))
    devices=subprocess.run([str(adb),'devices'],capture_output=True,text=True,timeout=10).stdout
    if serial in devices:raise ValueError('Isolated serial already connected; refusing to take ownership')
    log=(output/'emulator.log').open('wb');process=subprocess.Popen(owner['args'],env={**os.environ,'ANDROID_AVD_HOME':owner['avd_home']},stdout=log,stderr=subprocess.STDOUT,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    report={'serial':serial,'owned_launcher_pid':process.pid,'avd_home':owner['avd_home'],'args':owner['args'],'original_before':before,'prior_manager_exception':'artifacts/ticket15-manager/boot-71e3f9d2/summary.json'}
    (output/'owner.json').write_text(json.dumps(report,indent=2));started=time.monotonic();verified=False
    try:
        while time.monotonic()-started<120:
            result=subprocess.run([str(adb),'-s',serial,'shell','getprop','sys.boot_completed'],capture_output=True,text=True,timeout=5)
            if result.returncode==0 and result.stdout.strip()=='1':break
            if process.poll() is not None:raise ValueError('Owned emulator exited during boot')
            time.sleep(1)
        else:raise ValueError('Isolated emulator boot timeout')
        name=subprocess.run([str(adb),'-s',serial,'emu','avd','name'],capture_output=True,text=True,timeout=5)
        if name.returncode != 0 or name.stdout.splitlines()[0:1] != ['QA_Evidence_Test'] or process.poll() is not None:raise ValueError('Isolated AVD identity mismatch')
        verified=True
        report['boot_seconds']=round(time.monotonic()-started,3);print('ISOLATED_ANDROID_READY',flush=True)
        yield {'adb':str(adb),'serial':serial,'sdk':str(sdk),'report':report}
    finally:
        report.update(stop_owned_lab(process,adb,serial,verified))
        log.close();after={str(path):file_hash(path) for path in paths};report['original_after']=after;report['original_unchanged']=before==after;report['owned_launcher_stopped']=process.poll() is not None
        (output/'lab-summary.json').write_text(json.dumps(report,indent=2))
        if before!=after:raise AssertionError('Original AVD bytes changed during isolated lab')


if __name__=='__main__':
    import sys
    with isolated_lab(Path(sys.argv[1])):input()
