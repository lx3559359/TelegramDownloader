from datetime import UTC, datetime
from pathlib import Path

from telegram_downloader.domain import (
    IntegrityStatus,
    ItemStatus,
    MediaItem,
    MediaKind,
    ScanFilters,
    SourceKind,
    TaskRecord,
    TaskStatus,
)
from telegram_downloader.paths import PortablePaths
from telegram_downloader.repository import TaskRepository
from telegram_downloader.storage_cleanup import (
    StorageCleanupExecutor,
    StorageCleanupPlanner,
)
from telegram_downloader.storage_inventory import StorageInventoryService
from telegram_downloader.storage_models import StorageCategory, StorageResultCode
from telegram_downloader.update_protection import UpdateProtectionProvider

NOW = datetime(2026, 8, 22, 8, tzinfo=UTC)


def build_repository(paths: PortablePaths) -> tuple[TaskRepository, dict[str, Path]]:
    repository = TaskRepository(paths.database)
    repository.initialize()
    filters = ScanFilters(NOW, NOW, frozenset({MediaKind.VIDEO}), 20)
    task = TaskRecord(
        id="task-storage",
        source_kind=SourceKind.CHANNEL_OR_GROUP,
        source_ref="storage-peer",
        source_title="资料群",
        source_url="https://t.me/storage-peer",
        filters=filters,
        status=TaskStatus.COMPLETED,
        created_at=NOW,
        updated_at=NOW,
        display_title="资料群",
    )
    definitions = (
        ("done", ItemStatus.COMPLETED, IntegrityStatus.VERIFIED),
        ("paused", ItemStatus.PAUSED, IntegrityStatus.VERIFIED),
        ("retry", ItemStatus.WAITING_RETRY, IntegrityStatus.VERIFIED),
        ("repair", ItemStatus.COMPLETED, IntegrityStatus.HASH_MISMATCH),
        ("verified", ItemStatus.COMPLETED, IntegrityStatus.VERIFIED),
        ("gone", ItemStatus.COMPLETED, IntegrityStatus.VERIFIED),
    )
    targets: dict[str, Path] = {}
    items: list[MediaItem] = []
    for index, (name, status, integrity) in enumerate(definitions, start=1):
        target = paths.downloads / f"{name}.mp4"
        target.write_bytes(b"formal")
        targets[name] = target
        items.append(
            MediaItem(
                id=f"item-{name}",
                task_id=task.id,
                peer_ref=task.source_ref,
                message_id=index,
                grouped_id=None,
                media_id=f"media-{name}",
                media_kind=MediaKind.VIDEO,
                original_name=target.name,
                target_path=target,
                expected_size=len(b"formal"),
                message_date_utc=NOW,
                downloaded_bytes=len(b"formal"),
                status=status,
                integrity_status=integrity,
            )
        )
    repository.create_task(task, items)
    return repository, targets


def test_download_leftovers_require_completed_verified_repository_match(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = PortablePaths(tmp_path)
    paths.ensure_layout()
    repository, targets = build_repository(paths)
    leftovers = (
        paths.downloads / "done.mp4.part",
        paths.downloads / "done.mp4.part.corrupt",
        paths.downloads / "paused.mp4.part",
        paths.downloads / "retry.mp4.part",
        paths.downloads / "repair.mp4.corrupt",
        paths.downloads / "verified.mp4.corrupt.2",
        paths.downloads / "gone.mp4.part",
        paths.downloads / "unknown.bin.part",
    )
    targets["gone"].unlink()
    for path in leftovers:
        path.write_bytes(b"leftover")

    calls: list[tuple[Path, ...]] = []
    original_query = repository.maintenance_media_by_targets

    def counted_query(target_paths):
        calls.append(tuple(target_paths))
        return original_query(target_paths)

    monkeypatch.setattr(repository, "maintenance_media_by_targets", counted_query)

    inventory = StorageInventoryService(paths, repository).scan_download_candidates(
        NOW,
        active_paths=frozenset(),
    )

    by_name = {entry.relative_path.name: entry for entry in inventory.entries}
    assert by_name["done.mp4.part"].selectable is True
    assert by_name["done.mp4.part.corrupt"].selectable is True
    assert by_name["paused.mp4.part"].selectable is False
    assert by_name["retry.mp4.part"].reason is StorageResultCode.PROTECTED_BY_TASK
    assert by_name["repair.mp4.corrupt"].selectable is False
    assert by_name["verified.mp4.corrupt.2"].selectable is True
    assert by_name["gone.mp4.part"].selectable is False
    assert by_name["unknown.bin.part"].selectable is False
    assert "verified.mp4" not in by_name
    assert by_name["done.mp4.part"].task_id == "task-storage"
    assert by_name["done.mp4.part"].display_name == "资料群"
    assert len(calls) == 1
    assert set(calls[0]) == {
        *(path.resolve() for path in targets.values()),
        (paths.downloads / "unknown.bin").resolve(),
    }
    assert tuple(summary.category for summary in inventory.summaries) == (
        StorageCategory.DOWNLOAD_PART,
        StorageCategory.CORRUPT_ARCHIVE,
    )


def test_download_leftover_is_protected_while_path_is_active(tmp_path: Path) -> None:
    paths = PortablePaths(tmp_path)
    paths.ensure_layout()
    repository, _targets = build_repository(paths)
    candidate = paths.downloads / "done.mp4.part"
    candidate.write_bytes(b"leftover")

    inventory = StorageInventoryService(paths, repository).scan_download_candidates(
        NOW,
        active_paths=frozenset({candidate}),
    )

    assert len(inventory.entries) == 1
    assert inventory.entries[0].selectable is False
    assert inventory.entries[0].reason is StorageResultCode.PROTECTED_BY_TASK


def test_manual_cleanup_rechecks_real_repository_state_before_delete(
    tmp_path: Path,
) -> None:
    paths = PortablePaths(tmp_path)
    paths.ensure_layout()
    repository, _targets = build_repository(paths)
    candidate = paths.downloads / "done.mp4.part"
    candidate.write_bytes(b"leftover")
    inventory = StorageInventoryService(paths, repository).scan_download_candidates(
        NOW,
        active_paths=frozenset(),
    )
    entry = inventory.entries[0]
    plan = StorageCleanupPlanner().manual_download(inventory, [entry.id], NOW)
    repository.update_item_progress(
        "item-done",
        len(b"formal"),
        ItemStatus.WAITING_RETRY,
    )

    result = StorageCleanupExecutor(
        paths,
        repository,
        UpdateProtectionProvider(paths),
        utc_clock=lambda: NOW,
    ).execute(plan)

    assert candidate.exists()
    assert result.items[0].code is StorageResultCode.PROTECTED_BY_TASK
