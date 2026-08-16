from dataclasses import replace
from datetime import UTC, datetime

import pytest
from PySide6.QtCore import QItemSelectionModel, Qt
from PySide6.QtWidgets import QAbstractItemView, QMessageBox

from telegram_downloader.domain import IntegrityStatus, ItemStatus, MediaKind, TaskStatus
from telegram_downloader.file_integrity import IntegrityProgress
from telegram_downloader.ui.main import MainWindow
from telegram_downloader.ui.models import (
    TaskFilter,
    TaskItemSummary,
    TaskItemTableModel,
    TaskSummary,
    TaskTableModel,
)


def test_workbench_contains_required_controls(qtbot) -> None:
    window = MainWindow()
    qtbot.addWidget(window)

    assert window.windowTitle() == "Telegram 下载器"
    assert window.minimumSize().width() >= 1180
    assert window.minimumSize().height() >= 720
    assert window.link_input.placeholderText().startswith("粘贴")
    assert window.limit_input.minimum() == 1
    assert window.limit_input.maximum() == 100000
    assert window.limit_input.value() == 500
    assert window.task_table.model().columnCount() == 7
    assert window.account_badge.text() == "未登录"
    assert window.version_label.text() == "v0.11.1 · stable"
    assert set(window.media_checks) == set(MediaKind)
    assert all(check.isChecked() for check in window.media_checks.values())


def test_scan_button_emits_trimmed_link(qtbot) -> None:
    window = MainWindow()
    qtbot.addWidget(window)
    window.link_input.setText("  https://t.me/example/42  ")

    with qtbot.waitSignal(window.scan_requested, timeout=500) as signal:
        qtbot.mouseClick(window.scan_button, Qt.MouseButton.LeftButton)

    assert signal.args == ["https://t.me/example/42"]


def test_task_actions_emit_selected_task_id(qtbot) -> None:
    window = MainWindow()
    qtbot.addWidget(window)
    window.task_model.set_tasks(
        [
            TaskSummary(
                "task-7",
                "示例频道",
                TaskStatus.DOWNLOADING,
                "3 / 10",
                "120 MB",
                "2.5 MB/s",
                "40 秒",
                "—",
            )
        ]
    )
    window.task_table.selectRow(0)

    with qtbot.waitSignal(window.pause_requested, timeout=500) as signal:
        qtbot.mouseClick(window.pause_button, Qt.MouseButton.LeftButton)

    assert signal.args == ["task-7"]


def test_single_queued_task_can_be_prioritized(qtbot) -> None:
    window = MainWindow()
    qtbot.addWidget(window)
    window.set_task_summaries(
        [
            TaskSummary(
                "queued",
                "Queued task",
                TaskStatus.QUEUED,
                "0 / 1",
                "1 MB",
                "—",
                "—",
                "—",
                queue_position=2,
            )
        ]
    )
    window.task_table.selectRow(0)

    assert window.prioritize_button.isEnabled() is True
    with qtbot.waitSignal(window.prioritize_task_requested, timeout=500) as signal:
        qtbot.mouseClick(window.prioritize_button, Qt.MouseButton.LeftButton)

    assert signal.args == ["queued"]


def test_priority_requires_exactly_one_unarchived_queued_task(qtbot) -> None:
    window = MainWindow()
    qtbot.addWidget(window)
    queued = TaskSummary(
        "queued",
        "Queued",
        TaskStatus.QUEUED,
        "0 / 1",
        "1 MB",
        "—",
        "—",
        "—",
    )
    window.set_task_summaries(
        [
            queued,
            replace(queued, id="second"),
            replace(queued, id="active", status=TaskStatus.DOWNLOADING),
        ]
    )
    selection = window.task_table.selectionModel()
    flags = QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows
    selection.select(window.task_model.index(0, 0), flags)
    selection.select(window.task_model.index(1, 0), flags)
    assert window.prioritize_button.isEnabled() is False

    selection.clearSelection()
    window.task_table.selectRow(2)
    assert window.prioritize_button.isEnabled() is False

    window.set_task_summaries([replace(queued, archived=True)])
    window.task_filter.setCurrentIndex(window.task_filter.findData(TaskFilter.ARCHIVED))
    window.task_table.selectRow(0)
    assert window.prioritize_button.isEnabled() is False


