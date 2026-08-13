from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from telegram_downloader.update_contract import canonical_json, parse_latest_pointer, parse_version

LATEST_PATH = "releases/stable/latest.json"


class ModelScopePublishError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ModelScopeStorage:
    workspace: Path
    cache: Path
    state: Path
    temp: Path


def modelscope_release_names(version: str) -> tuple[str, ...]:
    parse_version(version)
    return (
        f"TelegramDownloader-{version}-source.zip",
        f"TelegramDownloader-{version}-win-x64-portable.zip",
        f"TelegramDownloader-{version}-win-x64-setup.exe",
        "release-notes.md",
        "update-manifest.json",
        "update-manifest.sig",
    )


def configure_project_storage() -> ModelScopeStorage:
    workspace = Path(os.environ["GITHUB_WORKSPACE"])
    if not workspace.is_absolute() or not workspace.is_dir() or workspace.is_symlink():
        raise ModelScopePublishError("ModelScope workspace is invalid")
    workspace = workspace.resolve(strict=True)
    storage = ModelScopeStorage(
        workspace,
        workspace / ".local" / "cache" / "modelscope",
        workspace / ".local" / "state" / "modelscope",
        workspace / ".local" / "temp" / "modelscope",
    )
    for path in (storage.cache, storage.state, storage.temp):
        path.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            raise ModelScopePublishError("ModelScope storage path is invalid")
    os.environ.update(
        {
            "MODELSCOPE_CACHE": str(storage.cache),
            "MODELSCOPE_HOME": str(storage.state),
            "HF_HOME": str(storage.cache / "huggingface"),
            "XDG_CACHE_HOME": str(storage.cache / "xdg"),
            "XDG_CONFIG_HOME": str(storage.state / "xdg"),
            "TEMP": str(storage.temp),
            "TMP": str(storage.temp),
        }
    )
    return storage


class ModelScopePublisher:
    def __init__(
        self,
        api: Any,
        repo_id: str,
        version: str,
        source: Path,
        storage: ModelScopeStorage,
        not_exist_error: type[Exception] | tuple[type[Exception], ...] = (),
    ) -> None:
        self.api = api
        self.repo_id = repo_id
        self.version = version
        self.source = source
        self.storage = storage
        self.not_exist_error = not_exist_error

    def ensure_public_repository(self) -> None:
        if not self.api.repo_exists(self.repo_id, "model"):
            self.api.create_repo(
                self.repo_id,
                "model",
                visibility="public",
                description="TelegramDownloader Windows releases",
            )
        info = self.api.get_repo(self.repo_id, "model")
        if getattr(info, "private", False) is True or str(getattr(info, "visibility", "")) == "1":
            raise ModelScopePublishError("ModelScope repository is private")

    def stage(self) -> None:
        self.ensure_public_repository()
        files = self._validated_source()
        for path in files:
            self.api.upload_file(
                self.repo_id,
                "model",
                path,
                f"releases/stable/{self.version}/{path.name}",
                commit_message=f"Publish TelegramDownloader {self.version}: {path.name}",
                disable_tqdm=True,
            )

    def verify(self) -> None:
        expected = sorted(modelscope_release_names(self.version))
        prefix = f"releases/stable/{self.version}/"
        actual = []
        for entry in self.api.list_repo_files(self.repo_id, "model", recursive=True):
            path = getattr(entry, "path", "")
            if path.startswith(prefix) and getattr(entry, "is_dir", True) is False:
                relative = path[len(prefix) :]
                if "/" in relative or "\\" in relative:
                    raise ModelScopePublishError("ModelScope release has nested files")
                actual.append(relative)
        if sorted(actual) != expected:
            raise ModelScopePublishError("ModelScope candidate asset set is invalid")

    def save_pointer(self) -> bytes:
        try:
            return self._download_bytes(LATEST_PATH)
        except self.not_exist_error:
            return b""

    def promote(self) -> None:
        self.api.upload_file(
            self.repo_id,
            "model",
            canonical_json({"schemaVersion": 1, "channel": "stable", "version": self.version}),
            LATEST_PATH,
            commit_message=f"Promote TelegramDownloader {self.version}",
            disable_tqdm=True,
        )

    def restore(self, previous: bytes) -> None:
        if previous:
            parse_latest_pointer(previous)
            self.api.upload_file(
                self.repo_id,
                "model",
                previous,
                LATEST_PATH,
                commit_message="Restore previous TelegramDownloader stable pointer",
                disable_tqdm=True,
            )
        else:
            self.api.delete_files(self.repo_id, "model", [LATEST_PATH])

    def download(self, destination: Path) -> None:
        _empty_directory(destination, self.storage.workspace)
        self.verify()
        for name in modelscope_release_names(self.version):
            (destination / name).write_bytes(
                self._download_bytes(f"releases/stable/{self.version}/{name}")
            )

    def verify_pointer(self) -> None:
        if parse_latest_pointer(self._download_bytes(LATEST_PATH)).version != self.version:
            raise ModelScopePublishError("ModelScope latest pointer mismatch")

    def _download_bytes(self, remote_path: str) -> bytes:
        with tempfile.TemporaryDirectory(prefix="download-", dir=self.storage.temp) as temporary:
            downloaded = self.api.download_file(
                self.repo_id,
                "model",
                remote_path,
                local_dir=temporary,
                force=True,
            )
            return Path(downloaded).read_bytes()

    def _validated_source(self) -> tuple[Path, ...]:
        names = modelscope_release_names(self.version)
        actual = sorted(path.name for path in self.source.iterdir() if path.name != "latest.json")
        if actual != sorted(names):
            raise ModelScopePublishError("ModelScope source asset set is invalid")
        files = tuple(self.source / name for name in names)
        if any(not path.is_file() or path.stat().st_size <= 0 for path in files):
            raise ModelScopePublishError("ModelScope source asset is invalid")
        return files


