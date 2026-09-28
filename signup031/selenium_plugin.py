"""Opt-in pytest integration. Capture precedes user fixture teardown; never changes reports."""
from pathlib import Path
import pytest


def pytest_addoption(parser):
    parser.addoption('--selenium-artifacts', default='artifacts/selenium', help='Original Selenium evidence output root')


def pytest_configure(config):
    config._qa_selenium = []
    config._qa_report_items=[]


def release_recorder(recorder):
    if recorder is None:return
    recorder.driver.execute=recorder.original_execute
    for value in (recorder.bodies,recorder.responses,recorder.requests,recorder.secrets):value.clear()
    recorder.snapshot=None;recorder.test_context=None;recorder.timeline=None


@pytest.fixture
def selenium_record(request):
    def attach(driver, **capture_options):
        if hasattr(request.node, '_qa_selenium_recorder'):
            raise ValueError('테스트당 한 Selenium driver만 연결할 수 있습니다')
        from signup031.selenium_recording import SeleniumRecorder
        from signup031.selenium_publication import CaptureStage
        stage=CaptureStage(request.config.getoption('--selenium-artifacts'));request.node._qa_stage=stage
        try:recorder = SeleniumRecorder(driver, stage.root, request.node.nodeid, **capture_options)
        except Exception:
            stage.discard();del request.node._qa_stage;raise
        request.node._qa_selenium_recorder = recorder
        request.config._qa_selenium.append((request.node, recorder))
        return recorder
    return attach


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    rows = getattr(item, '_qa_selenium_reports', [])
    rows.append({'when': report.when, 'outcome': report.outcome,
                 'message': str(report.longrepr) if report.longrepr else '',
                 'wasxfail': getattr(report, 'wasxfail', None)})
    item._qa_selenium_reports = rows
    if item not in item.config._qa_report_items:item.config._qa_report_items.append(item)
    recorder = getattr(item, '_qa_selenium_recorder', None)
    if recorder is not None and (report.when == 'call' or (report.when == 'setup' and (report.failed or report.skipped))):
        try:
            recorder.capture(report)
        except Exception as exc:
            # Recorder errors must never swallow the original assertion or change pytest outcome.
            reporter = item.config.pluginmanager.get_plugin('terminalreporter')
            if reporter:
                reporter.write_line('Selenium evidence capture failed: ' + type(exc).__name__)
    if report.when=='teardown' and not any(row['outcome']=='failed' for row in rows):
        try:
            stage=getattr(item,'_qa_stage',None)
            if stage:stage.discard()
        except (OSError,ValueError):
            reporter=item.config.pluginmanager.get_plugin('terminalreporter')
            if reporter:reporter.write_line('Selenium temporary cleanup refused; original pytest result preserved')
        finally:release_recorder(recorder)


def pytest_sessionfinish(session, exitstatus):
    terminal = session.config.pluginmanager.get_plugin('terminalreporter')
    from signup031.selenium_publication import CaptureStage,unavailable_failure
    for item in session.config._qa_report_items:
        reports=getattr(item,'_qa_selenium_reports',[]);recorder=getattr(item,'_qa_selenium_recorder',None);stage=getattr(item,'_qa_stage',None)
        try:
            if not any(row['outcome']=='failed' for row in reports):
                if stage:stage.discard()
                continue
            if stage is None:stage=CaptureStage(session.config.getoption('--selenium-artifacts'))
            if recorder is not None:
                try:path=recorder.finish(reports,exitstatus)
                except Exception as error:
                    path=unavailable_failure(stage,recorder.redact(item.nodeid),recorder.redact(reports),exitstatus,
                        '로컬 복원 자료 없음 / 수집 완료 실패 · '+type(error).__name__)
            else:path=unavailable_failure(stage,item.nodeid,reports,exitstatus,'로컬 복원 자료 없음 / 수집 연결 필요 · 첫 탐색 전에 selenium_record(driver)를 연결하세요.')
            path=stage.publish(path)
            if terminal:
                terminal.write_line('Selenium evidence: ' + str(path))
        except Exception as exc:
            if terminal:
                terminal.write_line('Selenium evidence save failed (original pytest exit preserved): ' + (recorder.redact(str(exc)) if recorder else type(exc).__name__))
        finally:
            release_recorder(recorder)