def test_scheduler_summary_formats_active_and_idle_resources(qtbot) -> None:
    window = MainWindow()
    qtbot.addWidget(window)

    window.set_scheduler_summary(
        active=1,
        queued=3,
        concurrency=3,
        speed_limit_kib=0,
    )
    assert (
        window.scheduler_summary.text()
        == "调度：1 个下载中 · 3 个等待 · 文件并发 3 · 不限速"
    )

    window.set_scheduler_summary(
        active=0,
        queued=0,
        concurrency=3,
        speed_limit_kib=2048,
    )
    assert window.scheduler_summary.text() == "调度：空闲 · 文件并发 3 · 限速 2.0 MB/s"


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (TaskStatus.QUEUED, (True, False, False, True)),
        (TaskStatus.DOWNLOADING, (True, False, False, True)),
        (TaskStatus.WAITING_RETRY, (True, False, False, True)),
        (TaskStatus.PAUSED, (False, True, False, True)),
        (TaskStatus.COMPLETED, (False, False, False, True)),
        (TaskStatus.PARTIAL_FAILURE, (False, False, True, True)),
    ],
)
def test_task_actions_match_selected_task_status(qtbot, status, expected) -> None:
    window = MainWindow()
    qtbot.addWidget(window)
    window.set_task_summaries(
        [
            TaskSummary(
                "task-1",
                "示例频道",
                status,
                "1 / 2",
                "1 MB",
                "—",
                "—",
                "—",
            )
        ]
    )

    window.task_table.selectRow(0)

    assert (
        window.pause_button.isEnabled(),
        window.resume_button.isEnabled(),
        window.retry_button.isEnabled(),
        window.open_button.isEnabled(),
    ) == expected


def test_task_model_exposes_chinese_status_and_id_role(qtbot) -> None:
    model = TaskTableModel()
    model.set_tasks(
        [
            TaskSummary(
                "t",
                "频道",
                TaskStatus.WAITING_RETRY,
                "1 / 2",
                "1 MB",
                "0",
                "4 秒",
                "Telegram 网络连接失败",
            )
        ]
    )

    assert model.data(model.index(0, 1), Qt.ItemDataRole.DisplayRole) == "等待重试"
    assert model.data(model.index(0, 0), Qt.ItemDataRole.UserRole) == "t"
    assert model.headerData(6, Qt.Orientation.Horizontal) == "错误"
    assert model.data(model.index(0, 6), Qt.ItemDataRole.DisplayRole) == "Telegram 网络连接失败"
    assert "Telegram 网络连接失败" in model.data(model.index(0, 0), Qt.ItemDataRole.ToolTipRole)


def test_task_model_filters_search_status_and_archives() -> None:
    model = TaskTableModel()
    model.set_tasks(
        [
            TaskSummary("a", "Alpha", TaskStatus.DOWNLOADING, "0 / 1", "1 B", "—", "—", "—"),
            TaskSummary("b", "Beta", TaskStatus.PAUSED, "0 / 1", "1 B", "—", "—", "—"),
            TaskSummary("c", "Gamma", TaskStatus.PARTIAL_FAILURE, "0 / 1", "1 B", "—", "—", "safe"),
            TaskSummary("d", "Done", TaskStatus.COMPLETED, "1 / 1", "1 B", "—", "—", "—"),
            TaskSummary(
                "e", "Old", TaskStatus.COMPLETED, "1 / 1", "1 B", "—", "—", "—", archived=True
            ),
        ]
    )

    assert model.filter_counts() == {
        TaskFilter.ALL: 4,
        TaskFilter.ACTIVE: 1,
        TaskFilter.PAUSED: 1,
        TaskFilter.FAILED: 1,
        TaskFilter.COMPLETED: 1,
        TaskFilter.ARCHIVED: 1,
    }
    assert model.rowCount() == 4
    assert model.row_for_task_id("e") is None

    model.set_filter(TaskFilter.FAILED, "amm")

    assert model.rowCount() == 1
    assert model.task_at(0).id == "c"
    assert model.row_for_task_id("c") == 0

    model.set_filter(TaskFilter.ARCHIVED, "")

    assert model.rowCount() == 1
    assert model.task_at(0).id == "e"


