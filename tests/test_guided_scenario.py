import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import pytest
import json
from test_manual_recording import manual_site
from test_scenario_editor import until
from PySide6.QtWidgets import QApplication
from signup031.scenario_editor import ScenarioEditor
from signup031.web_scenario import ACTION_LABELS,CHECK_LABELS


@pytest.fixture
def editor(tmp_path):
    app=QApplication.instance() or QApplication([])
    editor=ScenarioEditor(tmp_path);editor.show();app.processEvents()
    editor.set_config({'version':1,'id':'GUIDED','title':'기록에서 구성','url':'https://example.test',
        'steps':[{'action':'fill','locator':'label','target':'Search','value':'pear'},
                 {'action':'click','locator':'css','target':'#apply','value':''}],
        'checks':[],'draft':True,'source_manual':{'execution_id':'synthetic','evidence_sha256':'a'*64,
          'limitations':['URL changed during recording','unknown future limitation']}})
    yield editor
    editor.close()


def select_target(cell,pair):
    index=next(i for i in range(cell.choice.count()) if cell.choice.itemData(i) and tuple(cell.choice.itemData(i))==pair)
    cell.choice.setCurrentIndex(index)


def test_guided_targets_keep_identity_after_move_delete_and_advanced_edit(editor,tmp_path):
    assert editor.steps_table.isColumnHidden(1)
    assert '기록한 1번 동작' in editor.steps_table.cellWidget(0,2).choice.currentText()
    editor.add_check_button.click()
    select_target(editor.checks_table.cellWidget(0,2),('label','Search'))
    editor.checks_table.cellWidget(0,3).setText('4')
    editor.steps_table.setCurrentCell(0,0);editor._move_row(editor.steps_table,ACTION_LABELS,1)
    editor.advanced_check.setChecked(True)
    editor.steps_table.cellWidget(1,2).setText('Changed label')
    editor.steps_table.removeRow(1)
    editor.advanced_check.setChecked(False)
    assert editor.config()['checks'][0]['target']=='Search'
    assert editor.config()['checks'][0]['locator']=='label'
    path=tmp_path/'guided.json';editor.save_to(path);editor.load_from(path)
    assert editor.config()['checks'][0]['expected']==4
    assert editor.config()['source_manual']['evidence_sha256']=='a'*64


def test_typed_results_restore_values_and_plain_validation(editor):
    editor.start_run();assert editor.process is None
    assert '확인할 결과가 아직 없습니다' in editor.status_label.text()
    assert '결과 추가' in editor.status_label.text()
    editor.add_check_button.click();table=editor.checks_table
    select_target(table.cellWidget(0,2),('css','#apply'))
    kind=table.cellWidget(0,0);kind.setCurrentIndex(kind.findData('visible'))
    value=table.cellWidget(0,3);value.boolean.setCurrentIndex(value.boolean.findData(False))
    assert editor.config()['checks'][0]['expected'] is False
    kind.setCurrentIndex(kind.findData('text'));value.setText('완료')
    assert editor.config()['checks'][0]['expected']=='완료'
    kind.setCurrentIndex(kind.findData('visible'))
    assert editor.config()['checks'][0]['expected'] is False
    assert '주소가 달라졌습니다' in editor.source_label.text()
    assert '추가 제한' in editor.source_label.text()
    assert 'unknown future limitation' in editor.details_text.toPlainText()
    editor.url_edit.clear();editor.start_run()
    assert editor.process is None and '사이트 주소를 입력' in editor.status_label.text()


