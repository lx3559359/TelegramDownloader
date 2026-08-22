from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class EventKind(StrEnum):
    DOWNLOAD_COMPLETED = "download-completed"
    DOWNLOAD_FAILED = "download-failed"
    SUBSCRIPTION_MATCH = "subscription-match"
    AUTH_REQUIRED = "auth-required"
    DISK_FULL = "disk-full"
    SCHEDULE_OPENED = "schedule-opened"
    SCHEDULE_CLOSED = "schedule-closed"
    UPDATE_AVAILABLE = "update-available"
    STORAGE_CLEANED = "storage-cleaned"
    STORAGE_CLEANUP_FAILED = "storage-cleanup-failed"


class NotificationRoute(StrEnum):
    TASKS = "tasks"
    SUBSCRIPTIONS = "subscriptions"
    LOGIN = "login"
    UPDATE = "update"
    MAINTENANCE = "maintenance"


@dataclass(frozen=True, slots=True)
class ApplicationEvent:
    kind: EventKind
    identity: str
    count: int
    route: NotificationRoute
    private_context: str = ""
    byte_count: int = 0

    def __post_init__(self) -> None:
        if not self.identity:
            raise ValueError("通知事件标识不能为空")
        if not isinstance(self.count, int) or isinstance(self.count, bool) or self.count < 1:
            raise ValueError("通知事件数量必须是正整数")
        if (
            not isinstance(self.byte_count, int)
            or isinstance(self.byte_count, bool)
            or self.byte_count < 0
        ):
            raise ValueError("通知事件字节数必须是非负整数")


@dataclass(frozen=True, slots=True)
class NotificationPayload:
    title: str
    body: str
    route: NotificationRoute


@dataclass(slots=True)
class _PendingBatch:
    count: int
    byte_count: int
    deadline: float
    sequence: int


_TEXT: dict[EventKind, tuple[str, str]] = {
    EventKind.DOWNLOAD_COMPLETED: ("下载完成", "{count} 个下载任务已完成"),
    EventKind.DOWNLOAD_FAILED: ("下载需要处理", "{count} 个下载任务部分失败"),
    EventKind.SUBSCRIPTION_MATCH: ("订阅发现新媒体", "已加入 {count} 个媒体到下载队列"),
    EventKind.AUTH_REQUIRED: ("需要重新登录", "Telegram 登录已失效，请打开应用处理"),
    EventKind.DISK_FULL: ("磁盘空间不足", "下载已安全暂停，请释放应用所在磁盘空间"),
    EventKind.SCHEDULE_OPENED: ("下载时段已开始", "时段暂停的任务正在恢复"),
    EventKind.SCHEDULE_CLOSED: ("下载时段已结束", "活动下载正在安全暂停"),
    EventKind.UPDATE_AVAILABLE: ("发现正式版更新", "打开应用可查看并确认更新"),
    EventKind.STORAGE_CLEANED: (
        "已释放存储空间",
        "后台安全清理已删除 {count} 项，释放 {size}",
    ),
    EventKind.STORAGE_CLEANUP_FAILED: (
        "自动清理需要处理",
        "自动清理有 {count} 项未处理，请打开维护中心查看",
    ),
}

_TASK_TERMINALS = frozenset(
    {EventKind.DOWNLOAD_COMPLETED, EventKind.DOWNLOAD_FAILED}
)


class NotificationBatcher:
    def __init__(self, *, window_seconds: float = 5.0) -> None:
        if window_seconds <= 0:
            raise ValueError("通知合并窗口必须大于零")
        self.window_seconds = float(window_seconds)
        self._pending: dict[tuple[EventKind, NotificationRoute], _PendingBatch] = {}
        self._terminal_identities: set[str] = set()
        self._sequence = 0

    @property
    def next_deadline(self) -> float | None:
        if not self._pending:
            return None
        return min(batch.deadline for batch in self._pending.values())

    def record(self, event: ApplicationEvent, *, now: float) -> bool:
        if event.kind in _TASK_TERMINALS:
            if event.identity in self._terminal_identities:
                return False
            self._terminal_identities.add(event.identity)
        key = (event.kind, event.route)
        batch = self._pending.get(key)
        if batch is None:
            self._sequence += 1
            self._pending[key] = _PendingBatch(
                event.count,
                event.byte_count,
                float(now) + self.window_seconds,
                self._sequence,
            )
        else:
            batch.count += event.count
            batch.byte_count += event.byte_count
        return True

    def flush_due(self, *, now: float) -> list[NotificationPayload]:
        due = sorted(
            (
                (key, batch)
                for key, batch in self._pending.items()
                if batch.deadline <= now
            ),
            key=lambda entry: (entry[1].deadline, entry[1].sequence),
        )
        payloads: list[NotificationPayload] = []
        for (kind, route), batch in due:
            self._pending.pop((kind, route), None)
            title, body = _TEXT[kind]
            payloads.append(
                NotificationPayload(
                    title,
                    body.format(
                        count=batch.count,
                        size=format_size(batch.byte_count),
                    ),
                    route,
                )
            )
        return payloads


def download_event(kind: EventKind, task_id: str) -> ApplicationEvent:
    if kind not in _TASK_TERMINALS:
        raise ValueError("下载终态事件类型无效")
    return ApplicationEvent(
        kind,
        identity=task_id,
        count=1,
        route=NotificationRoute.TASKS,
    )


def disk_full_event(task_id: str) -> ApplicationEvent:
    return ApplicationEvent(
        EventKind.DISK_FULL,
        identity=task_id,
        count=1,
        route=NotificationRoute.TASKS,
    )


def subscription_match_event(run_id: str, queued: int) -> ApplicationEvent:
    return ApplicationEvent(
        EventKind.SUBSCRIPTION_MATCH,
        identity=run_id,
        count=queued,
        route=NotificationRoute.SUBSCRIPTIONS,
    )


def auth_required_event() -> ApplicationEvent:
    return ApplicationEvent(
        EventKind.AUTH_REQUIRED,
        identity="telegram-session-expired",
        count=1,
        route=NotificationRoute.LOGIN,
    )


def update_available_event(version: str) -> ApplicationEvent:
    return ApplicationEvent(
        EventKind.UPDATE_AVAILABLE,
        identity=f"update:{version}",
        count=1,
        route=NotificationRoute.UPDATE,
    )


def storage_cleaned_event(
    cleanup_id: str,
    deleted_count: int,
    released_bytes: int,
) -> ApplicationEvent:
    return ApplicationEvent(
        EventKind.STORAGE_CLEANED,
        identity=f"storage:{cleanup_id}",
        count=deleted_count,
        route=NotificationRoute.MAINTENANCE,
        byte_count=released_bytes,
    )


def storage_cleanup_failed_event(
    cleanup_id: str,
    failed_count: int,
) -> ApplicationEvent:
    return ApplicationEvent(
        EventKind.STORAGE_CLEANUP_FAILED,
        identity=f"storage:{cleanup_id}",
        count=failed_count,
        route=NotificationRoute.MAINTENANCE,
    )


def format_size(value: int) -> str:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("字节数必须是非负整数")
    if value < 1024:
        return f"{value} B"
    amount = float(value)
    for unit in ("KiB", "MiB", "GiB"):
        amount /= 1024
        if amount < 1024 or unit == "GiB":
            return f"{amount:.1f} {unit}"
    return f"{value} B"
