from pathlib import Path

import pytest

from telegram_downloader.paths import PathOutsideRootError, PortablePaths


def test_ensure_layout_creates_every_managed_directory(tmp_path: Path) -> None:
    paths = PortablePaths(tmp_path)
    paths.ensure_layout()

    assert paths.settings == tmp_path / "data" / "config" / "settings.json"
    assert paths.secrets == tmp_path / "data" / "config" / "secrets.dat"
    assert paths.database == tmp_path / "data" / "database" / "tasks.sqlite3"
    assert paths.catalog_database == tmp_path / "data" / "database" / "catalog.sqlite3"
    assert paths.log == tmp_path / "data" / "logs" / "app.log"
    assert paths.update_journal == tmp_path / "data" / "update" / "journal.json"
    assert paths.cache.is_dir()
    assert paths.thumbnail_cache == tmp_path / "data" / "cache" / "thumbnails"
    assert paths.thumbnail_cache.is_dir()
    assert paths.temp.is_dir()
    assert paths.diagnostics == tmp_path / "data" / "diagnostics"
    assert paths.diagnostics.is_dir()
    assert paths.diagnostic_temp == tmp_path / "data" / "temp" / "diagnostics"
    assert paths.diagnostic_temp.is_dir()
    assert paths.maintenance == tmp_path / "data" / "maintenance"
    assert paths.maintenance.is_dir()
    assert paths.storage_maintenance_state == (
        tmp_path / "data" / "maintenance" / "storage-state.json"
    )
    assert paths.update_staging.is_dir()
    assert paths.update_backup.is_dir()
    assert paths.update_helper.is_dir()
    assert paths.downloads.is_dir()


def test_guard_accepts_child_and_rejects_parent_escape(tmp_path: Path) -> None:
    paths = PortablePaths(tmp_path)
    assert paths.guard(tmp_path / "downloads" / "ok.bin") == (
        tmp_path / "downloads" / "ok.bin"
    ).resolve()
    with pytest.raises(PathOutsideRootError):
        paths.guard(tmp_path / ".." / "outside.bin")


def test_guard_rejects_relative_path_and_root_itself(tmp_path: Path) -> None:
    paths = PortablePaths(tmp_path)

    with pytest.raises(PathOutsideRootError):
        paths.guard(Path("relative.bin"))
    with pytest.raises(PathOutsideRootError):
        paths.guard(tmp_path)
