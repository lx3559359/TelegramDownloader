from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from telegram_downloader.update_contract import AssetVerificationError, verify_asset


class UpdateDownloadError(RuntimeError):
    pass


class InsufficientUpdateSpaceError(UpdateDownloadError):
    pass


@dataclass(slots=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    stream: BinaryIO

    def close(self) -> None:
        self.stream.close()


class UpdateTransport(Protocol):
    def open(self, url: str, start: int) -> HttpResponse: ...


class UrllibUpdateTransport:
    def __init__(self, timeout: float = 30.0) -> None:
        self.timeout = timeout

    def open(self, url: str, start: int) -> HttpResponse:
        headers = {"User-Agent": "TelegramDownloader-Updater/1"}
        if start:
            headers["Range"] = f"bytes={start}-"
        response = urlopen(Request(url, headers=headers), timeout=self.timeout)
        final = urlparse(response.geturl())
        if final.scheme != "https":
            response.close()
            raise UpdateDownloadError("更新下载被重定向到非 HTTPS 地址")
        return HttpResponse(response.status, dict(response.headers.items()), response)


class ResumableUpdateDownloader:
    def __init__(
        self,
        transport: UpdateTransport | None = None,
        *,
        reserve_bytes: int = 256 * 1024 * 1024,
        free_bytes: Callable[[Path], int] | None = None,
        chunk_size: int = 1024 * 1024,
    ) -> None:
        if reserve_bytes < 0 or chunk_size <= 0:
            raise ValueError("更新下载参数无效")
        self.transport = transport or UrllibUpdateTransport()
        self.reserve_bytes = reserve_bytes
        self.free_bytes = free_bytes or (lambda path: shutil.disk_usage(path).free)
        self.chunk_size = chunk_size

    def download(
        self,
        urls: Sequence[str],
        destination: Path,
        expected_size: int,
        expected_sha256: str,
        progress: Callable[[int, int], None] | None = None,
    ) -> Path:
        if not destination.is_absolute() or not urls:
            raise UpdateDownloadError("更新下载路径或来源无效")
        if expected_size <= 0 or len(expected_sha256) != 64:
            raise UpdateDownloadError("更新资产校验参数无效")
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_suffix(destination.suffix + ".part")
        metadata = destination.with_suffix(destination.suffix + ".part.json")
        self._prepare_partial(partial, metadata, expected_size, expected_sha256)

        if destination.exists():
            try:
                verify_asset(destination, expected_size, expected_sha256)
                return destination
            except AssetVerificationError:
                destination.unlink(missing_ok=True)

        failures: list[str] = []
        for url in urls:
            try:
                self._download_from(
                    url,
                    partial,
                    expected_size,
                    expected_sha256,
                    progress,
                )
                verify_asset(partial, expected_size, expected_sha256)
                os.replace(partial, destination)
                metadata.unlink(missing_ok=True)
                return destination
            except AssetVerificationError:
                failures.append("asset-verification")
                partial.unlink(missing_ok=True)
                metadata.unlink(missing_ok=True)
                self._write_metadata(metadata, expected_size, expected_sha256)
            except InsufficientUpdateSpaceError:
                raise
            except (OSError, TimeoutError, UpdateDownloadError) as exc:
                failures.append(type(exc).__name__)
        raise UpdateDownloadError(
            "所有更新来源均下载失败" + (f"（{', '.join(failures)}）" if failures else "")
        )

    def _download_from(
        self,
        url: str,
        partial: Path,
        expected_size: int,
        expected_sha256: str,
        progress: Callable[[int, int], None] | None,
    ) -> None:
        del expected_sha256
        existing = partial.stat().st_size if partial.exists() else 0
        if existing > expected_size:
            partial.unlink()
            existing = 0
        remaining = expected_size - existing
        if self.free_bytes(partial.parent) < remaining + self.reserve_bytes:
            raise InsufficientUpdateSpaceError("项目所在磁盘空间不足，无法安全下载更新")

        response = self.transport.open(url, existing)
        try:
            if response.status == 200:
                mode = "wb"
                existing = 0
            elif response.status == 206 and existing:
                content_range = response.headers.get("Content-Range", "")
                if not content_range.startswith(f"bytes {existing}-"):
                    raise UpdateDownloadError("更新服务器返回了无效的续传范围")
                mode = "ab"
            else:
                raise UpdateDownloadError(f"更新服务器返回 HTTP {response.status}")

            downloaded = existing
            with partial.open(mode) as stream:
                while True:
                    chunk = response.stream.read(self.chunk_size)
                    if not chunk:
                        break
                    downloaded += len(chunk)
                    if downloaded > expected_size:
                        raise UpdateDownloadError("更新服务器返回的数据超过清单大小")
                    stream.write(chunk)
                    if progress is not None:
                        progress(downloaded, expected_size)
                stream.flush()
                os.fsync(stream.fileno())
            if downloaded != expected_size:
                raise UpdateDownloadError("更新下载尚未完整")
        finally:
            response.close()

    @staticmethod
    def _prepare_partial(
        partial: Path,
        metadata: Path,
        expected_size: int,
        expected_sha256: str,
    ) -> None:
        expected = {"sha256": expected_sha256, "size": expected_size}
        if metadata.exists():
            try:
                actual = json.loads(metadata.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                actual = None
            if actual != expected:
                partial.unlink(missing_ok=True)
        ResumableUpdateDownloader._write_metadata(metadata, expected_size, expected_sha256)

    @staticmethod
    def _write_metadata(metadata: Path, expected_size: int, expected_sha256: str) -> None:
        content = (
            json.dumps(
                {"sha256": expected_sha256, "size": expected_size},
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode()
        temporary = metadata.with_suffix(metadata.suffix + ".tmp")
        with temporary.open("wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, metadata)
