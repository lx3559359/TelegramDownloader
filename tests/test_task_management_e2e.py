from dataclasses import replace
from datetime import UTC, datetime

import pytest

from telegram_downloader.controller import AppController
from telegram_downloader.domain import (
    ItemStatus,
    MediaItem,
    MediaKind,
    ScanFilters,
    SourceKind,
    TaskRecord,
    TaskStatus,
)
from telegram_downloader.paths import PortablePaths
from telegram_downloader.repository import AllMediaAlreadyExists, TaskRepository
from telegram_downloader.ui.main import MainWindow
from telegram_downloader.ui.models import TaskFilter


def completed_fixture(paths: PortablePaths) -> tuple[TaskRecord, MediaItem]:
    now = datetime(2026, 8, 16, tzinfo=UTC)
    task = TaskRecord(
        "finished-task",
        SourceKind.CHANNEL_OR_GROUP,
        "synthetic-peer",
        "Synthetic source",
        "https://t.me/synthetic",
        ScanFilters(now, now, frozenset({MediaKind.VIDEO}), 10),
        TaskStatus.COMPLETED,
        now,
        now,
        display_title="Finished synthetic task",
    )
    target = paths.downloads / "synthetic" / "video.mp4"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"media")
    item = MediaItem(
        "finished-item",
        task.id,
        task.source_ref,
        7,
        None,
        "media-7",
        MediaKind.VIDEO,
        "video.mp4",
        target,
        5,
        now,
        5,
        ItemStatus.COMPLETED,
    )
    return task, item


def test_task_management_persists_archive_restart_restore_and_dedup(
    qtbot,
    tmp_path,
) -> None:
    paths = PortablePaths(tmp_path / "application")
    paths.ensure_layout()
    repository = TaskRepository(paths.database)
    repository.initialize()
    completed, item = completed_fixture(paths)
    repository.create_task(completed, [item])

    window = MainWindow()
    qtbot.addWidget(window)
    controller = AppController.for_test(
        repository=repository,
        window=window,
        paths=paths,
    )
    window.task_selection_changed.connect(controller.select_task_details)
    controller.refresh_tasks(now=1.0)

    assert window.task_model.filter_counts()[TaskFilter.COMPLETED] == 1
    window.task_search.setText("finished")
    assert window.task_model.rowCount() == 1
    window.task_table.selectRow(0)
    assert window.task_item_model.rowCount() == 1
    assert window.task_item_model.item_at(0).id == item.id

    controller.archive_tasks([completed.id])

    assert repository.get_task(completed.id).archived_at is not None
    assert window.task_model.rowCount() == 0
    duplicate_task = replace(completed, id="duplicate-task", archived_at=None)
    with pytest.raises(AllMediaAlreadyExists):
        repository.create_task_deduplicating(
            duplicate_task,
            [replace(item, id="duplicate-item", task_id=duplicate_task.id)],
        )

    restarted = TaskRepository(paths.database)
    restarted.initialize()
    restarted_window = MainWindow()
    qtbot.addWidget(restarted_window)
    restarted_controller = AppController.for_test(
        repository=restarted,
        window=restarted_window,
        paths=paths,
    )
    restarted_controller.refresh_tasks(now=2.0)
    archived_index = restarted_window.task_filter.findData(TaskFilter.ARCHIVED)
    restarted_window.task_filter.setCurrentIndex(archived_index)

    assert restarted_window.task_model.rowCount() == 1
    assert restarted_window.task_model.task_at(0).archived is True
    restarted_controller.restore_tasks([completed.id])

    assert restarted.get_task(completed.id).archived_at is None
    assert restarted_window.task_model.rowCount() == 0
    restarted_window.task_filter.setCurrentIndex(
        restarted_window.task_filter.findData(TaskFilter.ALL)
    )
    assert restarted_window.task_model.rowCount() == 1
    assert paths.guard(item.target_path).is_file()
    assert paths.database.is_relative_to(paths.root)
    assert item.target_path.is_relative_to(paths.root)
