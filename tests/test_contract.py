import json
from pathlib import Path

import pytest

from signup031.contract import (
    BoundaryObservation,
    build_evidence,
    build_synthetic_password,
    capture_screenshot,
    classify_preparation_error,
    classify_measurements,
    finalize_evidence,
)
from signup031.storage import create_run_directory, write_evidence


def test_synthetic_password_is_exactly_128_and_contains_required_character_classes():
    password = build_synthetic_password()

    assert len(password) == 128
    assert any(character.isalpha() for character in password)
    assert any(character.isdigit() for character in password)
    assert any(not character.isalnum() for character in password)


@pytest.mark.parametrize("initial", [None, 0, 127, 129])
def test_measurement_not_equal_to_128_is_preparation_failure(initial):
    observation = classify_measurements(initial=initial, after_extra=None)

    assert observation.verdict == "preparation_failed"
    assert observation.pytest_status == "error"
    assert observation.pytest_phase == "setup"
    assert observation.initial_length is initial
    assert observation.after_extra_length is None
    assert "PREPARATION_FAILED" in observation.message


def test_129_after_extra_is_a_failed_boundary_result():
    observation = classify_measurements(initial=128, after_extra=129)

    assert observation.verdict == "failed"
    assert observation.pytest_status == "failed"
    assert observation.pytest_phase == "call"
    assert observation.initial_length == 128
    assert observation.after_extra_length == 129
    assert "expected 128 after extra character, observed 129" in observation.message


def test_128_after_extra_is_a_passed_boundary_result():
    observation = classify_measurements(initial=128, after_extra=128)

    assert observation.verdict == "passed"
    assert observation.pytest_status == "passed"
    assert observation.pytest_phase == "call"
    assert observation.after_extra_length == 128


def test_selector_or_browser_error_is_preparation_failure_without_fake_measurement():
    observation = classify_preparation_error(
        TimeoutError("password selector was not visible")
    )

    assert observation.verdict == "preparation_failed"
    assert observation.pytest_status == "error"
    assert observation.pytest_phase == "setup"
    assert observation.initial_length is None
    assert observation.after_extra_length is None
    assert observation.message == (
        "PREPARATION_FAILED: TimeoutError: password selector was not visible"
    )


def test_screenshot_failure_preserves_original_failed_result(tmp_path):
    observation = BoundaryObservation(
        verdict="failed",
        pytest_status="failed",
        pytest_phase="call",
        phase="boundary_assertion",
        initial_length=128,
        after_extra_length=129,
        message="original boundary failure",
    )

    screenshot = capture_screenshot(
        tmp_path / "boundary.png",
        lambda _path: (_ for _ in ()).throw(OSError("disk unavailable")),
    )
    evidence = build_evidence(
        execution_id="run-001",
        started_at="2026-09-14T00:00:00Z",
        finished_at="2026-09-14T00:00:01Z",
        target_kind="local_demo",
        target_url="local-demo://defective",
        browser_name="chromium",
        observation=observation,
        screenshot=screenshot,
        cleanup={"status": "completed", "errors": []},
        junit_path=tmp_path / "junit.xml",
    )

    assert evidence["result"]["business"] == {
        "status": "failed",
        "phase": "boundary_assertion",
        "message": "original boundary failure",
    }
    assert evidence["result"]["pytest"] == {"status": "failed", "phase": "call"}
    assert evidence["evidence"]["screenshot"]["status"] == "collection_failed"
    assert evidence["evidence"]["screenshot"]["reason"] == "OSError: disk unavailable"


def test_evidence_json_omits_the_synthetic_password(tmp_path):
    password = build_synthetic_password()
    evidence = build_evidence(
        execution_id="run-002",
        started_at="2026-09-14T00:00:00Z",
        finished_at="2026-09-14T00:00:01Z",
        target_kind="local_demo",
        target_url="local-demo://compliant",
        browser_name="chromium",
        observation=classify_measurements(initial=128, after_extra=128),
        screenshot={"status": "collected", "path": "boundary.png", "reason": None},
        cleanup={"status": "completed", "errors": []},
        junit_path=tmp_path / "junit.xml",
    )

    serialized = json.dumps(evidence)

    assert password not in serialized
    assert evidence["contract_version"] == "1"
    assert evidence["tc_id"] == "SIGNUP-031"
    assert evidence["expected_result_snapshot"]["maximum_length"] == 128


def test_each_run_directory_is_unique_and_preserves_existing_runs(tmp_path):
    first_id, first_path = create_run_directory(tmp_path)
    (first_path / "sentinel.txt").write_text("first", encoding="utf-8")

    second_id, second_path = create_run_directory(tmp_path)

    assert first_id != second_id
    assert first_path != second_path
    assert (first_path / "sentinel.txt").read_text(encoding="utf-8") == "first"


def test_finalize_evidence_records_exit_code_and_collected_junit(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    evidence_path = run_dir / "evidence.json"
    junit_path = run_dir / "junit.xml"
    junit_path.write_text("<testsuites />", encoding="utf-8")
    write_evidence(evidence_path, {"invocation": {}, "evidence": {"junit": {}}})

    finalize_evidence(evidence_path, junit_path=junit_path, exit_code=1)

    saved = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert saved["invocation"]["pytest_exit_code"] == 1
    assert saved["evidence"]["junit"] == {
        "status": "collected",
        "path": str(junit_path.resolve()),
        "reason": None,
    }


def test_cleanup_error_is_separate_from_business_and_pytest_results(tmp_path):
    evidence = build_evidence(
        execution_id="run-003",
        started_at="2026-09-15T00:00:00Z",
        finished_at="2026-09-15T00:00:01Z",
        target_kind="local_demo",
        target_url="local-demo://defective",
        browser_name="chromium",
        observation=classify_measurements(initial=128, after_extra=129),
        screenshot={"status": "collected", "path": "boundary.png", "reason": None},
        cleanup={
            "status": "completed_with_errors",
            "errors": ["input_clear: RuntimeError: injected cleanup failure"],
        },
        junit_path=tmp_path / "junit.xml",
    )

    assert evidence["result"]["business"]["status"] == "failed"
    assert evidence["result"]["pytest"] == {"status": "failed", "phase": "call"}
    assert evidence["evidence"]["screenshot"]["status"] == "collected"
    assert evidence["post_run"]["cleanup"] == {
        "status": "completed_with_errors",
        "errors": ["input_clear: RuntimeError: injected cleanup failure"],
    }
