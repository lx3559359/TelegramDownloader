from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QGraphicsDropShadowEffect

from telegram_downloader.diagnostics import (
    DiagnosticProgress,
    DiagnosticReport,
    DiagnosticResult,
    DiagnosticStatus,
)
from telegram_downloader.ui.diagnostics import (
    STATUS_LABELS,
    DiagnosticResultModel,
    DiagnosticsPage,
)
from telegram_downloader.ui.effects import ElevationLevel


def test_diagnostics_page_separates_progress_results_and_actions(qtbot) -> None:
    page = DiagnosticsPage()
    qtbot.addWidget(page)

    for card in (page.progress_card, page.results_card):
        assert isinstance(card.graphicsEffect(), QGraphicsDropShadowEffect)
        assert card.objectName() == "elevatedCard"
        assert card.property("elevation") == ElevationLevel.MAJOR.value
    assert isinstance(page.actions_card.graphicsEffect(), QGraphicsDropShadowEffect)
    assert page.actions_card.objectName() == "elevatedSubCard"
    assert page.actions_card.property("elevation") == ElevationLevel.SECONDARY.value
    assert page.status_banner.graphicsEffect() is None
    assert page.results_card.isAncestorOf(page.table)
    assert page.actions_card.isAncestorOf(page.start_button)

NOW = datetime(2026, 8, 16, 8, 0, tzinfo=UTC)


def diagnostic_result(
    status: DiagnosticStatus = DiagnosticStatus.PASSED,
) -> DiagnosticResult:
    return DiagnosticResult(
        "environment",
        "运行环境与路径",
        status,
        "check-result",
        "检查结果说明",
        123,
    )


def diagnostic_report(
    status: DiagnosticStatus = DiagnosticStatus.PASSED,
) -> DiagnosticReport:
    return DiagnosticReport.build(
        "0.10.0",
        NOW,
        NOW + timedelta(seconds=1),
        (diagnostic_result(status),),
    )


def test_result_model_exposes_four_columns_status_labels_and_stable_id() -> None:
    model = DiagnosticResultModel()

    assert set(STATUS_LABELS) == set(DiagnosticStatus)
    for status in DiagnosticStatus:
        model.set_results((diagnostic_result(status),))
        assert model.rowCount() == 1
        assert model.columnCount() == 4
        assert model.headerData(0, Qt.Orientation.Horizontal) == "检查项"
        assert model.headerData(1, Qt.Orientation.Horizontal) == "状态"
        assert model.headerData(2, Qt.Orientation.Horizontal) == "耗时"
        assert model.headerData(3, Qt.Orientation.Horizontal) == "说明"
        assert model.data(model.index(0, 1)) == STATUS_LABELS[status]
        assert model.data(model.index(0, 1), Qt.ItemDataRole.ForegroundRole) is not None
        assert model.data(model.index(0, 2)) == "123 ms"
        assert (
            model.data(model.index(0, 0), Qt.ItemDataRole.UserRole)
            == "environment"
        )


def test_page_button_state_tracks_run_report_and_history(qtbot) -> None:
    page = DiagnosticsPage()
    qtbot.addWidget(page)

    assert page.start_button.isEnabled()
    assert not page.cancel_button.isEnabled()
    assert not page.export_button.isEnabled()
    page.set_running(True)
    assert not page.start_button.isEnabled()
    assert page.cancel_button.isEnabled()
    assert not page.export_button.isEnabled()
    page.set_report(diagnostic_report(), historical=True)
    assert "历史结果" in page.report_context_label.text()
    assert "2026-08-16" in page.report_context_label.text()
    page.set_running(False)
    assert page.export_button.isEnabled()
    assert "检查完成" in page.status_banner.text()
    assert page.status_banner.property("status") == "passed"
    page.set_export_busy(True)
    assert page.export_button.isEnabled() is False
    assert "导出" in page.export_button.text()
    page.set_export_busy(False)
    assert page.export_button.isEnabled() is True


def test_page_exposes_cancellation_convergence_and_disables_repeat(qtbot) -> None:
    page = DiagnosticsPage()
    qtbot.addWidget(page)
    page.set_running(True)

    page.set_cancelling(True)

    assert page.progress_label.text() == "正在取消，当前本地检查完成后停止"
    assert page.status_banner.text() == "正在取消，当前本地检查完成后停止"
    assert page.cancel_button.isEnabled() is False
    assert page.start_button.isEnabled() is False

    page.set_running(True)

    assert page.cancel_button.isEnabled() is True


