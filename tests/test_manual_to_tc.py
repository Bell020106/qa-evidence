import hashlib
import json
import os
import shutil
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from test_manual_recording import manual_site
from test_scenario_editor import edit_row, until


@pytest.mark.parametrize('kind', ['search', 'note'])
def test_record_to_draft_expected_result_run_and_source_navigation(manual_site, tmp_path, kind):
    from signup031.manual_recording import ManualRecorder
    from signup031.manual_to_tc import draft_from_manual,resolve_manual_source
    from signup031.scenario_editor import ScenarioEditor
    from signup031.viewer import EvidenceViewerWindow
    from signup031.viewer_model import load_evidence_root
    from signup031.web_scenario import load_scenario
    field, button = ('#query', '#apply') if kind == 'search' else ('#note', '#show')
    with ManualRecorder(manual_site[0] + '/' + kind, 'Original ' + kind, tmp_path / 'runs', headless=True) as recorder:
        recorder.page.locator(field).fill('observed bug')
        recorder.page.locator(button).click()
        original_path = recorder.save()
    original = {p: p.read_bytes() for p in original_path.parent.rglob('*') if p.is_file()}
    request_count = manual_site[2].count
    app = QApplication.instance() or QApplication([])
    window = EvidenceViewerWindow(tmp_path / 'runs', replay_headless=True)
    window.show()
    editor=None
    try:
        assert window.records==[] and not window.draft_button.isVisible()
        editor=ScenarioEditor(tmp_path/'runs')
        editor.set_config(draft_from_manual(original_path));editor.show()
        assert editor.steps_table.rowCount() == 2
        assert editor._row_values(editor.steps_table, 0) == ['fill', 'css', field, 'observed bug']
        assert editor._row_values(editor.steps_table, 1) == ['click', 'css', button, '']
        assert editor.checks_table.rowCount() == 0
        assert manual_site[2].count == request_count
        QTest.mouseClick(editor.run_button, Qt.MouseButton.LeftButton)
        assert editor.process is None
        editor.id_edit.setText('DERIVED-' + kind)
        with pytest.raises(ValueError, match='원본'):
            editor.save_to(original_path.parent / 'archive' / 'converted-tc.json')
        assert not (original_path.parent / 'archive' / 'converted-tc.json').exists()
        draft_file = tmp_path / 'draft.json'
        editor.save_to(draft_file)
        draft = load_scenario(draft_file)
        assert draft['draft'] is True and draft['checks'] == []
        assert draft['source_manual']['execution_id'] == recorder.execution_id
        assert draft['source_manual']['evidence_sha256'] == hashlib.sha256(original_path.read_bytes()).hexdigest()
        editor.clear_config()
        editor.load_from(draft_file)
        assert editor.checks_table.rowCount() == 0
        QTest.mouseClick(editor.add_check_button, Qt.MouseButton.LeftButton)
        edit_row(editor.checks_table, 0, 'text', 'css', '#result' if kind == 'search' else 'output', 'QA expected healthy result')
        editor.save_to(draft_file)
        assert 'draft' not in load_scenario(draft_file)
        QTest.mouseClick(editor.run_button, Qt.MouseButton.LeftButton)
        until(lambda: editor.process is None)
        assert editor.last_evidence is not None
        payload = json.loads(editor.last_evidence.read_text(encoding='utf-8'))
        assert payload['result']['business']['status'] == 'failed'
        assert payload['checks'][0]['actual'] == 'observed bug'
        assert payload['execution']['id'] != recorder.execution_id
        assert payload['scenario_snapshot']['source_manual'] == draft['source_manual']
        all_records=load_evidence_root(tmp_path/'runs')
        original_record,message=resolve_manual_source(all_records,draft['source_manual'])
        assert original_record is not None and original_record.execution_id==recorder.execution_id,message
        window.load_root(tmp_path/'runs')
        assert len(window.records)==1 and window.records[0].business_status=='failed'
        assert window.records[0].archive_root is not None
        editor.close()
        assert not window.source_button.isVisible()
        assert all(p.read_bytes() == data for p, data in original.items())
    finally:
        if editor:
            until(lambda: editor.process is None)
            editor.close()
        window.close()