def _empty_directory(path: Path, workspace: Path) -> None:
    resolved = path.resolve()
    try:
        resolved.relative_to(workspace.resolve(strict=True))
    except ValueError as exc:
        raise ModelScopePublishError("ModelScope download path escaped workspace") from exc
    if resolved.exists():
        shutil.rmtree(resolved)
    resolved.mkdir(parents=True)


def save_pointer_file(path: Path, content: bytes) -> None:
    value = {
        "schemaVersion": 1,
        "exists": bool(content),
        "contentBase64": base64.b64encode(content).decode("ascii"),
        "sha256": hashlib.sha256(content).hexdigest(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8"
    )


def load_pointer_file(path: Path) -> bytes:
    value = json.loads(path.read_text(encoding="utf-8"))
    content = base64.b64decode(value["contentBase64"], validate=True)
    if hashlib.sha256(content).hexdigest() != value["sha256"]:
        raise ModelScopePublishError("ModelScope pointer backup is invalid")
    return content


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "operation",
        choices=(
            "ensure-repo",
            "stage",
            "verify",
            "download",
            "save-pointer",
            "restore-pointer",
            "promote",
            "verify-pointer",
        ),
    )
    parser.add_argument("--repository", default="lx3559359/TelegramDownloader")
    parser.add_argument("--version", required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--pointer-backup", type=Path)
    arguments = parser.parse_args()
    try:
        storage = configure_project_storage()
        token = os.environ["MODELSCOPE_API_TOKEN"]
        from modelscope_hub import HubApi, NotExistError

        publisher = ModelScopePublisher(
            HubApi(token=token),
            arguments.repository,
            arguments.version,
            arguments.source.resolve(),
            storage,
            NotExistError,
        )
        if arguments.operation == "ensure-repo":
            publisher.ensure_public_repository()
        elif arguments.operation == "stage":
            publisher.stage()
        elif arguments.operation == "verify":
            publisher.verify()
        elif arguments.operation == "download":
            publisher.download(arguments.destination)
        elif arguments.operation == "save-pointer":
            save_pointer_file(arguments.pointer_backup, publisher.save_pointer())
        elif arguments.operation == "restore-pointer":
            publisher.restore(load_pointer_file(arguments.pointer_backup))
        elif arguments.operation == "promote":
            publisher.promote()
        else:
            publisher.verify_pointer()
    except Exception:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
