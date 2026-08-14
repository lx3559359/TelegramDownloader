from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from telegram_downloader.domain import MediaKind, ScanFilters


class DialogKind(StrEnum):
    GROUP = "group"
    CHANNEL = "channel"


class SearchStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    INCOMPLETE = "incomplete"


@dataclass(frozen=True, slots=True)
class AccountProfile:
    account_id: str
    display_name: str


@dataclass(frozen=True, slots=True)
class ContentDialog:
    account_id: str
    peer_ref: str
    title: str
    username: str
    kind: DialogKind
    archived: bool
    available: bool
    last_synced_at: datetime


@dataclass(frozen=True, slots=True)
class ContentSearchQuery:
    keyword: str
    filters: ScanFilters

    def __post_init__(self) -> None:
        cleaned = self.keyword.strip()
        if not cleaned:
            raise ValueError("搜索关键词不能为空")
        if self.filters.date_from_utc > self.filters.date_to_utc:
            raise ValueError("开始日期不能晚于结束日期")
        if not self.filters.media_kinds:
            raise ValueError("请至少选择一种媒体类型")
        if not 1 <= self.filters.item_limit <= 10_000:
            raise ValueError("搜索结果上限必须在 1 到 10000 之间")
        object.__setattr__(self, "keyword", cleaned)

    @property
    def normalized_keyword(self) -> str:
        return unicodedata.normalize("NFKC", self.keyword).casefold()

    @property
    def filters_fingerprint(self) -> str:
        value = {
            "dateFrom": self.filters.date_from_utc.isoformat(),
            "dateTo": self.filters.date_to_utc.isoformat(),
            "itemLimit": self.filters.item_limit,
            "mediaKinds": sorted(kind.value for kind in self.filters.media_kinds),
        }
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class SearchCursor:
    offset_id: int = 0


@dataclass(frozen=True, slots=True)
class SearchSession:
    id: str
    account_id: str
    peer_ref: str
    dialog_title: str
    query: ContentSearchQuery
    status: SearchStatus
    generation: int
    cursor: SearchCursor | None
    exhausted: bool
    result_count: int
    created_at: datetime
    updated_at: datetime
    last_error: str | None = None


@dataclass(frozen=True, slots=True)
class SearchResult:
    id: str
    search_id: str
    account_id: str
    peer_ref: str
    message_id: int
    grouped_id: int | None
    media_id: str
    media_kind: MediaKind
    original_name: str
    expected_size: int | None
    message_date_utc: datetime
    excerpt: str
    thumbnail_key: str
    selected: bool = False
    available: bool = True
    queued: bool = False
