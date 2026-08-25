from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtTest import QSignalSpy

from telegram_downloader import app
from telegram_downloader.domain import (
    ItemStatus,
    MediaItem,
    MediaKind,
    ScanFilters,
    SourceKind,
    TaskRecord,
    TaskStatus,
)
from telegram_downloader.repository import ItemPage, ItemPageCursor, TaskSnapshot

TASK_COUNT = 10_000
MEDIA_COUNT = 50_000
PAGE_SIZE = 500


class SlowSyntheticTaskRepository:
    def __init__(self, root: Path, *, delay: float = 0.05) -> None:
        self.root = root
        self.delay = delay
        self.now = datetime(2026, 8, 25, tzinfo=UTC)
        filters = ScanFilters(
            self.now - timedelta(days=1),
            self.now,
            frozenset({MediaKind.VIDEO}),
            MEDIA_COUNT,
        )
        self.snapshots = {
            task_id: TaskSnapshot(
                TaskRecord(
                    task_id,
                    SourceKind.CHANNEL_OR_GROUP,
                    f"synthetic-peer-{index:05d}",
                    f"Synthetic source {index:05d}",
                    "synthetic://source",
                    filters,
                    TaskStatus.DOWNLOADING if index == 0 else TaskStatus.QUEUED,
                    self.now,
                    self.now,
                ),
                MEDIA_COUNT if index < 3 else 1,
                0,
                0,
                MEDIA_COUNT * 1024 if index < 3 else 1024,
                0,
                None,
            )
            for index in range(TASK_COUNT)
            for task_id in (f"task-{index:05d}",)
        }
        self.id_batches: list[tuple[str, ...]] = []
        self.patch_finished = threading.Event()
        self.block_task_id: str | None = None
        self.block_started = threading.Event()
        self.block_release = threading.Event()
        self.block_finished = threading.Event()

    def ensure_task_center_indexes(self) -> None:
        time.sleep(self.delay)

    def list_task_snapshots(self, *, include_archived: bool = False):
        assert include_archived is True
        time.sleep(self.delay)
        return list(self.snapshots.values())

    def list_task_snapshots_by_ids(
        self,
        task_ids,
        *,
        include_archived: bool = False,
    ):
        assert include_archived is True
        ordered = tuple(task_ids)
        self.id_batches.append(ordered)
        time.sleep(self.delay)
        result = [self.snapshots[task_id] for task_id in ordered if task_id in self.snapshots]
        self.patch_finished.set()
        return result

    def bump_progress(self, task_id: str) -> None:
        snapshot = self.snapshots[task_id]
        self.snapshots[task_id] = replace(snapshot, downloaded_bytes=1024)

    def list_items_page(
        self,
        task_id: str,
        *,
        after: ItemPageCursor | None = None,
        limit: int = PAGE_SIZE,
    ) -> ItemPage:
        assert limit == PAGE_SIZE
        if task_id == self.block_task_id:
            self.block_started.set()
            assert self.block_release.wait(timeout=2)
            self.block_finished.set()
        time.sleep(self.delay)
        offset = 0 if after is None else MEDIA_COUNT - after.message_id + 1
        stop = min(MEDIA_COUNT, offset + limit)
        items = tuple(self._item(task_id, index) for index in range(offset, stop))
        next_cursor = None
        if stop < MEDIA_COUNT:
            last = items[-1]
            next_cursor = ItemPageCursor(
                last.message_date_utc,
                last.message_id,
                last.id,
            )
        return ItemPage(items, next_cursor, MEDIA_COUNT)

    def get_items(self, item_ids):
        time.sleep(self.delay)
        result = []
        for item_id in dict.fromkeys(item_ids):
            task_id, raw_index = item_id.rsplit("-media-", 1)
            result.append(self._item(task_id, int(raw_index)))
        return result

    def _item(self, task_id: str, index: int) -> MediaItem:
        return MediaItem(
            f"{task_id}-media-{index:05d}",
            task_id,
            f"synthetic-peer-{task_id}",
            MEDIA_COUNT - index,
            None,
            f"synthetic-media-{index:05d}",
            MediaKind.VIDEO,
            f"item-{index:05d}.bin",
            self.root / "outputs" / task_id / f"item-{index:05d}.bin",
            1024,
            self.now - timedelta(seconds=index),
            0,
            ItemStatus.QUEUED,
        )