def test_task_item_model_formats_status_progress_size_and_id() -> None:
    model = TaskItemTableModel()
    model.set_items(
        [
            TaskItemSummary(
                "item",
                "video.mp4",
                MediaKind.VIDEO,
                ItemStatus.COMPLETED,
                10,
                10,
                2,
                "—",
            ),
            TaskItemSummary(
                "unknown",
                "document.bin",
                MediaKind.DOCUMENT,
                ItemStatus.DOWNLOADING,
                3,
                None,
                0,
                "—",
            ),
        ]
    )

    assert model.headerData(0, Qt.Orientation.Horizontal) == "文件"
    assert model.data(model.index(0, 0), Qt.ItemDataRole.UserRole) == "item"
    assert model.data(model.index(0, 1)) == "视频"
    assert model.data(model.index(0, 2)) == "已完成"
    assert model.data(model.index(0, 3)) == "未校验"
    assert model.data(model.index(0, 4)) == "100%"
    assert model.data(model.index(0, 5)) == "10 B / 10 B"
    assert model.data(model.index(0, 6)) == "2"
    assert model.data(model.index(1, 4)) == "—"
    assert model.data(model.index(1, 5)) == "3 B / 未知"
    assert model.item_at(0).id == "item"
    assert model.item_at(99) is None


def test_task_item_model_formats_integrity_and_verified_tooltip() -> None:
    model = TaskItemTableModel()
    verified_at = datetime(2026, 8, 16, 8, 9, tzinfo=UTC)
    model.set_items(
        [
            TaskItemSummary(
                "verified",
                "ok.bin",
                MediaKind.DOCUMENT,
                ItemStatus.COMPLETED,
                4,
                4,
                0,
                "—",
                IntegrityStatus.VERIFIED,
                verified_at,
            ),
            TaskItemSummary(
                "broken",
                "bad.bin",
                MediaKind.DOCUMENT,
                ItemStatus.FAILED,
                4,
                4,
                0,
                "本地文件哈希不一致",
                IntegrityStatus.HASH_MISMATCH,
            ),
        ]
    )

    assert model.headerData(3, Qt.Orientation.Horizontal) == "完整性"
    assert model.data(model.index(0, 3)) == "已校验"
    assert "2026-08-16 08:09 UTC" in model.data(
        model.index(0, 3),
        Qt.ItemDataRole.ToolTipRole,
    )
    assert model.data(model.index(1, 3)) == "哈希异常"


def test_scan_busy_state_disables_source_controls(qtbot) -> None:
    window = MainWindow()
    qtbot.addWidget(window)

    window.set_scan_busy(True)

    assert window.link_input.isEnabled() is False
    assert window.scan_button.isEnabled() is False
    assert window.scan_button.text() == "扫描中…"

    window.set_scan_busy(False)

    assert window.link_input.isEnabled() is True
    assert window.scan_button.isEnabled() is True
    assert window.scan_button.text() == "扫描预览"


def test_live_summary_updates_statistics_and_current_task(qtbot) -> None:
    window = MainWindow()
    qtbot.addWidget(window)
    window.set_task_summaries(
        [
            TaskSummary(
                "task-1",
                "示例频道",
                TaskStatus.DOWNLOADING,
                "1 / 2",
                "1.0 KB",
                "512 B/s",
                "1 秒",
                "—",
                completed_items=1,
                total_items=2,
                downloaded_bytes=512,
                total_bytes=1024,
                speed_bps=512,
                remaining_seconds=1,
            )
        ]
    )

    assert window.speed_value.text() == "512 B/s"
    assert window.completed_value.text() == "1"
    assert window.remaining_value.text() == "1"
    assert window.current_task_label.text() == "示例频道"
    assert window.current_progress.value() == 50
    assert "1 / 2" in window.current_detail.text()
    assert "剩余 1 秒" in window.current_detail.text()


