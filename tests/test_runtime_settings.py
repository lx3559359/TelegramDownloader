from dataclasses import replace

import pytest

from telegram_downloader.runtime_settings import RuntimeSettingsCoordinator
from telegram_downloader.settings import AppSettings, DownloadScheduleSettings


class Store:
    def __init__(self, value: AppSettings, *, fail_value: AppSettings | None = None) -> None:
        self.value = value
        self.fail_value = fail_value
        self.saved: list[AppSettings] = []

    def save(self, value: AppSettings) -> None:
        self.saved.append(value)
        if value == self.fail_value:
            raise OSError("private settings path")
        self.value = value


class Autostart:
    available = True

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled
        self.calls: list[bool] = []

    def reconcile(self, enabled: bool) -> None:
        self.calls.append(enabled)
        self.enabled = enabled


class Background:
    def __init__(self, settings: AppSettings) -> None:
        self.value = (settings.close_to_tray, settings.notifications_enabled)
        self.calls: list[tuple[bool, bool]] = []

    def configure(self, *, close_to_tray: bool, notifications_enabled: bool) -> None:
        self.value = (close_to_tray, notifications_enabled)
        self.calls.append(self.value)


class Schedule:
    def __init__(self, value: DownloadScheduleSettings, *, fail_once: bool = False) -> None:
        self.value = value
        self.fail_once = fail_once
        self.calls: list[DownloadScheduleSettings] = []

    async def reconfigure(self, value: DownloadScheduleSettings) -> None:
        self.calls.append(value)
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("schedule failure")
        self.value = value


@pytest.mark.asyncio
async def test_runtime_settings_apply_all_external_effects() -> None:
    previous = AppSettings()
    current = replace(
        previous,
        close_to_tray=False,
        notifications_enabled=False,
        autostart_enabled=True,
        download_schedule=DownloadScheduleSettings(True, (0,), 60, 120),
    )
    store = Store(previous)
    autostart = Autostart(False)
    background = Background(previous)
    schedule = Schedule(previous.download_schedule)
    effects = RuntimeSettingsCoordinator(store, autostart, background, schedule)

    await effects.apply(previous, current)

    assert store.value == current
    assert autostart.enabled is True
    assert background.value == (False, False)
    assert schedule.value == current.download_schedule


@pytest.mark.asyncio
async def test_settings_save_failure_rolls_back_autostart() -> None:
    previous = AppSettings()
    current = replace(previous, autostart_enabled=True)
    store = Store(previous, fail_value=current)
    autostart = Autostart(False)
    effects = RuntimeSettingsCoordinator(
        store,
        autostart,
        Background(previous),
        Schedule(previous.download_schedule),
    )

    with pytest.raises(OSError, match="private settings path"):
        await effects.apply(previous, current)

    assert store.value == previous
    assert autostart.calls == [True, False]


@pytest.mark.asyncio
async def test_schedule_failure_rolls_back_persistence_and_runtime_state() -> None:
    previous = AppSettings()
    current = replace(
        previous,
        close_to_tray=False,
        autostart_enabled=True,
        download_schedule=DownloadScheduleSettings(True, (0,), 60, 120),
    )
    store = Store(previous)
    autostart = Autostart(False)
    background = Background(previous)
    schedule = Schedule(previous.download_schedule, fail_once=True)
    effects = RuntimeSettingsCoordinator(store, autostart, background, schedule)

    with pytest.raises(RuntimeError, match="schedule failure"):
        await effects.apply(previous, current)

    assert store.value == previous
    assert autostart.enabled is False
    assert background.value == (True, True)
    assert schedule.value == previous.download_schedule
