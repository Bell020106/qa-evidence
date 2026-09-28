import os
import sys
from pathlib import Path
import pytest
from signup031 import owned_job


def test_frozen_worker_command_never_invokes_gui_or_python(monkeypatch,tmp_path):
    monkeypatch.setattr(sys,'frozen',True,raising=False)
    monkeypatch.setattr(sys,'executable',str(tmp_path/'QA Evidence.exe'))
    command=owned_job.python_command('signup031.ai_worker')
    assert command==[str(tmp_path/'QA Evidence Worker.exe'),'signup031.ai_worker']


def test_frozen_worker_environment_drops_development_python(monkeypatch):
    monkeypatch.setattr(sys,'frozen',True,raising=False)
    monkeypatch.setenv('PYTHONPATH','C:/private-development')
    monkeypatch.setenv('PYTHONHOME','C:/private-python')
    env=owned_job.worker_environment()
    assert 'PYTHONPATH' not in env
    assert 'PYTHONHOME' not in env


def test_worker_dispatch_rejects_arbitrary_modules():
    from signup031.desktop_worker import main
    assert main(['os'])==2


def test_frozen_default_data_is_outside_install(monkeypatch,tmp_path):
    from signup031.viewer import default_result_root
    monkeypatch.setattr(sys,'frozen',True,raising=False)
    monkeypatch.setattr(sys,'executable',str(tmp_path/'install/QA Evidence.exe'))
    monkeypatch.setenv('LOCALAPPDATA',str(tmp_path/'user'))
    assert default_result_root()==tmp_path/'user/QAEvidence/data'


def test_owned_start_gate_does_not_consume_worker_payload(monkeypatch,capsys):
    import io
    from signup031.desktop_worker import main
    stream=io.TextIOWrapper(io.BytesIO(b'start\n{"android":true}\n'),encoding='utf-8')
    monkeypatch.setattr(sys,'stdin',stream)
    import signup031.desktop_worker as runtime
    seen=[]
    monkeypatch.setattr(runtime.runpy,'run_module',lambda *a,**k:seen.append(sys.stdin.buffer.readline()))
    assert main(['--startup-gate','signup031.android_worker'])==0
    assert seen==[b'{"android":true}\n']


def test_legacy_worker_ownership_failure_restores_manual_controls(monkeypatch,tmp_path):
    os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
    from PySide6.QtWidgets import QApplication
    from signup031.manual_dialog import ManualRecordingDialog
    app=QApplication.instance() or QApplication([])
    dialog=ManualRecordingDialog(tmp_path,headless=True)
    dialog.url_edit.setText('http://127.0.0.1:9/');dialog.title_edit.setText('Ownership failure')
    def fail():raise OSError('owned-job failure')
    monkeypatch.setattr(owned_job,'OwnedJob',fail)
    dialog.start_recording()
    assert dialog.process is None
    assert dialog.start_button.isEnabled() and dialog.url_edit.isEnabled()
    assert 'owned-job failure' in dialog.status_label.text()
    dialog.close()


def test_build_environment_cannot_pick_unrelated_native_runtime(monkeypatch):
    import runpy
    build=runpy.run_path(str(Path(__file__).resolve().parents[1]/'packaging/windows/build_windows.py'))
    monkeypatch.setenv('PATH','C:/unrelated-poppler/bin;C:/unrelated-libheif/bin')
    monkeypatch.setenv('PYTHONPATH','C:/unrelated-python')
    env=build['build_environment']()
    assert 'unrelated' not in env['PATH']
    assert 'PYTHONPATH' not in env
    assert 'System32' in env['PATH']


def test_frozen_long_data_path_is_explicit_and_can_be_reselected(monkeypatch,tmp_path):
    os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
    from PySide6.QtWidgets import QApplication
    from signup031.viewer import EvidenceViewerWindow
    app=QApplication.instance() or QApplication([])
    monkeypatch.setattr(sys,'frozen',True,raising=False)
    monkeypatch.setattr(sys,'executable',str(tmp_path/'install/QA Evidence.exe'))
    viewer=EvidenceViewerWindow(tmp_path/('x'*150))
    assert '140' in viewer.message_label.text()
    assert not viewer.android_button.isEnabled()
    viewer.load_root(tmp_path/'short')
    assert viewer.android_button.isEnabled()
    viewer.close()
