# QA Evidence · IDE Selenium 실패 조사

**IDE에서 평소처럼 Selenium 테스트 실행 → 실패만 수집 → 로컬에서 직접 확인 → 조사 메모 → AI 원인 분석**을 위한 Windows 앱입니다. 코드는 사용자나 이 Codex 대화에서 작성하고, pytest는 IDE의 Python 환경에서 실행합니다. 앱 안에는 테스트 작성·실행·다른 서비스 연동 도구가 없습니다.

이 소스 저장소에는 로컬 빌드 결과와 실사용 실패 아카이브를 포함하지 않습니다. 개발 환경에서는 아래 절차로 실행하고, Windows 번들은 [빌드 스크립트](packaging/windows/build_windows.py)로 생성하세요. 생성물은 서명된 공개 릴리스가 아닙니다.

```powershell
python -m pip install -e '.[gui,selenium]'
python -m playwright install chromium
python -m signup031.viewer --root artifacts/selenium-failures
```

Windows 번들 생성에는 `packaging/windows/requirements-build.txt`의 고정 버전과 PyInstaller가 필요합니다.

```powershell
python packaging/windows/build_windows.py --output build/windows
```

## 처음 한 번 IDE에 연결하기

Python 3.11 이상, Chrome, 이 프로젝트의 **수집 도구 소스 폴더**가 필요합니다. Windows 설치 앱은 실패 자료를 읽는 앱이며, 설치만으로 IDE의 Python에 pytest 수집기가 추가되지는 않습니다.

두 폴더를 먼저 구분하세요.

- **수집 도구 소스 폴더**: 이 저장소처럼 `pyproject.toml`과 `signup031` 폴더가 함께 있는 곳
- **사용자 테스트 프로젝트**: 사용자의 `pytest.ini`, `conftest.py`, `test_*.py`를 둘 별도 폴더

앱에서 **사용법 · 용어 → IDE 연결 방법 → 내 폴더로 연결 안내 만들기**를 누르고 두 폴더를 선택하세요. 공백·한글이 들어간 실제 절대 경로를 사용해 설치 명령과 파일 내용을 따로 만듭니다. 각 블록의 복사 버튼은 파일을 자동 변경하지 않습니다.

예를 들어 수집 도구 소스가 `C:\내 작업\QA Evidence 소스`이면 사용자 테스트 프로젝트의 IDE 터미널에서 다음 형태의 명령을 실행합니다. `.` 대신 앱이 만든 실제 경로를 사용하세요.

```powershell
python -m pip install -e 'C:\내 작업\QA Evidence 소스[selenium]'
```

그 다음 안내의 **2. pytest.ini**, **3. conftest.py** 탭에서 파일별 블록을 복사합니다. 기존 파일은 덮어쓰지 않습니다. `pytest_plugins`가 이미 있으면 목록에 추가하고, `addopts`가 이미 있으면 출력 경로 옵션만 기존 줄 끝에 붙입니다. 출력 경로는 사용자 테스트 프로젝트 아래의 절대 경로로 생성됩니다.

```ini
# pytest.ini
[pytest]
addopts = --selenium-artifacts="C:\내 테스트 프로젝트\artifacts\selenium-failures"
```

```python
# conftest.py
pytest_plugins = ["signup031.selenium_plugin"]

import pytest
from selenium import webdriver
from signup031.selenium_recording import recording_options

@pytest.fixture
def driver(selenium_record):
    options = webdriver.ChromeOptions()  # 기존 options와 arguments를 유지하세요.
    recording_options(options=options)  # 수집용 logging 설정만 병합합니다.
    browser = webdriver.Chrome(options=options)
    try:
        selenium_record(browser)  # 첫 browser.get(...)보다 먼저 연결
        yield browser
    finally:
        browser.quit()
```

이미 driver fixture가 있다면 새 fixture를 중복 만들지 말고 안내의 **4. 기존 코드 수정**에서 수정 전·후 전체 함수를 비교하세요. 기존 Chrome 옵션·URL·assert를 유지하면서 `recording_options`와 `selenium_record`를 첫 `browser.get(...)`보다 앞에 둡니다. 일반 Python Run이나 unittest가 아니라 IDE의 **pytest 실행**을 사용하세요. CLI에서는 사용자 테스트 프로젝트 터미널에서 `python -m pytest`입니다. `-p signup031.selenium_plugin`과 `pytest_plugins`를 중복 등록하지 마세요. Chrome driver 첫 준비에는 다운로드가 필요할 수 있습니다.

