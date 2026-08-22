from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Protocol

from telegram_downloader.settings import AppSettings, DownloadScheduleSettings


class SettingsStorePort(Protocol):
    def save(self, settings: AppSettings) -> None: ...


class AutostartPort(Protocol):
    available: bool

    def reconcile(self, enabled: bool) -> None: ...


class BackgroundPort(Protocol):
    def configure(
        self,
        *,
        close_to_tray: bool,
        notifications_enabled: bool,
    ) -> None: ...


class SchedulePort(Protocol):
    async def reconfigure(self, settings: DownloadScheduleSettings) -> None: ...


class RuntimeSettingsCoordinator:
    def __init__(
        self,
        settings_store: SettingsStorePort,
        autostart: AutostartPort,
        background: BackgroundPort,
        schedule: SchedulePort,
    ) -> None:
        self.settings_store = settings_store
        self.autostart = autostart
        self.background = background
        self.schedule = schedule

    @property
    def autostart_available(self) -> bool:
        return self.autostart.available

    async def apply(self, previous: AppSettings, current: AppSettings) -> None:
        autostart_applied = False
        persisted = False
        try:
            await asyncio.to_thread(
                self.autostart.reconcile,
                current.autostart_enabled,
            )
            autostart_applied = True
            await asyncio.to_thread(self.settings_store.save, current)
            persisted = True
            self._configure_background(current)
            await self.schedule.reconfigure(current.download_schedule)
        except Exception:
            with suppress(Exception):
                await self.schedule.reconfigure(previous.download_schedule)
            with suppress(Exception):
                self._configure_background(previous)
            if persisted:
                with suppress(Exception):
                    await asyncio.to_thread(self.settings_store.save, previous)
            if autostart_applied:
                with suppress(Exception):
                    await asyncio.to_thread(
                        self.autostart.reconcile,
                        previous.autostart_enabled,
                    )
            raise

    def _configure_background(self, settings: AppSettings) -> None:
        self.background.configure(
            close_to_tray=settings.close_to_tray,
            notifications_enabled=settings.notifications_enabled,
        )
