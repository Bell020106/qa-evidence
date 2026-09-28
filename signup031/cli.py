import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlparse

from signup031.contract import (
    BoundaryObservation,
    build_evidence,
    finalize_evidence,
    utc_now,
)
from signup031.storage import create_run_directory, write_evidence


@dataclass(frozen=True)
class Target:
    kind: str
    name: str | None
    url: str
    selector: str | None
    label_pattern: str | None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run SIGNUP-031 and save JSON, JUnit, and screenshot evidence."
    )
    target_group = parser.add_mutually_exclusive_group()
    target_group.add_argument(
        "--demo", choices=("compliant", "defective", "preparation-failure")
    )
    target_group.add_argument("--live-url")
    parser.add_argument("--label-pattern", default=r"password|비밀번호")
    parser.add_argument('--record-archive', action='store_true')
    parser.add_argument(
        "--artifacts-dir",
        type=Path,
        default=Path("artifacts") / "signup-031",
    )
    return parser


def resolve_target(args: argparse.Namespace) -> Target:
    if args.live_url:
        parsed = urlparse(args.live_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("--live-url must use http:// or https://")
        return Target(
            kind="live",
            name=None,
            url=args.live_url,
            selector=None,
            label_pattern=args.label_pattern,
        )
    demo_name = args.demo or "compliant"
    return Target(
        kind="local_demo",
        name=demo_name,
        url=f"local-demo://{demo_name}",
        selector="#signup031-password",
        label_pattern=None,
    )


def build_pytest_command(
    *,
    project_root: Path,
    run_directory: Path,
    python_executable: str,
) -> list[str]:
    return [
        python_executable,
        "-m",
        "pytest",
        "-q",
        "--junitxml",
        str((run_directory / "junit.xml").resolve()),
        "signup031/scenario_signup031.py",
    ]


def _write_runner_failure(
    *,
    evidence_path: Path,
    junit_path: Path,
    execution_id: str,
    started_at: str,
    target: Target,
    message: str,
) -> None:
    observation = BoundaryObservation(
        verdict="preparation_failed",
        pytest_status="not_available",
        pytest_phase="collection",
        phase="runner",
        initial_length=None,
        after_extra_length=None,
        message=f"PREPARATION_FAILED: {message}",
    )
    payload = build_evidence(
        execution_id=execution_id,
        started_at=started_at,
        finished_at=utc_now(),
        target_kind=target.kind,
        target_url=target.url,
        browser_name="chromium",
        observation=observation,
        screenshot={
            "status": "not_collected",
            "path": str((evidence_path.parent / "boundary-after-extra.png").resolve()),
            "reason": message,
        },
        cleanup={"status": "not_started", "errors": []},
        junit_path=junit_path,
    )
    write_evidence(evidence_path, payload)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        target = resolve_target(args)
    except ValueError as exc:
        parser.error(str(exc))

    project_root = Path(__file__).resolve().parent.parent
    artifacts_root = args.artifacts_dir
    if not artifacts_root.is_absolute():
        artifacts_root = project_root / artifacts_root
    execution_id, run_directory = create_run_directory(artifacts_root)
    started_at = utc_now()
    junit_path = run_directory / "junit.xml"
    evidence_path = run_directory / "evidence.json"
    environment = os.environ.copy()
    environment.update(
        {
            "SIGNUP031_EXECUTION_ID": execution_id,
            "SIGNUP031_RUN_DIRECTORY": str(run_directory),
            "SIGNUP031_TARGET_KIND": target.kind,
            "SIGNUP031_TARGET_URL": target.url,
            "SIGNUP031_TARGET_NAME": target.name or "",
            "SIGNUP031_SELECTOR": target.selector or "",
            "SIGNUP031_LABEL_PATTERN": target.label_pattern or "",
            "SIGNUP031_RECORD_ARCHIVE": '1' if args.record_archive else '0',
        }
    )
    command = build_pytest_command(
        project_root=project_root,
        run_directory=run_directory,
        python_executable=sys.executable,
    )
    try:
        completed = subprocess.run(command, cwd=project_root, env=environment, check=False)
        exit_code = completed.returncode
    except OSError as exc:
        exit_code = 3
        _write_runner_failure(
            evidence_path=evidence_path,
            junit_path=junit_path,
            execution_id=execution_id,
            started_at=started_at,
            target=target,
            message=f"{type(exc).__name__}: {exc}",
        )

    if not evidence_path.is_file():
        _write_runner_failure(
            evidence_path=evidence_path,
            junit_path=junit_path,
            execution_id=execution_id,
            started_at=started_at,
            target=target,
            message=f"pytest exited {exit_code} before evidence was written",
        )
    finalize_evidence(evidence_path, junit_path=junit_path, exit_code=exit_code)
    print(f"execution_id={execution_id}")
    print(f"artifacts={run_directory}")
    print(f"evidence={evidence_path}")
    print(f"junit={junit_path}")
    return exit_code
