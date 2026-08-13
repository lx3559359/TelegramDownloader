from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from telegram_downloader.domain import (
    MediaItem,
    ParsedLink,
    ScanFilters,
    TaskRecord,
    TaskStatus,
)
from telegram_downloader.files import archive_target, disambiguate_target
from telegram_downloader.gateway import RemoteMedia, TelegramGateway


class EmptyScanError(ValueError):
    pass


class TaskWriter(Protocol):
    def create_task(self, task: TaskRecord, items: list[MediaItem]) -> None: ...


@dataclass(frozen=True, slots=True)
class ScanPreview:
    task: TaskRecord
    items: tuple[MediaItem, ...]
    known_bytes: int
    unknown_size_count: int


class TaskPlanner:
    def __init__(
        self,
        gateway: TelegramGateway,
        repository: TaskWriter,
        downloads: Path,
        uuid_factory: Callable[[], str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.gateway = gateway
        self.repository = repository
        self.downloads = downloads.resolve()
        self.uuid_factory = uuid_factory or (lambda: str(uuid4()))
        self.clock = clock or (lambda: datetime.now(UTC))

    async def scan(self, source: ParsedLink, filters: ScanFilters) -> ScanPreview:
        task_id = self.uuid_factory()
        now = self.clock()
        remote = self._deduplicate(
            [item async for item in self.gateway.scan(source, filters)]
        )
        if not remote:
            raise EmptyScanError("筛选范围内没有找到可下载媒体")

        task = TaskRecord(
            task_id,
            source.kind,
            source.entity_ref,
            remote[0].source_title,
            source.normalized_url,
            filters,
            TaskStatus.DRAFT,
            now,
            now,
        )
        planned: list[MediaItem] = []
        used: set[Path] = set()
        for item in remote:
            target = archive_target(
                self.downloads,
                item.source_title,
                item.message_date_utc,
                item.kind,
                item.original_name,
            )
            target = self._available_target(target, item.message_id, used)
            used.add(target)
            planned.append(
                MediaItem(
                    self.uuid_factory(),
                    task_id,
                    item.peer_ref,
                    item.message_id,
                    item.grouped_id,
                    item.media_id,
                    item.kind,
                    item.original_name,
                    target,
                    item.expected_size,
                    item.message_date_utc,
                )
            )

        return ScanPreview(
            task,
            tuple(planned),
            sum(item.expected_size or 0 for item in remote),
            sum(item.expected_size is None for item in remote),
        )

    def commit(self, preview: ScanPreview) -> TaskRecord:
        queued = replace(
            preview.task,
            status=TaskStatus.QUEUED,
            updated_at=self.clock(),
        )
        self.repository.create_task(queued, list(preview.items))
        return queued

    @staticmethod
    def _deduplicate(remote: list[RemoteMedia]) -> list[RemoteMedia]:
        seen: set[tuple[str, int, str]] = set()
        result: list[RemoteMedia] = []
        for item in remote:
            key = (item.peer_ref, item.message_id, item.media_id)
            if key in seen:
                continue
            seen.add(key)
            result.append(item)
        return result

    @staticmethod
    def _available_target(base: Path, message_id: int, used: set[Path]) -> Path:
        if base not in used and not base.exists():
            return base
        candidate = disambiguate_target(base, message_id)
        sequence = 2
        while candidate in used or candidate.exists():
            candidate = candidate.with_name(
                f"{disambiguate_target(base, message_id).stem}_{sequence}{base.suffix}"
            )
            sequence += 1
        return candidate
