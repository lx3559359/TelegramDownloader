from datetime import datetime, timedelta

import pytest

from telegram_downloader.download_schedule import evaluate_download_schedule
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