연결 확인에는 안내의 **5. 연결 확인·매번 사용**에서 `test_connection_check.py` 전체를 복사하세요. 외부 사이트 대신 localhost를 열고 PASS 1개와 의도한 FAIL 1개를 실행합니다. pytest 전체 실행이 끝난 뒤 앱에서 안내에 표시된 실패 저장 폴더를 선택했을 때 FAIL 1개만 보이면 연결된 것입니다. PASS만 실행해 폴더가 비어 있는 것도 정상입니다.

선택적으로 TC 기대값과 코드 발췌를 연결할 수 있습니다.

```python
selenium_record(browser, test_context={
    "tc_id": "SEARCH-01",
    "expected": "검색 결과가 표시되어야 한다",
    "automation_code": "직접 제공할 현재 테스트 코드 발췌",
})
```

테스트 ID 200자, 기대값 8000자, 코드 16000자 이내입니다. 제공된 문자열만 별도 해시 sidecar로 수집합니다. 저장소를 자동 탐색하지 않습니다. 비밀값을 넣지 마세요. [실제 예제의 conftest.py](examples/failure_investigation/conftest.py)는 `inspect.getsource(request.node.function)`으로 현재 함수만 명시적으로 넘깁니다.

## 다음부터 매번 사용하는 순서

1. IDE에서 원래 pytest 테스트를 끝까지 실행합니다. 실행 중 자료는 임시이며 전체 실행 종료 뒤 FAIL만 게시됩니다.
2. **실패 수집 폴더 선택**에서 연결 안내가 만든 절대 `artifacts/selenium-failures` 폴더를 엽니다. 앱과 pytest가 같은 폴더를 가리켜야 합니다.
3. 앱 목록에 새 실패가 자동 반영됩니다. 예를 들어 10개 중 2개가 실패했다면 2개만 보입니다. 이 숫자는 사용 흐름 설명이며 현재 실행 결과가 아닙니다. 선택한 기록과 열려 있는 미저장 메모는 유지됩니다.
4. 실패를 선택하고 **로컬에서 확인**을 누릅니다. 앱용 브라우저가 없으면 **웹 브라우저 준비**를 먼저 합니다. 원본 오류와 복원 상태를 구분해 보고, 화면에서 직접 클릭·입력한 내용을 **조사 메모**에 남깁니다. 로컬 화면에서 문제가 보이지 않았다는 사실만으로 코드 문제라고 단정하지 마세요.
5. **조사 저장 후 AI 분석**을 누릅니다. 저장 실패 시 진행하지 않습니다. 기대값·코드·메모와 실제 전송 미리보기를 확인한 뒤 요청하세요. 키가 없어도 자료 확인과 내보내기는 가능합니다.

**사용법 · 용어**와 **F1**으로 현재 기능의 도움말을 열 수 있습니다. 작은 창은 세로로 스크롤합니다. 원본 오류·복원 중단 이유·정확한 근거 ID는 상세에서 확인합니다.

## 어떤 결과를 보관하나요?

- pytest의 개별 setup/call/teardown report 중 실제 `failed`가 있는 테스트만 최종 저장합니다. PASS·SKIP·일반 XFAIL은 보관하지 않습니다. strict XPASS와 teardown 오류는 pytest가 실패로 판정하므로 보관합니다.
- 전체 세션의 exit 1을 모든 테스트의 실패로 간주하지 않습니다. 원래 판정과 종료 코드를 바꾸지 않습니다.
- 실행 중 자료는 출력 폴더의 고유 `.qa-capture-*` 임시 영역에 두고 앱 스캐너에서 제외합니다. 비실패 테스트의 임시 자료·큰 버퍼는 teardown 확정 후 정리합니다. 실패는 최종 session exit code까지 확인한 뒤 완성된 디렉터리를 원자적으로 공개합니다.
- 연결 전 실패는 pytest 오류만 남고 **로컬 복원 자료 없음 / 수집 연결 필요**로 표시됩니다. driver 조기 종료나 수집 실패는 별도로 남습니다. teardown에서 처음 실패하면 자료는 실패 이전의 마지막 관측이며 실패 순간 화면이 아닙니다.
- 과거 PASS가 섞인 폴더에서도 기본 목록에는 실패만 보입니다. 손상되거나 지원하지 않는 자료는 별도 안내하며 실패로 위장하지 않습니다. 기존 파일은 변경하지 않습니다.

