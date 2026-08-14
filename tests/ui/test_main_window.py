from PySide6.QtCore import Qt

from telegram_downloader.domain import MediaKind, TaskStatus
from telegram_downloader.ui.main import MainWindow
from telegram_downloader.ui.models import TaskSummary, TaskTableModel


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
    assert window.version_label.text() == "v0.2.3 · stable"
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
    assert "Telegram 网络连接失败" in model.data(
        model.index(0, 0), Qt.ItemDataRole.ToolTipRole
    )


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
