from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from typing import Protocol

from telegram_downloader.domain import IntegrityStatus, ItemStatus, MediaItem
from telegram_downloader.paths import PortablePaths

_VERIFYABLE_FAILURES = frozenset(
    {
        IntegrityStatus.MISSING,
        IntegrityStatus.SIZE_MISMATCH,
        IntegrityStatus.HASH_MISMATCH,
        IntegrityStatus.READ_ERROR,
    }
)


class IntegrityRepository(Protocol):
    def get_item(self, item_id: str) -> MediaItem: ...

    def record_integrity_success(
        self,
        item_id: str,
        sha256: str,
        verified_at: datetime,
    ) -> None: ...

    def record_integrity_failure(
        self,
        item_id: str,
        status: IntegrityStatus,
        safe_error: str,
    ) -> None: ...

    def prepare_integrity_repair(self, item_id: str) -> MediaItem: ...


@dataclass(frozen=True, slots=True)
class IntegrityProgress:
    completed: int
    total: int
    item_id: str
    file_name: str
    status: IntegrityStatus


@dataclass(frozen=True, slots=True)
class IntegritySummary:
    verified: int = 0
    baselined: int = 0
    missing: int = 0
    size_mismatch: int = 0
    hash_mismatch: int = 0
    read_error: int = 0
    skipped: int = 0
    cancelled: int = 0


@dataclass(frozen=True, slots=True)
class RepairPreparation:
    accepted_ids: tuple[str, ...] = ()
    skipped: int = 0


class IntegrityRepairError(RuntimeError):
    pass


class _HashCancelled(RuntimeError):
    pass


def _hash_file(path: Path, cancelled: Event) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            if cancelled.is_set():
                raise _HashCancelled
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    if cancelled.is_set():
        raise _HashCancelled
    return digest.hexdigest()


class FileIntegrityService:
    def __init__(
        self,
        repository: IntegrityRepository,
        paths: PortablePaths,
    ) -> None:
        self.repository = repository
        self.paths = paths

    async def verify(
        self,
        item_ids: list[str],
        *,
        progress: Callable[[IntegrityProgress], None] | None = None,
        cancelled: Event | None = None,
    ) -> IntegritySummary:
        ordered_ids = tuple(dict.fromkeys(item_ids))
        total = len(ordered_ids)
        cancel_event = cancelled or Event()
        summary = IntegritySummary()

        for index, item_id in enumerate(ordered_ids):
            if cancel_event.is_set():
                return replace(summary, cancelled=summary.cancelled + total - index)
            item = self.repository.get_item(item_id)
            if not self._is_verifyable(item):
                summary = replace(summary, skipped=summary.skipped + 1)
                self._report(progress, index, total, item, item.integrity_status)
                continue

            target = self.paths.guard(Path(item.target_path))
            try:
                if not target.exists():
                    status = IntegrityStatus.MISSING
                    self.repository.record_integrity_failure(
                        item.id,
                        status,
                        "本地文件缺失",
                    )
                    summary = replace(summary, missing=summary.missing + 1)
                else:
                    actual_size = target.stat().st_size
                    if (
                        item.expected_size is not None
                        and actual_size != item.expected_size
                    ):
                        status = IntegrityStatus.SIZE_MISMATCH
                        self.repository.record_integrity_failure(
                            item.id,
                            status,
                            "本地文件大小不一致",
                        )
                        summary = replace(
                            summary,
                            size_mismatch=summary.size_mismatch + 1,
                        )
                    else:
                        sha256 = await asyncio.to_thread(
                            _hash_file,
                            target,
                            cancel_event,
                        )
                        if (
                            item.content_sha256 is not None
                            and sha256 != item.content_sha256
                        ):
                            status = IntegrityStatus.HASH_MISMATCH
                            self.repository.record_integrity_failure(
                                item.id,
                                status,
                                "本地文件哈希不一致",
                            )
                            summary = replace(
                                summary,
                                hash_mismatch=summary.hash_mismatch + 1,
                            )
                        else:
                            status = IntegrityStatus.VERIFIED
                            self.repository.record_integrity_success(
                                item.id,
                                sha256,
                                datetime.now(UTC),
                            )
                            if item.content_sha256 is None:
                                summary = replace(
                                    summary,
                                    baselined=summary.baselined + 1,
                                )
                            else:
                                summary = replace(
                                    summary,
                                    verified=summary.verified + 1,
                                )
            except _HashCancelled:
                return replace(summary, cancelled=summary.cancelled + total - index)
            except FileNotFoundError:
                status = IntegrityStatus.MISSING
                self.repository.record_integrity_failure(
                    item.id,
                    status,
                    "本地文件缺失",
                )
                summary = replace(summary, missing=summary.missing + 1)
            except OSError:
                status = IntegrityStatus.READ_ERROR
                self.repository.record_integrity_failure(
                    item.id,
                    status,
                    "无法读取本地文件",
                )
                summary = replace(summary, read_error=summary.read_error + 1)

            self._report(progress, index, total, item, status)
        return summary

    def prepare_repairs(self, item_ids: list[str]) -> RepairPreparation:
        accepted: list[str] = []
        skipped = 0
        for item_id in dict.fromkeys(item_ids):
            item = self.repository.get_item(item_id)
            if (
                item.status is not ItemStatus.FAILED
                or item.integrity_status not in _VERIFYABLE_FAILURES
            ):
                skipped += 1
                continue
            target = self.paths.guard(Path(item.target_path))
            part = self.paths.guard(target.with_suffix(target.suffix + ".part"))
            moves: list[tuple[Path, Path]] = []
            try:
                for source in (target, part):
                    if not source.exists():
                        continue
                    quarantine = self._next_corrupt_path(source)
                    os.replace(source, quarantine)
                    moves.append((quarantine, source))
            except OSError:
                self._restore_moves(moves)
                skipped += 1
                continue

            try:
                self.repository.prepare_integrity_repair(item.id)
            except Exception:
                self._restore_moves(moves)
                raise
            accepted.append(item.id)
        return RepairPreparation(tuple(accepted), skipped)

    @staticmethod
    def _is_verifyable(item: MediaItem) -> bool:
        return (
            item.status is ItemStatus.COMPLETED
            or item.integrity_status in _VERIFYABLE_FAILURES
        )

    @staticmethod
    def _report(
        callback: Callable[[IntegrityProgress], None] | None,
        index: int,
        total: int,
        item: MediaItem,
        status: IntegrityStatus,
    ) -> None:
        if callback is not None:
            callback(
                IntegrityProgress(
                    index + 1,
                    total,
                    item.id,
                    item.original_name,
                    status,
                )
            )

    def _next_corrupt_path(self, source: Path) -> Path:
        candidate = self.paths.guard(source.with_suffix(source.suffix + ".corrupt"))
        sequence = 2
        while candidate.exists():
            candidate = self.paths.guard(
                source.with_suffix(source.suffix + f".corrupt.{sequence}")
            )
            sequence += 1
        return candidate

    @staticmethod
    def _restore_moves(moves: list[tuple[Path, Path]]) -> None:
        try:
            for quarantine, original in reversed(moves):
                os.replace(quarantine, original)
        except OSError as error:
            raise IntegrityRepairError("无法恢复完整性修复留档") from error
