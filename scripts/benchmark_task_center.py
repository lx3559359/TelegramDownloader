from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from collections.abc import Iterator, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtWidgets import QApplication  # noqa: E402

import telegram_downloader.repository as repository_module  # noqa: E402
from telegram_downloader.domain import (  # noqa: E402
    MediaItem,
    MediaKind,
    ScanFilters,
    SourceKind,
    TaskRecord,
    TaskStatus,
)
from telegram_downloader.repository import TaskRepository  # noqa: E402
from telegram_downloader.task_refresh import TaskRefreshCoordinator  # noqa: E402
from telegram_downloader.ui.models import (  # noqa: E402
    TaskFilter,
    TaskSummary,
    TaskTableModel,
)

TASK_COUNT = 10_000
MEDIA_COUNT = 50_000


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Synthetic task-center benchmark")
    parser.add_argument("--repeats", type=int, default=7)
    parser.add_argument("--repository-delay-ms", type=float, default=50.0)
    parser.add_argument("--max-model-ms", type=float, default=100.0)
    parser.add_argument("--max-gap-ms", type=float, default=20.0)
    args = parser.parse_args(argv)
    if args.repeats < 1:
        parser.error("--repeats must be at least 1")
    if args.repository_delay_ms < 0:
        parser.error("--repository-delay-ms cannot be negative")
    if args.max_model_ms <= 0 or args.max_gap_ms <= 0:
        parser.error("benchmark limits must be positive")
    return args


def synthetic_task_summaries() -> tuple[TaskSummary, ...]:
    statuses = (
        TaskStatus.QUEUED,
        TaskStatus.DOWNLOADING,
        TaskStatus.PAUSED,
        TaskStatus.COMPLETED,
        TaskStatus.PARTIAL_FAILURE,
    )
    return tuple(
        TaskSummary(
            f"task-{index:05d}",
            f"Synthetic task {index:05d}",
            statuses[index % len(statuses)],
            f"{index % 10} / 10",
            "10 MB",
            "1.0 MB/s" if index % len(statuses) == 1 else "—",
            "10 秒" if index % len(statuses) == 1 else "—",
            "—",
            index % 10,
            10,
            index * 1024,
            10 * 1024 * 1024,
            1024 * 1024 if index % len(statuses) == 1 else 0.0,
            10 if index % len(statuses) == 1 else None,
            False,
            index + 1 if index % len(statuses) == 0 else None,
        )
        for index in range(TASK_COUNT)
    )


def task_order_keys(tasks: Sequence[TaskSummary]) -> dict[str, tuple[float, str]]:
    return {
        task.id: (float(index), task.id)
        for index, task in enumerate(tasks)
    }


def timed_ms(operation) -> float:
    started = time.perf_counter()
    operation()
    return (time.perf_counter() - started) * 1000


