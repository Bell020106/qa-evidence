"""Practical, copy-by-file setup guide for a separate pytest project."""
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QApplication,QDialog,QFileDialog,QFormLayout,QHBoxLayout,QLabel,QLineEdit,
    QPlainTextEdit,QPushButton,QScrollArea,QTabWidget,QVBoxLayout,QWidget)


def _quoted(value):
    return '"'+str(value).replace('"','')+'"'


def _powershell_literal(value):
    return "'"+str(value).replace("'", "''")+"'"


def setup_materials(source_path,project_path):
    source=Path(source_path).resolve();project=Path(project_path).resolve()
    result=(project/'artifacts'/'selenium-failures').resolve()
    option='--selenium-artifacts='+_quoted(result)
    conftest='''pytest_plugins = ["signup031.selenium_plugin"]

import inspect
import pytest
from selenium import webdriver
from signup031.selenium_recording import recording_options


@pytest.fixture
def driver(request, selenium_record):
    # 기존 Chrome 옵션은 이 객체에 그대로 추가하세요.
    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    recording_options(options=options)
    browser = webdriver.Chrome(options=options)
    try:
        # 첫 browser.get(...)보다 먼저 연결합니다.
        selenium_record(browser, test_context={
            "tc_id": request.node.nodeid,
            "expected": "이 테스트가 기대하는 결과를 적으세요.",
            "automation_code": inspect.getsource(request.node.function),
        })
        yield browser
    finally:
        if browser.service.process is not None and browser.service.process.poll() is None:
            browser.quit()
'''
    connection_test='''from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
import pytest
from selenium.webdriver.common.by import By


@pytest.fixture(scope="module")
def local_site():
    page = """<!doctype html><meta charset='utf-8'>
    <label for='query'>검색어</label><input id='query'>
    <button id='search' onclick=\"result.textContent='검색 오류: '+query.value\">검색</button>
    <p id='result'>검색 전</p>""".encode("utf-8")
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def handle(self):
            try:
                super().handle()
            except ConnectionResetError:
                # Chrome가 페이지를 닫을 때 Windows에서 생기는 정상 연결 종료입니다.
                pass
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_pass(driver, local_site):
    driver.get(local_site)
    assert driver.find_element(By.ID, "result").text == "검색 전"


def test_fail(driver, local_site):
    driver.get(local_site)
    driver.find_element(By.ID, "query").send_keys("사과")
    driver.find_element(By.ID, "search").click()
    assert driver.find_element(By.ID, "result").text == "검색 결과: 사과"
'''
    direct_before='''from selenium import webdriver


def test_title():
    options = webdriver.ChromeOptions()
    options.add_argument("--window-size=1280,900")
    browser = webdriver.Chrome(options=options)
    try:
        browser.get("https://example.test/my-page")
        assert browser.title == "기대 제목"
    finally:
        browser.quit()
'''
    direct_after='''from selenium import webdriver
from signup031.selenium_recording import recording_options


def test_title(selenium_record):
    # 원래 옵션·URL·assert는 유지합니다.
    options = webdriver.ChromeOptions()
    options.add_argument("--window-size=1280,900")
    recording_options(options=options)
    browser = webdriver.Chrome(options=options)
    try:
        selenium_record(browser)  # 첫 get보다 앞
        browser.get("https://example.test/my-page")
        assert browser.title == "기대 제목"
    finally:
        browser.quit()
'''
    fixture_before='''import pytest
from selenium import webdriver


@pytest.fixture
def driver():
    options = webdriver.ChromeOptions()
    options.add_argument("--window-size=1280,900")
    browser = webdriver.Chrome(options=options)
    try:
        yield browser
    finally:
        browser.quit()
'''
    fixture_after='''import pytest
from selenium import webdriver
from signup031.selenium_recording import recording_options


@pytest.fixture
def driver(selenium_record):
    # 원래 옵션은 유지하고 수집 설정을 합칩니다.
    options = webdriver.ChromeOptions()
    options.add_argument("--window-size=1280,900")
    recording_options(options=options)
    browser = webdriver.Chrome(options=options)
    try:
        selenium_record(browser)  # 첫 browser.get(...)보다 앞
        yield browser
    finally:
        browser.quit()
'''
    return {
        'install_command':'python -m pip install -e '+_powershell_literal(str(source)+'[selenium]'),
        'result_path':str(result),
        'pytest_option':option,
        'pytest_ini_new':'[pytest]\naddopts = '+option+'\n',
        'conftest_new':conftest,
        'connection_test':connection_test,
        'run_command':'python -m pytest',
        'find_result_command':'Get-Item -LiteralPath '+_powershell_literal(result),
        'direct_before':direct_before,'direct_after':direct_after,
        'fixture_before':fixture_before,'fixture_after':fixture_after,
    }


