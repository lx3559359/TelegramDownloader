from __future__ import annotations

import asyncio
import os
import shutil
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from telegram_downloader.domain import ItemStatus, MediaItem
from telegram_downloader.gateway import TelegramGateway
from telegram_downloader.paths import PortablePaths


class DownloadPaused(RuntimeError):
    pass


class InsufficientSpaceError(RuntimeError):
    pass


class SizeMismatchError(RuntimeError):
    pass


class ProgressWriter(Protocol):
    def update_item_progress(
        self,
        item_id: str,
        downloaded_bytes: int,
        status: ItemStatus,
        error: str | None = None,
        retry_count: int | None = None,
    ) -> None: ...


class MediaDownloader:
    _DISK_RECHECK_BYTES = 16 * 1024 * 1024

    def __init__(
        self,
        gateway: TelegramGateway,
        repository: ProgressWriter,
        paths: PortablePaths,
        free_bytes: Callable[[Path], int] | None = None,
        reserve_bytes: int = 512 * 1024 * 1024,
        progress_interval: float = 0.5,
    ) -> None:
        if reserve_bytes < 0 or progress_interval < 0:
            raise ValueError("磁盘预留和进度间隔不能为负数")
        self.gateway = gateway
        self.repository = repository
        self.paths = paths
        self.free_bytes = free_bytes or (lambda path: shutil.disk_usage(path).free)
        self.reserve_bytes = reserve_bytes
        self.progress_interval = progress_interval

    async def download(
        self,
        item: MediaItem,
        should_pause: Callable[[], bool] | None = None,
    ) -> Path:
        pause_requested = should_pause or (lambda: False)
        target = self.paths.guard(item.target_path)
        target.parent.mkdir(parents=True, exist_ok=True)

        if target.exists():
            actual_size = target.stat().st_size
            if item.expected_size is None or actual_size == item.expected_size:
                self.repository.update_item_progress(
                    item.id,
                    actual_size,
                    ItemStatus.COMPLETED,
                )
                return target
            raise SizeMismatchError(
                f"目标文件已存在但大小不符: 期望 {item.expected_size}，实际 {actual_size}"
            )

        part = self.paths.guard(target.with_suffix(target.suffix + ".part"))
        offset = part.stat().st_size if part.exists() else 0
        if item.expected_size is not None and offset > item.expected_size:
            corrupt = self._next_corrupt_path(part)
            os.replace(part, corrupt)
            offset = 0

        try:
            self._ensure_space(target.parent, item.expected_size, offset)
        except InsufficientSpaceError:
            self.repository.update_item_progress(
                item.id,
                offset,
                ItemStatus.PAUSED,
            )
            raise
        if pause_requested():
            self.repository.update_item_progress(item.id, offset, ItemStatus.PAUSED)
            raise DownloadPaused("下载已暂停")

        self.repository.update_item_progress(item.id, offset, ItemStatus.DOWNLOADING)
        downloaded = offset
        bytes_since_disk_check = 0
        last_progress = time.monotonic()

        try:
            with part.open("ab") as stream:
                async for chunk in self.gateway.stream_media(
                    item.peer_ref,
                    item.message_id,
                    offset,
                ):
                    stream.write(chunk)
                    stream.flush()
                    downloaded += len(chunk)
                    bytes_since_disk_check += len(chunk)
                    if item.expected_size is not None and downloaded > item.expected_size:
                        raise SizeMismatchError(
                            "下载数据超过预期大小: "
                            f"期望 {item.expected_size}，实际至少 {downloaded}"
                        )

                    now = time.monotonic()
                    if self.progress_interval == 0 or now - last_progress >= self.progress_interval:
                        self.repository.update_item_progress(
                            item.id,
                            downloaded,
                            ItemStatus.DOWNLOADING,
                        )
                        last_progress = now

                    if bytes_since_disk_check >= self._DISK_RECHECK_BYTES:
                        self._ensure_space(target.parent, item.expected_size, downloaded)
                        bytes_since_disk_check %= self._DISK_RECHECK_BYTES

                    if pause_requested():
                        os.fsync(stream.fileno())
                        self.repository.update_item_progress(
                            item.id,
                            downloaded,
                            ItemStatus.PAUSED,
                        )
                        raise DownloadPaused("下载已暂停")
                stream.flush()
                os.fsync(stream.fileno())
        except asyncio.CancelledError:
            self.repository.update_item_progress(
                item.id,
                downloaded,
                ItemStatus.PAUSED,
            )
            raise
        except InsufficientSpaceError:
            self.repository.update_item_progress(
                item.id,
                downloaded,
                ItemStatus.PAUSED,
            )
            raise

        if item.expected_size is not None and downloaded != item.expected_size:
            raise SizeMismatchError(
                f"下载大小不符: 期望 {item.expected_size}，实际 {downloaded}"
            )
        os.replace(part, target)
        self.repository.update_item_progress(
            item.id,
            downloaded,
            ItemStatus.COMPLETED,
        )
        return target

    def _ensure_space(
        self,
        location: Path,
        expected_size: int | None,
        downloaded: int,
    ) -> None:
        reserve = self.reserve_bytes
        remaining = 0
        if expected_size is not None:
            remaining = max(0, expected_size - downloaded)
            reserve = max(reserve, int(expected_size * 0.05))
        required = remaining + reserve
        available = self.free_bytes(location)
        if available < required:
            raise InsufficientSpaceError(
                f"磁盘空间不足: 还需至少 {required} 字节，当前可用 {available} 字节"
            )

    def _next_corrupt_path(self, part: Path) -> Path:
        candidate = self.paths.guard(part.with_suffix(part.suffix + ".corrupt"))
        sequence = 2
        while candidate.exists():
            candidate = self.paths.guard(
                part.with_suffix(part.suffix + f".corrupt.{sequence}")
            )
            sequence += 1
        return candidate
