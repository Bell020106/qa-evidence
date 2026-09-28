import os
from pathlib import Path

import pytest

from signup031.browser_check import run_browser_boundary_check
from signup031.contract import build_evidence
from signup031.storage import write_evidence


@pytest.fixture
def signup031_run_result():
    run_directory = Path(os.environ["SIGNUP031_RUN_DIRECTORY"])
    target_kind = os.environ["SIGNUP031_TARGET_KIND"]
    target_url = os.environ["SIGNUP031_TARGET_URL"]
    target_name = os.environ.get("SIGNUP031_TARGET_NAME")
    selector = os.environ.get("SIGNUP031_SELECTOR") or None
    label_pattern = os.environ.get("SIGNUP031_LABEL_PATTERN") or None

    result = run_browser_boundary_check(
        target_kind=target_kind,
        target_url=target_url,
        target_name=target_name,
        selector=selector,
        label_pattern=label_pattern,
        screenshot_path=run_directory / "boundary-after-extra.png",
        archive_directory=run_directory / 'archive' if os.environ.get('SIGNUP031_RECORD_ARCHIVE') == '1' else None,
        execution_id=os.environ['SIGNUP031_EXECUTION_ID'],
    )
    evidence = build_evidence(
        execution_id=os.environ["SIGNUP031_EXECUTION_ID"],
        started_at=result.started_at,
        finished_at=result.finished_at,
        target_kind=target_kind,
        target_url=target_url,
        browser_name=result.browser_name,
        observation=result.observation,
        screenshot=result.screenshot,
        cleanup=result.cleanup,
        junit_path=run_directory / "junit.xml",
    )
    if result.archive is not None:
        evidence['replay'] = result.archive
    write_evidence(run_directory / "evidence.json", evidence)

    if result.observation.verdict == "preparation_failed":
        raise RuntimeError(result.observation.message)
    return result


def test_signup_031_password_maximum_boundary(signup031_run_result):
    assert signup031_run_result.observation.after_extra_length == 128, (
        signup031_run_result.observation.message
    )
