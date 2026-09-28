"""CSV registration preserves literal manual cases and immutable versions."""
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
from pathlib import Path
import sqlite3

import pytest

MAPPING = {'original_id': 0, 'title': 1, 'preconditions': 2, 'steps': 3, 'expected': 4, 'priority': 5}
CSV = 'ID,제목,사전 조건,절차,기대 결과,우선순위\n001,"로그인, 확인",준비,"첫 단계\n둘째 ""단계""",성공,P1\n002,검토,,수동 절차,,P2\n003,,,,,\n'


def api():
    assert importlib.util.find_spec('signup031.tc_import') is not None, 'CSV registration is not implemented'
    from signup031.tc_import import parse_csv, TCStore
    return parse_csv, TCStore


def register(store, path, *, project='demo', source='suite-a', mapping=MAPPING, **kwargs):
    parse, _ = api()
    batch = parse(path, **kwargs)
    return store.register(project, source, batch, mapping)


def test_csv_preserves_logical_rows_text_and_counts_after_store_reopen(tmp_path):
    parse, Store = api()
    path = tmp_path / 'cases.csv'; path.write_bytes(CSV.encode('utf-8-sig'))
    parsed = parse(path)
    assert parsed['rows'][0]['logical_row'] == 2
    assert parsed['rows'][0]['physical_start'] == 2 and parsed['rows'][0]['physical_end'] == 3
    store = Store(tmp_path / 'results'); outcome = store.register('demo', 'suite-a', parsed, MAPPING)
    assert outcome['counts'] == {'registered': 2, 'duplicate': 0, 'candidate': 0, 'error': 1, 'review_needed': 1}
    assert outcome['rows'][-1]['logical_row'] == 4 and '제목' in outcome['rows'][-1]['reason']
    reopened = Store(tmp_path / 'results'); rows = reopened.list_cases('demo')
    first = next(r for r in rows if r['original_id'] == '001')
    assert first['fields']['title'] == '로그인, 확인'
    assert first['fields']['steps'] == '첫 단계\n둘째 "단계"'
    assert first['version'] == 1 and first['automation'] is None
    assert first['source']['logical_row'] == 2 and first['source']['encoding'] == 'utf-8-sig'
    assert reopened.get_mapping('demo', parsed['headers']) == MAPPING
    assert path.read_text(encoding='utf-8-sig') == CSV


def test_reimport_concurrency_and_project_source_scopes(tmp_path):
    _, Store = api(); path = tmp_path / 'cases.csv'; path.write_text(CSV, encoding='utf-8')
    root = tmp_path / 'results'; Store(root)
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(lambda _: register(Store(root), path), range(4)))
    assert sum(r['counts']['registered'] for r in results) == 2
    assert len(Store(root).list_cases('demo')) == 2
    assert register(Store(root), path, project='other')['counts']['registered'] == 2
    assert register(Store(root), path, source='suite-b')['counts']['registered'] == 2


def test_changes_require_explicit_application_and_preserve_versions(tmp_path):
    _, Store = api(); store = Store(tmp_path / 'results')
    path = tmp_path / 'cases.csv'; path.write_text(CSV, encoding='utf-8')
    register(store, path); first = next(r for r in store.list_cases('demo') if r['original_id'] == '001')
    path.write_text(CSV.replace('로그인, 확인', '로그인 수정'), encoding='utf-8')
    result = register(store, path)
    assert result['counts']['candidate'] == 1 and result['counts']['duplicate'] == 1
    assert store.case(first['local_id'])['version'] == 1
    candidate = store.list_candidates('demo')[0]
    store.apply_candidate('demo', candidate['id'], target_id=first['local_id'], expected_version=1)
    assert store.case(first['local_id'])['version'] == 2
    assert store.version(first['local_id'], 1)['fields']['title'] == '로그인, 확인'
    assert store.version(first['local_id'], 2)['fields']['title'] == '로그인 수정'
    assert register(store, path)['counts']['candidate'] == 0


def test_duplicate_ids_are_errors_and_missing_ids_are_uncertain_across_batches(tmp_path):
    _, Store = api(); store = Store(tmp_path / 'results'); path = tmp_path / 'cases.csv'
    path.write_text('ID,제목,사전 조건,절차,기대 결과,우선순위\n01,첫째,,절차,기대,\n01,둘째,,절차,기대,\n,수동A,,절차,기대,\n,수동B,,절차,기대,\n', encoding='utf-8')
    result = register(store, path)
    assert result['counts']['error'] == 2 and result['counts']['registered'] == 2
    assert register(store, path)['counts']['registered'] == 0
    path.write_text(path.read_text(encoding='utf-8').replace('수동A','수동수정'), encoding='utf-8')
    result = register(store, path)
    assert result['counts']['candidate'] == 2
    candidate = store.list_candidates('demo')[0]
    assert candidate['target_id'] is None and candidate['reason'] == 'identity_uncertain'
    store.apply_candidate('demo', candidate['id'], as_new=True)
    assert len(store.list_cases('demo')) == 3


