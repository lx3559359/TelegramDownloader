from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from telegram_downloader.domain import MediaKind

SUPPORTED_INTERVAL_MINUTES = frozenset({5, 15, 30, 60, 180})


class SubscriptionState(StrEnum):
    BASELINING = "baselining"
    WAITING = "waiting"
    RUNNING = "running"
    PAUSED = "paused"
    WAITING_NETWORK = "waiting_network"
    AUTH_REQUIRED = "auth_required"
    FAILED = "failed"


class SubscriptionRunStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


def _normalized_keyword(value: str) -> str:
    return " ".join(value.casefold().split())


def _validate_rule_fields(
    peer_ref: str,
    keyword: str,
    media_kinds: frozenset[MediaKind],
    interval_minutes: int,
) -> None:
    if not peer_ref.strip():
        raise ValueError("请选择群组或频道")
    if not keyword.strip():
        raise ValueError("订阅关键词不能为空")
    if not media_kinds:
        raise ValueError("请至少选择一种媒体类型")
    if interval_minutes not in SUPPORTED_INTERVAL_MINUTES:
        raise ValueError("不支持的检查间隔")


@dataclass(frozen=True, slots=True)
class SubscriptionDraft:
    peer_ref: str
    keyword: str
    media_kinds: frozenset[MediaKind]
    interval_minutes: int = 30

    def __post_init__(self) -> None:
        object.__setattr__(self, "peer_ref", self.peer_ref.strip())
        object.__setattr__(self, "keyword", self.keyword.strip())
        _validate_rule_fields(
            self.peer_ref,
            self.keyword,
            self.media_kinds,
            self.interval_minutes,
        )

    @property
    def normalized_keyword(self) -> str:
        return _normalized_keyword(self.keyword)


@dataclass(frozen=True, slots=True)
class SubscriptionRule:
    id: str
    account_id: str
    peer_ref: str
    dialog_title: str
    keyword: str
    media_kinds: frozenset[MediaKind]
    interval_minutes: int
    enabled: bool
    state: SubscriptionState
    last_message_id: int | None
    next_run_at: datetime | None
    last_run_at: datetime | None
    last_error: str | None
    failure_count: int
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.account_id.strip():
            raise ValueError("订阅规则标识不能为空")
        if not self.dialog_title.strip():
            raise ValueError("群组或频道名称不能为空")
        _validate_rule_fields(
            self.peer_ref,
            self.keyword,
            self.media_kinds,
            self.interval_minutes,
        )
        if self.last_message_id is not None and self.last_message_id < 0:
            raise ValueError("消息游标不能为负数")
        if self.failure_count < 0:
            raise ValueError("失败次数不能为负数")

    @property
    def normalized_keyword(self) -> str:
        return _normalized_keyword(self.keyword)


@dataclass(frozen=True, slots=True)
class SubscriptionProgress:
    rule_id: str
    inspected: int
    matched: int
    queued: int
    duplicate: int
    phase: str


@dataclass(frozen=True, slots=True)
class SubscriptionRun:
    id: str
    rule_id: str
    account_id: str
    started_at: datetime
    finished_at: datetime
    status: SubscriptionRunStatus
    inspected: int
    matched: int
    queued: int
    duplicate: int
    error: str | None = None


@dataclass(frozen=True, slots=True)
class SubscriptionRunReport:
    run: SubscriptionRun
    task_ids: tuple[str, ...]
    last_processed_id: int
    has_more: bool
