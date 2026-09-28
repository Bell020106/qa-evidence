import json
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFontMetrics, QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QListWidget
import pytest

from signup031.viewer import EvidenceViewerWindow, render_window_to_png


@pytest.fixture(scope="module")
def qapp():
    application = QApplication.instance() or QApplication([])
    yield application


def _payload(status, *, execution_id, initial, after_extra, screenshot_status):
    pytest_status, pytest_phase = {
        "passed": ("passed", "call"),
        "failed": ("failed", "call"),
        "preparation_failed": ("error", "setup"),
    }[status]
    return {
        "contract_version": "1",
        "tc_id": "SIGNUP-031",
        "expected_result_snapshot": {"maximum_length": 128},
        "execution": {
            "id": execution_id,
            "started_at": f"2026-09-15T00:00:0{execution_id[-1]}Z",
            "finished_at": f"2026-09-15T00:00:1{execution_id[-1]}Z",
        },
        "target": {"kind": "local_demo", "url": f"local-demo://{status}"},
        "measurements": {
            "initial_length": initial,
            "after_extra_length": after_extra,
        },
        "result": {
            "business": {
                "status": status,
                "phase": "preparation" if status == "preparation_failed" else "boundary_assertion",
                "message": f"fixture {status}",
            },
            "pytest": {"status": pytest_status, "phase": pytest_phase},
        },
        "evidence": {
            "screenshot": {
                "status": screenshot_status,
                "path": "boundary-after-extra.png",
                "reason": "boundary measurement was not reached"
                if screenshot_status == "not_collected"
                else None,
            }
        },
        "post_run": {"cleanup": {"status": "completed", "errors": []}},
    }


@pytest.fixture
def review_root(qapp, tmp_path):
    cases = (
        ("passed", "run-1", 128, 128, "collected", QColor("#DDF5E8")),
        ("failed", "run-2", 128, 129, "collected", QColor("#FDE5E8")),
        ("preparation_failed", "run-3", 127, None, "not_collected", None),
    )
    for status, execution_id, initial, after_extra, screenshot_status, color in cases:
        run_directory = tmp_path / execution_id
        run_directory.mkdir()
        payload = _payload(
            status,
            execution_id=execution_id,
            initial=initial,
            after_extra=after_extra,
            screenshot_status=screenshot_status,
        )
        (run_directory / "evidence.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )
        if color is not None:
            image = QImage(640, 360, QImage.Format.Format_RGB32)
            image.fill(color)
            assert image.save(str(run_directory / "boundary-after-extra.png"), "PNG")
    return tmp_path


def _select_status(window, business_status):
    run_list = window.findChild(QListWidget, "runList")
    assert run_list is not None
    for row in range(run_list.count()):
        item = run_list.item(row)
        if item.data(Qt.ItemDataRole.UserRole).business_status == business_status:
            QTest.mouseClick(
                run_list.viewport(),
                Qt.MouseButton.LeftButton,
                pos=run_list.visualItemRect(item).center(),
            )
            QApplication.processEvents()
            return
    raise AssertionError(f"status not found: {business_status}")


def _label(window, object_name):
    label = window.findChild(QLabel, object_name)
    assert label is not None
    return label


def _has_no_image(label):
    pixmap = label.pixmap()
    return pixmap is None or pixmap.isNull()


def test_qt_window_filters_pass_and_switches_failures_without_stale_details(qapp, review_root):
    window = EvidenceViewerWindow(review_root)
    window.resize(1180, 760)
    window.show()
    QApplication.processEvents()

    run_list = window.findChild(QListWidget, "runList")
    assert run_list is not None and run_list.count() == 2
    assert all(run_list.item(row).data(Qt.ItemDataRole.UserRole).business_status!='passed' for row in range(run_list.count()))

    _select_status(window, "failed")
    assert _label(window, "statusBadge").text() == "실패"
    assert _label(window, "expectedValue").text() == "128"
    assert _label(window, "initialValue").text() == "128"
    assert _label(window, "actualValue").text() == "129"
    assert _label(window, "pytestValue").text() == "failed / call"
    assert _label(window, "screenshotLabel").pixmap() is not None
    assert not _label(window, "screenshotLabel").pixmap().isNull()

    _select_status(window, "preparation_failed")
    assert _label(window, "statusBadge").text() == "준비 실패"
    assert _label(window, "initialValue").text() == "127"
    assert _label(window, "actualValue").text() == "미측정"
    assert _label(window, "pytestValue").text() == "error / setup"
    assert _has_no_image(_label(window, "screenshotLabel"))
    assert "미수집" in _label(window, "screenshotLabel").text()

    window.close()


def test_window_font_contains_korean_glyphs_in_offscreen_rendering(qapp, review_root):
    window = EvidenceViewerWindow(review_root)

    assert QFontMetrics(window.font()).inFontUcs4(ord("실"))
    window.close()


def test_refreshing_empty_root_clears_previous_values_and_image(qapp, review_root, tmp_path):
    window = EvidenceViewerWindow(review_root)
    window.show()
    QApplication.processEvents()
    _select_status(window, "failed")
    assert _label(window, "actualValue").text() == "129"

    empty_root = tmp_path / "empty"
    empty_root.mkdir()
    window.load_root(empty_root)
    QApplication.processEvents()

    assert window.findChild(QListWidget, "runList").count() == 0
    assert _label(window, "actualValue").text() == "—"
    assert _has_no_image(_label(window, "screenshotLabel"))
    assert "표시할 실행 결과가 없습니다" in _label(window, "messageLabel").text()
    window.close()


def test_qt_grab_renders_nonempty_review_png(qapp, review_root, tmp_path):
    output = tmp_path / "viewer.png"
    window = EvidenceViewerWindow(review_root)
    window.resize(1180, 760)
    window.show()
    QApplication.processEvents()
    _select_status(window, "failed")

    assert render_window_to_png(window, output)
    assert output.is_file() and output.stat().st_size > 10_000
    window.close()
