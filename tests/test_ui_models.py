from dataclasses import replace

import pytest
from PySide6.QtCore import QPersistentModelIndex, Qt
from PySide6.QtTest import QSignalSpy

from telegram_downloader.domain import TaskStatus
from telegram_downloader.ui.models import TaskFilter, TaskSummary, TaskTableModel


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


def make_task_summaries(count: int) -> list[TaskSummary]:
    return [
        replace(
            task_summary(TaskStatus.QUEUED, None),
            id=f"task-{index:05d}",
            title=f"Synthetic task {index:05d}",
        )
        for index in range(count)
    ]


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


def test_incremental_task_update_changes_one_row_without_reset(qtbot) -> None:
    model = TaskTableModel()
    tasks = make_task_summaries(10_000)
    order_keys = {task.id: (float(row), task.id) for row, task in enumerate(tasks)}
    model.apply_snapshot(tasks, order_keys)
    reset_spy = QSignalSpy(model.modelReset)
    reset_count = reset_spy.count()
    changed = QSignalSpy(model.dataChanged)
    task = model.task_by_id("task-05000")
    assert task is not None

    model.apply_tasks(
        [replace(task, progress_text="1 / 2")],
        {task.id: order_keys[task.id]},
    )

    assert reset_spy.count() == reset_count
    assert changed.count() == 1
    assert model.row_for_task_id(task.id) is not None
    assert model.task_by_id(task.id).progress_text == "1 / 2"
    assert len(model.all_tasks()) == 10_000


def test_incremental_task_structure_and_filter_preserve_persistent_id(qtbot) -> None:
    model = TaskTableModel()
    first = replace(task_summary(TaskStatus.QUEUED, None), id="a", title="A")
    tracked = replace(task_summary(TaskStatus.PAUSED, None), id="b", title="B")
    removed = replace(task_summary(TaskStatus.COMPLETED, None), id="c", title="C")
    model.apply_snapshot(
        [first, tracked, removed],
        {"a": (0.0, "a"), "b": (1.0, "b"), "c": (2.0, "c")},
    )
    persistent = QPersistentModelIndex(model.index(model.row_for_task_id("b"), 0))
    reset_spy = QSignalSpy(model.modelReset)
    inserted_spy = QSignalSpy(model.rowsInserted)
    removed_spy = QSignalSpy(model.rowsRemoved)
    moved_spy = QSignalSpy(model.rowsMoved)
    inserted = replace(task_summary(TaskStatus.QUEUED, None), id="d", title="D")

    model.apply_tasks([inserted], {"d": (1.5, "d")})
    model.apply_tasks([], {}, removed_ids=("c",))
    model.apply_tasks([tracked], {"b": (-1.0, "b")})
    model.set_filter(TaskFilter.PAUSED)

    assert reset_spy.count() == 0
    assert inserted_spy.count() == 1
    assert removed_spy.count() == 1
    assert moved_spy.count() == 1
    assert persistent.isValid()
    assert persistent.data(Qt.ItemDataRole.UserRole) == "b"
    assert model.row_for_task_id("b") == 0


def test_incremental_task_snapshot_rejects_duplicate_ids(qtbot) -> None:
    model = TaskTableModel()
    duplicate = task_summary(TaskStatus.QUEUED, None)

    with pytest.raises(ValueError, match="任务视图包含重复 ID"):
        model.apply_snapshot(
            [duplicate, replace(duplicate, title="Duplicate")],
            {duplicate.id: (0.0, duplicate.id)},
        )


def test_filter_counts_classifies_each_matching_task_once(qtbot, monkeypatch) -> None:
    model = TaskTableModel()
    tasks = make_task_summaries(200)
    model.set_tasks(tasks)
    original = model._matches_filter
    calls = 0

    def counted(task: TaskSummary, selected: TaskFilter) -> bool:
        nonlocal calls
        calls += 1
        return original(task, selected)

    monkeypatch.setattr(model, "_matches_filter", counted)

    counts = model.filter_counts()

    assert calls == len(tasks)
    assert counts[TaskFilter.ALL] == len(tasks)
    assert counts[TaskFilter.ACTIVE] == len(tasks)
