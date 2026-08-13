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
    assert window.task_table.model().columnCount() == 6
    assert window.account_badge.text() == "未登录"
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
        [TaskSummary("t", "频道", TaskStatus.WAITING_RETRY, "1 / 2", "1 MB", "0", "4 秒")]
    )

    assert model.data(model.index(0, 1), Qt.ItemDataRole.DisplayRole) == "等待重试"
    assert model.data(model.index(0, 0), Qt.ItemDataRole.UserRole) == "t"
