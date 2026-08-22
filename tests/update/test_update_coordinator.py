from __future__ import annotations

import asyncio
import base64
import hashlib
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from telegram_downloader.app import BackgroundUpdatePrompt
from telegram_downloader.controller import AppController
from telegram_downloader.maintenance_activity import (
    ActivityKind,
    MaintenanceBusyError,
    OperationActivityRegistry,
)
from telegram_downloader.notifications import EventKind, NotificationRoute
from telegram_downloader.paths import PortablePaths
from telegram_downloader.settings import AppSettings
from telegram_downloader.update import (
    HelperLaunchRequest,
    UpdateCoordinator,
    UpdateStartupResult,
)
from telegram_downloader.update_contract import canonical_json
from telegram_downloader.update_sources import (
    GitHubSourceUrls,
    ModelScopeSourceUrls,
    UpdateSourceId,
)


def release_documents(
    version: str,
    runtime: bytes,
    *,
    minimum_updater_version: str = "0.1.0",
):
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
        "minimumUpdaterVersion": minimum_updater_version,
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
async def test_hidden_update_check_notifies_then_defers_dialog() -> None:
    events = []
    dialog_calls = []
    prompt = BackgroundUpdatePrompt(
        window_visible=lambda: False,
        show_dialog=lambda manifest: dialog_calls.append(manifest) or True,
        publish=events.append,
    )

    accepted = await prompt(SimpleNamespace(version="0.13.0"))

    assert accepted is False
    assert dialog_calls == []
    assert events[-1].kind is EventKind.UPDATE_AVAILABLE
    assert events[-1].route is NotificationRoute.UPDATE


@pytest.mark.asyncio
async def test_visible_update_check_awaits_existing_dialog() -> None:
    async def show_dialog(_manifest) -> bool:
        await asyncio.sleep(0)
        return True

    prompt = BackgroundUpdatePrompt(
        window_visible=lambda: True,
        show_dialog=show_dialog,
        publish=lambda _event: None,
    )

    assert await prompt(SimpleNamespace(version="0.13.0")) is True


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
async def test_accepted_update_waits_for_storage_maintenance_boundary(tmp_path) -> None:
    runtime = b"runtime"
    documents, keys = release_documents("0.2.0", runtime)
    downloader = Downloader(runtime)
    launched: list[HelperLaunchRequest] = []
    activity = OperationActivityRegistry()
    token = activity.try_track_maintenance(ActivityKind.STORAGE_CLEANUP)
    assert token is not None
    coordinator = UpdateCoordinator(
        PortablePaths(tmp_path),
        "0.1.0",
        keys,
        BytesClient(documents),
        downloader,
        launched.append,
        activity=activity,
    )

    try:
        with pytest.raises(MaintenanceBusyError, match="存储维护正在收尾"):
            await coordinator.startup(lambda _manifest: True, lambda: None)
    finally:
        token.release()

    assert downloader.calls == []
    assert launched == []


@pytest.mark.asyncio
async def test_signed_older_release_is_no_update_not_invalid(tmp_path) -> None:
    runtime = b"runtime"
    documents, keys = release_documents("0.3.1", runtime)
    coordinator = UpdateCoordinator(
        PortablePaths(tmp_path),
        "0.4.2",
        keys,
        BytesClient(documents),
        Downloader(runtime),
    )

    update = await coordinator.check_for_update()
    startup = await coordinator.startup(lambda _manifest: True, lambda: None)

    assert update.manifest is None
    assert update.blocked is False
    assert set(update.available_sources) == {
        UpdateSourceId.GITHUB,
        UpdateSourceId.MODELSCOPE,
    }
    assert startup is UpdateStartupResult.NO_UPDATE


@pytest.mark.asyncio
async def test_public_source_checks_are_reused_by_update_reconciliation(
    tmp_path,
    monkeypatch,
) -> None:
    runtime = b"runtime"
    documents, keys = release_documents("0.9.0", runtime)
    coordinator = UpdateCoordinator(
        PortablePaths(tmp_path),
        "0.8.0",
        keys,
        BytesClient(documents),
        Downloader(runtime),
    )

    checks = await coordinator.check_sources()
    calls = 0

    async def recorded_checks():
        nonlocal calls
        calls += 1
        return checks

    monkeypatch.setattr(coordinator, "check_sources", recorded_checks)
    update = await coordinator.check_for_update()

    assert {item.source for item in checks} == {
        UpdateSourceId.GITHUB,
        UpdateSourceId.MODELSCOPE,
    }
    assert update.version == "0.9.0"
    assert calls == 1


@pytest.mark.asyncio
async def test_newer_release_requiring_newer_updater_is_blocked(tmp_path) -> None:
    runtime = b"runtime"
    documents, keys = release_documents(
        "0.5.0",
        runtime,
        minimum_updater_version="0.4.3",
    )
    coordinator = UpdateCoordinator(
        PortablePaths(tmp_path),
        "0.4.2",
        keys,
        BytesClient(documents),
        Downloader(runtime),
    )

    result = await coordinator.startup(lambda _manifest: True, lambda: None)

    assert result is UpdateStartupResult.BLOCKED


@pytest.mark.asyncio
async def test_controller_start_never_checks_for_updates(tmp_path) -> None:
    calls = 0

    class Coordinator:
        async def startup(self, _prompt, _shutdown):
            nonlocal calls
            calls += 1

    controller = AppController.for_test(update_coordinator=Coordinator())

    await controller.start(background=False)
    await asyncio.sleep(0)

    assert calls == 0
    await controller.shutdown()


@pytest.mark.asyncio
async def test_manual_update_check_returns_result_and_reports_no_update() -> None:
    class Coordinator:
        async def startup(self, _prompt, _shutdown):
            return UpdateStartupResult.NO_UPDATE

    controller = AppController.for_test(update_coordinator=Coordinator())

    task = controller.check_for_updates()

    assert task is not None
    assert await task is UpdateStartupResult.NO_UPDATE
    assert "最新正式版" in controller.window.message.last_message


@pytest.mark.asyncio
async def test_manual_update_success_records_utc_without_draft_settings() -> None:
    class Coordinator:
        async def startup(self, _prompt, _shutdown):
            return UpdateStartupResult.NO_UPDATE

    class RecordingSettingsStore:
        def __init__(self) -> None:
            self.current = AppSettings(concurrency=3)
            self.saved = self.current

        def load(self) -> AppSettings:
            return self.current

        def save(self, value: AppSettings) -> None:
            self.saved = value
            self.current = value

    store = RecordingSettingsStore()
    controller = AppController.for_test(
        settings_store=store,
        settings=store.current,
        update_coordinator=Coordinator(),
        utc_now=lambda: datetime(2026, 8, 23, 2, 20, tzinfo=UTC),
    )

    task = controller.check_for_updates()

    assert task is not None
    assert await task is UpdateStartupResult.NO_UPDATE
    assert store.saved.concurrency == 3
    assert (
        store.saved.last_successful_update_check_utc
        == "2026-08-23T02:20:00Z"
    )


@pytest.mark.asyncio
async def test_controller_deduplicates_manual_update_checks() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    class Coordinator:
        async def startup(self, _prompt, _shutdown):
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()

    controller = AppController.for_test(update_coordinator=Coordinator())

    first = controller.check_for_updates()
    second = controller.check_for_updates()
    await started.wait()

    assert first is second
    assert calls == 1
    release.set()
    assert first is not None
    await first