def test_live_refresh_preserves_selected_task(qtbot) -> None:
    window = MainWindow()
    qtbot.addWidget(window)
    initial = TaskSummary(
        "task-1",
        "示例频道",
        TaskStatus.DOWNLOADING,
        "0 / 2",
        "1.0 KB",
        "—",
        "—",
        "—",
        total_items=2,
        total_bytes=1024,
    )
    updated = TaskSummary(
        "task-1",
        "示例频道",
        TaskStatus.DOWNLOADING,
        "1 / 2",
        "1.0 KB",
        "512 B/s",
        "1 秒",
        "—",
        completed_items=1,
        total_items=2,
        downloaded_bytes=512,
        total_bytes=1024,
        speed_bps=512,
        remaining_seconds=1,
    )
    window.set_task_summaries([initial])
    window.task_table.selectRow(0)

    window.set_task_summaries([updated])

    assert window.selected_task_id() == "task-1"
    assert window.pause_button.isEnabled() is True


def test_task_workspace_filters_and_emits_stable_multiselect_batches(qtbot) -> None:
    window = MainWindow()
    qtbot.addWidget(window)
    window.set_task_summaries(
        [
            TaskSummary("run", "Running", TaskStatus.DOWNLOADING, "0 / 1", "1 B", "—", "—", "—"),
            TaskSummary("pause", "Paused", TaskStatus.PAUSED, "0 / 1", "1 B", "—", "—", "—"),
            TaskSummary("done", "Done", TaskStatus.COMPLETED, "1 / 1", "1 B", "—", "—", "—"),
        ]
    )
    selection = window.task_table.selectionModel()
    flags = QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows
    selection.select(window.task_model.index(0, 0), flags)
    selection.select(window.task_model.index(1, 0), flags)

    assert window.task_table.selectionMode() is QAbstractItemView.SelectionMode.ExtendedSelection
    assert window.selected_task_ids() == ["run", "pause"]
    assert window.open_button.isEnabled() is False
    with qtbot.waitSignal(window.pause_tasks_requested, timeout=500) as caught:
        qtbot.mouseClick(window.pause_button, Qt.MouseButton.LeftButton)
    assert caught.args == [["run", "pause"]]

    window.task_search.setText("Done")

    assert window.task_model.rowCount() == 1
    assert window.task_model.task_at(0).id == "done"
    assert window.task_filter.currentText().startswith("全部 (1)")


def test_single_task_selection_loads_details_and_emits_open_media(qtbot) -> None:
    window = MainWindow()
    qtbot.addWidget(window)
    window.set_task_summaries(
        [
            TaskSummary(
                "done",
                "Completed task",
                TaskStatus.COMPLETED,
                "1 / 1",
                "10 B",
                "—",
                "—",
                "—",
            )
        ]
    )
    window.task_table.selectRow(0)
    window.set_task_items(
        "done",
        [
            TaskItemSummary(
                "media",
                "video.mp4",
                MediaKind.VIDEO,
                ItemStatus.COMPLETED,
                10,
                10,
                0,
                "—",
            )
        ],
    )

    assert window.task_detail_title.text() == "Completed task"
    assert window.task_item_model.rowCount() == 1
    window.task_item_table.selectRow(0)
    assert window.open_file_button.isEnabled() is True
    with qtbot.waitSignal(window.open_media_requested, timeout=500) as caught:
        window.task_item_table.doubleClicked.emit(window.task_item_model.index(0, 0))
    assert caught.args == ["media"]