def test_task_center_stays_responsive_with_large_synthetic_data(
    tmp_path,
) -> None:
    application, loop, controller = app.create_application(tmp_path)
    window = controller.window
    repository = SlowSyntheticTaskRepository(tmp_path)
    controller.repository = repository
    window.resize(1200, 760)
    window.show()
    application.processEvents()
    heartbeats: list[float] = []
    timer = QTimer()
    timer.setInterval(5)
    timer.timeout.connect(lambda: heartbeats.append(time.perf_counter()))

    async def wait_for_summary(task_id: str, downloaded_bytes: int) -> None:
        for _ in range(100):
            summary = window.task_model.task_by_id(task_id)
            if summary is not None and summary.downloaded_bytes == downloaded_bytes:
                return
            await asyncio.sleep(0.01)
        raise AssertionError("task patch was not applied")

    async def scenario() -> None:
        timer.start()
        heartbeat_start = len(heartbeats)
        await controller.task_refresh.activate()
        assert len(heartbeats) > heartbeat_start
        assert window.task_model.rowCount() == TASK_COUNT

        task_resets = QSignalSpy(window.task_model.modelReset)
        changed_rows: list[tuple[int, int]] = []
        window.task_model.dataChanged.connect(
            lambda first, last: changed_rows.append((first.row(), last.row()))
        )
        repository.bump_progress("task-00000")
        for _ in range(500):
            controller.task_refresh.mark_progress(("task-00000",))
        assert await asyncio.to_thread(repository.patch_finished.wait, 2) is True
        await wait_for_summary("task-00000", 1024)

        assert repository.id_batches == [("task-00000",)]
        assert changed_rows and set(changed_rows) == {(0, 0)}
        assert task_resets.count() == 0

        window.task_table.selectRow(0)
        await controller._async_actions.wait_idle()
        assert window.task_item_model.rowCount() == PAGE_SIZE
        assert window.selected_task_ids() == ["task-00000"]

        media_resets = QSignalSpy(window.task_item_model.modelReset)
        window.task_item_table.selectRow(10)
        selected_media = window.selected_media_ids()
        scrollbar = window.task_item_table.verticalScrollBar()
        scrollbar.setValue(min(100, scrollbar.maximum()))
        application.processEvents()
        top_anchor = scrollbar.value()

        window.task_items_page_requested.emit("task-00000")
        await controller._async_actions.wait_idle()

        assert window.task_item_model.rowCount() == PAGE_SIZE * 2
        assert media_resets.count() == 0
        assert window.selected_task_ids() == ["task-00000"]
        assert window.selected_media_ids() == selected_media
        assert scrollbar.value() == top_anchor

        repository.block_task_id = "task-00001"
        window.task_table.selectRow(1)
        assert await asyncio.to_thread(repository.block_started.wait, 2) is True
        window.task_table.selectRow(2)
        await controller._async_actions.wait_idle()
        repository.block_release.set()
        assert await asyncio.to_thread(repository.block_finished.wait, 2) is True
        await asyncio.sleep(0.05)

        first_item = window.task_item_model.item_at(0)
        assert first_item is not None
        assert first_item.id.startswith("task-00002-media-")

    try:
        loop.run_until_complete(scenario())
    finally:
        timer.stop()
        loop.run_until_complete(controller._async_actions.shutdown())
        loop.run_until_complete(controller.shutdown())
        window.close()
        loop.close()
        application.processEvents()
