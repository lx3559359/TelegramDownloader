import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from telegram_downloader.paths import PortablePaths
from telegram_downloader.storage_inventory import StorageInventoryService
from telegram_downloader.storage_models import StorageCategory, StoragePolicy
from telegram_downloader.update_contract import canonical_json
from telegram_downloader.update_protection import UpdateProtectionSnapshot

NOW = datetime(2026, 8, 22, 8, tzinfo=UTC)


def set_age(path: Path, age: timedelta) -> None:
    timestamp = (NOW - age).timestamp()
    os.utime(path, (timestamp, timestamp))


def write_file(path: Path, size: int, age: timedelta) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    set_age(path, age)
    return path


def write_runtime_backup(path: Path, version: str, age: timedelta) -> None:
    executable = path / "TelegramDownloader.exe"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_bytes(b"runtime")
    manifest = {
        "schemaVersion": 1,
        "version": version,
        "files": [
            {
                "path": "TelegramDownloader.exe",
                "size": len(b"runtime"),
                "sha256": "0" * 64,
            }
        ],
    }
    (path / "runtime-manifest.json").write_bytes(canonical_json(manifest))
    set_age(path, age)


def small_policy() -> StoragePolicy:
    return StoragePolicy(
        temp_retention_days=7,
        log_retention_days=30,
        thumbnail_limit_bytes=100,
        thumbnail_target_bytes=90,
        update_staging_retention_days=7,
        update_backup_keep_count=1,
    )


def test_automatic_inventory_applies_fixed_category_boundaries(tmp_path: Path) -> None:
    paths = PortablePaths(tmp_path)
    paths.ensure_layout()
    old_thumb = write_file(paths.thumbnail_cache / "a.thumb", 60, timedelta(days=2))
    write_file(paths.thumbnail_cache / "b.thumb", 41, timedelta(days=1))
    old_temp = write_file(paths.temp / "old.tmp", 10, timedelta(days=8))
    write_file(paths.temp / "boundary.tmp", 10, timedelta(days=7))
    write_file(paths.temp / "future.tmp", 10, timedelta(days=-1))
    write_file(paths.diagnostic_temp / "report.tmp", 10, timedelta(days=90))
    old_log = write_file(paths.log.parent / "app.log.1", 10, timedelta(days=31))
    write_file(paths.log, 10, timedelta(days=90))
    write_file(paths.log.parent / "unknown.log", 10, timedelta(days=90))
    old_staging = write_file(
        paths.update_staging / "old.zip", 10, timedelta(days=8)
    )
    protected_staging = write_file(
        paths.update_staging / "active.zip", 10, timedelta(days=8)
    )
    old_backup = paths.update_backup / "0.13.0-to-0.14.0-aaaaaaaa"
    recent_backup = paths.update_backup / "0.14.0-to-0.15.0-bbbbbbbb"
    write_runtime_backup(old_backup, "0.13.0", timedelta(days=2))
    write_runtime_backup(recent_backup, "0.14.0", timedelta(days=1))
    write_file(paths.downloads / "never.part", 10, timedelta(days=90))
    snapshot = UpdateProtectionSnapshot(
        frozenset({protected_staging.resolve()}), False
    )

    inventory = StorageInventoryService(
        paths,
        repository=None,
        policy=small_policy(),
    ).scan_automatic(NOW, snapshot, active_paths=frozenset())

    selected = {
        paths.root / Path(entry.relative_path.as_posix())
        for entry in inventory.entries
        if entry.selectable
    }
    assert old_thumb in selected
    assert old_temp in selected
    assert old_log in selected
    assert old_staging in selected
    assert protected_staging not in selected
    assert paths.log not in selected
    assert paths.diagnostic_temp / "report.tmp" not in selected
    assert paths.downloads / "never.part" not in selected
    assert any(path.is_relative_to(old_backup) for path in selected)
    assert not any(path.is_relative_to(recent_backup) for path in selected)
    assert tuple(summary.category for summary in inventory.summaries) == (
        StorageCategory.THUMBNAILS,
        StorageCategory.TEMP,
        StorageCategory.ROTATED_LOGS,
        StorageCategory.UPDATE_STAGING,
        StorageCategory.UPDATE_BACKUP,
    )
    assert inventory.disk_free_bytes >= 0


def test_thumbnail_selection_is_deterministic_for_equal_mtime(tmp_path: Path) -> None:
    paths = PortablePaths(tmp_path)
    paths.ensure_layout()
    first = write_file(paths.thumbnail_cache / "a.thumb", 40, timedelta(days=1))
    write_file(paths.thumbnail_cache / "b.thumb", 40, timedelta(days=1))
    write_file(paths.thumbnail_cache / "c.thumb", 40, timedelta(days=1))

    inventory = StorageInventoryService(
        paths, repository=None, policy=small_policy()
    ).scan_automatic(
        NOW,
        UpdateProtectionSnapshot(frozenset(), False),
        active_paths=frozenset(),
    )

    selected = [
        paths.root / Path(entry.relative_path.as_posix())
        for entry in inventory.entries
        if entry.category is StorageCategory.THUMBNAILS and entry.selectable
    ]
    assert selected == [first]


def test_thumbnail_inventory_includes_all_ordinary_files(tmp_path: Path) -> None:
    paths = PortablePaths(tmp_path)
    paths.ensure_layout()
    orphan = write_file(
        paths.thumbnail_cache / "orphan.cache", 101, timedelta(days=1)
    )

    inventory = StorageInventoryService(
        paths, repository=None, policy=small_policy()
    ).scan_automatic(
        NOW,
        UpdateProtectionSnapshot(frozenset(), False),
        active_paths=frozenset(),
    )

    selected = {
        paths.root / Path(entry.relative_path.as_posix())
        for entry in inventory.entries
        if entry.selectable
    }
    assert orphan in selected


def test_symlink_is_reported_unsafe_and_not_followed(tmp_path: Path) -> None:
    paths = PortablePaths(tmp_path)
    paths.ensure_layout()
    outside = write_file(tmp_path / "outside.bin", 20, timedelta(days=90))
    link = paths.temp / "linked.tmp"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("Windows symlink privilege is unavailable")

    inventory = StorageInventoryService(
        paths, repository=None, policy=small_policy()
    ).scan_automatic(
        NOW,
        UpdateProtectionSnapshot(frozenset(), False),
        active_paths=frozenset(),
    )

    entry = next(item for item in inventory.entries if item.relative_path.name == link.name)
    assert entry.selectable is False
    assert outside.exists()


def test_linked_backup_root_outside_app_is_not_followed(tmp_path: Path) -> None:
    paths = PortablePaths(tmp_path / "app")
    paths.ensure_layout()
    outside = write_file(
        tmp_path / "outside" / "private.bin", 20, timedelta(days=90)
    )
    paths.update_backup.rmdir()
    try:
        paths.update_backup.symlink_to(outside.parent, target_is_directory=True)
    except OSError:
        pytest.skip("Windows symlink privilege is unavailable")

    inventory = StorageInventoryService(
        paths, repository=None, policy=small_policy()
    ).scan_automatic(
        NOW,
        UpdateProtectionSnapshot(frozenset(), False),
        active_paths=frozenset(),
    )

    backup = next(
        summary
        for summary in inventory.summaries
        if summary.category is StorageCategory.UPDATE_BACKUP
    )
    assert backup.total_count == 0
    assert outside.exists()
