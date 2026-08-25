from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

from telegram_downloader.domain import TaskStatus
from telegram_downloader.repository import TaskSnapshot
from telegram_downloader.scheduler import SchedulerSnapshot
from telegram_downloader.ui.models import TaskSummary


@dataclass(frozen=True, slots=True)
class ProgressSample:
    sampled_at: float
    downloaded_bytes: int


@dataclass(frozen=True, slots=True)
class TaskDashboard:
    total_speed_bps: float
    completed_items: int
    remaining_items: int
    current_task_id: str | None


@dataclass(frozen=True, slots=True)
class TaskView:
    ordered: tuple[TaskSummary, ...]
    by_id: Mapping[str, TaskSummary]
    order_keys: Mapping[str, tuple[float, str]]
    progress_samples: Mapping[str, ProgressSample]
    dashboard: TaskDashboard


@dataclass(frozen=True, slots=True)
class TaskViewPatch:
    replacements: Mapping[str, TaskSummary]
    order_keys: Mapping[str, tuple[float, str]]
    progress_samples: Mapping[str, ProgressSample]
    removed_ids: frozenset[str]


def build_task_view(
    snapshots: Sequence[TaskSnapshot],
    *,
    scheduler_state: SchedulerSnapshot,
    queue_positions: Mapping[str, int],
    sampled_at: float,
    previous_samples: Mapping[str, ProgressSample],
) -> TaskView:
    del scheduler_state
    summaries: dict[str, TaskSummary] = {}
    order_keys: dict[str, tuple[float, str]] = {}
    progress_samples: dict[str, ProgressSample] = {}
    for index, snapshot in enumerate(snapshots):
        summary, sample = _summary_from_snapshot(
            snapshot,
            queue_positions=queue_positions,
            sampled_at=sampled_at,
            previous_sample=previous_samples.get(snapshot.task.id),
        )
        summaries[summary.id] = summary
        order_keys[summary.id] = _order_key(snapshot, fallback_index=index)
        if sample is not None:
            progress_samples[summary.id] = sample
    ordered = tuple(
        summaries[task_id]
        for task_id in sorted(summaries, key=order_keys.__getitem__)
    )
    return TaskView(
        ordered,
        _readonly(summaries),
        _readonly(order_keys),
        _readonly(progress_samples),
        _dashboard(ordered),
    )


def build_task_patch(
    snapshots: Sequence[TaskSnapshot],
    *,
    requested_ids: Sequence[str],
    scheduler_state: SchedulerSnapshot,
    queue_positions: Mapping[str, int],
    sampled_at: float,
    previous_samples: Mapping[str, ProgressSample],
) -> TaskViewPatch:
    del scheduler_state
    requested = tuple(dict.fromkeys(requested_ids))
    requested_set = frozenset(requested)
    replacements: dict[str, TaskSummary] = {}
    order_keys: dict[str, tuple[float, str]] = {}
    progress_samples: dict[str, ProgressSample] = {}
    returned_ids: set[str] = set()
    for index, snapshot in enumerate(snapshots):
        task_id = snapshot.task.id
        if task_id not in requested_set:
            continue
        summary, sample = _summary_from_snapshot(
            snapshot,
            queue_positions=queue_positions,
            sampled_at=sampled_at,
            previous_sample=previous_samples.get(task_id),
        )
        returned_ids.add(task_id)
        replacements[task_id] = summary
        order_keys[task_id] = _order_key(snapshot, fallback_index=index)
        if sample is not None:
            progress_samples[task_id] = sample
    return TaskViewPatch(
        _readonly(replacements),
        _readonly(order_keys),
        _readonly(progress_samples),
        frozenset(requested_set - returned_ids),
    )


def patch_task_view(previous: TaskView, patch: TaskViewPatch) -> TaskView:
    by_id = dict(previous.by_id)
    order_keys = dict(previous.order_keys)
    progress_samples = dict(previous.progress_samples)
    changed_ids = set(patch.replacements) | set(patch.removed_ids)
    for task_id in patch.removed_ids:
        by_id.pop(task_id, None)
        order_keys.pop(task_id, None)
    for task_id, replacement in patch.replacements.items():
        by_id[task_id] = replacement
        order_keys[task_id] = patch.order_keys[task_id]
    for task_id in changed_ids:
        progress_samples.pop(task_id, None)
    progress_samples.update(patch.progress_samples)
    ordered = tuple(
        by_id[task_id] for task_id in sorted(by_id, key=order_keys.__getitem__)
    )
    dashboard = _patched_dashboard(previous, patch, ordered)
    return TaskView(
        ordered,
        _readonly(by_id),
        _readonly(order_keys),
        _readonly(progress_samples),
        dashboard,
    )


