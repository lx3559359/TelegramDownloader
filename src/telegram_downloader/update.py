from __future__ import annotations

import asyncio
import inspect
import os
import shutil
import subprocess
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from telegram_downloader.paths import PortablePaths
from telegram_downloader.update_contract import (
    LATEST_MAX_BYTES,
    MANIFEST_MAX_BYTES,
    SIGNATURE_MAX_BYTES,
    ReleaseManifest,
    UpdateContractError,
    UpdatePolicyError,
    parse_latest_pointer,
    parse_version,
    verify_asset,
    verify_manifest,
)
from telegram_downloader.update_sources import (
    GitHubSourceUrls,
    ModelScopeSourceUrls,
    ReconciledUpdate,
    SourceCheck,
    SourceReconciliationError,
    SourceStatus,
    UpdateSourceId,
    reconcile_sources,
)


class BytesClient(Protocol):
    async def get(self, url: str, maximum: int) -> bytes: ...


class RuntimeDownloader(Protocol):
    def download(
        self,
        urls: tuple[str, ...],
        destination: Path,
        expected_size: int,
        expected_sha256: str,
    ) -> Path: ...


class UpdateStartupResult(StrEnum):
    NO_UPDATE = "no-update"
    BLOCKED = "blocked"
    DECLINED = "declined"
    LAUNCHED = "launched"


@dataclass(frozen=True, slots=True)
class HelperLaunchRequest:
    helper: Path
    root: Path
    package: Path
    version: str
    parent_pid: int


class HttpBytesClient:
    def __init__(self, timeout: float = 15.0) -> None:
        self.timeout = timeout

    async def get(self, url: str, maximum: int) -> bytes:
        return await asyncio.to_thread(self._get_sync, url, maximum)

    def _get_sync(self, url: str, maximum: int) -> bytes:
        request = Request(url, headers={"User-Agent": "TelegramDownloader-Updater/1"})
        with urlopen(request, timeout=self.timeout) as response:
            if urlparse(response.geturl()).scheme != "https":
                raise OSError("insecure redirect")
            content = response.read(maximum + 1)
        if len(content) > maximum:
            raise UpdateContractError("更新源文档超过大小限制")
        return content


