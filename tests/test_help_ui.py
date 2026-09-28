"""Ticket24 replaces removed-tool help checks with the failure-only public UI."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import pytest
from PySide6.QtCore import Qt,QCoreApplication,QEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication,QDialog,QVBoxLayout
from test_viewer_qt import review_root,qapp

def test_help_only_current_topics_and_copyable_ide_setup(review_root):
    from signup031.viewer import EvidenceViewerWindow
    app=QApplication.instance() or QApplication([]);viewer=EvidenceViewerWindow(review_root);viewer.show();app.processEvents()
    viewer.activateWindow();viewer.run_list.setFocus();QTest.qWait(50);QTest.keyClick(viewer.run_list,Qt.Key.Key_F1);QTest.qWait(30)
    help_window=viewer.help_dialog
    assert help_window is not None and help_window.isVisible()
    assert set(help_window.ids)=={'start','selenium','replay','report','failure-analysis','browser','terms','troubleshooting'}
    before={p:p.read_bytes() for p in review_root.rglob('*') if p.is_file()}
    for topic in help_window.ids:
        help_window.select_topic(topic);assert help_window.body.toPlainText().strip()
    help_window.select_topic('selenium');help_window.copy_setup.click();app.processEvents()
    assert help_window.setup_guide is not None and help_window.setup_guide.isVisible()
    assert help_window.setup_guide.parentWidget() is help_window
    assert help_window.setup_guide.copy_buttons['install_command'].isEnabled() is False
    help_window.setup_guide.close()
    help_window.close();viewer.close();assert all(p.read_bytes()==raw for p,raw in before.items())

@pytest.mark.parametrize('topic',['report','failure-analysis','browser'])
def test_current_modal_help_preserves_input_and_releases_modal(review_root,topic):
    from signup031.viewer import EvidenceViewerWindow
    app=QApplication.instance() or QApplication([]);viewer=EvidenceViewerWindow(review_root);viewer.show()
    if topic=='browser':viewer.open_browser_setup();dialog=viewer.browser_dialog;field=None
    else:
        viewer.open_investigation();parent=viewer.investigation_dialog
        if topic=='report':dialog=parent;field=dialog.local_notes;field.setPlainText('저장하지 않은 조사')
        else:parent.open_analysis();dialog=parent.analysis_dialog;field=dialog.expected;field.setPlainText('추가 기대 결과')
    app.processEvents();dialog.resize(900,620);app.processEvents();assert dialog.width()==900 and dialog.height()==620
    dialog.activateWindow();(field or dialog.help_button).setFocus();QTest.qWait(50)
    QTest.keyClick(field or dialog.help_button,Qt.Key.Key_F1);QTest.qWait(30)
    assert dialog.help_dialog is not None and dialog.help_dialog.isVisible()
    assert dialog.help_dialog.current_topic==topic and dialog.help_dialog.parentWidget() is dialog
    dialog.help_dialog.close();app.processEvents()
    if field:assert field.toPlainText() in ('저장하지 않은 조사','추가 기대 결과')
    dialog.close();app.processEvents();assert not dialog.isVisible() and app.activeModalWidget() is not dialog
    if topic=='failure-analysis':parent.close();app.processEvents()
    viewer.activateWindow();viewer.run_list.setFocus();QTest.qWait(50);QTest.keyClick(viewer.run_list,Qt.Key.Key_F1);QTest.qWait(30)
    assert viewer.help_dialog is not None and viewer.help_dialog.isVisible()
    viewer.help_dialog.close();viewer.close()

def test_pending_help_layout_dies_with_its_dialog(monkeypatch):
    import sys
    from signup031.help_dialog import add_help
    app=QApplication.instance() or QApplication([]);errors=[];monkeypatch.setattr(sys,'excepthook',lambda *args:errors.append(args))
    dialog=QDialog();layout=QVBoxLayout(dialog);add_help(dialog,'selenium',layout)
    dialog.deleteLater();QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete);app.processEvents();assert not errors