def test_page_progress_is_bounded_and_shows_current_check(qtbot) -> None:
    page = DiagnosticsPage()
    qtbot.addWidget(page)
    progress = DiagnosticProgress(
        2,
        9,
        "disk",
        "磁盘空间",
        DiagnosticStatus.RUNNING,
    )

    page.set_progress(progress)

    assert (page.progress_bar.minimum(), page.progress_bar.maximum()) == (0, 9)
    assert page.progress_bar.value() == 2
    assert page.progress_bar.format() == "2 / 9 · 22%"
    assert "磁盘空间" in page.progress_label.text()
    assert page.status_banner.property("status") == "running"


def test_page_selection_updates_safe_details_and_clears_without_report(qtbot) -> None:
    page = DiagnosticsPage()
    qtbot.addWidget(page)
    report = DiagnosticReport.build(
        "0.18.4",
        NOW,
        NOW + timedelta(seconds=1),
        (
            DiagnosticResult(
                "disk",
                "磁盘空间",
                DiagnosticStatus.WARNING,
                "download-disk-space-low",
                "下载所在磁盘可用空间低于 1 GiB",
                1,
                {
                    "downloadFreeBytes": 512 * 1024**2,
                    "privateUnknown": "D:/private",
                },
            ),
            DiagnosticResult(
                "credentials",
                "登录凭据",
                DiagnosticStatus.WARNING,
                "credentials-not-configured",
                "尚未配置 Telegram 登录凭据",
                1,
                {
                    "credentialsConfigured": False,
                    "privateUnknown": "api-secret",
                },
            ),
        ),
    )

    page.set_report(report, historical=False)

    assert page.table.currentIndex().row() == 0
    assert "清理下载所在磁盘" in page.details_remediation_label.text()
    assert "512 MiB" in page.details_metrics_label.text()
    assert "D:/private" not in page.details_metrics_label.text()

    page.table.setCurrentIndex(page.model.index(1, 0))

    assert "重新录入" in page.details_remediation_label.text()
    assert "API 凭据完整：否" in page.details_metrics_label.text()
    assert "api-secret" not in page.details_metrics_label.text()

    page.set_report(None, historical=True)

    assert page.details_remediation_label.text() == ""
    assert page.details_metrics_label.text() == ""


def test_page_emits_intent_only_signals_and_displays_safe_error(qtbot) -> None:
    page = DiagnosticsPage()
    qtbot.addWidget(page)

    with qtbot.waitSignal(page.run_requested, timeout=500):
        qtbot.mouseClick(page.start_button, Qt.MouseButton.LeftButton)
    page.set_running(True)
    with qtbot.waitSignal(page.cancel_requested, timeout=500):
        qtbot.mouseClick(page.cancel_button, Qt.MouseButton.LeftButton)
    page.set_running(False)
    page.set_report(diagnostic_report(), historical=False)
    with qtbot.waitSignal(page.export_requested, timeout=500):
        qtbot.mouseClick(page.export_button, Qt.MouseButton.LeftButton)
    with qtbot.waitSignal(page.open_directory_requested, timeout=500):
        qtbot.mouseClick(page.open_button, Qt.MouseButton.LeftButton)

    page.show_error("诊断报告保存失败")
    assert page.error_label.isVisible() is False
    page.show()
    assert page.error_label.isVisible() is True
    assert page.error_label.text() == "诊断报告保存失败"


@pytest.mark.parametrize("size", [(766, 660), (866, 720)])
def test_page_keeps_table_and_bottom_actions_visible(qtbot, size) -> None:
    page = DiagnosticsPage()
    qtbot.addWidget(page)
    page.resize(*size)
    page.show()
    qtbot.wait(20)

    assert page.table.isVisible()
    assert page.table.height() >= 180
    for button in (
        page.start_button,
        page.cancel_button,
        page.export_button,
        page.open_button,
    ):
        assert button.isVisible()
        bottom_right = button.mapTo(page, button.rect().bottomRight())
        assert page.rect().contains(bottom_right)