def test_cached_values_survive_row_move_and_modes(editor,tmp_path):
    editor.add_check_button.click();editor.add_check_button.click()
    for row in range(2):select_target(editor.checks_table.cellWidget(row,2),('label','Search'))
    kind=editor.checks_table.cellWidget(0,0);value=editor.checks_table.cellWidget(0,3)
    value.setText('0');kind.setCurrentIndex(kind.findData('text'));value.setText('')
    kind.setCurrentIndex(kind.findData('visible'));value.setText('false')
    editor.checks_table.cellWidget(1,3).setText('4')
    editor.checks_table.setCurrentCell(0,0);editor._move_row(editor.checks_table,CHECK_LABELS,1)
    editor.advanced_check.setChecked(True);editor.advanced_check.setChecked(False)
    kind=editor.checks_table.cellWidget(1,0)
    assert editor.config()['checks'][1]['expected'] is False
    kind.setCurrentIndex(kind.findData('input_length'));assert editor.config()['checks'][1]['expected']==0
    kind.setCurrentIndex(kind.findData('text'));assert editor.config()['checks'][1]['expected']==''
    editor.save_to(tmp_path/'roundtrip.json');editor.load_from(tmp_path/'roundtrip.json')
    assert editor.config()['checks'][1]['expected']==''
    editor.checks_table.setRowCount(0);editor.save_to(tmp_path/'draft.json')
    assert '초안 저장됨' in editor.status_label.text()
    editor.start_run();assert editor.process is None


def test_recorded_guided_three_results_run_pass_and_mismatch(manual_site,tmp_path):
    from signup031.manual_recording import ManualRecorder
    from signup031.manual_to_tc import draft_from_manual
    with ManualRecorder(manual_site[0], '합성 기록',tmp_path/'runs',headless=True) as recorder:
        recorder.page.locator('#query').fill('pear')
        recorder.page.locator('#apply').click()
        recorder.page.locator('#result').click()
        original=recorder.save()
    before={p:p.read_bytes() for p in original.parent.rglob('*') if p.is_file()}
    app=QApplication.instance() or QApplication([])
    editor=ScenarioEditor(tmp_path/'runs');editor.show();app.processEvents();editor.set_config(draft_from_manual(original))
    try:
        for row,(kind,target,expected) in enumerate((('input_length','#query','4'),('text','#result','pear'),('visible','#result','true'))):
            editor.add_check_button.click();combo=editor.checks_table.cellWidget(row,0);combo.setCurrentIndex(combo.findData(kind))
            select_target(editor.checks_table.cellWidget(row,2),('css',target));editor.checks_table.cellWidget(row,3).setText(expected)
        assert not editor.advanced_check.isChecked()
        file=tmp_path/'guided.json';editor.save_to(file);editor.load_from(file)
        editor.start_run();until(lambda:editor.process is None,45)
        passed=json.loads(editor.last_evidence.read_text(encoding='utf-8'))
        assert passed['result']['business']['status']=='passed'
        assert [c['expected'] for c in passed['scenario_snapshot']['checks']]==[4,'pear',True]
        editor.checks_table.cellWidget(1,3).setText('different')
        editor.start_run();until(lambda:editor.process is None,45)
        failed=json.loads(editor.last_evidence.read_text(encoding='utf-8'))
        assert failed['result']['business']['status']=='failed'
        assert all(p.read_bytes()==data for p,data in before.items())
    finally:
        until(lambda:editor.process is None,45);editor.close()


def test_invalid_fields_block_and_raw_file_error_is_collapsed(editor,tmp_path,monkeypatch):
    from PySide6.QtWidgets import QFileDialog
    editor.add_check_button.click()
    select_target(editor.checks_table.cellWidget(0,2),('label','Search'))
    editor.start_run();assert editor.process is None and '결과 1번' in editor.status_label.text()
    editor.checks_table.cellWidget(0,3).setText('4')
    editor.steps_table.cellWidget(0,2).setText('')
    editor.start_run();assert editor.process is None and '동작 1번' in editor.status_label.text()
    editor.title_edit.clear();editor.start_run();assert editor.process is None and '제목' in editor.status_label.text()
    broken=tmp_path/'broken.json';broken.write_text('{broken')
    monkeypatch.setattr(QFileDialog,'getOpenFileName',lambda *args:(str(broken),''))
    editor._load_dialog();assert '파일 형식과 버전' in editor.status_label.text()
    assert 'Expecting property name' in editor.details_text.toPlainText()
    assert not editor.details_toggle.isChecked()
