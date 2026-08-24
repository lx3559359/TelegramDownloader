from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from telegram_downloader.domain import ItemStatus, MediaItem
from telegram_downloader.download_io import (
    BatchSubmit,
    BufferedPartWriter,
    submit_batch,
)
from telegram_downloader.download_paths import DownloadPathPolicy
from telegram_downloader.download_persistence import (
    DownloadPersistence,
    ThreadedDownloadPersistence,
)
from telegram_downloader.gateway import TelegramGateway
from telegram_downloader.paths import PortablePaths
from telegram_downloader.repository import ItemProgressUpdate
from telegram_downloader.resource_control import AsyncBandwidthLimiter


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

    def complete_item(
        self,
        item_id: str,
        downloaded_bytes: int,
        sha256: str,
        verified_at: datetime,
    ) -> None: ...


class _Digest(Protocol):
    def update(self, data: bytes) -> None: ...

    def hexdigest(self) -> str: ...


def _hash_file(path: Path) -> _Digest:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest


async def persist_writer(writer: BufferedPartWriter) -> int:
    operation = asyncio.create_task(writer.flush(durable=True))
    try:
        return await asyncio.shield(operation)
    except asyncio.CancelledError:
        await operation
        raise


class MediaDownloader:
    _DISK_RECHECK_BYTES = 16 * 1024 * 1024
    _PAUSE_POLL_SECONDS = 0.05

    def __init__(
        self,
        gateway: TelegramGateway,
        repository: ProgressWriter,
        paths: PortablePaths,
        free_bytes: Callable[[Path], int] | None = None,
        reserve_bytes: int = 512 * 1024 * 1024,
        progress_interval: float = 0.5,
        bandwidth: AsyncBandwidthLimiter | None = None,
        write_batch_bytes: int = 1024 * 1024,
        write_batch_interval: float = 0.5,
        batch_submit: BatchSubmit | None = None,
        download_paths: DownloadPathPolicy | PortablePaths | None = None,
        persistence: DownloadPersistence | None = None,
    ) -> None:
        if reserve_bytes < 0 or progress_interval < 0:
            raise ValueError("磁盘预留和进度间隔不能为负数")
        if write_batch_bytes <= 0 or write_batch_interval <= 0:
            raise ValueError("写入批次大小和间隔必须大于零")
        self.gateway = gateway
        self.repository = repository
        self.paths = paths
        self.download_paths = download_paths or paths
        self.free_bytes = free_bytes or (lambda path: shutil.disk_usage(path).free)
        self.reserve_bytes = reserve_bytes
        self.progress_interval = progress_interval
        self.bandwidth = bandwidth or AsyncBandwidthLimiter()
        self.write_batch_bytes = write_batch_bytes
        self.write_batch_interval = write_batch_interval
        self.batch_submit = batch_submit or submit_batch
        self.persistence = persistence or ThreadedDownloadPersistence(repository)

    async def download(
        self,
        item: MediaItem,
        should_pause: Callable[[], bool] | None = None,
    ) -> Path:
        pause_requested = should_pause or (lambda: False)
        target = self.download_paths.guard(item.target_path)
        target.parent.mkdir(parents=True, exist_ok=True)

        if target.exists():
            actual_size = target.stat().st_size
            if item.expected_size is None or actual_size == item.expected_size:
                digest = await asyncio.to_thread(_hash_file, target)
                await self._complete_item(
                    item.id,
                    actual_size,
                    digest.hexdigest(),
                    datetime.now(UTC),
                )
                return target
            raise SizeMismatchError(
                f"目标文件已存在但大小不符: 期望 {item.expected_size}，实际 {actual_size}"
            )

        part = self.download_paths.guard(target.with_suffix(target.suffix + ".part"))
        offset = part.stat().st_size if part.exists() else 0
        if item.expected_size is not None and offset > item.expected_size:
            corrupt = self._next_corrupt_path(part)
            os.replace(part, corrupt)
            offset = 0

        try:
            self._ensure_space(target.parent, item.expected_size, offset)
        except InsufficientSpaceError:
            await self._persist_progress(
                item.id,
                offset,
                ItemStatus.PAUSED,
            )
            raise
        if pause_requested():
            await self._persist_progress(item.id, offset, ItemStatus.PAUSED)
            raise DownloadPaused("下载已暂停")

        await self._persist_progress(item.id, offset, ItemStatus.DOWNLOADING)
        bytes_since_disk_check = 0
        last_progress = time.monotonic()
        writer: BufferedPartWriter | None = None

        try:
            digest = (
                await asyncio.to_thread(_hash_file, part)
                if offset
                else hashlib.sha256()
            )
            writer = BufferedPartWriter(
                part,
                digest,
                offset=offset,
                batch_bytes=self.write_batch_bytes,
                batch_interval=self.write_batch_interval,
                submit=self.batch_submit,
            )
            async for chunk in self.gateway.stream_media(
                item.peer_ref,
                item.message_id,
                offset,
            ):
                bandwidth_ready = await self._acquire_bandwidth(
                    len(chunk),
                    pause_requested,
                )
                if not bandwidth_ready or pause_requested():
                    durable_offset = await persist_writer(writer)
                    await self._persist_progress(
                        item.id,
                        durable_offset,
                        ItemStatus.PAUSED,
                    )
                    raise DownloadPaused("下载已暂停")
                await writer.append(bytes(chunk))
                bytes_since_disk_check += len(chunk)
                if (
                    item.expected_size is not None
                    and writer.received_bytes > item.expected_size
                ):
                    raise SizeMismatchError(
                        "下载数据超过预期大小: "
                        f"期望 {item.expected_size}，实际至少 {writer.received_bytes}"
                    )

                now = time.monotonic()
                if (
                    self.progress_interval == 0
                    or now - last_progress >= self.progress_interval
                ):
                    await self.persistence.record_progress(
                        ItemProgressUpdate(
                            item.id,
                            writer.persisted_bytes,
                            ItemStatus.DOWNLOADING,
                        )
                    )
                    last_progress = now

                if bytes_since_disk_check >= self._DISK_RECHECK_BYTES:
                    self._ensure_space(
                        target.parent,
                        item.expected_size,
                        writer.received_bytes,
                    )
                    bytes_since_disk_check %= self._DISK_RECHECK_BYTES

                if pause_requested():
                    durable_offset = await persist_writer(writer)
                    await self._persist_progress(
                        item.id,
                        durable_offset,
                        ItemStatus.PAUSED,
                    )
                    raise DownloadPaused("下载已暂停")
            await writer.flush(durable=True)
        except asyncio.CancelledError:
            durable_offset = (
                await persist_writer(writer) if writer is not None else offset
            )
            await self._persist_progress(
                item.id,
                durable_offset,
                ItemStatus.PAUSED,
            )
            raise
        except InsufficientSpaceError:
            durable_offset = (
                await persist_writer(writer) if writer is not None else offset
            )
            await self._persist_progress(
                item.id,
                durable_offset,
                ItemStatus.PAUSED,
            )
            raise

        if writer is None:
            raise RuntimeError("下载写入器未初始化")
        if (
            item.expected_size is not None
            and writer.persisted_bytes != item.expected_size
        ):
            raise SizeMismatchError(
                "下载大小不符: "
                f"期望 {item.expected_size}，实际 {writer.persisted_bytes}"
            )
        os.replace(part, target)
        await self._complete_item(
            item.id,
            writer.persisted_bytes,
            writer.hexdigest(),
            datetime.now(UTC),
        )
        return target

    async def _persist_progress(
        self,
        item_id: str,
        downloaded_bytes: int,
        status: ItemStatus,
        error: str | None = None,
        retry_count: int | None = None,
    ) -> None:
        await self.persistence.execute(
            lambda: self.repository.update_item_progress(
                item_id,
                downloaded_bytes,
                status,
                error,
                retry_count,
            ),
            flush_item_ids=(item_id,),
        )

    async def _complete_item(
        self,
        item_id: str,
        downloaded_bytes: int,
        sha256: str,
        verified_at: datetime,
    ) -> None:
        await self.persistence.execute(
            lambda: self.repository.complete_item(
                item_id,
                downloaded_bytes,
                sha256,
                verified_at,
            ),
            flush_item_ids=(item_id,),
        )

    async def _acquire_bandwidth(
        self,
        byte_count: int,
        pause_requested: Callable[[], bool],
    ) -> bool:
        operation = asyncio.ensure_future(self.bandwidth.acquire(byte_count))
        try:
            while not operation.done():
                if pause_requested():
                    return False
                await asyncio.wait(
                    (operation,),
                    timeout=self._PAUSE_POLL_SECONDS,
                )
            await operation
            return not pause_requested()
        finally:
            if not operation.done():
                operation.cancel()
                await asyncio.gather(operation, return_exceptions=True)

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
        candidate = self.download_paths.guard(part.with_suffix(part.suffix + ".corrupt"))
        sequence = 2
        while candidate.exists():
            candidate = self.download_paths.guard(
                part.with_suffix(part.suffix + f".corrupt.{sequence}")
            )
            sequence += 1
        return candidate