class UpdateCoordinator:
    def __init__(
        self,
        paths: PortablePaths,
        current_version: str,
        trusted_keys: Mapping[str, Ed25519PublicKey],
        client: BytesClient,
        downloader: RuntimeDownloader,
        helper_launcher: Callable[[HelperLaunchRequest], None] | None = None,
        *,
        github: GitHubSourceUrls | None = None,
        modelscope: ModelScopeSourceUrls | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.paths = paths
        self.current_version = current_version
        self.trusted_keys = trusted_keys
        self.client = client
        self.downloader = downloader
        self.helper_launcher = helper_launcher or launch_update_helper
        self.github = github or GitHubSourceUrls("lx3559359", "TelegramDownloader")
        self.modelscope = modelscope or ModelScopeSourceUrls("lx3559359/TelegramDownloader")
        self.clock = clock

    async def check_for_update(self) -> ReconciledUpdate:
        checks = await self.check_sources()
        update = reconcile_sources(checks, self.current_version)
        if (
            update.manifest is not None
            and parse_version(self.current_version)
            < parse_version(update.manifest.minimum_updater_version)
        ):
            raise UpdatePolicyError("当前更新器版本过低，无法安全应用该更新")
        return update

    async def check_sources(self) -> tuple[SourceCheck, SourceCheck]:
        github, modelscope = await asyncio.gather(
            self._check_source(UpdateSourceId.GITHUB),
            self._check_source(UpdateSourceId.MODELSCOPE),
        )
        return github, modelscope

    async def startup(
        self,
        prompt: Callable[[ReleaseManifest], bool | Awaitable[bool]],
        request_shutdown: Callable[[], None],
    ) -> UpdateStartupResult:
        try:
            update = await self.check_for_update()
        except (SourceReconciliationError, UpdateContractError):
            return UpdateStartupResult.BLOCKED
        if update.manifest is None:
            return UpdateStartupResult.BLOCKED if update.blocked else UpdateStartupResult.NO_UPDATE

        decision = prompt(update.manifest)
        accepted = await decision if inspect.isawaitable(decision) else decision
        if not accepted:
            return UpdateStartupResult.DECLINED

        asset = update.manifest.runtime
        url_by_source = {
            UpdateSourceId.GITHUB: asset.urls.github,
            UpdateSourceId.MODELSCOPE: asset.urls.modelscope,
        }
        urls = tuple(url_by_source[source] for source in update.available_sources)
        if not urls:
            return UpdateStartupResult.BLOCKED
        package = self.paths.guard(self.paths.update_staging / asset.name)
        await asyncio.to_thread(
            self.downloader.download,
            urls,
            package,
            asset.size,
            asset.sha256,
        )
        verify_asset(package, asset.size, asset.sha256)
        helper = self._stage_helper(update.manifest.version)
        request = HelperLaunchRequest(
            helper,
            self.paths.root,
            package,
            update.manifest.version,
            os.getpid(),
        )
        self.helper_launcher(request)
        request_shutdown()
        return UpdateStartupResult.LAUNCHED

    async def _check_source(self, source: UpdateSourceId) -> SourceCheck:
        started = self.clock()
        urls = self.github if source is UpdateSourceId.GITHUB else self.modelscope
        try:
            latest_bytes = await self.client.get(urls.latest(), LATEST_MAX_BYTES)
            latest = parse_latest_pointer(latest_bytes)
            manifest_bytes, signature = await asyncio.gather(
                self.client.get(urls.manifest(latest.version), MANIFEST_MAX_BYTES),
                self.client.get(urls.signature(latest.version), SIGNATURE_MAX_BYTES),
            )
            verified = verify_manifest(
                manifest_bytes,
                signature,
                self.trusted_keys,
            )
            if verified.manifest.version != latest.version:
                raise UpdateContractError("版本指针与签名清单不一致")
            return SourceCheck(
                source,
                SourceStatus.VALID,
                max(0.0, (self.clock() - started) * 1000),
                verified,
            )
        except (OSError, TimeoutError, ConnectionError) as exc:
            return SourceCheck(
                source,
                SourceStatus.UNAVAILABLE,
                max(0.0, (self.clock() - started) * 1000),
                error=type(exc).__name__,
            )
        except (UpdateContractError, ValueError) as exc:
            return SourceCheck(
                source,
                SourceStatus.INVALID,
                max(0.0, (self.clock() - started) * 1000),
                error=type(exc).__name__,
            )

    def _stage_helper(self, version: str) -> Path:
        source = self.paths.guard(self.paths.root / "UpdateHelper.exe")
        if not source.is_file():
            raise FileNotFoundError("安装目录中缺少 UpdateHelper.exe")
        directory = self.paths.guard(self.paths.update_helper / version)
        directory.mkdir(parents=True, exist_ok=True)
        target = self.paths.guard(directory / "UpdateHelper.exe")
        temporary = self.paths.guard(directory / "UpdateHelper.exe.tmp")
        with source.open("rb") as input_stream, temporary.open("wb") as output_stream:
            shutil.copyfileobj(input_stream, output_stream, length=1024 * 1024)
            output_stream.flush()
            os.fsync(output_stream.fileno())
        os.replace(temporary, target)
        return target


def launch_update_helper(request: HelperLaunchRequest) -> None:
    creation_flags = 0
    if os.name == "nt":
        creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    subprocess.Popen(
        [
            str(request.helper),
            "--root",
            str(request.root),
            "--package",
            str(request.package),
            "--version",
            request.version,
            "--parent-pid",
            str(request.parent_pid),
        ],
        cwd=str(request.root),
        close_fds=True,
        creationflags=creation_flags,
    )
