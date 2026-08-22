from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from telegram_downloader.batch_import import (
    MAX_BATCH_MEDIA,
    BatchLinkIssue,
    parse_batch_links,
)
from telegram_downloader.content import (
    ALL_DIALOGS_SCOPE_REF,
    ALL_DIALOGS_TITLE,
    ContentSearchQuery,
)
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


@dataclass(frozen=True, slots=True)
class BatchScanProgress:
    completed: int
    total: int


@dataclass(frozen=True, slots=True)
class BatchScanPreview:
    preview: ScanPreview
    input_count: int
    unique_link_count: int
    invalid_link_count: int
    duplicate_link_count: int
    scanned_media_count: int
    internal_duplicate_count: int
    existing_media_count: int
    empty_link_count: int
    issues: tuple[BatchLinkIssue, ...]

    @property
    def items(self) -> tuple[MediaItem, ...]:
        return self.preview.items

    @property
    def known_bytes(self) -> int:
        return self.preview.known_bytes

    @property
    def unknown_size_count(self) -> int:
        return self.preview.unknown_size_count


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
        if not remote:
            raise EmptyScanError("筛选范围内没有找到可下载媒体")
        source_title = remote[0].source_title
        return self._build_preview(
            source_kind=source.kind,
            source_ref=source.entity_ref,
            source_title=source_title,
            source_url=source.normalized_url,
            filters=filters,
            remote=remote,
            display_title=None,
            empty_message="扫描媒体已全部存在于下载队列",
            skip_existing=True,
        )

    async def scan_batch(
        self,
        values: tuple[str, ...],
        filters: ScanFilters,
        *,
        on_progress: Callable[[BatchScanProgress], None] | None = None,
    ) -> BatchScanPreview:
        collection = parse_batch_links(values)
        remote: list[RemoteMedia] = []
        seen: set[tuple[str, int, str]] = set()
        scanned_media_count = 0
        internal_duplicate_count = 0
        empty_link_count = 0
        total = len(collection.links)
        for completed, source in enumerate(collection.links, 1):
            source_count = 0
            async for item in self.gateway.scan(source, filters):
                source_count += 1
                scanned_media_count += 1
                key = (item.peer_ref, item.message_id, item.media_id)
                if key in seen:
                    internal_duplicate_count += 1
                    continue
                seen.add(key)
                remote.append(item)
                if len(remote) > MAX_BATCH_MEDIA:
                    raise ValueError(
                        f"批量预检最多支持 {MAX_BATCH_MEDIA} 个唯一媒体，"
                        "请缩小链接数量、日期范围或数量上限"
                    )
            if source_count == 0:
                empty_link_count += 1
            if on_progress is not None:
                on_progress(BatchScanProgress(completed, total))

        if not remote:
            raise EmptyScanError("批量链接在当前筛选范围内没有找到可下载媒体")
        existing = self.repository.existing_media_keys(seen)
        available = [
            item
            for item in remote
            if (item.peer_ref, item.message_id, item.media_id) not in existing
        ]
        if not available:
            raise EmptyScanError("批量扫描媒体已全部存在于下载队列")

        normalized = "\n".join(item.normalized_url for item in collection.links)
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
        preview = self._build_preview(
            source_kind=SourceKind.BATCH_IMPORT,
            source_ref=f"batch:{digest}",
            source_title="批量链接导入",
            source_url=f"telegram-batch://{digest}",
            filters=filters,
            remote=available,
            display_title=f"批量链接导入（{len(collection.links)} 个链接）",
            empty_message="批量扫描媒体已全部存在于下载队列",
            skip_existing=False,
        )
        return BatchScanPreview(
            preview,
            collection.input_count,
            len(collection.links),
            len(collection.issues),
            collection.duplicate_count,
            scanned_media_count,
            internal_duplicate_count,
            len(existing),
            empty_link_count,
            collection.issues,
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

    def plan_account_search(
        self,
        query: ContentSearchQuery,
        selected: list[RemoteMedia],
    ) -> ScanPreview:
        return self._build_preview(
            source_kind=SourceKind.ACCOUNT_SEARCH,
            source_ref=ALL_DIALOGS_SCOPE_REF,
            source_title=ALL_DIALOGS_TITLE,
            source_url="account-search://all-dialogs",
            filters=query.filters,
            remote=selected,
            display_title=f"{ALL_DIALOGS_TITLE}（搜索：{query.keyword}）",
            empty_message="所选媒体已全部存在于下载队列",
            skip_existing=True,
        )

    def plan_subscription(
        self,
        source_ref: str,
        source_title: str,
        rule_summary: str,
        selected: list[RemoteMedia],
    ) -> ScanPreview:
        if not selected:
            raise EmptyScanError("订阅匹配媒体已全部存在于下载队列")
        dates = [item.message_date_utc for item in selected]
        filters = ScanFilters(
            min(dates),
            max(dates),
            frozenset(item.kind for item in selected),
            len(selected),
        )
        return self._build_preview(
            source_kind=SourceKind.CHANNEL_OR_GROUP,
            source_ref=source_ref,
            source_title=source_title,
            source_url=f"telegram://peer/{source_ref}",
            filters=filters,
            remote=selected,
            display_title=f"{source_title}（自动订阅：{rule_summary}）",
            empty_message="订阅匹配媒体已全部存在于下载队列",
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
            keys = {(item.peer_ref, item.message_id, item.media_id) for item in remote}
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

    def commit(self, preview: ScanPreview) -> SelectedCommit:
        return self._commit_deduplicating(
            preview,
            "扫描媒体已全部存在于下载队列",
        )

    def commit_selected(self, preview: ScanPreview) -> SelectedCommit:
        return self._commit_deduplicating(
            preview,
            "所选媒体已全部存在于下载队列",
        )

    def _commit_deduplicating(
        self,
        preview: ScanPreview,
        empty_message: str,
    ) -> SelectedCommit:
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
            raise EmptyScanError(empty_message) from exc
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