## 실제 localhost 예제

프로젝트 루트에서 다음을 실행합니다. 예제의 [pytest.ini](examples/failure_investigation/pytest.ini)와 [conftest.py](examples/failure_investigation/conftest.py)가 위 IDE 연결을 그대로 사용합니다.

```powershell
python -m pytest -c examples/failure_investigation/pytest.ini examples/failure_investigation/test_search_failure.py
$LASTEXITCODE  # 1: 의도적으로 만든 assertion 실패
```

검색어 `사과`를 입력했을 때 기대값은 `검색 결과: 사과`, 실제 화면은 `검색 오류: 사과`라서 실패합니다. 테스트 종료 시 localhost 서버와 Chrome도 종료됩니다. 앱에서 `artifacts/selenium-failures`를 열고 로컬 화면에서 검색어를 `배`로 바꿔 검색하면 `검색 오류: 배`를 직접 확인할 수 있습니다.

PASS·FAIL·SKIP·XFAIL·strict XPASS·setup/teardown·조기 종료를 함께 확인하려면 파일 대신 `examples/failure_investigation` 폴더를 실행합니다. 이는 의도적인 실패 모음이며 pytest 전체 통과 예제가 아닙니다.

예제를 실행하면 로컬 `artifacts/selenium-failures`에 실패 결과가 생성됩니다. 이 폴더는 비공개 작업 산출물이며 Git에 포함하지 않습니다. 실행별 `evidence.json`, `archive`, `test-context.json`은 필요한 범위에서 로컬로 함께 보관하세요. 없는 첨부를 만들어내거나 과거 미수집 실행을 소급 복원하지 않습니다.

## 지원 범위와 검증

로컬 Chrome/Chromium Selenium 세션의 수집된 GET/HEAD 응답·DOM·지원 동작을 기반으로 오프라인 조사를 합니다. 미수집 HTTP/WebSocket은 차단하며 원격 서버로 fallback하지 않습니다. 서버 DB·로그인·전체 JS 메모리·미수집 영상·모든 사이트를 완전히 복제하지 않습니다. 복원 상태, 원본 pytest 실패, 사람의 수동 판단은 서로 별개입니다.

AI는 제한된 오류·관측·명시적으로 제공한 기대값/코드·저장된 조사 메모를 바탕으로 원인 후보와 다음 확인을 제안합니다. 전체 HAR·HTML·저장소는 자동 전송하지 않습니다. 별도 OpenAI API 키와 모델이 필요하며 Codex 계정을 대신 사용하지 않습니다. 키는 창 메모리에만 두고 닫을 때 지웁니다. 근거·응답 구조 검증 후 분석 이력을 `.qa`에 저장하며 pytest 결과를 덮어쓰지 않습니다.

실제 외부 AI API/분석 정확도는 검증하지 않았습니다. 연결 검증은 명시적인 localhost HTTP 대역으로 수행합니다. 서명·공개 배포·clean Windows VM 검증도 포함하지 않습니다.

2026-09-28 HelpyChat 적용 결과와 한계는 [공개 검증 요약](docs/qa/helpychat-validation-2026-09-28.md)에 집계했습니다. 원시 HAR, 화면 캡처, 인증 프로필과 로컬 report는 저장소에 포함하지 않습니다.

업로드 전 제품 검증은 [제품 테스트 결과](docs/qa/product-validation-2026-09-28.md)에 기록했습니다. 전체 실행은 386개 통과·1개 실패였으며, 브라우저가 차단한 테스트용 포트 오류 항목은 코드 변경 없는 단독 재실행에서 통과했습니다.

개발 환경에서 앱을 직접 실행할 때만 Qt와 앱용 Chromium을 추가합니다. 설치 앱 사용자는 Qt를 따로 설치하지 않습니다. 아래 `C:\path\to\QA Evidence source`는 실제 소스 폴더로 바꾸세요.

```powershell
python -m pip install -e 'C:\path\to\QA Evidence source[gui,selenium]'
python -m playwright install chromium
python -m signup031.viewer --root artifacts/selenium-failures
```