def test_mapping_header_position_encoding_and_malformed_input(tmp_path):
    parse, Store = api(); store = Store(tmp_path / 'results'); path = tmp_path / 'cases.csv'
    path.write_bytes(('설명\n' + CSV).encode('cp949'))
    with pytest.raises(ValueError, match='인코딩'): parse(path)
    parsed = parse(path, encoding='cp949', header_row=2)
    assert parsed['headers'][1] == '제목' and parsed['rows'][0]['logical_row'] == 3
    store.register('demo','suite-a',parsed,MAPPING)
    moved = list(reversed(parsed['headers']))
    assert store.get_mapping('demo', moved) is None
    path.write_text('제목,제목\na,b\n',encoding='utf-8')
    parsed = parse(path)
    assert parsed['duplicate_headers'] == ['제목']
    with pytest.raises(ValueError): store.register('demo','s',parsed,{'title':7})
    assert store.register('demo','s',parsed,{'title':1})['counts']['registered'] == 1
    path.write_text('title\n"unterminated',encoding='utf-8')
    with pytest.raises(ValueError, match='CSV'): parse(path)


def test_literal_cells_never_execute_and_failed_transaction_or_cancel_is_atomic(tmp_path, monkeypatch):
    _, Store = api(); store = Store(tmp_path / 'results'); path = tmp_path / 'cases.csv'
    path.write_text('ID,제목,사전 조건,절차,기대 결과,우선순위\n01,=HYPERLINK(""http://127.0.0.1:9""),,__import__("os"),literal,\n',encoding='utf-8')
    # Literal content is data; any request/process invocation is a regression.
    import socket, subprocess
    monkeypatch.setattr(socket.socket, 'connect', lambda *_: pytest.fail('unexpected network'))
    monkeypatch.setattr(subprocess, 'Popen', lambda *_a, **_k: pytest.fail('unexpected execution'))
    result = register(store,path); assert result['counts']['registered'] == 1
    assert '__import__' in store.list_cases('demo')[0]['fields']['steps']
    path.write_text(CSV,encoding='utf-8')
    with sqlite3.connect(store.db) as conn:
        conn.execute("CREATE TRIGGER fail_insert BEFORE INSERT ON tc_versions WHEN NEW.logical_row=3 BEGIN SELECT RAISE(ABORT,'controlled storage failure'); END")
    with pytest.raises(sqlite3.DatabaseError): register(store,path,source='new-source')
    assert len(store.list_cases('demo')) == 1
    with sqlite3.connect(store.db) as conn: conn.execute('DROP TRIGGER fail_insert')
    assert register(store,path,source='new-source')['counts']['registered'] == 2
    parse, _ = api(); batch = parse(path)
    calls = []
    def cancel(): calls.append(1); return len(calls) > 1
    with pytest.raises(ValueError, match='취소'): store.register('demo','cancelled-source',batch,MAPPING,cancel=cancel)
    assert len(store.list_cases('demo')) == 3


def test_input_limits_fail_before_registration(tmp_path):
    parse, _ = api(); path = tmp_path / 'cases.csv'
    path.write_bytes(b'x' * (10 * 1024 * 1024 + 1))
    with pytest.raises(ValueError, match='상한'): parse(path)
    path.write_text('title\n' + 'row\n'*10001,encoding='utf-8')
    with pytest.raises(ValueError, match='상한'): parse(path)


def test_candidate_preview_version_conflict_and_no_id_target_selection(tmp_path):
    _, Store = api(); store = Store(tmp_path/'results'); path = tmp_path/'cases.csv'
    path.write_text('ID,title\n,first\n',encoding='utf-8')
    register(store,path,mapping={'title':1,'original_id':0})
    first = store.list_cases('demo')[0]
    path.write_text('ID,title\n,second\n',encoding='utf-8')
    register(store,path,mapping={'title':1,'original_id':0})
    register(store,path,mapping={'title':1,'original_id':0})
    assert len(store.list_candidates('demo')) == 1
    second = store.list_candidates('demo')[0]
    path.write_text('ID,title\n,third\n',encoding='utf-8')
    register(store,path,mapping={'title':1,'original_id':0})
    third = next(c for c in store.list_candidates('demo') if c['id'] != second['id'])
    store.apply_candidate('demo',second['id'],target_id=first['local_id'],expected_version=1)
    with pytest.raises(ValueError, match='버전'):
        store.apply_candidate('demo',third['id'],target_id=first['local_id'],expected_version=1)
    assert store.case(first['local_id'])['fields']['title'] == 'second'


