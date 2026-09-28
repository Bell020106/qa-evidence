"""Exercise the desktop author/run/read/replay flow with real child processes."""
import json
import os
from pathlib import Path
import time

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QFileDialog, QPushButton
import pytest

from test_web_scenarios import web_site, scenario_for


def until(condition, seconds=25):
    deadline = time.monotonic() + seconds
    while not condition() and time.monotonic() < deadline:
        QTest.qWait(30)
    assert condition()


def edit_row(table, row, kind, locator, target, value):
    table.cellWidget(row, 0).setCurrentIndex(table.cellWidget(row, 0).findData(kind))
    table.cellWidget(row, 1).setCurrentIndex(table.cellWidget(row, 1).findData(locator))
    table.cellWidget(row, 2).setText(target)
    table.cellWidget(row, 3).setText(value)


def test_desktop_author_save_load_run_view_replay(web_site, tmp_path, monkeypatch):
    from signup031.scenario_editor import ScenarioEditor
    from signup031.viewer_model import _load_one
    from signup031.replay import ReplaySession
    from signup031.viewer import EvidenceViewerWindow
    app = QApplication.instance() or QApplication([])
    window = EvidenceViewerWindow(tmp_path / 'runs', replay_headless=True)
    window.show()
    assert not window.other_tools.isVisible()
    editor = ScenarioEditor(tmp_path/'runs');editor.show()
    editor.url_edit.setText(web_site[0] + '/search')
    editor.id_edit.setText('FILTER-UI')
    editor.title_edit.setText('검색 결과 확인')
    edit_row(editor.steps_table, 0, 'fill', 'label', 'Search', 'pear')
    QTest.mouseClick(editor.add_step_button, Qt.MouseButton.LeftButton)
    edit_row(editor.steps_table, 1, 'click', 'css', '#filter', '')
    edit_row(editor.checks_table, 0, 'text', 'css', '#result', 'Pear: 2')
    QTest.mouseClick(editor.add_check_button, Qt.MouseButton.LeftButton)
    edit_row(editor.checks_table, 1, 'visible', 'css', '#result', 'true')
    config_file = tmp_path / 'user-tc.json'
    monkeypatch.setattr(QFileDialog, 'getSaveFileName', lambda *a: (str(config_file), ''))
    save_button = next(b for b in editor.findChildren(QPushButton) if b.text() == '설정 저장')
    QTest.mouseClick(save_button, Qt.MouseButton.LeftButton)
    assert config_file.is_file()
    editor.url_edit.clear()
    monkeypatch.setattr(QFileDialog, 'getOpenFileName', lambda *a: (str(config_file), ''))
    load_button = next(b for b in editor.findChildren(QPushButton) if b.text() == '설정 불러오기')
    QTest.mouseClick(load_button, Qt.MouseButton.LeftButton)
    assert editor.config()['steps'][1] == {'action': 'click', 'locator': 'css', 'target': '#filter', 'value': ''}
    assert editor.config()['checks'][1]['expected'] is True
    assert editor.url_edit.text() == web_site[0] + '/search'
    ticks = []
    timer = QTimer()
    timer.timeout.connect(lambda: ticks.append(1))
    timer.start(20)
    try:
        QTest.mouseClick(editor.run_button, Qt.MouseButton.LeftButton)
        until(lambda: editor.last_evidence is not None)
        assert len(ticks) > 2
        assert '통과' in editor.status_label.text()
        original = editor.last_evidence.read_bytes()
        editor.title_edit.setText('Changed after execution')
        assert editor.last_evidence.read_bytes() == original
        record=_load_one(editor.last_evidence,tmp_path/'runs')
        assert record.tc_id=='FILTER-UI' and record.business_status=='passed'
        assert record.scenario_snapshot['title']=='검색 결과 확인'
        assert [check['expected'] for check in record.scenario_snapshot['checks']]==['Pear: 2',True]
        editor.close()
        window.load_root(tmp_path/'runs')
        assert window.records==[] and not window.replay_button.isVisible()
        with ReplaySession(record.archive_root,headless=True) as replay:
            assert replay.restore()['status']=='ready'
    finally:
        timer.stop()
        editor.close()
        window.close()


def test_editor_invalid_configuration_stays_in_ui_without_process(tmp_path):
    from signup031.scenario_editor import ScenarioEditor
    app = QApplication.instance() or QApplication([])
    editor = ScenarioEditor(tmp_path)
    editor.show()
    editor.id_edit.setText('TC-1')
    editor.title_edit.setText('Invalid URL')
    editor.url_edit.setText('javascript:alert(1)')
    edit_row(editor.steps_table, 0, 'fill', 'css', '#query', 'pear')
    edit_row(editor.checks_table, 0, 'text', 'css', '#result', 'Pear: 2')
    QTest.mouseClick(editor.run_button, Qt.MouseButton.LeftButton)
    assert editor.process is None
    assert '사이트 주소' in editor.status_label.text()
    assert not list(tmp_path.rglob('evidence.json'))
    editor.close()
