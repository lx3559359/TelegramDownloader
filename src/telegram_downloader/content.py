from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from telegram_downloader.domain import MediaKind, ScanFilters

ALL_DIALOGS_SCOPE_REF = "__all_dialogs__"
ALL_DIALOGS_TITLE = "全部会话"


class DialogKind(StrEnum):
    GROUP = "group"
    CHANNEL = "channel"


class SearchStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    INCOMPLETE = "incomplete"


class SearchScope(StrEnum):
    SINGLE_DIALOG = "single_dialog"
    ALL_DIALOGS = "all_dialogs"


class ContentSourceKind(StrEnum):
    GROUP = "group"
    CHANNEL = "channel"
    PRIVATE = "private"
    BOT = "bot"
    SAVED = "saved"
    UNKNOWN = "unknown"


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
    offset_rate: int = 0
    offset_peer_ref: str | None = None

    def __post_init__(self) -> None:
        if self.offset_id < 0 or self.offset_rate < 0:
            raise ValueError("搜索游标不能为负数")

    def to_json(self) -> str:
        return json.dumps(
            {
                "version": 1,
                "offsetId": self.offset_id,
                "offsetRate": self.offset_rate,
                "offsetPeerRef": self.offset_peer_ref,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, value: str) -> SearchCursor:
        try:
            payload = json.loads(value)
            if payload.get("version") != 1:
                raise ValueError
            peer_ref = payload.get("offsetPeerRef")
            if peer_ref is not None and not isinstance(peer_ref, str):
                raise ValueError
            return cls(
                offset_id=int(payload["offsetId"]),
                offset_rate=int(payload["offsetRate"]),
                offset_peer_ref=peer_ref,
            )
        except (
            AttributeError,
            KeyError,
            TypeError,
            ValueError,
        ) as error:
            raise ValueError("搜索游标格式无效") from error


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
    scope: SearchScope = SearchScope.SINGLE_DIALOG


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
    source_title: str = ""
    source_kind: ContentSourceKind = ContentSourceKind.UNKNOWN