def test_integrity_actions_use_stable_selected_media_ids(qtbot, monkeypatch) -> None:
    window = MainWindow()
    qtbot.addWidget(window)
    window.set_task_summaries(
        [TaskSummary("done", "Done", TaskStatus.COMPLETED, "2 / 2", "8 B", "—", "—", "—")]
    )
    window.task_table.selectRow(0)
    window.set_task_items(
        "done",
        [
            TaskItemSummary(
                "healthy",
                "healthy.bin",
                MediaKind.DOCUMENT,
                ItemStatus.COMPLETED,
                4,
                4,
                0,
                "—",
                IntegrityStatus.VERIFIED,
            ),
            TaskItemSummary(
                "broken",
                "broken.bin",
                MediaKind.DOCUMENT,
                ItemStatus.FAILED,
                4,
                4,
                0,
                "本地文件缺失",
                IntegrityStatus.MISSING,
            ),
        ],
    )
    selection = window.task_item_table.selectionModel()
    flags = QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows
    selection.select(window.task_item_model.index(0, 0), flags)
    selection.select(window.task_item_model.index(1, 0), flags)

    assert (
        window.task_item_table.selectionMode()
        is QAbstractItemView.SelectionMode.ExtendedSelection
    )
    assert window.selected_media_ids() == ["healthy", "broken"]
    assert window.open_file_button.isEnabled() is False
    with qtbot.waitSignal(window.verify_media_requested, timeout=500) as verify:
        qtbot.mouseClick(window.verify_media_button, Qt.MouseButton.LeftButton)
    assert verify.args == [["healthy", "broken"]]

    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes,
    )
    with qtbot.waitSignal(window.repair_media_requested, timeout=500) as repair:
        qtbot.mouseClick(window.repair_media_button, Qt.MouseButton.LeftButton)
    assert repair.args == [["broken"]]


def test_integrity_busy_progress_and_cancel_feedback(qtbot) -> None:
    window = MainWindow()
    qtbot.addWidget(window)
    window.show()
    window.set_task_summaries(
        [TaskSummary("done", "Done", TaskStatus.COMPLETED, "1 / 1", "4 B", "—", "—", "—")]
    )
    window.task_table.selectRow(0)
    window.set_task_items(
        "done",
        [
            TaskItemSummary(
                "media",
                "file.bin",
                MediaKind.DOCUMENT,
                ItemStatus.COMPLETED,
                4,
                4,
                0,
                "—",
            )
        ],
    )
    window.task_item_table.selectRow(0)

    window.set_integrity_busy(True)
    window.set_integrity_progress(
        IntegrityProgress(1, 3, "media", "file.bin", IntegrityStatus.VERIFIED)
    )

    assert window.integrity_progress_panel.isVisibleTo(window) is True
    assert window.integrity_progress.value() == 1
    assert window.integrity_progress.maximum() == 3
    assert "file.bin" in window.integrity_progress_label.text()
    assert window.verify_media_button.isEnabled() is False
    assert window.repair_media_button.isEnabled() is False
    assert window.verify_tasks_button.isEnabled() is False
    with qtbot.waitSignal(window.integrity_cancel_requested, timeout=500):
        qtbot.mouseClick(window.integrity_cancel_button, Qt.MouseButton.LeftButton)

    window.set_integrity_busy(False)
    assert window.integrity_progress_panel.isHidden() is True
    assert window.verify_media_button.isEnabled() is True


def test_task_level_verify_emits_completed_and_partial_task_ids(qtbot) -> None:
    window = MainWindow()
    qtbot.addWidget(window)
    window.set_task_summaries(
        [
            TaskSummary("done", "Done", TaskStatus.COMPLETED, "1 / 1", "4 B", "—", "—", "—"),
            TaskSummary(
                "partial",
                "Partial",
                TaskStatus.PARTIAL_FAILURE,
                "0 / 1",
                "4 B",
                "—",
                "—",
                "缺失",
            ),
        ]
    )
    selection = window.task_table.selectionModel()
    flags = QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows
    selection.select(window.task_model.index(0, 0), flags)
    selection.select(window.task_model.index(1, 0), flags)

    with qtbot.waitSignal(window.verify_tasks_requested, timeout=500) as signal:
        qtbot.mouseClick(window.verify_tasks_button, Qt.MouseButton.LeftButton)

    assert signal.args == [["done", "partial"]]


def test_synchronous_detail_result_is_not_overwritten_by_loading_hint(qtbot) -> None:
    window = MainWindow()
    qtbot.addWidget(window)
    window.set_task_summaries(
        [
            TaskSummary(
                "done",
                "Completed task",
                TaskStatus.COMPLETED,
                "1 / 1",
                "10 B",
                "—",
                "—",
                "—",
            )
        ]
    )
    detail = TaskItemSummary(
        "media",
        "video.mp4",
        MediaKind.VIDEO,
        ItemStatus.COMPLETED,
        10,
        10,
        0,
        "—",
    )

    def load_details(task_ids) -> None:
        if task_ids == ["done"]:
            window.set_task_items("done", [detail])

    window.task_selection_changed.connect(load_details)
    window.task_table.selectRow(0)

    assert window.task_item_model.rowCount() == 1
    assert window.task_detail_hint.text() == "共 1 个媒体文件"


