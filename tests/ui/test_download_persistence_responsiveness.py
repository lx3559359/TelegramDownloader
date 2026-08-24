import asyncio
import threading
import time
from contextlib import suppress
from time import perf_counter

from PySide6.QtCore import Qt, QTimer

from telegram_downloader import app
from telegram_downloader.domain import ItemStatus
from telegram_downloader.download_persistence import DownloadPersistenceCoordinator
from telegram_downloader.repository import ItemProgressUpdate


class SyntheticRepository:
    def __init__(self) -> None:
        self.updates = []
        self.terminal_started = threading.Event()
        self.terminal_release = threading.Event()
        self.terminal_durable = False

    def update_item_progresses(self, updates) -> None:
        time.sleep(0.05)
        self.updates.extend(updates)

    def commit_terminal(self) -> None:
        time.sleep(0.05)
        self.terminal_started.set()
        assert self.terminal_release.wait(timeout=2)
        self.terminal_durable = True


def test_qt_timer_remains_responsive_until_terminal_is_durable(tmp_path) -> None:
    application, loop, controller = app.create_application(tmp_path)
    repository = SyntheticRepository()
    persistence = DownloadPersistenceCoordinator(repository)
    ticks: list[float] = []
    timer = QTimer()
    timer.setTimerType(Qt.TimerType.PreciseTimer)
    timer.setInterval(5)
    timer.timeout.connect(lambda: ticks.append(perf_counter()))
    timer.start()

    async def exercise() -> None:
        await persistence.record_progress(
            ItemProgressUpdate(
                "synthetic-item",
                20,
                ItemStatus.DOWNLOADING,
            )
        )
        terminal = asyncio.create_task(
            persistence.execute(
                repository.commit_terminal,
                flush_item_ids=("synthetic-item",),
            )
        )
        started = await asyncio.to_thread(repository.terminal_started.wait, 1)
        assert started is True
        assert terminal.done() is False
        await asyncio.sleep(0.05)
        repository.terminal_release.set()
        await terminal
        assert repository.terminal_durable is True
        await persistence.close()

    try:
        loop.run_until_complete(exercise())
        assert len(ticks) >= 10
        gaps = [
            (current - previous) * 1000
            for previous, current in zip(ticks, ticks[1:], strict=False)
        ]
        assert max(gaps) <= 20.0
        assert repository.updates[-1].downloaded_bytes == 20
    finally:
        timer.stop()
        repository.terminal_release.set()
        with suppress(Exception):
            loop.run_until_complete(persistence.close())
        loop.run_until_complete(controller._async_actions.shutdown())
        controller.window.close()
        loop.close()
        application.processEvents()
