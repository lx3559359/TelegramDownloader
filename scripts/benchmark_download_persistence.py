from __future__ import annotations

import argparse
import asyncio
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from telegram_downloader.domain import (  # noqa: E402
    ItemStatus,
    MediaItem,
    MediaKind,
)
from telegram_downloader.download_persistence import (  # noqa: E402
    DownloadPersistenceCoordinator,
)
from telegram_downloader.downloader import MediaDownloader  # noqa: E402
from telegram_downloader.paths import PortablePaths  # noqa: E402


class SyntheticGateway:
    async def stream_media(self, _peer_ref: str, _message_id: int, offset: int):
        for index in range(offset, 20):
            await asyncio.sleep(0)
            yield bytes((index,))


class SyntheticRepository:
    def __init__(self, delay_seconds: float) -> None:
        self.delay_seconds = delay_seconds
        self.media_persistence_writes = 0
        self.latest_downloaded_bytes = 0
        self.terminal_durable = False

    def _delay(self) -> None:
        time.sleep(self.delay_seconds)

    def update_item_progress(
        self,
        _item_id: str,
        downloaded_bytes: int,
        _status: ItemStatus,
        _error: str | None = None,
        _retry_count: int | None = None,
    ) -> None:
        self._delay()
        self.media_persistence_writes += 1
        self.latest_downloaded_bytes = downloaded_bytes

    def update_item_progresses(self, updates) -> None:
        self._delay()
        self.media_persistence_writes += 1
        for update in updates:
            self.latest_downloaded_bytes = update.downloaded_bytes

    def complete_item(
        self,
        _item_id: str,
        downloaded_bytes: int,
        _sha256: str,
        _verified_at: datetime,
    ) -> None:
        self._delay()
        self.media_persistence_writes += 1
        self.latest_downloaded_bytes = downloaded_bytes
        self.terminal_durable = True


async def measure(
    repository_delay_ms: float,
    heartbeat_ms: float,
) -> tuple[float, SyntheticRepository]:
    with TemporaryDirectory(prefix="tg-download-persistence-") as temporary:
        paths = PortablePaths(Path(temporary))
        paths.ensure_layout()
        repository = SyntheticRepository(repository_delay_ms / 1000)
        persistence = DownloadPersistenceCoordinator(repository)
        item = MediaItem(
            "synthetic-item",
            "synthetic-task",
            "synthetic-peer",
            1,
            None,
            "synthetic-media",
            MediaKind.DOCUMENT,
            "synthetic.bin",
            paths.downloads / "synthetic.bin",
            20,
            datetime(2026, 8, 24, tzinfo=UTC),
        )
        downloader = MediaDownloader(
            SyntheticGateway(),
            repository,
            paths,
            free_bytes=lambda _path: 10**9,
            reserve_bytes=0,
            progress_interval=0,
            persistence=persistence,
        )
        gaps: list[float] = []
        running = True

        async def heartbeat() -> None:
            interval = heartbeat_ms / 1000
            previous = perf_counter()
            next_sample = previous + interval
            while running:
                await asyncio.sleep(0)
                current = perf_counter()
                if current < next_sample:
                    continue
                gaps.append((current - previous) * 1000)
                previous = current
                next_sample = current + interval

        pulse = asyncio.create_task(heartbeat())
        try:
            await asyncio.sleep(0)
            await downloader.download(item)
        finally:
            running = False
            await pulse
            await persistence.close()

        return max(gaps, default=0.0), repository


def positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--repository-delay-ms",
        type=positive_float,
        default=50.0,
    )
    parser.add_argument("--heartbeat-ms", type=positive_float, default=5.0)
    parser.add_argument("--max-gap-ms", type=positive_float, default=20.0)
    args = parser.parse_args()

    max_gap_ms, repository = asyncio.run(
        measure(args.repository_delay_ms, args.heartbeat_ms)
    )
    print(f"MAX_EVENT_LOOP_GAP_MS={max_gap_ms:.2f}")
    print(f"MEDIA_PERSISTENCE_WRITES={repository.media_persistence_writes}")
    print(f"LATEST_DOWNLOADED_BYTES={repository.latest_downloaded_bytes}")
    print(f"TERMINAL_DURABLE={str(repository.terminal_durable).lower()}")
    passed = (
        max_gap_ms <= args.max_gap_ms
        and repository.media_persistence_writes <= 4
        and repository.latest_downloaded_bytes == 20
        and repository.terminal_durable
    )
    print(
        "DOWNLOAD_PERSISTENCE_BENCHMARK_OK"
        if passed
        else "DOWNLOAD_PERSISTENCE_BENCHMARK_FAILED"
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