def test_archive_and_restore_actions_require_confirmation(
    qtbot,
    monkeypatch,
) -> None:
    window = MainWindow()
    qtbot.addWidget(window)
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes,
    )
    completed = TaskSummary(
        "done",
        "Done",
        TaskStatus.COMPLETED,
        "1 / 1",
        "1 B",
        "—",
        "—",
        "—",
    )
    window.set_task_summaries([completed])
    window.task_table.selectRow(0)

    assert window.archive_button.isEnabled() is True
    assert window.restore_button.isEnabled() is False
    with qtbot.waitSignal(window.archive_tasks_requested, timeout=500) as caught:
        qtbot.mouseClick(window.archive_button, Qt.MouseButton.LeftButton)
    assert caught.args == [["done"]]

    window.set_task_summaries([replace(completed, archived=True)])
    window.task_filter.setCurrentIndex(window.task_filter.findData(TaskFilter.ARCHIVED))
    window.task_table.selectRow(0)

    assert window.archive_button.isEnabled() is False
    assert window.restore_button.isEnabled() is True
    with qtbot.waitSignal(window.restore_tasks_requested, timeout=500) as caught:
        qtbot.mouseClick(window.restore_button, Qt.MouseButton.LeftButton)
    assert caught.args == [["done"]]


def test_content_navigation_switches_page_and_hides_statistics(qtbot) -> None:
    window = MainWindow()
    qtbot.addWidget(window)
    window.show()

    qtbot.mouseClick(window.content_nav_button, Qt.MouseButton.LeftButton)

    assert window.page_stack.currentWidget() is window.content_page
    assert window.statistics_panel.isHidden() is True
    assert window.content_nav_button.property("active") is True

    qtbot.mouseClick(window.tasks_nav_button, Qt.MouseButton.LeftButton)
    assert window.page_stack.currentWidget() is window.task_page
    assert window.statistics_panel.isHidden() is False
    assert window.tasks_nav_button.property("active") is True


def test_content_navigation_emits_activation_and_link_preview_routes_to_tasks(
    qtbot,
) -> None:
    window = MainWindow()
    qtbot.addWidget(window)

    with qtbot.waitSignal(window.content_activated, timeout=500):
        qtbot.mouseClick(window.content_nav_button, Qt.MouseButton.LeftButton)

    with qtbot.waitSignal(window.scan_requested, timeout=500) as caught:
        window.open_link_preview("https://t.me/Zhangzhoulao66/56156")

    assert window.page_stack.currentWidget() is window.task_page
    assert window.link_input.text() == "https://t.me/Zhangzhoulao66/56156"
    assert caught.args == ["https://t.me/Zhangzhoulao66/56156"]


def test_subscription_navigation_switches_page_and_emits_activation(qtbot) -> None:
    window = MainWindow()
    qtbot.addWidget(window)
    window.show()

    with qtbot.waitSignal(window.subscriptions_activated, timeout=500):
        qtbot.mouseClick(
            window.subscriptions_nav_button,
            Qt.MouseButton.LeftButton,
        )

    assert window.page_stack.currentWidget() is window.subscriptions_page
    assert window.statistics_panel.isHidden() is True
    assert window.subscriptions_nav_button.property("active") is True


def test_diagnostics_navigation_switches_page_and_emits_activation(qtbot) -> None:
    window = MainWindow()
    qtbot.addWidget(window)
    window.resize(1180, 720)
    window.show()

    with qtbot.waitSignal(window.diagnostics_activated, timeout=500):
        qtbot.mouseClick(window.diagnostics_nav_button, Qt.MouseButton.LeftButton)

    assert window.page_stack.currentWidget() is window.diagnostics_page
    assert window.statistics_panel.isHidden() is True
    assert window.diagnostics_nav_button.property("active") is True
    assert window.diagnostics_page.start_button.isVisible()
    assert window.diagnostics_page.open_button.isVisible()
