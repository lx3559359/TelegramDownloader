import asyncio
from datetime import datetime, timedelta

import pytest

from telegram_downloader.download_schedule import (
    DownloadScheduleController,
    evaluate_download_schedule,
)
from telegram_downloader.notifications import EventKind
from telegram_downloader.settings import DownloadScheduleSettings

MONDAY = datetime(2026, 8, 24, 10, 0).astimezone()


def schedule(
    start: int,
    end: int,
    days: tuple[int, ...] = (0,),
) -> DownloadScheduleSettings:
    return DownloadScheduleSettings(True, days, start, end)


def test_same_day_window_and_next_boundary() -> None:
    value = evaluate_download_schedule(schedule(9 * 60, 17 * 60), MONDAY)

    assert value.allowed is True
    assert value.next_boundary == MONDAY.replace(hour=17)


def test_cross_midnight_end_segment_belongs_to_start_day() -> None:
    tuesday_0100 = MONDAY.replace(day=25, hour=1)

    value = evaluate_download_schedule(schedule(22 * 60, 2 * 60), tuesday_0100)

    assert value.allowed is True
    assert value.next_boundary == tuesday_0100.replace(hour=2)


def test_equal_times_mean_full_selected_day() -> None:
    assert evaluate_download_schedule(schedule(0, 0), MONDAY).allowed is True
    assert evaluate_download_schedule(schedule(0, 0), MONDAY.replace(day=25)).allowed is False


def test_disabled_schedule_is_always_open() -> None:
    value = evaluate_download_schedule(DownloadScheduleSettings(), MONDAY)

    assert value.allowed is True
    assert value.next_boundary is None


@pytest.mark.parametrize(
    ("hour", "expected"),
    [(8, False), (9, True), (16, True), (17, False)],
)
def test_same_day_transition_table(hour: int, expected: bool) -> None:
    assert (
        evaluate_download_schedule(
            schedule(9 * 60, 17 * 60),
            MONDAY.replace(hour=hour),
        ).allowed
        is expected
    )


def test_sunday_cross_midnight_wraps_into_monday() -> None:
    monday_0100 = MONDAY.replace(hour=1)

    value = evaluate_download_schedule(
        schedule(22 * 60, 2 * 60, days=(6,)),
        monday_0100,
    )

    assert value.allowed is True
    assert value.next_boundary == monday_0100.replace(hour=2)


def test_next_boundary_wraps_to_next_selected_week() -> None:
    monday_after_window = MONDAY.replace(hour=18)

    value = evaluate_download_schedule(
        schedule(9 * 60, 17 * 60),
        monday_after_window,
    )

    assert value.allowed is False
    assert value.next_boundary == monday_after_window + timedelta(days=7, hours=-9)


def test_naive_time_is_rejected() -> None:
    with pytest.raises(ValueError, match="时区"):
        evaluate_download_schedule(schedule(0, 0), datetime(2026, 8, 24))


class FakeScheduler:
    def __init__(self) -> None:
        self.schedule_states: list[bool] = []

    async def set_schedule_open(self, opened: bool) -> set[str]:
        self.schedule_states.append(opened)
        return set()


@pytest.mark.asyncio
async def test_schedule_controller_applies_initial_gate_before_queue_restore() -> None:
    scheduler = FakeScheduler()
    events = []
    controller = DownloadScheduleController(
        lambda: scheduler,
        schedule(9 * 60, 17 * 60),
        now=lambda: MONDAY.replace(hour=8),
        sleep=lambda _seconds: _never_return(),
        publish=events.append,
    )

    await controller.start()
    await controller.shutdown()

    assert scheduler.schedule_states == [False]
    assert events[-1].kind is EventKind.SCHEDULE_CLOSED


@pytest.mark.asyncio
async def test_reconfigure_recalculates_and_opens_immediately() -> None:
    scheduler = FakeScheduler()
    controller = DownloadScheduleController(
        lambda: scheduler,
        schedule(9 * 60, 17 * 60),
        now=lambda: MONDAY.replace(hour=8),
        sleep=lambda _seconds: _never_return(),
    )
    await controller.start()

    await controller.reconfigure(DownloadScheduleSettings())
    await controller.shutdown()

    assert scheduler.schedule_states == [False, True]


@pytest.mark.asyncio
async def test_new_scheduler_receives_current_gate_even_without_time_transition() -> None:
    schedulers = [FakeScheduler(), FakeScheduler()]
    selected = 0
    controller = DownloadScheduleController(
        lambda: schedulers[selected],
        schedule(9 * 60, 17 * 60),
        now=lambda: MONDAY.replace(hour=8),
        sleep=lambda _seconds: _never_return(),
    )
    await controller.start()
    selected = 1

    await controller.refresh()
    await controller.shutdown()

    assert schedulers[0].schedule_states == [False]
    assert schedulers[1].schedule_states == [False]


@pytest.mark.asyncio
async def test_controller_sleeps_only_until_nearby_schedule_boundary() -> None:
    sleeps: list[float] = []

    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        await asyncio.Event().wait()

    controller = DownloadScheduleController(
        lambda: FakeScheduler(),
        schedule(9 * 60, 17 * 60),
        now=lambda: MONDAY.replace(hour=16, minute=59, second=30),
        sleep=record_sleep,
    )
    await controller.start()
    await asyncio.sleep(0)

    assert sleeps == [30.0]
    await controller.shutdown()


async def _never_return() -> None:
    await asyncio.Event().wait()
