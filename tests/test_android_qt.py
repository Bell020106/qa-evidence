import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtWidgets import QApplication
from signup031.viewer import EvidenceViewerWindow
from test_android_adapter import scenario, Device
from signup031.android_adapter import run_scenario


def test_mobile_viewer_shows_actual_checks_and_android_environment(tmp_path):
    app=QApplication.instance() or QApplication([])
    config=scenario();config['checks'][0]['expected']='length:99'
    run_scenario(config,tmp_path/'run',Device())
    viewer=EvidenceViewerWindow(tmp_path)
    viewer.show();app.processEvents()
    assert 'Android' in viewer.pytest_value.text()
    assert 'length:11' in viewer.checks_label.text()
    assert 'app_version_name' in viewer.checks_label.text()
    assert viewer.investigation_button.isEnabled()
    assert not viewer.replay_button.isEnabled()
    viewer.close()


def test_mobile_dialog_roundtrip_and_duplicate_start_guard(tmp_path):
    app=QApplication.instance() or QApplication([])
    viewer=EvidenceViewerWindow(tmp_path)
    viewer.open_android()
    dialog=viewer.android_dialog
    dialog.set_config(scenario())
    assert dialog.config()==scenario()
    dialog.process=object()
    dialog.run_test()
    assert not list(tmp_path.rglob('evidence.json'))
    dialog.process=None
    dialog.close();viewer.close()


def test_viewer_close_also_closes_mobile_dialog(tmp_path):
    app=QApplication.instance() or QApplication([])
    viewer=EvidenceViewerWindow(tmp_path);viewer.show();viewer.open_android();app.processEvents()
    viewer.close();app.processEvents()
    assert not viewer.android_dialog.isVisible()


def test_mobile_log_opens_only_current_hash_verified_content(tmp_path):
    from signup031.viewer_model import _load_one
    app=QApplication.instance() or QApplication([])
    result=run_scenario(scenario(),tmp_path/'run',Device())
    path=tmp_path/'run/evidence.json'
    assert result['result']['status']=='passed' and path.is_file()
    viewer=EvidenceViewerWindow(tmp_path);viewer.show();app.processEvents()
    assert viewer.records==[]
    assert not viewer.android_button.isVisible() and not viewer.android_log_button.isVisible()
    record=_load_one(path,tmp_path)
    assert record.business_status=='passed' and 'measured' in record.android_log
    (tmp_path/'run/app.log').write_text('injected log')
    tampered=_load_one(path,tmp_path)
    assert tampered.android_log is None
    assert 'injected log' not in (tampered.android_log or '')
    viewer.close()


def test_timeout_and_cancel_are_distinct_and_partial_checks_stay_unperformed(tmp_path):
    import json
    from signup031.android_dialog import finish_interrupted
    from signup031.android_adapter import _atomic_json
    result=run_scenario(scenario(),tmp_path/'run',Device())
    result['result']['status']='running'
    result['checks'][0].update(status='not_run',actual=None)
    path=tmp_path/'run/evidence.json';_atomic_json(path,result)
    finish_interrupted(path,'execution_error','Android timeout')
    value=json.loads(path.read_bytes())
    assert value['result']['status']=='execution_error'
    assert value['checks'][0]['status']=='not_run'
    assert value['finished_at']
    # A late cancellation must not overwrite a completed immutable result.
    before=path.read_bytes();finish_interrupted(path,'cancelled','late cancel')
    assert path.read_bytes()==before
