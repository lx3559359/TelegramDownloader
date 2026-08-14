from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path


class SourceKind(StrEnum):
    SINGLE_MESSAGE = "single_message"
    CHANNEL_OR_GROUP = "channel_or_group"


class MediaKind(StrEnum):
    PHOTO = "photo"
    VIDEO = "video"
    AUDIO = "audio"
    VOICE = "voice"
    DOCUMENT = "document"
    ARCHIVE = "archive"


class TaskStatus(StrEnum):
    DRAFT = "draft"
    SCANNING = "scanning"
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    PAUSED = "paused"
    WAITING_RETRY = "waiting_retry"
    COMPLETED = "completed"
    PARTIAL_FAILURE = "partial_failure"


class ItemStatus(StrEnum):
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    PAUSED = "paused"
    WAITING_RETRY = "waiting_retry"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ParsedLink:
    normalized_url: str
    entity_ref: str
    kind: SourceKind
    message_id: int | None


@dataclass(frozen=True, slots=True)
class ScanFilters:
    date_from_utc: datetime
    date_to_utc: datetime
    media_kinds: frozenset[MediaKind]
    item_limit: int


@dataclass(frozen=True, slots=True)
class TaskRecord:
    id: str
    source_kind: SourceKind
    source_ref: str
    source_title: str
    source_url: str
    filters: ScanFilters
    status: TaskStatus
    created_at: datetime
    updated_at: datetime
    last_error: str | None = None
    display_title: str | None = None


@dataclass(frozen=True, slots=True)
class MediaItem:
    id: str
    task_id: str
    peer_ref: str
    message_id: int
    grouped_id: int | None
    media_id: str
    media_kind: MediaKind
    original_name: str
    target_path: Path
    expected_size: int | None
    message_date_utc: datetime
    downloaded_bytes: int = 0
    status: ItemStatus = ItemStatus.QUEUED
    retry_count: int = 0
    last_error: str | None = None
