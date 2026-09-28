import json
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET


def test_preparation_failure_json_matches_actual_pytest_junit_error(tmp_path):
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "signup031",
            "--demo",
            "preparation-failure",
            "--artifacts-dir",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    run_directories = [path for path in tmp_path.iterdir() if path.is_dir()]
    assert completed.returncode == 1, completed.stdout + completed.stderr
    assert len(run_directories) == 1
    run_directory = run_directories[0]
    evidence = json.loads((run_directory / "evidence.json").read_text(encoding="utf-8"))
    junit_root = ET.parse(run_directory / "junit.xml").getroot()
    suite = junit_root.find("testsuite")

    assert evidence["result"]["business"]["status"] == "preparation_failed"
    assert evidence["result"]["business"]["phase"] == "preparation"
    assert evidence["result"]["pytest"] == {"status": "error", "phase": "setup"}
    assert evidence["invocation"]["pytest_exit_code"] == 1
    assert suite is not None
    assert suite.attrib["tests"] == "1"
    assert suite.attrib["errors"] == "1"
    assert suite.attrib["failures"] == "0"
