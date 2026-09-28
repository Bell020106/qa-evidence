import json
from pathlib import Path

import pytest

from signup031.viewer_model import load_evidence_root


def _current_payload(*, execution_id="run-current", screenshot_status="not_collected"):
    return {
        "contract_version": "1",
        "tc_id": "SIGNUP-031",
        "expected_result_snapshot": {"maximum_length": 128},
        "execution": {
            "id": execution_id,
            "started_at": "2026-09-15T00:00:00Z",
            "finished_at": "2026-09-15T00:00:01Z",
        },
        "target": {"kind": "local_demo", "url": "local-demo://compliant"},
        "measurements": {"initial_length": 128, "after_extra_length": 128},
        "result": {
            "business": {
                "status": "passed",
                "phase": "boundary_assertion",
                "message": "passed",
            },
            "pytest": {"status": "passed", "phase": "call"},
        },
        "evidence": {
            "screenshot": {
                "status": screenshot_status,
                "path": "boundary-after-extra.png",
                "reason": None,
            }
        },
        "post_run": {"cleanup": {"status": "completed", "errors": []}},
    }


def _write_payload(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_bad_incomplete_and_legacy_json_are_isolated_from_valid_records(tmp_path):
    _write_payload(tmp_path / "valid" / "evidence.json", _current_payload())
    bad_path = tmp_path / "bad" / "evidence.json"
    bad_path.parent.mkdir()
    bad_path.write_text("{not-json", encoding="utf-8")
    _write_payload(
        tmp_path / "incomplete" / "evidence.json",
        {"contract_version": "1", "execution": {"id": "run-incomplete"}},
    )
    _write_payload(
        tmp_path / "legacy" / "evidence.json",
        {
            "contract_version": "1",
            "execution": {"id": "run-legacy"},
            "result": {"verdict": "passed", "raw_status": "passed"},
        },
    )

    records = load_evidence_root(tmp_path)

    assert len(records) == 4
    assert {record.business_status for record in records} == {
        "passed",
        "read_error",
        "incomplete",
        "legacy",
    }
    legacy = next(record for record in records if record.business_status == "legacy")
    assert legacy.status_label == "구형 형식"
    assert legacy.after_extra_length is None
    assert "result.business/result.pytest" in legacy.message


@pytest.mark.parametrize("invalid_status", [[], {}])
def test_non_scalar_business_status_is_incomplete_without_hiding_valid_record(
    tmp_path, invalid_status
):
    _write_payload(tmp_path / "valid" / "evidence.json", _current_payload())
    invalid = _current_payload(execution_id="run-invalid-status")
    invalid["result"]["business"]["status"] = invalid_status
    _write_payload(tmp_path / "invalid" / "evidence.json", invalid)

    records = load_evidence_root(tmp_path)

    assert len(records) == 2
    assert {record.business_status for record in records} == {"passed", "incomplete"}


@pytest.mark.parametrize("business_status", ["passed", "failed"])
@pytest.mark.parametrize(
    "field_path",
    [
        ("expected_result_snapshot", "maximum_length"),
        ("measurements", "initial_length"),
        ("measurements", "after_extra_length"),
    ],
)
def test_pass_and_fail_require_all_boundary_numbers(tmp_path, business_status, field_path):
    payload = _current_payload(execution_id=f"run-{business_status}")
    payload["result"]["business"]["status"] = business_status
    payload["result"]["pytest"]["status"] = (
        "passed" if business_status == "passed" else "failed"
    )
    payload["measurements"]["after_extra_length"] = (
        128 if business_status == "passed" else 129
    )
    payload[field_path[0]][field_path[1]] = None
    _write_payload(tmp_path / "run" / "evidence.json", payload)

    record = load_evidence_root(tmp_path)[0]

    assert record.business_status == "incomplete"
    assert ".".join(field_path) in record.message


def test_missing_tc_id_is_incomplete(tmp_path):
    payload = _current_payload()
    del payload["tc_id"]
    _write_payload(tmp_path / "run" / "evidence.json", payload)

    record = load_evidence_root(tmp_path)[0]

    assert record.business_status == "incomplete"
    assert "tc_id" in record.message


def test_unsupported_contract_version_is_incomplete(tmp_path):
    payload = _current_payload()
    payload["contract_version"] = "999"
    _write_payload(tmp_path / "run" / "evidence.json", payload)

    record = load_evidence_root(tmp_path)[0]

    assert record.business_status == "incomplete"
    assert "contract_version" in record.message


def test_preparation_failure_keeps_nullable_measurements(tmp_path):
    payload = _current_payload(execution_id="run-preparation")
    payload["result"]["business"].update(
        status="preparation_failed",
        phase="preparation",
        message="PREPARATION_FAILED",
    )
    payload["result"]["pytest"] = {"status": "error", "phase": "setup"}
    payload["measurements"] = {"initial_length": None, "after_extra_length": None}
    _write_payload(tmp_path / "run" / "evidence.json", payload)

    record = load_evidence_root(tmp_path)[0]

    assert record.business_status == "preparation_failed"
    assert record.initial_length is None
    assert record.after_extra_length is None


def test_moved_screenshot_path_uses_same_run_filename_inside_selected_root(tmp_path):
    run_directory = tmp_path / "run"
    screenshot = run_directory / "boundary-after-extra.png"
    screenshot.parent.mkdir()
    screenshot.write_bytes(b"local-image")
    payload = _current_payload(screenshot_status="collected")
    payload["evidence"]["screenshot"]["path"] = "C:/old-machine/results/boundary-after-extra.png"
    _write_payload(run_directory / "evidence.json", payload)

    record = load_evidence_root(tmp_path)[0]

    assert record.screenshot.status == "available"
    assert record.screenshot.path == screenshot.resolve()


def test_existing_screenshot_outside_selected_root_is_not_loaded(tmp_path):
    selected_root = tmp_path / "selected"
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"outside")
    payload = _current_payload(screenshot_status="collected")
    payload["evidence"]["screenshot"]["path"] = str(outside)
    _write_payload(selected_root / "run" / "evidence.json", payload)

    record = load_evidence_root(selected_root)[0]

    assert record.screenshot.status == "outside_root"
    assert record.screenshot.path is None


def test_empty_root_has_no_records(tmp_path):
    assert load_evidence_root(tmp_path) == []
