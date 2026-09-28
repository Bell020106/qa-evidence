from dataclasses import dataclass
from datetime import UTC, datetime
import json
import platform
from pathlib import Path
from typing import Callable

from signup031.storage import write_evidence


@dataclass(frozen=True)
class BoundaryObservation:
    verdict: str
    pytest_status: str
    pytest_phase: str
    phase: str
    initial_length: int | None
    after_extra_length: int | None
    message: str


def build_synthetic_password() -> str:
    return "Aa1!" * 32


def classify_measurements(*, initial: int | None, after_extra: int | None) -> BoundaryObservation:
    if initial != 128:
        return BoundaryObservation(
            verdict="preparation_failed",
            pytest_status="error",
            pytest_phase="setup",
            phase="preparation",
            initial_length=initial,
            after_extra_length=None,
            message=(
                "PREPARATION_FAILED: expected 128 characters before boundary check, "
                f"observed {initial!r}"
            ),
        )
    if after_extra is None:
        return BoundaryObservation(
            verdict="preparation_failed",
            pytest_status="error",
            pytest_phase="setup",
            phase="measurement",
            initial_length=initial,
            after_extra_length=None,
            message="PREPARATION_FAILED: final length measurement is unavailable",
        )
    if after_extra == 128:
        return BoundaryObservation(
            verdict="passed",
            pytest_status="passed",
            pytest_phase="call",
            phase="boundary_assertion",
            initial_length=initial,
            after_extra_length=after_extra,
            message="SIGNUP-031 passed: expected 128 after extra character, observed 128",
        )
    return BoundaryObservation(
        verdict="failed",
        pytest_status="failed",
        pytest_phase="call",
        phase="boundary_assertion",
        initial_length=initial,
        after_extra_length=after_extra,
        message=(
            "SIGNUP-031 failed: expected 128 after extra character, "
            f"observed {after_extra}"
        ),
    )


def classify_preparation_error(
    error: Exception, *, initial: int | None = None
) -> BoundaryObservation:
    return BoundaryObservation(
        verdict="preparation_failed",
        pytest_status="error",
        pytest_phase="setup",
        phase="preparation",
        initial_length=initial,
        after_extra_length=None,
        message=f"PREPARATION_FAILED: {type(error).__name__}: {error}",
    )


def capture_screenshot(path: Path, capture: Callable[[Path], None]) -> dict:
    absolute_path = path.resolve()
    try:
        absolute_path.parent.mkdir(parents=True, exist_ok=True)
        capture(absolute_path)
        if not absolute_path.is_file():
            raise FileNotFoundError("capture returned without creating the screenshot")
    except Exception as exc:
        return {
            "status": "collection_failed",
            "path": str(absolute_path),
            "reason": f"{type(exc).__name__}: {exc}",
        }
    return {"status": "collected", "path": str(absolute_path), "reason": None}


def build_evidence(
    *,
    execution_id: str,
    started_at: str,
    finished_at: str,
    target_kind: str,
    target_url: str,
    browser_name: str,
    observation: BoundaryObservation,
    screenshot: dict,
    cleanup: dict,
    junit_path: Path,
) -> dict:
    return {
        "contract_version": "1",
        "tc_id": "SIGNUP-031",
        "expected_result_snapshot": {
            "maximum_length": 128,
            "initial_length": 128,
            "after_extra_length": 128,
            "behavior": "the extra character is rejected",
        },
        "execution": {
            "id": execution_id,
            "started_at": started_at,
            "finished_at": finished_at,
        },
        "target": {"kind": target_kind, "url": target_url},
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "browser": browser_name,
        },
        "measurements": {
            "initial_length": observation.initial_length,
            "after_extra_length": observation.after_extra_length,
        },
        "result": {
            "business": {
                "status": observation.verdict,
                "phase": observation.phase,
                "message": observation.message,
            },
            "pytest": {
                "status": observation.pytest_status,
                "phase": observation.pytest_phase,
            },
        },
        "steps": [
            {
                "name": "fill_synthetic_password",
                "status": "completed" if observation.initial_length is not None else "not_completed",
                "observed_length": observation.initial_length,
            },
            {
                "name": "append_one_character",
                "status": "completed" if observation.after_extra_length is not None else "not_completed",
                "observed_length": observation.after_extra_length,
            },
        ],
        "evidence": {
            "screenshot": screenshot,
            "junit": {
                "status": "pending",
                "path": str(junit_path.resolve()),
                "reason": None,
            },
        },
        "post_run": {"cleanup": cleanup},
        "invocation": {"pytest_exit_code": None},
    }


def finalize_evidence(evidence_path: Path, *, junit_path: Path, exit_code: int) -> None:
    payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    payload.setdefault("invocation", {})["pytest_exit_code"] = exit_code
    absolute_junit_path = junit_path.resolve()
    payload.setdefault("evidence", {})["junit"] = {
        "status": "collected" if absolute_junit_path.is_file() else "collection_failed",
        "path": str(absolute_junit_path),
        "reason": None if absolute_junit_path.is_file() else "JUnit XML was not created",
    }
    write_evidence(evidence_path, payload)


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