def _summary_from_snapshot(
    snapshot: TaskSnapshot,
    *,
    queue_positions: Mapping[str, int],
    sampled_at: float,
    previous_sample: ProgressSample | None,
) -> tuple[TaskSummary, ProgressSample | None]:
    task = snapshot.task
    archived = task.archived_at is not None
    total_bytes = None if snapshot.unknown_size_count else snapshot.known_size
    speed, sample = _sample_speed(
        task.status if not archived else TaskStatus.COMPLETED,
        snapshot.downloaded_bytes,
        sampled_at,
        previous_sample,
    )
    remaining_seconds = None
    if total_bytes is not None and speed > 0:
        remaining_seconds = max(
            0,
            round((total_bytes - snapshot.downloaded_bytes) / speed),
        )
    error_text = task.last_error or snapshot.item_error or "—"
    summary = TaskSummary(
        task.id,
        getattr(task, "display_title", None) or task.source_title,
        task.status,
        f"{snapshot.completed_items} / {snapshot.total_items}",
        _format_bytes(snapshot.known_size)
        + (" + 未知" if snapshot.unknown_size_count else ""),
        _format_rate(speed),
        _format_duration(remaining_seconds),
        error_text,
        snapshot.completed_items,
        snapshot.total_items,
        snapshot.downloaded_bytes,
        total_bytes,
        speed,
        remaining_seconds,
        archived,
        queue_positions.get(task.id)
        if task.status is TaskStatus.QUEUED and not archived
        else None,
    )
    return summary, sample


def _sample_speed(
    status: TaskStatus,
    downloaded: int,
    sampled_at: float,
    previous: ProgressSample | None,
) -> tuple[float, ProgressSample | None]:
    if status is not TaskStatus.DOWNLOADING:
        return 0.0, None
    sample = ProgressSample(sampled_at, downloaded)
    if previous is None:
        return 0.0, sample
    elapsed = sampled_at - previous.sampled_at
    delta = downloaded - previous.downloaded_bytes
    speed = delta / elapsed if elapsed > 0 and delta > 0 else 0.0
    return speed, sample


def _order_key(snapshot: TaskSnapshot, *, fallback_index: int) -> tuple[float, str]:
    created_at = getattr(snapshot.task, "created_at", None)
    if created_at is None:
        return float(fallback_index), snapshot.task.id
    return -created_at.timestamp(), snapshot.task.id


def _dashboard(summaries: Sequence[TaskSummary]) -> TaskDashboard:
    total_speed = 0.0
    completed = 0
    remaining = 0
    for summary in summaries:
        speed, done, left = _dashboard_contribution(summary)
        total_speed += speed
        completed += done
        remaining += left
    return TaskDashboard(
        total_speed,
        completed,
        remaining,
        _current_task_id(summaries),
    )


def _patched_dashboard(
    previous: TaskView,
    patch: TaskViewPatch,
    ordered: Sequence[TaskSummary],
) -> TaskDashboard:
    speed = previous.dashboard.total_speed_bps
    completed = previous.dashboard.completed_items
    remaining = previous.dashboard.remaining_items
    changed_ids = set(patch.replacements) | set(patch.removed_ids)
    for task_id in changed_ids:
        old = previous.by_id.get(task_id)
        if old is not None:
            old_speed, old_completed, old_remaining = _dashboard_contribution(old)
            speed -= old_speed
            completed -= old_completed
            remaining -= old_remaining
        replacement = patch.replacements.get(task_id)
        if replacement is not None:
            new_speed, new_completed, new_remaining = _dashboard_contribution(replacement)
            speed += new_speed
            completed += new_completed
            remaining += new_remaining
    return TaskDashboard(
        max(0.0, speed),
        max(0, completed),
        max(0, remaining),
        _current_task_id(ordered),
    )


def _dashboard_contribution(summary: TaskSummary) -> tuple[float, int, int]:
    if summary.archived:
        return 0.0, 0, 0
    speed = summary.speed_bps if summary.status is TaskStatus.DOWNLOADING else 0.0
    remaining = max(0, summary.total_items - summary.completed_items)
    return speed, summary.completed_items, remaining


def _current_task_id(summaries: Sequence[TaskSummary]) -> str | None:
    return next(
        (
            summary.id
            for summary in summaries
            if not summary.archived
            and summary.status in {TaskStatus.DOWNLOADING, TaskStatus.WAITING_RETRY}
        ),
        None,
    )


def _format_bytes(value: int) -> str:
    amount = float(value)
    units = ("B", "KB", "MB", "GB", "TB")
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{value} B"


def _format_rate(value: float) -> str:
    return "—" if value <= 0 else f"{_format_bytes(round(value))}/s"


def _format_duration(value: int | None) -> str:
    if value is None:
        return "—"
    if value < 60:
        return f"{value} 秒"
    minutes, seconds = divmod(value, 60)
    return f"{minutes} 分 {seconds} 秒"


def _readonly[KeyT, ValueT](values: Mapping[KeyT, ValueT]) -> Mapping[KeyT, ValueT]:
    return MappingProxyType(dict(values))
