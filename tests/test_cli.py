from pathlib import Path

import pytest

from signup031.cli import build_parser, build_pytest_command, resolve_target


def test_no_target_option_defaults_to_compliant_local_demo():
    args = build_parser().parse_args([])

    target = resolve_target(args)

    assert target.kind == "local_demo"
    assert target.name == "compliant"
    assert target.url == "local-demo://compliant"
    assert target.selector == "#signup031-password"


def test_live_target_requires_explicit_https_url_and_uses_label_pattern():
    args = build_parser().parse_args(
        ["--live-url", "https://accounts.example.test/signup"]
    )

    target = resolve_target(args)

    assert target.kind == "live"
    assert target.url == "https://accounts.example.test/signup"
    assert target.selector is None
    assert target.label_pattern == r"password|비밀번호"


@pytest.mark.parametrize(
    "url",
    ["file:///tmp/signup.html", "javascript:alert(1)", "accounts.example.test/signup"],
)
def test_live_target_rejects_non_http_urls(url):
    args = build_parser().parse_args(["--live-url", url])

    with pytest.raises(ValueError, match="http:// or https://"):
        resolve_target(args)


def test_pytest_command_requests_junit_in_the_same_run_directory(tmp_path):
    command = build_pytest_command(
        project_root=Path("C:/workspace"),
        run_directory=tmp_path,
        python_executable="python-placeholder",
    )

    assert command[:3] == ["python-placeholder", "-m", "pytest"]
    assert str(tmp_path / "junit.xml") in command
    assert command[-1] == "signup031/scenario_signup031.py"
