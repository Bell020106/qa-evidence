from datetime import UTC, datetime
import json
from pathlib import Path
from uuid import uuid4


def create_run_directory(root: Path) -> tuple[str, Path]:
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    while True:
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
        execution_id = f"{timestamp}-{uuid4().hex[:8]}"
        run_directory = root / execution_id
        try:
            run_directory.mkdir()
        except FileExistsError:
            continue
        return execution_id, run_directory


def write_evidence(path: Path, payload: dict) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    temporary_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(path)
