from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from telegram_downloader.notifications import (
    ApplicationEvent,
    EventKind,
    NotificationRoute,
)
from telegram_downloader.settings import DownloadScheduleSettings


@dataclass(frozen=True, slots=True)
class DownloadScheduleState:
    allowed: bool
    next_boundary: datetime | None


def _minute_of_day(value: datetime) -> int:
    return value.hour * 60 + value.minute


def _allowed_at(settings: DownloadScheduleSettings, now: datetime) -> bool:
    if not settings.enabled:
        return True
    minute = _minute_of_day(now)
    selected = settings.weekdays
    if settings.start_minute == settings.end_minute:
        return now.weekday() in selected
    if settings.start_minute < settings.end_minute:
        return now.weekday() in selected and settings.start_minute <= minute < settings.end_minute
    previous_day = (now.weekday() - 1) % 7
    return (now.weekday() in selected and minute >= settings.start_minute) or (
        previous_day in selected and minute < settings.end_minute
    )


def evaluate_download_schedule(
    settings: DownloadScheduleSettings,
    now: datetime,
) -> DownloadScheduleState:
    if now.utcoffset() is None:
        raise ValueError("下载时段计算要求本地时区时间")
    allowed = _allowed_at(settings, now)
    if not settings.enabled:
        return DownloadScheduleState(True, None)
    cursor = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
    limit = cursor + timedelta(days=8)
    while cursor <= limit:
        if _allowed_at(settings, cursor) != allowed:
            return DownloadScheduleState(allowed, cursor)
        cursor += timedelta(minutes=1)
    raise RuntimeError("无法计算下载时段的下一边界")


class ScheduleScheduler(Protocol):
    async def set_schedule_open(self, opened: bool) -> set[str]: ...


class DownloadScheduleController:
    def __init__(
        self,
        scheduler: Callable[[], ScheduleScheduler | None],
        settings: DownloadScheduleSettings,
        *,
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        publish: Callable[[ApplicationEvent], None] | None = None,
    ) -> None:
        self.scheduler = scheduler
        self.settings = settings
        self.now = now or (lambda: datetime.now().astimezone())
        self.sleep = sleep or asyncio.sleep
        self.publish = publish or (lambda _event: None)
        self._task: asyncio.Task[None] | None = None
        self._last_allowed: bool | None = None
        self._last_scheduler: ScheduleScheduler | None = None
        self._next_delay_seconds = 60.0
        self._refresh_lock = asyncio.Lock()

    async def start(self) -> None:
        await self.refresh()
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def refresh(self) -> None:
        async with self._refresh_lock:
            current = self.now()
            state = evaluate_download_schedule(self.settings, current)
            self._next_delay_seconds = _next_delay_seconds(current, state)
            selected = self.scheduler()
            state_changed = state.allowed != self._last_allowed
            scheduler_changed = selected is not self._last_scheduler
            if selected is not None and (state_changed or scheduler_changed):
                await selected.set_schedule_open(state.allowed)
            if state_changed and (self._last_allowed is not None or self.settings.enabled):
                self.publish(_schedule_event(state.allowed))
            self._last_allowed = state.allowed
            self._last_scheduler = selected

    async def reconfigure(self, settings: DownloadScheduleSettings) -> None:
        self.settings = settings
        await self.refresh()

    async def shutdown(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _run(self) -> None:
        while True:
            await self.sleep(self._next_delay_seconds)
            await self.refresh()


def _schedule_event(opened: bool) -> ApplicationEvent:
    kind = EventKind.SCHEDULE_OPENED if opened else EventKind.SCHEDULE_CLOSED
    return ApplicationEvent(
        kind,
        identity=kind.value,
        count=1,
        route=NotificationRoute.TASKS,
    )


def _next_delay_seconds(now: datetime, state: DownloadScheduleState) -> float:
    if state.next_boundary is None:
        return 60.0
    remaining = (state.next_boundary - now).total_seconds()
    return max(0.05, min(60.0, remaining))