class SetupGuideDialog(QDialog):
    def __init__(self,parent=None):
        super().__init__(parent);self.setWindowTitle('IDE 실패 수집 · 처음 연결 안내');self.setWindowModality(Qt.WindowModality.WindowModal)
        self.resize(1050,760);self.setMinimumSize(700,500);self.materials={};self.copy_buttons={};self.blocks={}
        layout=QVBoxLayout(self)
        self.role_text=QLabel('수집 도구 소스 폴더와 사용자 테스트 프로젝트는 서로 다른 폴더입니다. 앱 설치만으로 IDE Python에 수집 도구가 설치되지는 않습니다.')
        self.role_text.setWordWrap(True);layout.addWidget(self.role_text)
        form=QFormLayout();self.source_path=QLineEdit();self.source_path.setPlaceholderText('pyproject.toml이 있는 수집 도구 소스 폴더')
        self.project_path=QLineEdit();self.project_path.setPlaceholderText('pytest.ini·conftest.py·test_*.py를 둘 사용자 테스트 프로젝트')
        form.addRow('수집 도구 소스 폴더',self._path_row(self.source_path,self.choose_source));form.addRow('사용자 테스트 프로젝트',self._path_row(self.project_path,self.choose_project));layout.addLayout(form)
        self.path_status=QLabel('두 폴더를 선택하면 공백·한글을 포함한 절대 경로로 복사 내용을 만듭니다.');self.path_status.setWordWrap(True);layout.addWidget(self.path_status)
        self.tabs=QTabWidget();layout.addWidget(self.tabs,1)
        self._add_start_tab();self._add_ini_tab();self._add_conftest_tab();self._add_code_tab();self._add_check_tab()
        self.copy_status=QLabel('복사 버튼은 파일을 자동으로 바꾸지 않습니다. 해당 파일을 직접 열어 붙여넣고 저장하세요.');self.copy_status.setWordWrap(True);layout.addWidget(self.copy_status)
        close=QPushButton('닫고 돌아가기');close.clicked.connect(self.close);layout.addWidget(close)
        self.source_path.textChanged.connect(self.refresh);self.project_path.textChanged.connect(self.refresh);self.refresh()

    def _path_row(self,field,handler):
        widget=QWidget();row=QHBoxLayout(widget);row.setContentsMargins(0,0,0,0);row.addWidget(field,1);button=QPushButton('폴더 선택');button.clicked.connect(handler);row.addWidget(button);return widget
    def choose_source(self):
        path=QFileDialog.getExistingDirectory(self,'pyproject.toml이 있는 수집 도구 소스 폴더',self.source_path.text())
        if path:self.source_path.setText(path)
    def choose_project(self):
        path=QFileDialog.getExistingDirectory(self,'사용자 Selenium 테스트 프로젝트 폴더',self.project_path.text())
        if path:self.project_path.setText(path)

    def _page(self):
        content=QWidget();layout=QVBoxLayout(content);layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        scroll=QScrollArea();scroll.setWidgetResizable(True);scroll.setWidget(content);return scroll,layout
    def _text(self,layout,text):
        label=QLabel(text);label.setWordWrap(True);label.setTextFormat(Qt.TextFormat.PlainText);layout.addWidget(label);return label
    def _block(self,layout,key,title,height=120):
        self._text(layout,title);area=QPlainTextEdit();area.setReadOnly(True);area.setMinimumHeight(height);area.setObjectName('setupBlock_'+key);layout.addWidget(area)
        button=QPushButton(title+' 복사');button.setEnabled(False);button.clicked.connect(lambda _=False,k=key,t=title:self.copy_block(k,t));layout.addWidget(button)
        self.blocks[key]=area;self.copy_buttons[key]=button
    def _add_start_tab(self):
        page,layout=self._page();self._text(layout,'처음 한 번 연결\n1. 위에서 수집 도구 소스 폴더와 내 테스트 프로젝트를 각각 선택합니다.\n2. 테스트 프로젝트의 터미널에서 설치 명령을 실행합니다. 완료되면 Successfully installed 또는 이미 설치됨이 표시됩니다.\n3. 다음 탭에서 파일을 하나씩 만듭니다.');self._block(layout,'install_command','테스트 프로젝트 터미널에 붙일 설치 명령',55)
        self.folder_tree=QPlainTextEdit();self.folder_tree.setReadOnly(True);self.folder_tree.setMaximumHeight(125);layout.addWidget(self.folder_tree)
        self._block(layout,'result_path','앱에서 선택할 실패 저장 폴더',55);self.tabs.addTab(page,'1. 폴더·설치')
    def _add_ini_tab(self):
        page,layout=self._page();self._text(layout,'pytest.ini는 pytest 실행 옵션을 적는 파일입니다. test_*.py와 같은 프로젝트 최상위 폴더에 둡니다.\n파일이 없으면 첫 블록으로 새로 만드세요. 이미 [pytest]와 addopts가 있으면 기존 줄을 지우지 말고 두 번째 블록의 옵션만 기존 addopts 끝에 붙입니다.');self._block(layout,'pytest_ini_new','pytest.ini 새 파일 전체',85);self._block(layout,'pytest_option','기존 addopts 끝에 붙일 한 옵션',55);self.tabs.addTab(page,'2. pytest.ini')
    def _add_conftest_tab(self):
        page,layout=self._page();self._text(layout,'conftest.py는 여러 테스트가 함께 쓰는 브라우저 준비 코드를 두는 파일입니다. 새 프로젝트는 아래 전체를 사용합니다. 기존 파일이 있으면 덮어쓰지 말고 pytest_plugins와 driver fixture에 필요한 줄을 합칩니다. pytest_plugins 방식과 명령줄 -p 방식은 하나만 사용하세요.');self._block(layout,'conftest_new','conftest.py 새 파일 전체',310);self.tabs.addTab(page,'3. conftest.py')
    def _add_code_tab(self):
        page,layout=self._page();self._text(layout,'기존 URL·assert·Chrome 옵션은 그대로 둡니다. Selenium을 만드는 위치에만 수집 연결을 추가합니다. 공통 driver fixture가 있으면 그 fixture 하나를 수정하고, 테스트 함수 안에서 매번 browser를 만들면 아래의 다른 전·후 예시를 사용하세요. 각 블록은 빠진 import가 없는 전체 함수입니다.');self._block(layout,'fixture_before','공통 driver fixture 수정 전 전체',190);self._block(layout,'fixture_after','공통 driver fixture 수정 후 전체',250);self._block(layout,'direct_before','테스트 함수에서 직접 만드는 수정 전 전체',180);self._block(layout,'direct_after','테스트 함수에서 직접 만드는 수정 후 전체',250);self.tabs.addTab(page,'4. 기존 코드 수정')
    def _add_check_tab(self):
        page,layout=self._page();self._text(layout,'연결 확인 전용 파일 test_connection_check.py를 테스트 프로젝트에 만듭니다. 외부 사이트 대신 내 PC의 임시 localhost를 사용합니다. 실행 결과는 PASS 1개와 의도한 FAIL 1개입니다. 전체 pytest가 끝난 뒤 FAIL 1개만 앱 목록에 나타나면 연결 성공입니다. PASS만 실행했다면 목록이 비는 것이 정상입니다.');self._block(layout,'connection_test','test_connection_check.py 새 파일 전체',330);self._block(layout,'run_command','테스트 프로젝트 터미널에서 실행',55);self._block(layout,'find_result_command','실패 저장 폴더의 정확한 절대 경로 확인',65);self._text(layout,'다음부터 매번\nIDE에서 원래 pytest를 실행 → 전체 실행 종료 → 앱에서 같은 실패 저장 폴더 선택 → 새 실패 선택 → 로컬에서 확인 → 조사 메모 저장 → AI 원인 분석. 로컬에서 같은 문제가 안 보여도 코드 문제로 바로 확정하지 말고 타이밍·환경·자료 누락을 함께 확인하세요. AI는 별도 키/모델 연결이 있을 때만 실행되며 결과는 추정입니다.');self.tabs.addTab(page,'5. 연결 확인·매번 사용')

    def refresh(self,*_):
        source=Path(self.source_path.text().strip()) if self.source_path.text().strip() else None
        project=Path(self.project_path.text().strip()) if self.project_path.text().strip() else None
        valid_source=bool(source and source.is_dir() and (source/'pyproject.toml').is_file() and (source/'signup031'/'__init__.py').is_file())
        valid_project=bool(project and project.is_dir())
        if not valid_source or not valid_project:
            self.materials={};self.path_status.setText(('수집 도구 폴더에서 pyproject.toml과 signup031 패키지를 함께 찾지 못했습니다. ' if source and not valid_source else '')+('사용자 테스트 프로젝트 폴더를 확인하세요.' if project and not valid_project else '두 폴더를 선택하세요.'))
            for button in self.copy_buttons.values():button.setEnabled(False)
            return
        self.materials=setup_materials(source,project)
        for key,area in self.blocks.items():area.setPlainText(self.materials[key]);self.copy_buttons[key].setEnabled(True)
        self.folder_tree.setPlainText(str(project.resolve())+'\n├─ pytest.ini          ← pytest 실행 설정\n├─ conftest.py         ← 공통 브라우저·수집 연결\n├─ test_connection_check.py  ← 먼저 실행할 PASS 1 + FAIL 1 예제\n└─ artifacts\n   └─ selenium-failures ← 앱에서 선택할 폴더')
        self.path_status.setText('두 폴더를 구분했습니다. 설치 명령은 수집 도구 소스를, 저장 경로는 사용자 테스트 프로젝트를 가리킵니다.')
    def copy_block(self,key,title):
        QApplication.clipboard().setText(self.materials[key]);self.copy_status.setText(title+'을(를) 복사했습니다. 설명에 적힌 파일만 열어 붙여넣고 저장하세요.')