def test_limited_empty_invalid_and_cancelled_drafts(manual_site, tmp_path):
    from signup031.manual_recording import ManualRecorder
    from signup031.manual_to_tc import draft_from_manual
    from signup031.scenario_editor import ScenarioEditor
    from signup031.web_scenario import validate_scenario, load_scenario
    with ManualRecorder(manual_site[0], 'Limited', tmp_path / 'runs', headless=True) as recorder:
        recorder.page.locator('#password').fill('PRIVATE-NOT-TC')
        path = recorder.save()
    draft = draft_from_manual(path)
    assert draft['steps'] == [] and draft['checks'] == []
    assert any('sensitive' in reason for reason in draft['source_manual']['limitations'])
    assert 'PRIVATE-NOT-TC' not in json.dumps(draft)
    with pytest.raises(ValueError):
        validate_scenario(draft)
    app = QApplication.instance() or QApplication([])
    editor = ScenarioEditor(tmp_path / 'runs')
    editor.set_config(draft)
    assert '민감한 입력값' in editor.source_label.text()
    assert 'sensitive' in editor.details_text.toPlainText()
    assert '동작 없음' in editor.source_label.text()
    editor.save_to(tmp_path / 'empty.json')
    assert load_scenario(tmp_path / 'empty.json')['steps'] == []
    editor.start_run()
    assert editor.process is None
    editor.reject()  # cancel doesn't run or write original evidence
    assert len(list((tmp_path / 'runs').rglob('evidence.json'))) == 1
    broken = tmp_path / 'bad' / 'evidence.json'
    broken.parent.mkdir()
    broken.write_text('{}')
    with pytest.raises(ValueError):
        draft_from_manual(broken)
    (path.parent / 'archive' / 'resources.har').write_bytes(b'corrupt')
    with pytest.raises(ValueError):
        draft_from_manual(path)


def test_source_lookup_after_move_missing_duplicate_and_archive_identity(manual_site, tmp_path):
    from signup031.manual_recording import ManualRecorder
    from signup031.manual_to_tc import draft_from_manual, resolve_manual_source
    from signup031.viewer_model import load_evidence_root
    from signup031.web_runner import run_scenario
    from signup031.web_scenario import validate_scenario
    with ManualRecorder(manual_site[0], 'Source', tmp_path / 'runs', headless=True) as recorder:
        recorder.page.locator('#query').fill('input')
        path = recorder.save()
    config = draft_from_manual(path)
    config.pop('draft')
    config['checks'] = [{'kind': 'input_length', 'locator': 'css', 'target': '#query', 'expected': 5}]
    result = run_scenario(config, tmp_path / 'runs')
    moved = tmp_path / 'moved'
    shutil.move(str(tmp_path / 'runs'), moved)
    records = load_evidence_root(moved)
    original, _ = resolve_manual_source(records, config['source_manual'])
    assert original.execution_id == recorder.execution_id
    derived = next(record for record in records if record.scenario_snapshot)
    assert derived.archive_root is not None
    assert resolve_manual_source([derived], config['source_manual'])[0] is None
    assert resolve_manual_source([original, original], config['source_manual'])[0] is None
    original.source_path.write_text('{}')
    assert resolve_manual_source(records, config['source_manual'])[0] is None
    # Same source format but different metadata in archive is an identity failure.
    manifest_path = derived.archive_root / 'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    manifest['scenario_snapshot']['source_manual']['execution_id'] = 'different-original'
    manifest_path.write_text(json.dumps(manifest))
    reloaded = next(record for record in load_evidence_root(moved) if record.scenario_snapshot)
    assert reloaded.archive_root is None
    config['source_manual']['evidence_sha256'] = '../outside'
    with pytest.raises(ValueError, match='hash'):
        validate_scenario(config)
