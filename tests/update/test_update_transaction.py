from __future__ import annotations

import hashlib
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from telegram_downloader.paths import PortablePaths
from telegram_downloader.update_helper import (
    RuntimePackageError,
    UpdateTransaction,
    UpdateTransactionError,
)


def inventory(version: str, files: dict[str, bytes]) -> bytes:
    value = {
        "schemaVersion": 1,
        "version": version,
        "files": [
            {
                "path": name,
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
            for name, content in sorted(files.items())
        ],
    }
    return (
        json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode()


def write_runtime(root: Path, version: str, files: dict[str, bytes]) -> None:
    for name, content in files.items():
        target = root / Path(name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    (root / "runtime-manifest.json").write_bytes(inventory(version, files))


def build_package(path: Path, version: str, files: dict[str, bytes]) -> None:
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr("runtime-manifest.json", inventory(version, files))
        for name, content in files.items():
            archive.writestr(name, content)


def test_transaction_replaces_only_managed_files_and_preserves_user_data(tmp_path) -> None:
    paths = PortablePaths(tmp_path)
    paths.ensure_layout()
    write_runtime(tmp_path, "0.1.0", {"TelegramDownloader.exe": b"old", "_internal/lib": b"v1"})
    (paths.data / "sentinel.db").write_bytes(b"data")
    (paths.downloads / "video.mp4").write_bytes(b"download")
    (tmp_path / "user-note.txt").write_text("keep", encoding="utf-8")
    package = paths.update_staging / "runtime.zip"
    build_package(
        package,
        "0.2.0",
        {"TelegramDownloader.exe": b"new", "UpdateHelper.exe": b"helper", "_internal/lib": b"v2"},
    )
    launched: list[Path] = []
    waited: list[tuple[int, float]] = []

    transaction = UpdateTransaction(
        paths,
        process_waiter=lambda pid, timeout: waited.append((pid, timeout)),
        health_runner=lambda _exe, confirmation, _timeout: confirmation.write_text("ok") or True,
        app_launcher=launched.append,
    )
    transaction.apply(package, "0.2.0", parent_pid=42)

    assert (tmp_path / "TelegramDownloader.exe").read_bytes() == b"new"
    assert (tmp_path / "_internal/lib").read_bytes() == b"v2"
    assert (paths.data / "sentinel.db").read_bytes() == b"data"
    assert (paths.downloads / "video.mp4").read_bytes() == b"download"
    assert (tmp_path / "user-note.txt").read_text(encoding="utf-8") == "keep"
    assert launched == [tmp_path / "TelegramDownloader.exe"]
    assert waited == [(42, 120.0)]
    assert not paths.update_journal.exists()


def test_health_failure_rolls_back_and_restarts_old_runtime(tmp_path) -> None:
    paths = PortablePaths(tmp_path)
    paths.ensure_layout()
    write_runtime(tmp_path, "0.1.0", {"TelegramDownloader.exe": b"old"})
    package = paths.update_staging / "runtime.zip"
    build_package(package, "0.2.0", {"TelegramDownloader.exe": b"new", "new.dll": b"new"})
    launched: list[Path] = []
    transaction = UpdateTransaction(
        paths,
        health_runner=lambda *_args: False,
        app_launcher=launched.append,
    )

    with pytest.raises(UpdateTransactionError):
        transaction.apply(package, "0.2.0", parent_pid=0)

    assert (tmp_path / "TelegramDownloader.exe").read_bytes() == b"old"
    assert not (tmp_path / "new.dll").exists()
    assert launched == [tmp_path / "TelegramDownloader.exe"]


def test_interrupted_transaction_is_recovered_idempotently(tmp_path) -> None:
    class SimulatedCrash(BaseException):
        pass

    paths = PortablePaths(tmp_path)
    paths.ensure_layout()
    write_runtime(tmp_path, "0.1.0", {"TelegramDownloader.exe": b"old"})
    package = paths.update_staging / "runtime.zip"
    build_package(package, "0.2.0", {"TelegramDownloader.exe": b"new"})

    def crash(stage: str) -> None:
        if stage == "installed":
            raise SimulatedCrash

    with pytest.raises(SimulatedCrash):
        UpdateTransaction(paths, fault=crash).apply(package, "0.2.0", parent_pid=0)

    recovery = UpdateTransaction(paths)
    assert recovery.recover_interrupted() is True
    assert recovery.recover_interrupted() is False
    assert (tmp_path / "TelegramDownloader.exe").read_bytes() == b"old"
    assert not paths.update_journal.exists()


def test_locked_managed_file_fails_without_losing_old_runtime(tmp_path, monkeypatch) -> None:
    paths = PortablePaths(tmp_path)
    paths.ensure_layout()
    executable = tmp_path / "TelegramDownloader.exe"
    write_runtime(tmp_path, "0.1.0", {"TelegramDownloader.exe": b"old"})
    package = paths.update_staging / "runtime.zip"
    build_package(package, "0.2.0", {"TelegramDownloader.exe": b"new"})
    from telegram_downloader import update_helper

    real_replace = update_helper.os.replace

    def locked_replace(source, destination):
        if Path(source) == executable and "backup" in Path(destination).parts:
            raise PermissionError("locked")
        return real_replace(source, destination)

    monkeypatch.setattr(update_helper.os, "replace", locked_replace)

    with pytest.raises(UpdateTransactionError):
        UpdateTransaction(paths, app_launcher=lambda _path: None).apply(
            package, "0.2.0", parent_pid=0
        )

    assert executable.read_bytes() == b"old"
    assert not paths.update_journal.exists()


@pytest.mark.parametrize(
    "name",
    ["../escape.exe", "data/config.json", "downloads/file.bin", "C:/escape.exe", "x\\y.dll"],
)
def test_runtime_inventory_rejects_unsafe_paths(tmp_path, name: str) -> None:
    paths = PortablePaths(tmp_path)
    paths.ensure_layout()
    package = paths.update_staging / "runtime.zip"
    build_package(package, "0.2.0", {name: b"bad"})

    with pytest.raises(RuntimePackageError):
        UpdateTransaction(paths).apply(package, "0.2.0", parent_pid=0)
