import json
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtWidgets import QApplication
from test_viewer_qt import review_root,qapp


def test_home_exposes_only_failure_investigation(review_root):
    from signup031.viewer import EvidenceViewerWindow
    app=QApplication.instance() or QApplication([]);viewer=EvidenceViewerWindow(review_root);viewer.show();app.processEvents()
    assert viewer.ide_button.isVisible()
    assert not viewer.configure_button.isVisible() and not viewer.ai_button.isVisible()
    assert viewer.local_investigation_button.isVisible() and viewer.analysis_button.isVisible()
    assert not viewer.other_tools_button.isVisible() and not viewer.server_import_button.isVisible()
    assert all(record.business_status in ('failed','preparation_failed','execution_error') for record in viewer.records)
    viewer.open_investigation();dialog=viewer.investigation_dialog;app.processEvents()
    assert dialog.tabs.count()==1 and not dialog.jira_button.isVisible()
    dialog.close()
    viewer.close()


def test_auto_new_failure_preserves_selected_record_and_unsaved_notes(review_root):
    import shutil
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from signup031.viewer import EvidenceViewerWindow
    app=QApplication.instance() or QApplication([]);viewer=EvidenceViewerWindow(review_root);viewer.show();app.processEvents()
    viewer.open_investigation();dialog=viewer.investigation_dialog;dialog.local_notes.setPlainText('아직 저장하지 않은 조사')
    selected=viewer.run_list.currentItem().data(Qt.ItemDataRole.UserRole).execution_id;count=viewer.run_list.count()
    source=next(r.source_path for r in viewer.records if r.archive_root is None)
    raw=json.loads(source.read_bytes());raw['execution']['id']='new-completed-failure'
    pending=review_root/'.qa-capture-test';pending.mkdir();(pending/'evidence.json').write_text(json.dumps(raw))
    QTest.qWait(1700);assert viewer.run_list.count()==count
    target=review_root/'new-failure';pending.rename(target)
    QTest.qWait(1700)
    assert viewer.run_list.count()==count+1
    assert viewer.run_list.currentItem().data(Qt.ItemDataRole.UserRole).execution_id==selected
    assert viewer.investigation_dialog is dialog and dialog.local_notes.toPlainText()=='아직 저장하지 않은 조사'
    assert '새 실패' in viewer.new_failure_label.text()
    same_item=viewer.run_list.currentItem();viewer.load_root(review_root)
    assert viewer.run_list.currentItem() is same_item
    # External modification/removal is observed without discarding the selected item.
    raw['result']['business']['message']='updated failure detail'
    (target/'evidence.json').write_text(json.dumps(raw));QTest.qWait(1700)
    assert any('updated failure detail' in r.message for r in viewer.records)
    (target/'evidence.json').unlink();QTest.qWait(1700)
    assert viewer.run_list.count()==count and viewer.run_list.currentItem() is same_item
    assert dialog.local_notes.toPlainText()=='아직 저장하지 않은 조사'
    dialog.close()
    empty=review_root/'empty';empty.mkdir();viewer.load_root(empty);assert viewer.run_list.count()==0
    viewer.load_root(review_root);assert viewer.run_list.count()==count
    viewer.close()


def test_save_then_analysis_uses_current_notes_and_blocks_stale_save(review_root):
    from signup031.investigation_dialog import InvestigationDialog
    from signup031.investigation import InvestigationStore
    from signup031.local_investigation import LocalInvestigationStore
    app=QApplication.instance() or QApplication([])
    eid=InvestigationStore(review_root).records()[0].execution_id
    dialog=InvestigationDialog(review_root,eid);dialog.show();app.processEvents()
    dialog.local_notes.setPlainText('방금 직접 입력해서 확인한 내용')
    dialog.open_analysis();analysis=dialog.analysis_dialog
    assert analysis is not None
    assert LocalInvestigationStore(review_root).load(eid)['notes']=='방금 직접 입력해서 확인한 내용'
    analysis.model.setText('synthetic-model');analysis.preview_request()
    rows=json.loads(analysis.previewed['input'][0]['content'])['evidence']
    assert next(r['content'] for r in rows if r['id']=='local.notes')=='방금 직접 입력해서 확인한 내용'
    assert '수동 조사 메모' in analysis.collected.toPlainText()
    analysis.close();app.processEvents()
    dialog.local_notes.setPlainText('두 번째 저장 후 분석');dialog.open_analysis();app.processEvents()
    assert '두 번째 저장 후 분석' in analysis.collected.toPlainText()
    analysis.close();app.processEvents()
    store=LocalInvestigationStore(review_root);doc=store.load(eid);doc['notes']='다른 창 저장';store.save(doc)
    dialog.local_notes.setPlainText('이 입력은 저장 충돌');dialog.open_analysis();app.processEvents()
    assert not analysis.isVisible() and '저장 실패' in dialog.status_label.text()
    assert dialog.local_notes.toPlainText()=='이 입력은 저장 충돌'
    dialog.close()


def test_truncated_restore_is_not_reported_as_not_run():
    from signup031.failure_presentation import readable_evidence
    text=readable_evidence({'evidence':[{'id':'local.restore','source':'local_investigation','content':'{"state":"partial",','truncated':True}]})
    assert '미실행' not in text and '일부 생략' in text
