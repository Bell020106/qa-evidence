import json
import os
from pathlib import Path
import subprocess
import sys
import time

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from signup031.viewer import EvidenceViewerWindow


@pytest.fixture
def recorded_root(tmp_path):
    completed = subprocess.run([sys.executable, '-m', 'signup031', '--demo', 'defective',
                                '--record-archive', '--artifacts-dir', str(tmp_path)],
                               capture_output=True, timeout=45)
    assert completed.returncode == 1
    return tmp_path


def wait_until(condition, timeout=15):
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        QTest.qWait(30)
    assert condition()


def test_qt_button_real_child_ready_responsive_and_stop(recorded_root):
    app = QApplication.instance() or QApplication([])
    window = EvidenceViewerWindow(recorded_root, replay_headless=True)
    window.show()
    app.processEvents()
    window.watch_timer.stop()
    before = {p: p.read_bytes() for p in recorded_root.rglob('*') if p.is_file()}
    ticks = []
    timer = QTimer()
    timer.timeout.connect(lambda: ticks.append(1))
    timer.start(20)
    try:
        assert window.replay_button.isEnabled()
        QTest.mouseClick(window.replay_button, Qt.MouseButton.LeftButton)
        wait_until(lambda: '수동 테스트 가능' in window.replay_label.text())
        assert window.replay_process.processId() > 0
        assert not window.run_list.isEnabled()
        assert not window.findChild(type(window.replay_button), 'refreshButton').isEnabled()
        assert len(ticks) > 2
        QTest.mouseClick(window.stop_replay_button, Qt.MouseButton.LeftButton)
        wait_until(lambda: window.replay_process is None)
        added = subprocess.run([sys.executable, '-m', 'signup031', '--demo', 'defective',
                                '--record-archive', '--artifacts-dir', str(recorded_root)],
                               capture_output=True, timeout=45)
        assert added.returncode == 1
        window.poll_failures()
        assert window.replay_label.text() == '재현 종료'
        assert window.run_list.count()==2
        assert all(path.read_bytes()==raw for path,raw in before.items())
        selected=window.run_list.currentRow();selected_path=window.run_list.currentItem().data(Qt.ItemDataRole.UserRole).source_path
        other_item=window.run_list.item(1-selected);other_path=other_item.data(Qt.ItemDataRole.UserRole).source_path
        other_payload=json.loads(other_path.read_text(encoding='utf-8'))
        other_payload['result']['business']['message']+=' · 다른 기록 수정'
        other_path.write_text(json.dumps(other_payload),encoding='utf-8');window.poll_failures()
        assert window.replay_label.text()=='재현 종료' and window.run_list.count()==2
        other_path.unlink();window.poll_failures()
        assert window.replay_label.text()=='재현 종료' and window.run_list.count()==1
        selected_payload=json.loads(selected_path.read_text(encoding='utf-8'))
        selected_payload['result']['business']['message']+=' · 선택 기록 수정'
        selected_path.write_text(json.dumps(selected_payload),encoding='utf-8');window.poll_failures()
        assert window.replay_label.text()!='재현 종료'
        empty=recorded_root/'empty-root';empty.mkdir();window.load_root(empty)
        assert window.replay_label.text()!='재현 종료'
        window.load_root(recorded_root);assert window.run_list.count()==1
        selected_path=window.run_list.currentItem().data(Qt.ItemDataRole.UserRole).source_path
        window.replay_label.setText('재현 종료');selected_path.unlink();window.poll_failures()
        assert window.run_list.count()==0 and window.replay_label.text()!='재현 종료'
        assert window.run_list.isEnabled()
    finally:
        timer.stop()
        window.stop_replay()
        wait_until(lambda: window.replay_process is None)
        window.close()


def test_qt_child_reports_files_removed_after_list_load(recorded_root):
    app = QApplication.instance() or QApplication([])
    window = EvidenceViewerWindow(recorded_root, replay_headless=True)
    window.show()
    app.processEvents()
    next(recorded_root.rglob('resources.har')).unlink()
    QTest.mouseClick(window.replay_button, Qt.MouseButton.LeftButton)
    wait_until(lambda: window.replay_process is None)
    assert '복원 실패' in window.replay_label.text()
    window.load_root(recorded_root)
    assert not window.replay_button.isEnabled()
    assert '재현 자료 오류' in window.replay_label.text()
    window.close()


def test_legacy_evidence_has_no_replay_button(recorded_root):
    evidence = next(recorded_root.rglob('evidence.json'))
    payload = json.loads(evidence.read_text(encoding='utf-8'))
    payload.pop('replay')
    evidence.write_text(json.dumps(payload))
    app = QApplication.instance() or QApplication([])
    window = EvidenceViewerWindow(recorded_root)
    assert not window.replay_button.isEnabled()
    assert '재현 자료 없음' in window.replay_label.text()
    window.close()


@pytest.mark.parametrize('kind', ['event', 'start', 'stderr'])
def test_replay_failure_reason_survives_navigation_restore(recorded_root, kind):
    from PySide6.QtCore import QProcess
    app = QApplication.instance() or QApplication([])
    window = EvidenceViewerWindow(recorded_root)
    reason = 'ValueError: recorded target missing or ambiguous: video:nth-of-type(1)'
    class Child:
        def readAllStandardOutput(self):
            body = getattr(self, 'output', b''); self.output = b''; return body
        def readAllStandardError(self):
            body = getattr(self, 'stderr', b''); self.stderr = b''; return body
        def errorString(self): return 'synthetic executable missing'
        def deleteLater(self): pass
    child = Child(); window.replay_process = child
    window.replay_limitations = ['iframe content not recorded']
    if kind == 'event':
        # Also accept the final event when the process closes without a newline.
        child.output = json.dumps({'status':'failed','reason':reason}).encode()
        window._replay_finished(1, QProcess.ExitStatus.NormalExit)
        assert reason in window.replay_label.text()
        assert '저장 당시 조작한 요소' in window.replay_label.text()
        assert 'iframe content not recorded' in window.replay_label.text()
    elif kind == 'start':
        window._replay_error(QProcess.ProcessError.FailedToStart)
        assert 'synthetic executable missing' in window.replay_label.text()
    else:
        child.stderr = b'x' * 10000 + b'\nsynthetic native failure'
        window._replay_finished(1, QProcess.ExitStatus.CrashExit)
        assert '작업자 오류 출력' in window.replay_label.text()
        assert 'synthetic native failure' not in window.replay_label.text()
        assert len(window.replay_stderr) <= 4096
        assert len(window.replay_label.text()) < 6000
    assert window.replay_process is None and window.run_list.isEnabled()
    assert window.replay_label.textFormat() == Qt.TextFormat.PlainText
    window.close()
