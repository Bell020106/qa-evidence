import json
import shutil

import pytest
from test_manual_recording import manual_site


@pytest.fixture
def investigation_runs(manual_site, tmp_path):
    from signup031.manual_recording import ManualRecorder
    from signup031.web_runner import run_scenario
    root = tmp_path / 'runs'
    config = {'version': 1, 'id': 'SEARCH', 'title': 'Search', 'url': manual_site[0],
              'steps': [{'action': 'fill', 'locator': 'css', 'target': '#query', 'value': 'bug'},
                        {'action': 'click', 'locator': 'css', 'target': '#apply', 'value': ''}],
              'checks': [{'kind': 'text', 'locator': 'css', 'target': '#result', 'expected': 'healthy'}]}
    failed = run_scenario(config, root)
    config['steps'][0]['value'] = 'healthy'
    passed = run_scenario(config, root)
    with ManualRecorder(manual_site[0] + '/note', 'Manual', root, headless=True) as recorder:
        recorder.page.locator('#note').fill('actual only')
        manual = recorder.save()
    return root, failed, passed, manual


def test_notes_report_retest_move_and_originals_unchanged(investigation_runs, tmp_path):
    from signup031.investigation import InvestigationStore, investigation_candidates
    root, failed, passed, manual = investigation_runs
    original = {p.relative_to(root): p.read_bytes() for p in root.rglob('*') if p.is_file()}
    store = InvestigationStore(root)
    fid = json.loads(failed.read_text(encoding='utf-8'))['execution']['id']
    pid = json.loads(passed.read_text(encoding='utf-8'))['execution']['id']
    mid = json.loads(manual.read_text(encoding='utf-8'))['execution']['id']
    doc = store.load(fid)
    assert doc['classification'] == '미분류'
    doc['notes'] = 'QA note'
    doc['grounds'] = 'Observed result differs from approved requirement'
    doc = store.save(doc)
    doc['classification'] = '제품 결함'
    doc['report']['title'] = 'Edited report'
    doc['report']['expected'] = 'Requirement: healthy'
    doc = store.save(doc)
    assert len(doc['history']) == 2
    candidates = investigation_candidates(store.record(fid))
    assert candidates and all(c['status'] == '미확인' and c['evidence'] and c['missing'] and c['next'] for c in candidates)
    doc = store.link_retest(doc, pid, 'Corrected test input for verification')
    assert doc['retests'][0]['source_status'] == 'failed'
    assert doc['retests'][0]['target_status'] == 'passed'
    report = store.export(doc)
    text = report.read_text(encoding='utf-8')
    assert 'Edited report' in text and 'Requirement: healthy' in text
    assert '미수집' in text and '../' in text and str(root) not in text
    manual_doc = store.load(mid)
    assert manual_doc['report']['expected'] == '미수집'
    manual_doc['classification'] = '환경'
    manual_doc['report']['title'] = 'Manual QA report'
    manual_doc = store.save(manual_doc)
    assert 'Manual QA report' in store.export(manual_doc).read_text(encoding='utf-8')
    assert all((root / p).read_bytes() == data for p, data in original.items())
    moved = tmp_path / 'moved'
    shutil.move(str(root), moved)
    reopened = InvestigationStore(moved)
    assert reopened.load(fid) == doc
    assert reopened.record(mid).business_status == 'unjudged'
    assert reopened.record(fid).business_status == 'failed'
    assert reopened.record(pid).business_status == 'passed'
    assert reopened.retest_details(doc)[0]['available']


