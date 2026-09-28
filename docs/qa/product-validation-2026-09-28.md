# 제품 검증 — 2026-09-28

GitHub 업로드 전 Windows 개발 환경에서 다음 제품 테스트를 실행했다.

```powershell
$env:QT_QPA_PLATFORM = 'offscreen'
python -m pytest tests -q
```

- 전체 실행: 387개 중 **386 PASS, 1 FAIL**, 오류/건너뜀 0. 약 427초.
- 실패 항목: `tests/test_manual_to_tc.py::test_record_to_draft_expected_result_run_and_source_navigation[note]`.
- 관측: 테스트 HTTP 서버에 자동 할당된 localhost 포트 5061에서 Chromium의 `net::ERR_UNSAFE_PORT`가 발생했다. 화면 이동 단계에서 차단되어 해당 실행의 기능 assertion에 도달하지 못했다.
- 코드 변경 없이 실패 항목만 한 번 다시 실행한 결과: **1 PASS**, 약 3초. 최초 전체 실행 실패는 그대로 보존했으며 전체 387개가 한 실행에서 통과한 것으로 표현하지 않는다.

테스트 서버는 운영체제에 포트 자동 할당을 요청한다. 브라우저가 금지하는 포트를 선택할 가능성이 남아 있으므로 테스트 환경의 재현성 개선 과제다. 이번 게시 작업에서 제품 코드나 assertion을 수정하지 않았다.

이 결과는 제품 자체의 로컬 검증이며 [실제 HelpyChat 적용 결과](helpychat-validation-2026-09-28.md)의 55개 실행과 별개다. 외부 서비스의 전체 복원, 외부 AI 분석 정확도, 깨끗한 Windows 설치 환경 또는 GitHub Actions 통과를 증명하지 않는다.
