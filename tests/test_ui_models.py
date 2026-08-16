from PySide6.QtCore import Qt

from telegram_downloader.domain import TaskStatus
from telegram_downloader.ui.models import TaskSummary, TaskTableModel


def task_summary(status: TaskStatus, queue_position: int | None) -> TaskSummary:
    return TaskSummary(
        "task",
        "Synthetic task",
        status,
        "0 / 1",
        "1 MB",
        "—",
        "—",
        "—",
        queue_position=queue_position,
    )


def test_queued_task_status_includes_known_queue_position(qtbot) -> None:
    model = TaskTableModel()
    model.set_tasks([task_summary(TaskStatus.QUEUED, 2)])

    assert model.data(model.index(0, 1), Qt.ItemDataRole.DisplayRole) == "等待中 · 第 2 位"
    assert "等待中 · 第 2 位" in model.data(
        model.index(0, 0),
        Qt.ItemDataRole.ToolTipRole,
    )


def test_unknown_queue_position_keeps_plain_queued_status(qtbot) -> None:
    model = TaskTableModel()
    model.set_tasks([task_summary(TaskStatus.QUEUED, None)])

    assert model.data(model.index(0, 1), Qt.ItemDataRole.DisplayRole) == "等待中"


def test_nonqueued_status_ignores_stale_queue_position(qtbot) -> None:
    model = TaskTableModel()
    model.set_tasks([task_summary(TaskStatus.PAUSED, 3)])

    assert model.data(model.index(0, 1), Qt.ItemDataRole.DisplayRole) == "已暂停"
