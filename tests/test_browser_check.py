from signup031 import browser_check


def test_cleanup_exception_preserves_completed_measurement_and_screenshot(
    monkeypatch, tmp_path
):
    original_cleanup = browser_check._cleanup_browser_resources

    def fail_cleanup(*args, **kwargs):
        original_cleanup(*args, **kwargs)
        raise RuntimeError("injected cleanup failure")

    monkeypatch.setattr(browser_check, "_cleanup_browser_resources", fail_cleanup)

    result = browser_check.run_browser_boundary_check(
        target_kind="local_demo",
        target_url="local-demo://compliant",
        target_name="compliant",
        selector="#signup031-password",
        label_pattern=None,
        screenshot_path=tmp_path / "boundary.png",
    )

    assert result.observation.verdict == "passed"
    assert result.observation.initial_length == 128
    assert result.observation.after_extra_length == 128
    assert result.screenshot["status"] == "collected"
    assert result.cleanup == {
        "status": "failed",
        "errors": ["RuntimeError: injected cleanup failure"],
    }
