from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from telegram_downloader.domain import MediaKind
from telegram_downloader.subscription_matching import SubscriptionCriteria

SUPPORTED_INTERVAL_MINUTES = frozenset({5, 15, 30, 60, 180})
SUPPORTED_HISTORY_DAYS = frozenset({0, 1, 3, 7, 30})


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


def _validate_rule_fields(
    peer_ref: str,
    criteria: SubscriptionCriteria,
    media_kinds: frozenset[MediaKind],
    interval_minutes: int,
    history_days: int,
) -> None:
    if not peer_ref.strip():
        raise ValueError("请选择群组或频道")
    if not isinstance(criteria, SubscriptionCriteria):
        raise ValueError("订阅规则条件无效")
    if not media_kinds:
        raise ValueError("请至少选择一种媒体类型")
    if interval_minutes not in SUPPORTED_INTERVAL_MINUTES:
        raise ValueError("不支持的检查间隔")
    if history_days not in SUPPORTED_HISTORY_DAYS:
        raise ValueError("不支持的历史补抓范围")


@dataclass(frozen=True, slots=True)
class SubscriptionDraft:
    peer_ref: str
    criteria: SubscriptionCriteria
    media_kinds: frozenset[MediaKind]
    interval_minutes: int = 30
    history_days: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "peer_ref", self.peer_ref.strip())
        _validate_rule_fields(
            self.peer_ref,
            self.criteria,
            self.media_kinds,
            self.interval_minutes,
            self.history_days,
        )

    @property
    def keyword(self) -> str:
        return self.criteria.summary

    @property
    def matcher_fingerprint(self) -> str:
        return self.criteria.fingerprint


@dataclass(frozen=True, slots=True)
class SubscriptionRule:
    id: str
    account_id: str
    peer_ref: str
    dialog_title: str
    criteria: SubscriptionCriteria
    media_kinds: frozenset[MediaKind]
    interval_minutes: int
    history_days: int
    enabled: bool
    state: SubscriptionState
    last_message_id: int | None
    backfill_from_utc: datetime | None
    backfill_through_id: int | None
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
            self.criteria,
            self.media_kinds,
            self.interval_minutes,
            self.history_days,
        )
        if self.last_message_id is not None and self.last_message_id < 0:
            raise ValueError("消息游标不能为负数")
        if self.backfill_through_id is not None and self.backfill_through_id < 0:
            raise ValueError("补抓截止消息不能为负数")
        if self.backfill_through_id is not None and self.backfill_from_utc is None:
            raise ValueError("补抓截止消息缺少补抓起点")
        if self.backfill_from_utc is not None and self.backfill_from_utc.tzinfo is None:
            raise ValueError("补抓起点必须包含时区")
        if self.history_days == 0 and (
            self.backfill_from_utc is not None or self.backfill_through_id is not None
        ):
            raise ValueError("未启用历史补抓时不能保存补抓状态")
        if self.failure_count < 0:
            raise ValueError("失败次数不能为负数")

    @property
    def keyword(self) -> str:
        return self.criteria.summary

    @property
    def normalized_keyword(self) -> str:
        return self.criteria.fingerprint


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
