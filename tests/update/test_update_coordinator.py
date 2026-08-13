from __future__ import annotations

import asyncio
import base64
import hashlib

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from telegram_downloader.controller import AppController
from telegram_downloader.paths import PortablePaths
from telegram_downloader.update import (
    HelperLaunchRequest,
    UpdateCoordinator,
    UpdateStartupResult,
)
from telegram_downloader.update_contract import canonical_json
from telegram_downloader.update_sources import GitHubSourceUrls, ModelScopeSourceUrls


def release_documents(version: str, runtime: bytes):
    github = GitHubSourceUrls("lx3559359", "TelegramDownloader")
    modelscope = ModelScopeSourceUrls("lx3559359/TelegramDownloader")
    runtime_name = f"TelegramDownloader-{version}-win-x64-portable.zip"
    installer_name = f"TelegramDownloader-{version}-win-x64-setup.exe"
    value = {
        "schemaVersion": 1,
        "channel": "stable",
        "platform": "windows",
        "architecture": "x64",
        "version": version,
        "publishedAt": "2026-08-13T12:00:00Z",
        "minimumUpdaterVersion": "0.1.0",
        "keyId": "test",
        "releaseNotes": "更新说明",
        "assets": {
            "runtime": {
                "name": runtime_name,
                "size": len(runtime),
                "sha256": hashlib.sha256(runtime).hexdigest(),
                "urls": {
                    "github": github.asset(version, runtime_name),
                    "modelscope": modelscope.asset(version, runtime_name),
                },
            },
            "installer": {
                "name": installer_name,
                "size": 1,
                "sha256": hashlib.sha256(b"i").hexdigest(),
                "urls": {
                    "github": github.asset(version, installer_name),
                    "modelscope": modelscope.asset(version, installer_name),
                },
            },
        },
    }
    private = Ed25519PrivateKey.generate()
    manifest = canonical_json(value)
    signature = base64.b64encode(private.sign(manifest)) + b"\n"
    latest = canonical_json({"schemaVersion": 1, "channel": "stable", "version": version})
    documents = {
        github.latest(): latest,
        github.manifest(version): manifest,
        github.signature(version): signature,
        modelscope.latest(): latest,
        modelscope.manifest(version): manifest,
        modelscope.signature(version): signature,
    }
    return documents, {"test": private.public_key()}


class BytesClient:
    def __init__(self, documents):
        self.documents = documents

    async def get(self, url: str, maximum: int) -> bytes:
        value = self.documents[url]
        assert len(value) <= maximum
        return value


class Downloader:
    def __init__(self, content: bytes):
        self.content = content
        self.calls = []

    def download(self, urls, destination, size, digest):
        self.calls.append((urls, destination, size, digest))
        destination.write_bytes(self.content)
        return destination


@pytest.mark.asyncio
async def test_declined_update_does_not_download_or_launch(tmp_path) -> None:
    runtime = b"runtime"
    documents, keys = release_documents("0.2.0", runtime)
    downloader = Downloader(runtime)
    launched: list[HelperLaunchRequest] = []
    coordinator = UpdateCoordinator(
        PortablePaths(tmp_path),
        "0.1.0",
        keys,
        BytesClient(documents),
        downloader,
        launched.append,
    )

    result = await coordinator.startup(lambda _manifest: False, lambda: None)

    assert result is UpdateStartupResult.DECLINED
    assert downloader.calls == []
    assert launched == []


@pytest.mark.asyncio
async def test_accepted_update_downloads_then_launches_project_local_helper(tmp_path) -> None:
    paths = PortablePaths(tmp_path)
    paths.ensure_layout()
    (tmp_path / "UpdateHelper.exe").write_bytes(b"helper")
    runtime = b"runtime"
    documents, keys = release_documents("0.2.0", runtime)
    downloader = Downloader(runtime)
    launched: list[HelperLaunchRequest] = []
    shutdown = []
    coordinator = UpdateCoordinator(
        paths,
        "0.1.0",
        keys,
        BytesClient(documents),
        downloader,
        launched.append,
    )

    result = await coordinator.startup(lambda _manifest: True, lambda: shutdown.append(True))

    assert result is UpdateStartupResult.LAUNCHED
    assert launched[0].helper.is_relative_to(tmp_path)
    assert launched[0].package.is_relative_to(tmp_path)
    assert launched[0].parent_pid > 0
    assert shutdown == [True]


@pytest.mark.asyncio
async def test_controller_starts_update_check_without_blocking_login(tmp_path) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    class Coordinator:
        async def startup(self, prompt, shutdown):
            started.set()
            await release.wait()

    controller = AppController.for_test(update_coordinator=Coordinator())

    await asyncio.wait_for(controller.start(), timeout=0.2)
    await asyncio.wait_for(started.wait(), timeout=0.2)
    assert controller.login_dialog is not None
    release.set()
    await controller.shutdown()
