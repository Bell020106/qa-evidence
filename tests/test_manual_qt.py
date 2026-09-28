import os
import time
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt, QTimer
from PySide6.QtTest import QTest
from test_manual_recording import manual_site


def until(condition):
    deadline = time.monotonic() + 25
    while not condition() and time.monotonic() < deadline:
        QTest.qWait(30)
    assert condition()


def test_manual_desktop_start_save_list_replay_and_duplicate_controls(manual_site, tmp_path):
    from signup031.viewer import EvidenceViewerWindow
    from signup031.viewer_model import _load_one
    from signup031.replay import ReplaySession
    app = QApplication.instance() or QApplication([])
    window = EvidenceViewerWindow(tmp_path, replay_headless=True)
    window.show()
    window.open_manual_recorder()
    dialog = window.manual_dialog
    dialog.url_edit.setText(manual_site[0] + '/note')
    dialog.title_edit.setText('수동 관찰')
    ticks = []
    timer = QTimer()
    timer.timeout.connect(lambda: ticks.append(1))
    timer.start(20)
    try:
        QTest.mouseClick(dialog.start_button, Qt.MouseButton.LeftButton)
        until(lambda: dialog.save_button.isEnabled())
        assert not dialog.start_button.isEnabled()
        assert len(ticks) > 2
        QTest.mouseClick(dialog.save_button, Qt.MouseButton.LeftButton)
        assert not dialog.save_button.isEnabled()
        until(lambda: dialog.process is None)
        assert dialog.last_evidence is not None
        dialog.close()
        assert window.records==[] and not window.other_tools.isVisible()
        record=_load_one(dialog.last_evidence,tmp_path)
        assert record.business_status=='unjudged'
        assert record.pytest_status is None and record.archive_root is not None
        with ReplaySession(record.archive_root,headless=True) as replay:
            assert replay.restore()['status']=='ready'
    finally:
        timer.stop()
        if dialog.process:
            dialog.cancel_recording()
            until(lambda: dialog.process is None)
        window.stop_replay()
        until(lambda: window.replay_process is None)
        dialog.close()
        window.close()


def test_manual_cancel_and_bad_url_are_not_saved(manual_site, tmp_path):
    from signup031.manual_dialog import ManualRecordingDialog
    app = QApplication.instance() or QApplication([])
    dialog = ManualRecordingDialog(tmp_path, headless=True)
    dialog.show()
    dialog.title_edit.setText('취소')
    dialog.url_edit.setText('file:///secret')
    dialog.start_recording()
    assert dialog.process is None
    assert 'URL' in dialog.status_label.text()
    dialog.url_edit.setText(manual_site[0])
    dialog.start_recording()
    until(lambda: dialog.save_button.isEnabled())
    dialog.cancel_recording()
    until(lambda: dialog.process is None)
    assert '미저장' in dialog.status_label.text()
    assert not list(tmp_path.rglob('evidence.json'))
    assert not list(tmp_path.rglob('*.har'))
    dialog.close()


def test_main_window_close_stops_owned_recorder_and_browser(manual_site, tmp_path):
    from signup031.viewer import EvidenceViewerWindow
    app = QApplication.instance() or QApplication([])
    window = EvidenceViewerWindow(tmp_path, replay_headless=True)
    window.show()
    window.open_manual_recorder()
    dialog = window.manual_dialog
    dialog.url_edit.setText(manual_site[0])
    dialog.title_edit.setText('창 닫기')
    dialog.start_recording()
    until(lambda: dialog.save_button.isEnabled())
    process = dialog.process
    finished = []
    process.finished.connect(lambda code, status: finished.append(code))
    window.close()
    until(lambda: dialog.process is None and not window.isVisible())
    assert finished == [0]
    assert not list(tmp_path.rglob('*.har'))
    assert not list(tmp_path.rglob('evidence.json'))


def test_browser_start_failure_is_visible_and_leaves_no_har(tmp_path):
    from signup031.manual_dialog import ManualRecordingDialog
    app = QApplication.instance() or QApplication([])
    dialog = ManualRecordingDialog(tmp_path, headless=True)
    dialog.url_edit.setText('http://127.0.0.1:1/unavailable')
    dialog.title_edit.setText('시작 오류')
    dialog.start_recording()
    until(lambda: dialog.process is None)
    assert '실패' in dialog.status_label.text()
    assert dialog.last_evidence is None
    assert not list(tmp_path.rglob('*.har'))
    dialog.close()