def benchmark_models(
    tasks: tuple[TaskSummary, ...],
    order_keys: dict[str, tuple[float, str]],
    repeats: int,
) -> tuple[list[float], list[float], list[float], int]:
    initial_runs: list[float] = []
    for _ in range(repeats):
        model = TaskTableModel()
        initial_runs.append(
            timed_ms(lambda model=model: model.apply_snapshot(tasks, order_keys))
        )

    model = TaskTableModel()
    model.apply_snapshot(tasks, order_keys)
    resets = 0

    def record_reset() -> None:
        nonlocal resets
        resets += 1

    model.modelReset.connect(record_reset)
    filter_runs: list[float] = []
    for index in range(repeats):
        query = f"synthetic task {index:05d}"
        filter_runs.append(
            timed_ms(lambda query=query: model.set_filter(TaskFilter.ALL, query))
        )
        model.set_filter(TaskFilter.ALL, "")

    patch_runs: list[float] = []
    original = tasks[TASK_COUNT // 2]
    for index in range(repeats):
        updated = replace(
            original,
            progress_text=f"{index + 1} / 10",
            downloaded_bytes=original.downloaded_bytes + index + 1,
        )
        patch_runs.append(
            timed_ms(
                lambda updated=updated: model.apply_tasks(
                    (updated,),
                    {updated.id: order_keys[updated.id]},
                )
            )
        )
    return initial_runs, filter_runs, patch_runs, resets


def synthetic_task(now: datetime) -> TaskRecord:
    return TaskRecord(
        "media-task",
        SourceKind.CHANNEL_OR_GROUP,
        "synthetic-peer",
        "Synthetic source",
        "synthetic://source",
        ScanFilters(
            now - timedelta(days=1),
            now,
            frozenset({MediaKind.VIDEO}),
            MEDIA_COUNT,
        ),
        TaskStatus.QUEUED,
        now,
        now,
    )


def synthetic_media_items(root: Path, now: datetime) -> Iterator[MediaItem]:
    for index in range(MEDIA_COUNT):
        yield MediaItem(
            f"media-{index:05d}",
            "media-task",
            "synthetic-peer",
            index + 1,
            None,
            f"synthetic-media-{index:05d}",
            MediaKind.VIDEO,
            f"item-{index:05d}.bin",
            root / "outputs" / f"item-{index:05d}.bin",
            1024,
            now - timedelta(seconds=index),
        )


def build_synthetic_repository(root: Path) -> TaskRepository:
    repository = TaskRepository(root / "benchmark.sqlite3")
    repository.initialize()
    now = datetime(2026, 8, 25, tzinfo=UTC)
    task = synthetic_task(now)
    columns = repository_module._ITEM_COLUMNS
    marks = ",".join("?" for _ in range(18))
    with repository._connection() as connection:
        TaskRepository._insert_task(connection, task)
        connection.executemany(
            f"INSERT INTO media_items ({columns}) VALUES ({marks})",
            (
                TaskRepository._item_values(item)
                for item in synthetic_media_items(root, now)
            ),
        )
    repository.ensure_task_center_indexes()
    return repository


def benchmark_media(repository: TaskRepository, repeats: int) -> list[float]:
    repository.list_items_page("media-task", limit=500)
    return [
        timed_ms(lambda: repository.list_items_page("media-task", limit=500))
        for _ in range(repeats)
    ]


async def benchmark_event_loop(
    repository_delay_ms: float,
) -> tuple[float, int]:
    await asyncio.to_thread(lambda: None)
    gaps: list[float] = []
    applied = asyncio.Event()
    dirty_batches = 0
    running = True

    async def ticker() -> None:
        previous = asyncio.get_running_loop().time()
        while running:
            await asyncio.sleep(0.005)
            current = asyncio.get_running_loop().time()
            gaps.append(max(0.0, current - previous - 0.005))
            previous = current

    async def load_full() -> tuple[()]:
        return ()

    async def load_ids(task_ids: tuple[str, ...]) -> tuple[str, ...]:
        nonlocal dirty_batches
        dirty_batches += 1
        await asyncio.to_thread(time.sleep, repository_delay_ms / 1000)
        return task_ids

    coordinator = TaskRefreshCoordinator[
        tuple[()],
        tuple[str, ...],
    ](
        load_full=load_full,
        load_ids=load_ids,
        apply_full=lambda _value: None,
        apply_patch=lambda _value: applied.set(),
        progress_interval=0.02,
        reconcile_interval=5.0,
    )
    ticker_task = asyncio.create_task(ticker())
    try:
        await coordinator.activate()
        for _ in range(500):
            coordinator.mark_progress(("task",))
        await asyncio.wait_for(applied.wait(), timeout=2.0)
    finally:
        running = False
        await coordinator.close()
        await ticker_task
    return max(gaps, default=0.0) * 1000, dirty_batches


def print_runs(
    initial: Sequence[float],
    filtered: Sequence[float],
    patched: Sequence[float],
    media: Sequence[float],
) -> None:
    for index, values in enumerate(
        zip(initial, filtered, patched, media, strict=True),
        start=1,
    ):
        initial_ms, filter_ms, patch_ms, media_ms = values
        print(
            f"RUN_{index}="
            f"initial:{initial_ms:.2f},filter:{filter_ms:.2f},"
            f"patch:{patch_ms:.2f},media:{media_ms:.2f}"
        )


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    application = QApplication.instance() or QApplication([])
    del application
    tasks = synthetic_task_summaries()
    order_keys = task_order_keys(tasks)
    initial, filtered, patched, resets = benchmark_models(
        tasks,
        order_keys,
        args.repeats,
    )
    with TemporaryDirectory(prefix="task-center-benchmark-") as temporary:
        repository = build_synthetic_repository(Path(temporary))
        media = benchmark_media(repository, args.repeats)
    max_gap_ms, dirty_batches = asyncio.run(
        benchmark_event_loop(args.repository_delay_ms)
    )

    print_runs(initial, filtered, patched, media)
    initial_median = median(initial)
    filter_median = median(filtered)
    patch_median = median(patched)
    media_median = median(media)
    print(f"TASKS={TASK_COUNT}")
    print(f"TASK_INITIAL_MEDIAN_MS={initial_median:.2f}")
    print(f"TASK_FILTER_MEDIAN_MS={filter_median:.2f}")
    print(f"TASK_ONE_ROW_MEDIAN_MS={patch_median:.2f}")
    print(f"MEDIA_ITEMS={MEDIA_COUNT}")
    print(f"MEDIA_FIRST_PAGE_MEDIAN_MS={media_median:.2f}")
    print(f"MAX_EVENT_LOOP_GAP_MS={max_gap_ms:.2f}")
    print(f"MODEL_RESETS_AFTER_INITIAL={resets}")
    print(f"DIRTY_BATCHES_FOR_500_EVENTS={dirty_batches}")

    medians = (initial_median, filter_median, patch_median, media_median)
    passed = (
        all(value <= args.max_model_ms for value in medians)
        and max_gap_ms <= args.max_gap_ms
        and resets == 0
        and dirty_batches == 1
    )
    if passed:
        print("TASK_CENTER_BENCHMARK_OK")
        return 0
    print("TASK_CENTER_BENCHMARK_FAILED", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
