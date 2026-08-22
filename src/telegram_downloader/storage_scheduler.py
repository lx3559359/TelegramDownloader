from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Protocol

from telegram_downloader.maintenance_activity import OperationActivityRegistry
from telegram_downloader.settings import StorageMaintenanceSettings
from telegram_downloader.storage_maintenance import (
    StorageMaintenanceError,
    StorageMaintenanceService,
)
from telegram_downloader.storage_models import StorageResultCode, StorageTrigger

logger = logging.getLogger(__name__)


class _MaintenanceService(Protocol):
    settings: StorageMaintenanceSettings

    def load_state(self) -> Any: ...

    async def clean_safe(self, trigger: StorageTrigger) -> Any: ...

    async def shutdown(self) -> None: ...


class _ActivityRegistry(Protocol):
    async def wait_for_continuous_idle(self, seconds: float) -> bool: ...


class StorageMaintenanceScheduler:
    def __init__(
        self,
        service: StorageMaintenanceService | _MaintenanceService,
        activity: OperationActivityRegistry | _ActivityRegistry,
        settings_getter: Callable[[], StorageMaintenanceSettings],
        *,
        utc_clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.service = service
        self.activity = activity
        self.settings_getter = settings_getter
        self.utc_clock = utc_clock or (lambda: datetime.now(UTC))
        self.sleep = sleep
        self._settings = self._read_settings()
        self.service.settings = self._settings
        self._changed = asyncio.Event()
        self._runner: asyncio.Task[None] | None = None
        self._shutdown = False
        self._service_shutdown = False

    def start(self) -> None:
        if self._shutdown:
            raise RuntimeError("存储维护调度器已经关闭")
        if self._runner is not None and not self._runner.done():
            return
        self._runner = asyncio.create_task(self._run())

    def reconfigure(self, settings: StorageMaintenanceSettings) -> None:
        if not isinstance(settings, StorageMaintenanceSettings):
            raise ValueError("存储维护调度设置无效")
        self._settings = settings
        self.service.settings = settings
        self._changed.set()

    async def shutdown(self) -> None:
        if self._shutdown and self._service_shutdown:
            return
        self._shutdown = True
        self._changed.set()
        runner = self._runner
        if runner is not None:
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)
            self._runner = None
        if not self._service_shutdown:
            self._service_shutdown = True
            await self.service.shutdown()

    async def _run(self) -> None:
        enabled_session = False
        while not self._shutdown:
            settings = self._settings
            if not settings.automatic_enabled:
                enabled_session = False
                await self._wait_for_change()
                continue
            if not enabled_session:
                completed = await self._wait_or_change(settings.startup_delay_seconds)
                if not completed:
                    enabled_session = False
                    continue
                enabled_session = True
            if not self._settings.automatic_enabled:
                continue

            try:
                state = self.service.load_state()
                due_at = state.next_due_at
            except StorageMaintenanceError:
                due_at = None
            except Exception:
                logger.warning("storage scheduler state load failed")
                due_at = None
            if due_at is not None:
                delay = max(0.0, (due_at - self.utc_clock()).total_seconds())
                if not await self._wait_or_change(delay):
                    continue
            if not self._settings.automatic_enabled:
                continue
            idle = await self._wait_for_idle_or_change(self._settings.idle_required_seconds)
            if not idle or not self._settings.automatic_enabled:
                continue
            try:
                result = await self.service.clean_safe(StorageTrigger.AUTOMATIC)
                code = result.result_code
            except StorageMaintenanceError:
                logger.warning("storage scheduler cleanup attempt failed")
                code = StorageResultCode.LOCAL_ERROR
            except Exception:
                logger.warning("storage scheduler cleanup attempt failed")
                code = StorageResultCode.LOCAL_ERROR
            if code not in {
                StorageResultCode.COMPLETED,
                StorageResultCode.NOTHING_TO_CLEAN,
            }:
                await self._wait_or_change(self._settings.busy_retry_seconds)

    async def _wait_for_change(self) -> None:
        event = self._take_change_event()
        await event.wait()

    async def _wait_or_change(self, seconds: float) -> bool:
        if self._shutdown:
            return False
        if seconds <= 0:
            await asyncio.sleep(0)
            return not self._shutdown
        event = self._take_change_event()
        sleeper = asyncio.create_task(self.sleep(seconds))
        changed = asyncio.create_task(event.wait())
        done, pending = await asyncio.wait(
            (sleeper, changed),
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        if changed in done:
            if not sleeper.done():
                sleeper.cancel()
                await asyncio.gather(sleeper, return_exceptions=True)
            return False
        changed.cancel()
        await asyncio.gather(changed, return_exceptions=True)
        return not self._shutdown

    async def _wait_for_idle_or_change(self, seconds: float) -> bool:
        event = self._take_change_event()
        idle = asyncio.create_task(self.activity.wait_for_continuous_idle(seconds))
        changed = asyncio.create_task(event.wait())
        done, pending = await asyncio.wait(
            (idle, changed),
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        if changed in done:
            if not idle.done():
                idle.cancel()
                await asyncio.gather(idle, return_exceptions=True)
            return False
        changed.cancel()
        await asyncio.gather(changed, return_exceptions=True)
        return bool(idle.result()) and not self._shutdown

    def _take_change_event(self) -> asyncio.Event:
        self._changed.clear()
        return self._changed

    def _read_settings(self) -> StorageMaintenanceSettings:
        settings = self.settings_getter()
        if not isinstance(settings, StorageMaintenanceSettings):
            raise ValueError("存储维护调度设置无效")
        return settings