def test_invalid_retests_corruption_write_failure_and_boundaries(investigation_runs, tmp_path):
    from signup031.investigation import InvestigationStore
    root, failed, passed, manual = investigation_runs
    store = InvestigationStore(root)
    fid, pid, mid = [json.loads(p.read_text(encoding='utf-8'))['execution']['id'] for p in (failed, passed, manual)]
    doc = store.save(store.load(fid))
    for target in (fid, 'missing'):
        with pytest.raises(ValueError):
            store.link_retest(doc, target, 'reason')
    with pytest.raises(ValueError, match='이유'):
        store.link_retest(doc, mid, '')
    linked = store.link_retest(doc, mid, 'Different TC and URL; exploratory comparison')
    assert any('TC' in difference for difference in linked['retests'][0]['differences'])
    assert any('URL' in difference for difference in linked['retests'][0]['differences'])
    with pytest.raises(ValueError):
        store.link_retest(linked, mid, 'duplicate')
    with pytest.raises(ValueError):
        store.save(doc)  # stale editor must not overwrite a newer investigation
    duplicate = root / 'duplicate'
    shutil.copytree(passed.parent, duplicate)
    with pytest.raises(ValueError, match='중복'):
        store.load(pid)
    har = manual.parent / 'archive' / 'resources.har'
    har_bytes = har.read_bytes()
    har.write_bytes(b'broken HAR')
    assert not store.retest_details(linked)[0]['available']
    har.write_bytes(har_bytes)
    manual.unlink()
    assert not store.retest_details(linked)[0]['available']
    qa_file = store.path_for(fid)
    saved = qa_file.read_bytes()
    qa_file.write_text('{broken')
    with pytest.raises(ValueError):
        store.load(fid)
    with pytest.raises(ValueError):
        store.save(linked)
    assert qa_file.read_text(encoding='utf-8') == '{broken'
    qa_file.write_bytes(saved)
    import stat
    qa_file.chmod(stat.S_IREAD)
    try:
        changed = store.load(fid)
        changed['notes'] = 'write must fail without losing previous data'
        with pytest.raises(OSError):
            store.save(changed)
        assert qa_file.read_bytes() == saved
    finally:
        qa_file.chmod(stat.S_IREAD | stat.S_IWRITE)
    assert not list(qa_file.parent.glob('*.tmp'))
    # Actual filesystem collision at export destination, not a mocked writer.
    (root / '.qa' / 'exports').write_text('not a directory')
    with pytest.raises((OSError, ValueError)):
        store.export(linked)
    assert qa_file.read_bytes() == saved
    with pytest.raises(ValueError):
        InvestigationStore(failed.parent).save(linked)
    with pytest.raises(ValueError):
        store.load('../outside')
    outside = tmp_path / 'outside'
    outside.mkdir()
    # Windows junction boundary: no elevated symlink privilege needed.
    import subprocess
    junction_root = tmp_path / 'junction-root'
    junction_root.mkdir()
    subprocess.run(['cmd', '/c', 'mklink', '/J', str(junction_root / '.qa'), str(outside)], check=True, capture_output=True)
    with pytest.raises(ValueError):
        InvestigationStore(junction_root).path_for(fid)
    assert list(outside.iterdir()) == []


def test_collected_legacy_environment_is_used_and_compared(tmp_path):
    from test_viewer_model import _current_payload, _write_payload
    from signup031.investigation import InvestigationStore, execution_differences, investigation_candidates
    first = _current_payload(execution_id='legacy-one')
    first['environment'] = {'browser': 'chromium', 'browser_version': '152.0', 'driver_version': '152.1', 'platform': 'collected OS A', 'python': '3.11'}
    second = _current_payload(execution_id='legacy-two')
    second['environment'] = {'browser': 'firefox', 'browser_version': '153.0', 'driver_version': '153.1', 'platform': 'collected OS B', 'python': '3.11'}
    _write_payload(tmp_path / 'one' / 'evidence.json', first)
    _write_payload(tmp_path / 'two' / 'evidence.json', second)
    store = InvestigationStore(tmp_path)
    assert 'collected OS A' in store.load('legacy-one')['report']['environment']
    differences = execution_differences(store.record('legacy-one'), store.record('legacy-two'))
    assert any('firefox' in d and 'chromium' in d for d in differences)
    assert any('152.0' in d and '153.0' in d for d in differences)
    assert any('152.1' in d and '153.1' in d for d in differences)
    assert all(c['status'] == '미확인' for c in investigation_candidates(store.record('legacy-one')))