def test_qt_file_mapping_registration_and_restart(tmp_path, monkeypatch):
    import os
    os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
    from PySide6.QtWidgets import QApplication, QFileDialog
    from signup031.viewer import EvidenceViewerWindow
    from test_scenario_editor import until
    path=tmp_path/'cases.csv'; path.write_bytes(CSV.encode('utf-8-sig'))
    app=QApplication.instance() or QApplication([])
    root=tmp_path/'results'; viewer=EvidenceViewerWindow(root)
    try:
        assert hasattr(viewer,'tc_catalog_button'), 'TC catalog needs a PC entrypoint'
        viewer.tc_catalog_button.click(); catalog=viewer.tc_catalog_dialog
        catalog.project_edit.setText('demo')
        monkeypatch.setattr(QFileDialog,'getOpenFileName',lambda *_a,**_k:(str(path),'CSV'))
        catalog.import_button.click(); importer=catalog.import_dialog
        until(lambda: importer.process is None)
        assert importer.path_edit.text()==str(path)
        importer.source_edit.setText('suite-a')
        for field,index in MAPPING.items(): importer.mappings[field].setCurrentIndex(index+1)
        importer.start_import(); importer.start_import()
        until(lambda: importer.process is None)
        assert '등록 2' in importer.status_label.text() and '오류 1' in importer.status_label.text()
        importer.close(); catalog.refresh()
        assert catalog.table.rowCount()==2
        catalog.close(); viewer.close()
        restarted=EvidenceViewerWindow(root)
        try:
            restarted.tc_catalog_button.click(); catalog=restarted.tc_catalog_dialog
            catalog.project_edit.setText('demo'); catalog.refresh()
            assert catalog.table.rowCount()==2
            catalog.table.selectRow(0)
            assert '001' in catalog.details.toPlainText() and '자동 실행 설정 없음' in catalog.details.toPlainText()
            catalog.import_button.click(); importer=catalog.import_dialog
            until(lambda: importer.process is None)
            assert importer.mappings['title'].currentData()==1
            importer.source_edit.setText('suite-a'); importer.start_import()
            until(lambda: importer.process is None)
            assert '등록 0' in importer.status_label.text() and '중복 2' in importer.status_label.text()
            importer.close(); catalog.close()
        finally: restarted.close()
    finally: viewer.close(); app.processEvents()


def test_whitespace_id_preserved_and_stale_candidate_can_be_explicitly_reviewed(tmp_path):
    _,Store=api(); store=Store(tmp_path/'results'); path=tmp_path/'cases.csv'
    path.write_text('ID,title\n   ,first\n',encoding='utf-8')
    mapping={'original_id':0,'title':1}; register(store,path,mapping=mapping)
    target=store.list_cases('demo')[0]
    path.write_text('ID,title\n   ,changed\n',encoding='utf-8'); register(store,path,mapping=mapping)
    candidate=store.list_candidates('demo')[0]
    store.apply_candidate('demo',candidate['id'],target_id=target['local_id'],expected_version=1)
    assert store.case(target['local_id'])['fields']['original_id']=='   '
    path.write_text('ID,title\n001,first\n',encoding='utf-8'); register(store,path,mapping=mapping)
    target=next(r for r in store.list_cases('demo') if r['original_id']=='001')
    for title in ('second','third'):
        path.write_text('ID,title\n001,'+title+'\n',encoding='utf-8'); register(store,path,mapping=mapping)
    candidates=store.list_candidates('demo')
    store.apply_candidate('demo',candidates[0]['id'],target_id=target['local_id'],expected_version=1)
    with pytest.raises(ValueError): store.apply_candidate('demo',candidates[1]['id'],target_id=target['local_id'],expected_version=2)
    store.review_candidate('demo',candidates[1]['id'],expected_version=2)
    store.apply_candidate('demo',candidates[1]['id'],target_id=target['local_id'],expected_version=2)
    assert store.case(target['local_id'])['version']==3


