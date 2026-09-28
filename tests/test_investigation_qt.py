"""The public failure-only investigation replaces report/retest editing (ticket24)."""
import json,os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtWidgets import QApplication
from test_investigation import investigation_runs
from test_manual_recording import manual_site

def test_real_qt_local_notes_reopen_without_legacy_workflows(investigation_runs):
    from signup031.viewer import EvidenceViewerWindow
    root,failed,passed,manual=investigation_runs
    before={p:p.read_bytes() for p in root.rglob('*') if p.is_file()}
    app=QApplication.instance() or QApplication([]);window=EvidenceViewerWindow(root);window.show()
    window._scenario_recorded(str(failed));window.open_investigation();dialog=window.investigation_dialog;app.processEvents()
    assert dialog.tabs.count()==1 and dialog.tabs.tabText(0)=='로컬 수동 조사'
    assert not dialog.save_button.isVisible() and not dialog.export_button.isVisible() and not dialog.jira_button.isVisible()
    dialog.local_notes.setPlainText('재열기 후에도 남을 직접 관측');dialog.local_save.click()
    assert '저장 완료' in dialog.status_label.text();dialog.close();window.close()
    window=EvidenceViewerWindow(root);window.show();window._scenario_recorded(str(failed));window.open_investigation();dialog=window.investigation_dialog
    assert dialog.local_notes.toPlainText()=='재열기 후에도 남을 직접 관측'
    assert all(p.read_bytes()==raw for p,raw in before.items())
    assert all(r.business_status in ('failed','preparation_failed','execution_error') for r in window.records)
    path=dialog.local_store.path_for(dialog.execution_id);saved=path.read_bytes()
    import stat
    path.chmod(stat.S_IREAD)
    try:
        dialog.local_notes.setPlainText('저장 실패 후 유지할 입력');dialog.local_save.click()
        assert '저장 실패' in dialog.status_label.text() and dialog.local_notes.toPlainText()=='저장 실패 후 유지할 입력'
        assert path.read_bytes()==saved
    finally:path.chmod(stat.S_IREAD|stat.S_IWRITE)
    dialog.close();path.write_text('{bad',encoding='utf-8');window.open_investigation();dialog=window.investigation_dialog
    assert '실패' in dialog.status_label.text() and not dialog.tabs.isEnabled()
    dialog.close();window.close()
