from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from telegram_downloader.content import ContentSearchQuery
from telegram_downloader.domain import (
    MediaItem,
    ParsedLink,
    ScanFilters,
    SourceKind,
    TaskRecord,
    TaskStatus,
)
from telegram_downloader.files import archive_target, disambiguate_target
from telegram_downloader.gateway import RemoteMedia, TelegramGateway
from telegram_downloader.repository import AllMediaAlreadyExists


class EmptyScanError(ValueError):
    pass


class TaskWriter(Protocol):
    def create_task(self, task: TaskRecord, items: list[MediaItem]) -> None: ...

    def create_task_deduplicating(
        self,
        task: TaskRecord,
        items: list[MediaItem],
    ) -> list[MediaItem]: ...

    def existing_media_keys(
        self,
        keys: set[tuple[str, int, str]],
    ) -> set[tuple[str, int, str]]: ...


@dataclass(frozen=True, slots=True)
class ScanPreview:
    task: TaskRecord
    items: tuple[MediaItem, ...]
    known_bytes: int
    unknown_size_count: int


@dataclass(frozen=True, slots=True)
class SelectedCommit:
    task: TaskRecord
    accepted_keys: frozenset[tuple[str, int, str]]
    skipped_count: int


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
        remote = [item async for item in self.gateway.scan(source, filters)]
        source_title = remote[0].source_title if remote else source.entity_ref
        return self._build_preview(
            source_kind=source.kind,
            source_ref=source.entity_ref,
            source_title=source_title,
            source_url=source.normalized_url,
            filters=filters,
            remote=remote,
            display_title=None,
            empty_message="筛选范围内没有找到可下载媒体",
            skip_existing=False,
        )

    def plan_selected(
        self,
        source_ref: str,
        source_title: str,
        query: ContentSearchQuery,
        selected: list[RemoteMedia],
    ) -> ScanPreview:
        return self._build_preview(
            source_kind=SourceKind.CHANNEL_OR_GROUP,
            source_ref=source_ref,
            source_title=source_title,
            source_url=f"telegram://peer/{source_ref}",
            filters=query.filters,
            remote=selected,
            display_title=f"{source_title}（搜索：{query.keyword}）",
            empty_message="所选媒体已全部存在于下载队列",
            skip_existing=True,
        )

    def existing_media_keys(
        self,
        keys: set[tuple[str, int, str]],
    ) -> set[tuple[str, int, str]]:
        return self.repository.existing_media_keys(keys)

    def _build_preview(
        self,
        *,
        source_kind: SourceKind,
        source_ref: str,
        source_title: str,
        source_url: str,
        filters: ScanFilters,
        remote: list[RemoteMedia],
        display_title: str | None,
        empty_message: str,
        skip_existing: bool,
    ) -> ScanPreview:
        task_id = self.uuid_factory()
        now = self.clock()
        remote = self._deduplicate(remote)
        if skip_existing:
            keys = {
                (item.peer_ref, item.message_id, item.media_id) for item in remote
            }
            existing = self.repository.existing_media_keys(keys)
            remote = [
                item
                for item in remote
                if (item.peer_ref, item.message_id, item.media_id) not in existing
            ]
        if not remote:
            raise EmptyScanError(empty_message)

        task = TaskRecord(
            task_id,
            source_kind,
            source_ref,
            source_title,
            source_url,
            filters,
            TaskStatus.DRAFT,
            now,
            now,
            display_title=display_title,
        )
        planned: list[MediaItem] = []
        used: set[Path] = set()
        for item in remote:
            target = archive_target(
                self.downloads,
                source_title,
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

    def commit_selected(self, preview: ScanPreview) -> SelectedCommit:
        queued = replace(
            preview.task,
            status=TaskStatus.QUEUED,
            updated_at=self.clock(),
        )
        try:
            accepted = self.repository.create_task_deduplicating(
                queued,
                list(preview.items),
            )
        except AllMediaAlreadyExists as exc:
            raise EmptyScanError("所选媒体已全部存在于下载队列") from exc
        accepted_keys = frozenset(
            (item.peer_ref, item.message_id, item.media_id) for item in accepted
        )
        return SelectedCommit(
            queued,
            accepted_keys,
            len(preview.items) - len(accepted),
        )

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
