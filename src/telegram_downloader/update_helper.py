from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4
from zipfile import BadZipFile, ZipFile, ZipInfo

from telegram_downloader.paths import PortablePaths
from telegram_downloader.update_contract import canonical_json, parse_version

_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_MAX_INVENTORY_BYTES = 4 * 1024 * 1024
_MAX_FILES = 50_000
_MAX_TOTAL_BYTES = 16 * 1024 * 1024 * 1024
_WINDOWS_RESERVED = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}


class RuntimePackageError(ValueError):
    pass


class UpdateTransactionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RuntimeFile:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class RuntimeInventory:
    version: str
    files: tuple[RuntimeFile, ...]


class UpdateTransaction:
    def __init__(
        self,
        paths: PortablePaths,
        *,
        process_waiter: Callable[[int, float], None] | None = None,
        health_runner: Callable[[Path, Path, float], bool] | None = None,
        app_launcher: Callable[[Path], None] | None = None,
        fault: Callable[[str], None] | None = None,
    ) -> None:
        self.paths = paths
        self.process_waiter = process_waiter or wait_for_process_exit
        self.health_runner = health_runner or run_health_check
        self.app_launcher = app_launcher or launch_application
        self.fault = fault or (lambda _stage: None)

    def apply(
        self,
        package: Path,
        version: str,
        *,
        parent_pid: int,
        wait_timeout: float = 120.0,
        health_timeout: float = 30.0,
    ) -> None:
        package = self.paths.guard(package.resolve())
        parse_version(version)
        inventory = read_runtime_package(package, version)
        current = read_installed_inventory(self.paths.root)
        self._assert_no_unmanaged_collisions(current, inventory)
        self.process_waiter(parent_pid, wait_timeout)

        transaction_id = uuid4().hex
        extraction = self.paths.guard(self.paths.update_staging / f"extracted-{transaction_id}")
        backup = self.paths.guard(
            self.paths.update_backup
            / f"{current.version}-to-{inventory.version}-{transaction_id[:8]}"
        )
        extract_runtime_package(package, extraction, inventory)
        backup.mkdir(parents=True, exist_ok=False)
        old_files = [item.path for item in current.files] + ["runtime-manifest.json"]
        new_files = [item.path for item in inventory.files] + ["runtime-manifest.json"]
        journal: dict[str, Any] = {
            "schemaVersion": 1,
            "transactionId": transaction_id,
            "state": "prepared",
            "oldVersion": current.version,
            "targetVersion": inventory.version,
            "backup": str(backup.relative_to(self.paths.root)).replace("\\", "/"),
            "extraction": str(extraction.relative_to(self.paths.root)).replace("\\", "/"),
            "oldFiles": old_files,
            "newFiles": new_files,
            "backedUp": [],
            "installed": [],
        }
        self._write_journal(journal)

        try:
            for relative in old_files:
                source = self._runtime_target(relative)
                if not source.exists():
                    continue
                target = self.paths.guard(backup / Path(relative))
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(source, target)
                journal["backedUp"].append(relative)
                journal["state"] = "backing-up"
                self._write_journal(journal)
            self.fault("backed-up")

            for relative in new_files:
                source = self.paths.guard(extraction / Path(relative))
                target = self._runtime_target(relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(source, target)
                journal["installed"].append(relative)
                journal["state"] = "installing"
                self._write_journal(journal)
            self.fault("installed")

            confirmation = self.paths.guard(
                self.paths.update_staging / f"health-{transaction_id}.ok"
            )
            confirmation.unlink(missing_ok=True)
            executable = self._runtime_target("TelegramDownloader.exe")
            healthy = self.health_runner(executable, confirmation, health_timeout)
            if not healthy or not confirmation.is_file():
                raise UpdateTransactionError("新版本健康检查失败")

            journal["state"] = "committed"
            self._write_journal(journal)
            self._record_result("committed", current.version, inventory.version)
            self.paths.update_journal.unlink(missing_ok=True)
            self._remove_tree(extraction)
            confirmation.unlink(missing_ok=True)
            self.app_launcher(executable)
        except Exception as exc:
            self._rollback(journal)
            old_executable = self._runtime_target("TelegramDownloader.exe")
            if old_executable.exists():
                self.app_launcher(old_executable)
            self._record_result("rolled-back", current.version, inventory.version)
            if isinstance(exc, UpdateTransactionError):
                raise
            raise UpdateTransactionError(f"更新替换失败（{type(exc).__name__}）") from exc

    def recover_interrupted(self) -> bool:
        if not self.paths.update_journal.exists():
            return False
        journal = self._read_journal()
        if journal["state"] == "committed":
            self.paths.update_journal.unlink(missing_ok=True)
            return False
        self._rollback(journal)
        executable = self._runtime_target("TelegramDownloader.exe")
        if executable.exists():
            self.app_launcher(executable)
        self._record_result(
            "recovered-rollback",
            str(journal["oldVersion"]),
            str(journal["targetVersion"]),
        )
        return True

    def _rollback(self, journal: dict[str, Any]) -> None:
        for relative in reversed(journal["installed"]):
            target = self._runtime_target(relative)
            if target.is_file():
                target.unlink()
        backup = self.paths.guard(self.paths.root / Path(journal["backup"]))
        for relative in reversed(journal["backedUp"]):
            source = self.paths.guard(backup / Path(relative))
            target = self._runtime_target(relative)
            if source.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(source, target)
        extraction = self.paths.guard(self.paths.root / Path(journal["extraction"]))
        self._remove_tree(extraction)
        self._remove_tree(backup)
        self.paths.update_journal.unlink(missing_ok=True)

    def _assert_no_unmanaged_collisions(
        self,
        current: RuntimeInventory,
        incoming: RuntimeInventory,
    ) -> None:
        managed = {item.path.casefold() for item in current.files}
        managed.add("runtime-manifest.json")
        for item in incoming.files:
            target = self._runtime_target(item.path)
            if target.exists() and item.path.casefold() not in managed:
                raise RuntimePackageError(f"更新会覆盖未受管文件: {item.path}")

    def _runtime_target(self, relative: str) -> Path:
        _validate_relative_path(relative)
        return self.paths.guard(self.paths.root / Path(relative))

    def _write_journal(self, value: dict[str, Any]) -> None:
        _atomic_write(self.paths.update_journal, canonical_json(value))

    def _read_journal(self) -> dict[str, Any]:
        return load_update_journal(self.paths)

    def _record_result(self, status: str, old_version: str, target_version: str) -> None:
        result = self.paths.guard(self.paths.update / "result.json")
        _atomic_write(
            result,
            canonical_json(
                {
                    "oldVersion": old_version,
                    "status": status,
                    "targetVersion": target_version,
                }
            ),
        )

    def _remove_tree(self, path: Path) -> None:
        guarded = self.paths.guard(path)
        if guarded.exists():
            shutil.rmtree(guarded)


def load_update_journal(paths: PortablePaths) -> dict[str, Any]:
    try:
        value = json.loads(
            paths.update_journal.read_text(encoding="utf-8"),
            object_pairs_hook=_pairs_without_duplicates,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, RuntimePackageError) as exc:
        raise UpdateTransactionError("更新恢复日志损坏") from exc
    expected = {
        "schemaVersion",
        "transactionId",
        "state",
        "oldVersion",
        "targetVersion",
        "backup",
        "extraction",
        "oldFiles",
        "newFiles",
        "backedUp",
        "installed",
    }
    if (
        not isinstance(value, dict)
        or set(value) != expected
        or not isinstance(value["schemaVersion"], int)
        or isinstance(value["schemaVersion"], bool)
        or value["schemaVersion"] != 1
    ):
        raise UpdateTransactionError("更新恢复日志格式无效")
    transaction_id = value["transactionId"]
    if not isinstance(transaction_id, str) or re.fullmatch(
        r"[a-f0-9]{32}", transaction_id
    ) is None:
        raise UpdateTransactionError("更新恢复日志事务标识无效")
    try:
        parse_version(value["oldVersion"])
        parse_version(value["targetVersion"])
        for key in ("oldFiles", "newFiles", "backedUp", "installed"):
            files = value[key]
            if not isinstance(files, list) or len(files) != len(set(files)):
                raise UpdateTransactionError("更新恢复日志文件列表无效")
            for relative in files:
                _validate_relative_path(relative)
        for key in ("backup", "extraction"):
            _validate_update_relative_path(value[key])
    except (RuntimePackageError, TypeError, ValueError) as exc:
        raise UpdateTransactionError("更新恢复日志格式无效") from exc
    if value["state"] not in {"prepared", "backing-up", "installing", "committed"}:
        raise UpdateTransactionError("更新恢复日志状态无效")
    return value


def read_installed_inventory(root: Path) -> RuntimeInventory:
    path = root / "runtime-manifest.json"
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise RuntimePackageError("当前运行时缺少受管文件清单") from exc
    return _parse_inventory(content)


def read_runtime_package(package: Path, expected_version: str) -> RuntimeInventory:
    try:
        with ZipFile(package) as archive:
            info = archive.getinfo("runtime-manifest.json")
            if info.file_size > _MAX_INVENTORY_BYTES:
                raise RuntimePackageError("运行时清单过大")
            inventory = _parse_inventory(archive.read(info))
            if inventory.version != expected_version:
                raise RuntimePackageError("运行时包版本与更新清单不一致")
            _validate_archive(archive, inventory)
            return inventory
    except (BadZipFile, KeyError, OSError) as exc:
        raise RuntimePackageError("运行时 ZIP 无效") from exc


def extract_runtime_package(
    package: Path,
    destination: Path,
    inventory: RuntimeInventory,
) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    try:
        with ZipFile(package) as archive:
            for item in (*inventory.files, RuntimeFile("runtime-manifest.json", 0, "")):
                info = archive.getinfo(item.path)
                target = destination / Path(item.path)
                target.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                size = 0
                with archive.open(info) as source, target.open("wb") as output:
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        output.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                if item.path != "runtime-manifest.json" and (
                    size != item.size or digest.hexdigest() != item.sha256
                ):
                    raise RuntimePackageError(f"运行时文件校验失败: {item.path}")
    except Exception:
        if destination.exists():
            shutil.rmtree(destination)
        raise


def _parse_inventory(content: bytes) -> RuntimeInventory:
    if not content or len(content) > _MAX_INVENTORY_BYTES or content.startswith(b"\xef\xbb\xbf"):
        raise RuntimePackageError("运行时清单大小或编码无效")
    try:
        value = json.loads(content.decode("utf-8"), object_pairs_hook=_pairs_without_duplicates)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimePackageError("运行时清单不是有效 JSON") from exc
    if canonical_json(value) != content:
        raise RuntimePackageError("运行时清单不是规范 JSON")
    if not isinstance(value, dict) or set(value) != {"schemaVersion", "version", "files"}:
        raise RuntimePackageError("运行时清单字段无效")
    if value["schemaVersion"] != 1:
        raise RuntimePackageError("运行时清单协议不受支持")
    try:
        parse_version(value["version"])
    except (TypeError, ValueError) as exc:
        raise RuntimePackageError("运行时版本无效") from exc
    if not isinstance(value["files"], list) or not 1 <= len(value["files"]) <= _MAX_FILES:
        raise RuntimePackageError("运行时文件列表无效")
    files: list[RuntimeFile] = []
    seen: set[str] = set()
    total = 0
    for raw in value["files"]:
        if not isinstance(raw, dict) or set(raw) != {"path", "size", "sha256"}:
            raise RuntimePackageError("运行时文件字段无效")
        _validate_relative_path(raw["path"])
        folded = raw["path"].casefold()
        if folded in seen or folded == "runtime-manifest.json":
            raise RuntimePackageError("运行时文件路径重复")
        seen.add(folded)
        size = raw["size"]
        digest = raw["sha256"]
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise RuntimePackageError("运行时文件大小无效")
        if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
            raise RuntimePackageError("运行时文件哈希无效")
        total += size
        if total > _MAX_TOTAL_BYTES:
            raise RuntimePackageError("运行时包展开后过大")
        files.append(RuntimeFile(raw["path"], size, digest))
    if "telegramdownloader.exe" not in seen:
        raise RuntimePackageError("运行时包缺少主程序")
    return RuntimeInventory(value["version"], tuple(files))


def _validate_archive(archive: ZipFile, inventory: RuntimeInventory) -> None:
    expected = {item.path for item in inventory.files} | {"runtime-manifest.json"}
    actual: set[str] = set()
    for info in archive.infolist():
        if info.is_dir():
            continue
        _validate_relative_path(info.filename)
        if _is_symlink(info):
            raise RuntimePackageError("运行时 ZIP 不允许符号链接")
        if info.filename in actual:
            raise RuntimePackageError("运行时 ZIP 包含重复文件")
        actual.add(info.filename)
    if actual != expected:
        raise RuntimePackageError("运行时 ZIP 与受管文件清单不一致")
    by_name = {item.path: item for item in inventory.files}
    for name, item in by_name.items():
        if archive.getinfo(name).file_size != item.size:
            raise RuntimePackageError(f"运行时 ZIP 文件大小不一致: {name}")


def _validate_relative_path(value: object) -> None:
    if not isinstance(value, str) or not value or len(value) > 260 or "\\" in value:
        raise RuntimePackageError("运行时文件路径无效")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise RuntimePackageError("运行时文件路径越界")
    if path.parts[0].casefold() in {"data", "downloads"}:
        raise RuntimePackageError("运行时包不得写入用户数据目录")
    for part in path.parts:
        stem = part.rstrip(" .").split(".", 1)[0].casefold()
        if part != part.rstrip(" .") or ":" in part or stem in _WINDOWS_RESERVED:
            raise RuntimePackageError("运行时文件路径不兼容 Windows")


def _validate_update_relative_path(value: object) -> None:
    if not isinstance(value, str) or not value or len(value) > 260 or "\\" in value:
        raise UpdateTransactionError("更新恢复路径无效")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise UpdateTransactionError("更新恢复路径越界")
    for part in path.parts:
        stem = part.rstrip(" .").split(".", 1)[0].casefold()
        if part != part.rstrip(" .") or ":" in part or stem in _WINDOWS_RESERVED:
            raise UpdateTransactionError("更新恢复路径不兼容 Windows")
    if tuple(part.casefold() for part in path.parts[:2]) != ("data", "update"):
        raise UpdateTransactionError("更新恢复路径不在 data/update 下")


def _pairs_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RuntimePackageError("JSON 包含重复字段")
        result[key] = value
    return result


def _is_symlink(info: ZipInfo) -> bool:
    return (info.external_attr >> 16) & 0o170000 == 0o120000


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def wait_for_process_exit(pid: int, timeout: float) -> None:
    if pid <= 0:
        return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.1)
    raise UpdateTransactionError("等待旧程序退出超时")


def run_health_check(executable: Path, confirmation: Path, timeout: float) -> bool:
    process = subprocess.Popen(
        [str(executable), "--update-health-check", str(confirmation)],
        cwd=str(executable.parent),
        close_fds=True,
    )
    try:
        return process.wait(timeout=timeout) == 0 and confirmation.is_file()
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
        return False


def launch_application(executable: Path) -> None:
    try:
        subprocess.Popen([str(executable)], cwd=str(executable.parent), close_fds=True)
    except OSError:
        return


def helper_main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="UpdateHelper")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--parent-pid", type=int, required=True)
    arguments = parser.parse_args(argv)
    paths = PortablePaths(arguments.root)
    paths.ensure_layout()
    try:
        transaction = UpdateTransaction(paths)
        if transaction.recover_interrupted():
            return 0
        transaction.apply(
            arguments.package,
            arguments.version,
            parent_pid=arguments.parent_pid,
        )
    except (RuntimePackageError, UpdateTransactionError, OSError, ValueError):
        return 1
    return 0