def test_qt_large_input_cancel_keeps_ui_responsive_and_database_atomic(tmp_path):
    import os
    os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
    from PySide6.QtCore import QTimer
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from signup031.viewer import EvidenceViewerWindow
    from signup031.tc_catalog_dialog import CsvImportDialog
    from test_scenario_editor import until
    _,Store=api(); app=QApplication.instance() or QApplication([])
    viewer=EvidenceViewerWindow(tmp_path/'results'); viewer.open_tc_catalog(); catalog=viewer.tc_catalog_dialog
    catalog.project_edit.setText('demo')
    path=tmp_path/'large.csv'; path.write_text('title\n'+'large manual case\n'*10000,encoding='utf-8')
    importer=CsvImportDialog(catalog,path)
    try:
        until(lambda:importer.process is None)
        assert importer.snapshot['total_rows']==10000 and importer.preview.rowCount()==20
        with sqlite3.connect(catalog.store.db) as blocker:
            blocker.execute('BEGIN IMMEDIATE')
            ticks=[]; timer=QTimer(); timer.timeout.connect(lambda:ticks.append(1)); timer.start(20)
            importer.start_import(); importer.start_import(); QTest.qWait(250)
            assert len(ticks)>=3 and importer.process is not None
            importer.cancel(); until(lambda:importer.process is None,3)
            timer.stop()
        assert catalog.store.count_cases('demo')==0
        assert '취소' in importer.status_label.text()
        with sqlite3.connect(catalog.store.db) as blocker:
            blocker.execute('BEGIN IMMEDIATE')
            catalog.import_dialog=importer
            importer.start_import(); QTest.qWait(100); viewer.close()
            until(lambda:importer.process is None,3)
        assert catalog.store.count_cases('demo')==0
    finally: importer.close(); catalog.close(); viewer.close(); app.processEvents()


def test_all_cases_and_candidates_are_pageable_and_apply_fails_fast_when_busy(tmp_path):
    import time
    _,Store=api(); store=Store(tmp_path/'results'); path=tmp_path/'many.csv'
    original='ID,title\n'+''.join(f'{i:04d},Case {i}\n' for i in range(1005))
    path.write_text(original,encoding='utf-8'); register(store,path,mapping={'original_id':0,'title':1})
    assert store.list_cases('demo',limit=10,offset=1000)[-1]['original_id']=='1004'
    path.write_text(original.replace('Case','Changed'),encoding='utf-8'); register(store,path,mapping={'original_id':0,'title':1})
    assert len(store.list_candidates('demo',limit=10,offset=1000))==5
    candidate=store.list_candidates('demo')[0]
    with sqlite3.connect(store.db) as blocker:
        blocker.execute('BEGIN IMMEDIATE'); started=time.monotonic()
        with pytest.raises(sqlite3.OperationalError):
            store.apply_candidate('demo',candidate['id'],target_id=candidate['target_id'],expected_version=1)
        assert time.monotonic()-started < 1


def test_qt_page_change_updates_selected_case_and_version_details(tmp_path):
    import os
    os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
    from PySide6.QtWidgets import QApplication
    from signup031.viewer import EvidenceViewerWindow
    _,Store=api(); root=tmp_path/'results'; store=Store(root); path=tmp_path/'page.csv'
    path.write_text('ID,title\n'+''.join(f'{i},Case {i}\n' for i in range(205)),encoding='utf-8')
    register(store,path,mapping={'original_id':0,'title':1})
    app=QApplication.instance() or QApplication([]); viewer=EvidenceViewerWindow(root); viewer.open_tc_catalog()
    catalog=viewer.tc_catalog_dialog
    try:
        catalog.project_edit.setText('demo'); catalog.refresh(); catalog.table.selectRow(0)
        assert 'Case 0' in catalog.details.toPlainText()
        catalog.next_button.click()
        assert catalog.table.rowCount()==5
        assert 'Case 200' in catalog.details.toPlainText() and 'Case 0' not in catalog.details.toPlainText()
        assert catalog.version_combo.currentData()==1
    finally:catalog.close();viewer.close();app.processEvents()


def test_qt_unwritable_catalog_reports_failure_without_replacing_existing_file(tmp_path):
    import os
    os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
    from PySide6.QtWidgets import QApplication
    from signup031.viewer import EvidenceViewerWindow
    root=tmp_path/'results';root.mkdir();occupied=root/'.tc';occupied.write_text('existing file',encoding='utf-8')
    app=QApplication.instance() or QApplication([]);viewer=EvidenceViewerWindow(root)
    try:
        viewer.open_tc_catalog()
        assert 'TC 목록 열기 실패' in viewer.source_label.text()
        assert occupied.read_text(encoding='utf-8')=='existing file'
    finally:viewer.close();app.processEvents()
