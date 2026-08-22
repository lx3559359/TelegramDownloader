from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

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
