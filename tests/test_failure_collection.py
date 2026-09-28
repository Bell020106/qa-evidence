"""IDE-style plugin registration, actual child pytest, no recorder-only substitute."""
import json,os,subprocess,sys
from pathlib import Path


def test_ide_mixed_run_publishes_only_individual_failures(tmp_path):
    (tmp_path/'pytest.ini').write_text('[pytest]\naddopts = -q\n',encoding='utf-8')
    (tmp_path/'conftest.py').write_text('pytest_plugins = ["signup031.selenium_plugin"]\n',encoding='utf-8')
    (tmp_path/'test_ide.py').write_text('''import pytest
def test_pass():pass
def test_failure():assert False, 'unconnected assertion'
@pytest.mark.skip(reason='not run')
def test_skip():pass
@pytest.mark.xfail(reason='expected')
def test_xfail():assert False
@pytest.mark.xfail(strict=True,reason='unexpected success')
def test_strict_xpass():pass
@pytest.fixture
def setup_error():raise RuntimeError('before driver connection')
def test_setup(setup_error):pass
@pytest.fixture
def teardown_error():
    yield
    raise RuntimeError('teardown failure')
def test_teardown(teardown_error):pass
''',encoding='utf-8')
    result=subprocess.run([sys.executable,'-m','pytest',str(tmp_path),'--selenium-artifacts',str(tmp_path/'runs')],cwd=tmp_path,capture_output=True,timeout=30)
    assert result.returncode==1,result.stdout+result.stderr
    paths=list((tmp_path/'runs').glob('*/evidence.json'));assert len(paths)==4
    from signup031.viewer_model import load_evidence_root
    records=load_evidence_root(tmp_path/'runs');assert len(records)==4
    assert all(r.business_status in ('failed','preparation_failed') and r.archive_root is None for r in records)
    for path in paths:
        payload=json.loads(path.read_bytes());assert payload['result']['pytest']['status']=='failed'
        assert '수집 연결 필요' in payload['replay']['reason']
        assert payload['selenium_record']['start_url'] is None
    assert not list(tmp_path.rglob('.qa-capture-*'))


def test_recording_options_preserve_existing_options():
    from selenium import webdriver
    from signup031.selenium_recording import recording_options
    options=webdriver.ChromeOptions();options.add_argument('--window-size=900,620');options.set_capability('acceptInsecureCerts',True)
    options.set_capability('goog:loggingPrefs',{'driver':'WARNING'})
    assert recording_options(options=options,headless=True) is options
    assert '--window-size=900,620' in options.arguments and options.capabilities['acceptInsecureCerts']
    assert options.capabilities['goog:loggingPrefs']=={'driver':'WARNING','performance':'ALL','browser':'ALL'}


def test_staging_invisible_and_cleanup_rejects_replaced_owner(tmp_path):
    import pytest
    from signup031.selenium_publication import CaptureStage,unavailable_failure
    from signup031.viewer_model import load_evidence_root
    output=tmp_path/'results';output.mkdir();keep=output/'existing.txt';keep.write_text('keep')
    stage=CaptureStage(output)
    reports=[{'when':'call','outcome':'failed','message':'synthetic','wasxfail':None}]
    path=unavailable_failure(stage,'synthetic',reports,1,'수집 연결 필요')
    assert load_evidence_root(output)==[] # Even completed evidence stays invisible until publication.
    assert load_evidence_root(stage.root)==[] # Selecting staging itself cannot bypass publication.
    published=stage.publish(path);assert published.is_file() and len(load_evidence_root(output))==1
    assert keep.read_text()=='keep'
    stage=CaptureStage(output);moved=tmp_path/'owned-moved';stage.root.rename(moved)
    stage.root.mkdir();replacement=stage.root/'keep.txt';replacement.write_text('not owned')
    with pytest.raises(ValueError,match='소유'):stage.discard()
    assert replacement.read_text()=='not owned'
