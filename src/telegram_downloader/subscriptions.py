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
    keyword_hits: int
    matched: int
    queued: int
    duplicate: int
    phase: str

    def __post_init__(self) -> None:
        _validate_diagnostic_counts(
            self.inspected,
            self.keyword_hits,
            self.matched,
            self.duplicate,
            queued=self.queued,
        )


@dataclass(frozen=True, slots=True)
class SubscriptionRun:
    id: str
    rule_id: str
    account_id: str
    started_at: datetime
    finished_at: datetime
    status: SubscriptionRunStatus
    inspected: int
    keyword_hits: int
    matched: int
    queued: int
    duplicate: int
    error: str | None = None

    def __post_init__(self) -> None:
        _validate_diagnostic_counts(
            self.inspected,
            self.keyword_hits,
            self.matched,
            self.duplicate,
            queued=self.queued,
        )


@dataclass(frozen=True, slots=True)
class SubscriptionProbeProgress:
    rule_id: str
    inspected: int
    keyword_hits: int
    matched: int
    phase: str

    def __post_init__(self) -> None:
        _validate_diagnostic_counts(
            self.inspected,
            self.keyword_hits,
            self.matched,
            0,
        )


@dataclass(frozen=True, slots=True)
class SubscriptionProbeSample:
    message_id: int
    message_date_utc: datetime
    media_kind: MediaKind
    original_name: str
    expected_size: int | None
    already_queued: bool
    excerpt: str

    def __post_init__(self) -> None:
        if self.message_id <= 0:
            raise ValueError("探测样本消息标识必须大于零")
        if self.expected_size is not None and self.expected_size < 0:
            raise ValueError("探测样本文件大小不能为负数")


@dataclass(frozen=True, slots=True)
class SubscriptionProbeReport:
    rule_id: str
    inspected: int
    keyword_hits: int
    matched: int
    duplicate: int
    samples: tuple[SubscriptionProbeSample, ...]
    finished_at: datetime

    def __post_init__(self) -> None:
        _validate_diagnostic_counts(
            self.inspected,
            self.keyword_hits,
            self.matched,
            self.duplicate,
        )
        if len(self.samples) > 20:
            raise ValueError("探测样本最多保留 20 项")


@dataclass(frozen=True, slots=True)
class SubscriptionRunReport:
    run: SubscriptionRun
    task_ids: tuple[str, ...]
    last_processed_id: int
    has_more: bool


def _validate_diagnostic_counts(
    inspected: int,
    keyword_hits: int,
    matched: int,
    duplicate: int,
    *,
    queued: int | None = None,
) -> None:
    values = (inspected, keyword_hits, matched, duplicate)
    if any(value < 0 for value in values) or (queued is not None and queued < 0):
        raise ValueError("订阅诊断计数不能为负数")
    if keyword_hits > inspected:
        raise ValueError("关键词命中数不能大于扫描消息数")
    if duplicate > matched:
        raise ValueError("重复媒体数不能大于匹配媒体数")
    if queued is not None and queued > matched:
        raise ValueError("新增任务数不能大于匹配媒体数")
