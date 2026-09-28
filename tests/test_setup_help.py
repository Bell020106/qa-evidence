import os
from pathlib import Path
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from PySide6.QtWidgets import QApplication


def test_materials_use_distinct_absolute_unicode_paths(tmp_path):
    from signup031.setup_help import setup_materials
    source=tmp_path/'수집 도구 source';project=tmp_path/'내 테스트 프로젝트'
    source.mkdir();project.mkdir();(source/'pyproject.toml').write_text('[project]\nname="collector"')
    (source/'signup031').mkdir();(source/'signup031'/'__init__.py').write_text('')
    materials=setup_materials(source,project)
    assert str(source.resolve()) in materials['install_command']
    assert "'"+str(source.resolve())+"[selenium]'" in materials['install_command']
    result=str((project/'artifacts/selenium-failures').resolve())
    assert result in materials['pytest_ini_new'] and result in materials['pytest_option']
    assert str(project.resolve()) not in materials['install_command']
    assert 'pytest_plugins = ["signup031.selenium_plugin"]' in materials['conftest_new']
    assert 'recording_options(options=options)' in materials['conftest_new']
    assert 'selenium_record(browser' in materials['conftest_new']
    assert 'http://127.0.0.1:' in materials['connection_test']
    assert 'def test_pass' in materials['connection_test'] and 'def test_fail' in materials['connection_test']
    assert 'except ConnectionResetError:' in materials['connection_test']
    assert materials['run_command']=='python -m pytest'
    assert materials['find_result_command'].startswith('Get-Item -LiteralPath ')
    for key in ('conftest_new','connection_test','fixture_before','fixture_after','direct_before','direct_after'):
        compile(materials[key],key,'exec')
    assert 'def test_title(selenium_record):' in materials['direct_after']
    assert 'options.add_argument("--window-size=1280,900")' in materials['direct_before']
    assert 'options.add_argument("--window-size=1280,900")' in materials['direct_after']
    assert 'browser.get("https://example.test/my-page")' in materials['direct_before']
    assert 'browser.get("https://example.test/my-page")' in materials['direct_after']


def test_setup_dialog_has_file_steps_and_individual_copy(tmp_path):
    from signup031.setup_help import SetupGuideDialog
    app=QApplication.instance() or QApplication([])
    source=tmp_path/'도구 소스 폴더';project=tmp_path/'테스트 프로젝트 폴더'
    source.mkdir();project.mkdir();(source/'pyproject.toml').write_text('[project]\nname="collector"')
    (source/'signup031').mkdir();(source/'signup031'/'__init__.py').write_text('')
    dialog=SetupGuideDialog();dialog.source_path.setText(str(source));dialog.project_path.setText(str(project));app.processEvents()
    assert dialog.tabs.count()>=5
    assert {'install_command','pytest_ini_new','conftest_new','connection_test','run_command','fixture_before','fixture_after'}<=set(dialog.copy_buttons)
    assert '수집 도구 소스 폴더' in dialog.role_text.text() and '사용자 테스트 프로젝트' in dialog.role_text.text()
    assert 'pytest.ini' in dialog.folder_tree.toPlainText() and 'conftest.py' in dialog.folder_tree.toPlainText()
    dialog.copy_buttons['pytest_ini_new'].click()
    assert QApplication.clipboard().text()==dialog.materials['pytest_ini_new']
    assert 'pytest.ini' in dialog.copy_status.text() and '복사' in dialog.copy_status.text()
    dialog.copy_buttons['conftest_new'].click()
    assert QApplication.clipboard().text()==dialog.materials['conftest_new']
    dialog.resize(900,620);dialog.show();app.processEvents()
    assert dialog.width()==900 and dialog.height()==620
    dialog.close()


def test_powershell_commands_quote_apostrophes(tmp_path):
    from signup031.setup_help import setup_materials
    source=tmp_path/"O'Brien 도구";project=tmp_path/"내 프로젝트's"
    materials=setup_materials(source,project)
    assert "O''Brien 도구" in materials['install_command']
    assert "내 프로젝트''s" in materials['find_result_command']
