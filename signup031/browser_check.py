from dataclasses import dataclass
from pathlib import Path
import re
from contextlib import ExitStack

from signup031.contract import (
    BoundaryObservation,
    build_synthetic_password,
    capture_screenshot,
    classify_measurements,
    classify_preparation_error,
    utc_now,
)


@dataclass(frozen=True)
class BrowserRunResult:
    started_at: str
    finished_at: str
    browser_name: str
    observation: BoundaryObservation
    screenshot: dict
    cleanup: dict
    archive: dict | None = None


def _cleanup_browser_resources(*, locator, context, browser, playwright) -> dict:
    errors = []
    actions = (
        ("input_clear", locator, lambda resource: resource.fill("")),
        ("context_close", context, lambda resource: resource.close()),
        ("browser_close", browser, lambda resource: resource.close()),
        ("playwright_stop", playwright, lambda resource: resource.stop()),
    )
    for action_name, resource, action in actions:
        if resource is None:
            continue
        try:
            action(resource)
        except Exception as exc:
            errors.append(f"{action_name}: {type(exc).__name__}: {exc}")
    return {
        "status": "completed_with_errors" if errors else "completed",
        "errors": errors,
    }


def run_browser_boundary_check(
    *,
    target_kind: str,
    target_url: str,
    target_name: str | None,
    selector: str | None,
    label_pattern: str | None,
    screenshot_path: Path,
    archive_directory: Path | None = None,
    execution_id: str | None = None,
) -> BrowserRunResult:
    started_at = utc_now()
    screenshot = {
        "status": "not_collected",
        "path": str(screenshot_path.resolve()),
        "reason": "boundary measurement was not reached",
    }
    observation = classify_preparation_error(RuntimeError("browser run did not start"))
    cleanup = {"status": "not_started", "errors": []}
    playwright = None
    browser = None
    context = None
    locator = None
    initial_length = None
    stack = ExitStack()
    navigation_url = target_url
    archive = None
    capture_error = None
    try:
        from playwright.sync_api import sync_playwright

        playwright = sync_playwright().start()
        browser = playwright.chromium.launch(headless=True)
        options = {'service_workers': 'block'}
        if archive_directory is not None:
            try:
                archive_directory.mkdir(parents=True, exist_ok=False)
                options.update(record_har_path=str(archive_directory / 'resources.har'),
                               record_har_content='embed', record_har_mode='full')
            except Exception as exc:
                capture_error = str(exc)
        context = browser.new_context(**options)
        page = context.new_page()
        if target_kind == "local_demo":
            if target_name not in {"compliant", "defective", "preparation-failure"}:
                raise ValueError(f"unknown local demo: {target_name!r}")
            demo_path = Path(__file__).resolve().parent / "demos" / f"{target_name}.html"
            if archive_directory is not None and capture_error is None:
                from signup031.archive import serve_demo
                navigation_url = stack.enter_context(serve_demo(demo_path))
            else:
                navigation_url = demo_path.as_uri()
            page.goto(navigation_url, wait_until="domcontentloaded")
        elif target_kind == "live":
            page.goto(target_url, wait_until="domcontentloaded", timeout=30_000)
        else:
            raise ValueError(f"unknown target kind: {target_kind!r}")

        if selector:
            locator = page.locator(selector)
        elif label_pattern:
            locator = page.get_by_label(re.compile(label_pattern, re.IGNORECASE)).first
        else:
            raise ValueError("a selector or accessible label pattern is required")

        locator.wait_for(state="visible", timeout=10_000)
        synthetic_password = build_synthetic_password()
        locator.fill(synthetic_password)
        initial_length = len(locator.input_value())
        if initial_length != 128:
            observation = classify_measurements(initial=initial_length, after_extra=None)
        else:
            locator.press_sequentially("Z")
            after_extra_length = len(locator.input_value())
            observation = classify_measurements(
                initial=initial_length,
                after_extra=after_extra_length,
            )
            screenshot = capture_screenshot(
                screenshot_path,
                lambda path: page.screenshot(path=str(path), full_page=True),
            )
    except Exception as exc:
        observation = classify_preparation_error(exc, initial=initial_length)
        screenshot = {
            "status": "not_collected",
            "path": str(screenshot_path.resolve()),
            "reason": observation.message,
        }
    finally:
        try:
            cleanup = _cleanup_browser_resources(
                locator=locator,
                context=context,
                browser=browser,
                playwright=playwright,
            )
        except Exception as exc:
            cleanup = {
                "status": "failed",
                "errors": [f"{type(exc).__name__}: {exc}"],
            }
        stack.close()
        if archive_directory is not None:
            try:
                if capture_error:
                    raise ValueError(capture_error)
                from signup031.archive import finish_archive
                archive = finish_archive(
                    archive_directory, execution_id=execution_id, target_kind=target_kind,
                    target_url=target_url, navigation_url=navigation_url, selector=selector,
                    label_pattern=label_pattern, observation=observation,
                    close_errors=[e for e in cleanup['errors'] if 'context_close' in e],
                )
            except Exception as exc:
                archive = {'status': 'failed', 'reason': str(exc), 'replay_verification': 'not_run'}
    return BrowserRunResult(
        started_at=started_at,
        finished_at=utc_now(),
        browser_name="chromium",
        observation=observation,
        screenshot=screenshot,
        cleanup=cleanup,
        archive=archive,
    )
