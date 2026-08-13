from __future__ import annotations

from pathlib import Path

import pytest

from scripts.release.publish_github import github_asset_names
from scripts.release.publish_modelscope import (
    ModelScopePublisher,
    ModelScopeStorage,
    configure_project_storage,
    modelscope_release_names,
)
from scripts.release.release_transaction import ReleaseTransactionError, publish_transaction
from scripts.release.verify_remote_release import compare_release_directories


def test_exact_release_asset_sets() -> None:
    common = {
        "TelegramDownloader-0.1.0-source.zip",
        "TelegramDownloader-0.1.0-win-x64-portable.zip",
        "TelegramDownloader-0.1.0-win-x64-setup.exe",
        "release-notes.md",
        "update-manifest.json",
        "update-manifest.sig",
    }
    assert set(modelscope_release_names("0.1.0")) == common
    assert set(github_asset_names("0.1.0")) == common | {"latest.json"}


def test_modelscope_cache_state_and_temp_are_workspace_local(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    paths = configure_project_storage()

    assert paths.workspace == tmp_path.resolve()
    for path in (paths.cache, paths.state, paths.temp):
        assert path.is_relative_to(tmp_path)


def test_modelscope_release_operations_are_pinned_to_main(tmp_path) -> None:
    class Api:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str | None]] = []

        def repo_exists(self, *_args) -> bool:
            return True

        def get_repo(self, *_args):
            return type("Repo", (), {"private": False, "visibility": "0"})()

        def upload_file(self, *_args, revision=None, **_kwargs) -> None:
            self.calls.append(("upload", revision))

        def list_repo_files(self, *_args, revision=None, **_kwargs):
            self.calls.append(("list", revision))
            return []

        def download_file(self, *_args, revision=None, **_kwargs):
            self.calls.append(("download", revision))
            raise FileNotFoundError

        def delete_files(self, *_args, revision=None, **_kwargs) -> None:
            self.calls.append(("delete", revision))

    source = tmp_path / "candidate"
    source.mkdir()
    for name in modelscope_release_names("0.1.0"):
        (source / name).write_bytes(b"asset")
    storage = ModelScopeStorage(tmp_path, tmp_path / "cache", tmp_path / "state", tmp_path / "temp")
    storage.temp.mkdir()
    api = Api()
    publisher = ModelScopePublisher(api, "lx3559359/TelegramDownloader", "0.1.0", source, storage)

    publisher.stage()
    publisher.promote()
    publisher.restore(b"")
    with pytest.raises(FileNotFoundError):
        publisher.save_pointer()

    assert api.calls
    assert all(revision == "main" for _, revision in api.calls)


class Platform:
    def __init__(self, name: str, events: list[str], fail: str | None = None):
        self.name = name
        self.events = events
        self.fail = fail
        self.previous = b"old"

    def stage(self) -> None:
        self.events.append(f"{self.name}:stage")
        if self.fail == "stage":
            raise RuntimeError("stage")

    def verify(self) -> None:
        self.events.append(f"{self.name}:verify")
        if self.fail == "verify":
            raise RuntimeError("verify")

    def save_pointer(self) -> bytes:
        self.events.append(f"{self.name}:save")
        return self.previous

    def promote(self) -> None:
        self.events.append(f"{self.name}:promote")
        if self.fail == "promote":
            raise RuntimeError("promote")

    def restore(self, previous: bytes) -> None:
        assert previous == self.previous
        self.events.append(f"{self.name}:restore")


def test_candidate_failure_advances_neither_pointer() -> None:
    events: list[str] = []
    github = Platform("github", events)
    modelscope = Platform("modelscope", events, fail="verify")

    with pytest.raises(ReleaseTransactionError):
        publish_transaction(github, modelscope)

    assert "github:promote" not in events
    assert "modelscope:promote" not in events


def test_final_publish_failure_restores_modelscope_pointer() -> None:
    events: list[str] = []
    github = Platform("github", events, fail="promote")
    modelscope = Platform("modelscope", events)

    with pytest.raises(ReleaseTransactionError):
        publish_transaction(github, modelscope)

    assert events[-2:] == ["github:promote", "modelscope:restore"]


def test_transaction_is_idempotent_when_adapters_are_idempotent() -> None:
    events: list[str] = []
    github = Platform("github", events)
    modelscope = Platform("modelscope", events)

    publish_transaction(github, modelscope)
    publish_transaction(github, modelscope)

    assert events.count("github:promote") == 2
    assert events.count("modelscope:promote") == 2


def test_remote_directory_comparison_is_byte_exact(tmp_path) -> None:
    local = tmp_path / "local"
    remote = tmp_path / "remote"
    local.mkdir()
    remote.mkdir()
    names = modelscope_release_names("0.1.0")
    for index, name in enumerate(names):
        content = f"asset-{index}".encode()
        (local / name).write_bytes(content)
        (remote / name).write_bytes(content)

    compare_release_directories(local, remote, names)
    (remote / names[0]).write_bytes(b"changed")
    with pytest.raises(ValueError):
        compare_release_directories(local, remote, names)


def test_release_script_stages_both_platforms_before_promoting() -> None:
    root = Path(__file__).parents[2]
    script = (root / "scripts" / "release" / "release.ps1").read_text(encoding="utf-8")

    github_stage = script.index("publish_github stage")
    modelscope_stage = script.index("publish_modelscope stage")
    modelscope_promote = script.index("publish_modelscope promote")
    github_promote = script.index("publish_github promote")
    assert github_stage < modelscope_promote
    assert modelscope_stage < modelscope_promote
    assert modelscope_promote < github_promote
    assert "publish_modelscope restore-pointer" in script
    assert "publish_github demote" in script
    assert "gh repo create 'lx3559359/TelegramDownloader' --public" in script
    assert "publish_modelscope ensure-repo" in script
    assert "MODELSCOPE_API_TOKEN" in script
    assert "Write-Output $env:MODELSCOPE_API_TOKEN" not in script
