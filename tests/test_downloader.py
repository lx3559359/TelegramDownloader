import asyncio
import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from telegram_downloader.domain import ItemStatus, MediaItem, MediaKind
from telegram_downloader.downloader import (
    DownloadPaused,
    InsufficientSpaceError,
    MediaDownloader,
    SizeMismatchError,
)
from telegram_downloader.paths import PathOutsideRootError, PortablePaths


class FakeGateway:
    def __init__(self, chunks):
        self.chunks = chunks
        self.offset = None
        self.calls = 0

    async def stream_media(self, peer_ref, message_id, offset):
        self.calls += 1
        self.offset = offset
        for chunk in self.chunks:
            yield chunk


class FakeRepository:
    def __init__(self):
        self.updates = []
        self.completed = []

    def update_item_progress(
        self,
        item_id,
        downloaded_bytes,
        status,
        error=None,
        retry_count=None,
    ):
        self.updates.append((item_id, downloaded_bytes, status))

    def complete_item(self, item_id, downloaded_bytes, sha256, verified_at):
        self.completed.append((item_id, downloaded_bytes, sha256, verified_at))


class RecordingBandwidth:
    def __init__(self, part: Path) -> None:
        self.part = part
        self.byte_counts: list[int] = []
        self.part_sizes: list[int] = []

    async def acquire(self, byte_count: int) -> None:
        self.byte_counts.append(byte_count)
        self.part_sizes.append(self.part.stat().st_size if self.part.exists() else 0)


class CancellingBandwidth:
    def __init__(self) -> None:
        self.calls = 0

    async def acquire(self, _byte_count: int) -> None:
        self.calls += 1
        if self.calls == 2:
            raise asyncio.CancelledError


class PausingBandwidth:
    def __init__(self, pause) -> None:
        self.pause = pause

    async def acquire(self, _byte_count: int) -> None:
        self.pause()


class BlockingBandwidth:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def acquire(self, _byte_count: int) -> None:
        self.started.set()
        await self.release.wait()


def item(target: Path, size: int | None = 6) -> MediaItem:
    return MediaItem(
        "i",
        "t",
        "peer",
        7,
        None,
        "m",
        MediaKind.VIDEO,
        "x.mp4",
        target,
        size,
        datetime(2026, 8, 13, tzinfo=UTC),
    )


def downloader(paths, gateway, repository, **kwargs) -> MediaDownloader:
    return MediaDownloader(
        gateway,
        repository,
        paths,
        free_bytes=lambda _: 10**9,
        reserve_bytes=0,
        progress_interval=0,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_resumes_from_part_size_and_atomically_finishes(tmp_path: Path) -> None:
    paths = PortablePaths(tmp_path)
    paths.ensure_layout()
    target = paths.downloads / "x.mp4"
    target.with_suffix(".mp4.part").write_bytes(b"ab")
    gateway, repo = FakeGateway([b"cd", b"ef"]), FakeRepository()

    result = await downloader(paths, gateway, repo).download(item(target))

    assert result == target
    assert gateway.offset == 2
    assert target.read_bytes() == b"abcdef"
    assert not target.with_suffix(".mp4.part").exists()
    assert repo.completed[0][0:3] == (
        "i",
        6,
        hashlib.sha256(b"abcdef").hexdigest(),
    )
    assert repo.completed[0][3].tzinfo is not None


@pytest.mark.asyncio
async def test_refuses_download_when_space_is_below_remaining_plus_reserve(
    tmp_path: Path,
) -> None:
    paths = PortablePaths(tmp_path)
    paths.ensure_layout()
    repository = FakeRepository()
    media_downloader = MediaDownloader(
        FakeGateway([b"abcdef"]),
        repository,
        paths,
        free_bytes=lambda _: 7,
        reserve_bytes=2,
    )

    with pytest.raises(InsufficientSpaceError):
        await media_downloader.download(item(paths.downloads / "x.mp4"))
    assert repository.updates[-1] == ("i", 0, ItemStatus.PAUSED)


@pytest.mark.asyncio
async def test_oversized_partial_is_preserved_and_restarted(tmp_path: Path) -> None:
    paths = PortablePaths(tmp_path)
    paths.ensure_layout()
    target = paths.downloads / "x.mp4"
    part = target.with_suffix(".mp4.part")
    part.write_bytes(b"too-large")
    gateway, repo = FakeGateway([b"abcdef"]), FakeRepository()

    await downloader(paths, gateway, repo).download(item(target))

    assert gateway.offset == 0
    assert target.read_bytes() == b"abcdef"
    assert target.with_suffix(".mp4.part.corrupt").read_bytes() == b"too-large"


@pytest.mark.asyncio
async def test_pause_retains_partial_and_persists_exact_offset(tmp_path: Path) -> None:
    paths = PortablePaths(tmp_path)
    paths.ensure_layout()
    target = paths.downloads / "x.mp4"
    gateway, repo = FakeGateway([b"ab", b"cd"]), FakeRepository()
    checks = iter([False, False, False, False, True])

    with pytest.raises(DownloadPaused):
        await downloader(paths, gateway, repo).download(
            item(target),
            should_pause=lambda: next(checks),
        )

    assert target.with_suffix(".mp4.part").read_bytes() == b"ab"
    assert repo.updates[-1] == ("i", 2, ItemStatus.PAUSED)
    assert repo.completed == []


@pytest.mark.asyncio
async def test_size_mismatch_keeps_partial_and_never_creates_final(tmp_path: Path) -> None:
    paths = PortablePaths(tmp_path)
    paths.ensure_layout()
    target = paths.downloads / "x.mp4"

    repo = FakeRepository()
    with pytest.raises(SizeMismatchError):
        await downloader(paths, FakeGateway([b"abc"]), repo).download(item(target))

    assert target.with_suffix(".mp4.part").read_bytes() == b"abc"
    assert not target.exists()
    assert repo.completed == []


@pytest.mark.asyncio
async def test_existing_verified_final_skips_network(tmp_path: Path) -> None:
    paths = PortablePaths(tmp_path)
    paths.ensure_layout()
    target = paths.downloads / "x.mp4"
    target.write_bytes(b"abcdef")
    gateway, repo = FakeGateway([]), FakeRepository()

    await downloader(paths, gateway, repo).download(item(target))

    assert gateway.calls == 0
    assert repo.completed[0][0:3] == (
        "i",
        6,
        hashlib.sha256(b"abcdef").hexdigest(),
    )


@pytest.mark.asyncio
async def test_fresh_download_records_sha256(tmp_path: Path) -> None:
    paths = PortablePaths(tmp_path)
    paths.ensure_layout()
    target = paths.downloads / "x.mp4"
    gateway, repo = FakeGateway([b"abc", b"def"]), FakeRepository()

    await downloader(paths, gateway, repo).download(item(target))

    assert repo.completed[0][0:3] == (
        "i",
        6,
        hashlib.sha256(b"abcdef").hexdigest(),
    )


@pytest.mark.asyncio
async def test_pause_during_bandwidth_wait_does_not_write_the_released_chunk(
    tmp_path: Path,
) -> None:
    paths = PortablePaths(tmp_path)
    paths.ensure_layout()
    target = paths.downloads / "x.mp4"
    repository = FakeRepository()
    paused = False

    def request_pause() -> None:
        nonlocal paused
        paused = True

    media_downloader = downloader(
        paths,
        FakeGateway([b"abcdef"]),
        repository,
        bandwidth=PausingBandwidth(request_pause),
    )

    with pytest.raises(DownloadPaused):
        await media_downloader.download(item(target), should_pause=lambda: paused)

    assert target.with_suffix(".mp4.part").read_bytes() == b""
    assert repository.updates[-1] == ("i", 0, ItemStatus.PAUSED)


@pytest.mark.asyncio
async def test_pause_interrupts_a_blocked_bandwidth_wait(tmp_path: Path) -> None:
    paths = PortablePaths(tmp_path)
    paths.ensure_layout()
    target = paths.downloads / "x.mp4"
    repository = FakeRepository()
    bandwidth = BlockingBandwidth()
    paused = False
    media_downloader = downloader(
        paths,
        FakeGateway([b"abcdef"]),
        repository,
        bandwidth=bandwidth,
    )
    operation = asyncio.create_task(
        media_downloader.download(item(target), should_pause=lambda: paused)
    )
    await bandwidth.started.wait()

    paused = True

    with pytest.raises(DownloadPaused):
        await asyncio.wait_for(operation, timeout=0.5)
    assert target.with_suffix(".mp4.part").read_bytes() == b""
    assert repository.updates[-1] == ("i", 0, ItemStatus.PAUSED)


@pytest.mark.asyncio
async def test_downloader_accounts_every_chunk_before_writing(tmp_path: Path) -> None:
    paths = PortablePaths(tmp_path)
    paths.ensure_layout()
    target = paths.downloads / "x.mp4"
    part = target.with_suffix(".mp4.part")
    bandwidth = RecordingBandwidth(part)
    repo = FakeRepository()

    await downloader(
        paths,
        FakeGateway([b"abc", b"de"]),
        repo,
        bandwidth=bandwidth,
        write_batch_bytes=3,
    ).download(item(target, size=5))

    assert bandwidth.byte_counts == [3, 2]
    assert bandwidth.part_sizes == [0, 3]
    assert target.read_bytes() == b"abcde"


@pytest.mark.asyncio
async def test_limiter_cancellation_preserves_partial_progress(tmp_path: Path) -> None:
    paths = PortablePaths(tmp_path)
    paths.ensure_layout()
    target = paths.downloads / "x.mp4"
    repo = FakeRepository()

    with pytest.raises(asyncio.CancelledError):
        await downloader(
            paths,
            FakeGateway([b"ab", b"cd"]),
            repo,
            bandwidth=CancellingBandwidth(),
        ).download(item(target, size=4))

    assert target.with_suffix(".mp4.part").read_bytes() == b"ab"
    assert not target.exists()
    assert repo.updates[-1] == ("i", 2, ItemStatus.PAUSED)
    assert repo.completed == []


@pytest.mark.asyncio
async def test_rejects_target_outside_portable_root(tmp_path: Path) -> None:
    paths = PortablePaths(tmp_path / "root")
    paths.ensure_layout()

    with pytest.raises(PathOutsideRootError):
        await downloader(paths, FakeGateway([]), FakeRepository()).download(
            item(tmp_path / "outside.mp4")
        )


@pytest.mark.asyncio
async def test_download_batches_8_mib_of_64_kib_chunks(tmp_path: Path) -> None:
    paths = PortablePaths(tmp_path)
    paths.ensure_layout()
    target = paths.downloads / "large.bin"
    chunk = b"x" * (64 * 1024)
    calls: list[tuple[int, bool]] = []

    async def submit(path, digest, data, durable):
        calls.append((len(data), durable))
        with path.open("ab", buffering=0) as stream:
            stream.write(data)
        digest.update(data)
        return len(data)

    repo = FakeRepository()
    media = MediaDownloader(
        FakeGateway([chunk] * 128),
        repo,
        paths,
        free_bytes=lambda _: 10**9,
        reserve_bytes=0,
        progress_interval=0,
        write_batch_bytes=1024 * 1024,
        write_batch_interval=60.0,
        batch_submit=submit,
    )
    await media.download(item(target, size=8 * 1024 * 1024))
    assert len([value for value in calls if value[0]]) == 8
    assert calls[-1][1] is True
    assert repo.completed[0][1] == 8 * 1024 * 1024
    assert repo.completed[0][2] == hashlib.sha256(chunk * 128).hexdigest()


@pytest.mark.asyncio
async def test_progress_never_leads_confirmed_part_size(tmp_path: Path) -> None:
    paths = PortablePaths(tmp_path)
    paths.ensure_layout()
    target = paths.downloads / "x.mp4"
    repo = FakeRepository()
    media = MediaDownloader(
        FakeGateway([b"ab", b"cd"]),
        repo,
        paths,
        free_bytes=lambda _: 10**9,
        reserve_bytes=0,
        progress_interval=0,
        write_batch_bytes=4,
    )
    await media.download(item(target, size=4))
    assert all(value <= 4 for _item, value, _status in repo.updates)
    assert repo.updates[-1][1] == 4


@pytest.mark.asyncio
async def test_slow_batch_write_allows_event_loop_heartbeat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import time

    from telegram_downloader import download_io

    paths = PortablePaths(tmp_path)
    paths.ensure_layout()
    target = paths.downloads / "heartbeat.bin"
    real_append = download_io.append_batch
    ticks = 0
    running = True

    def slow_append(path, digest, data, durable):
        time.sleep(0.05)
        return real_append(path, digest, data, durable)

    async def heartbeat():
        nonlocal ticks
        while running:
            ticks += 1
            await asyncio.sleep(0)

    monkeypatch.setattr(download_io, "append_batch", slow_append)
    pulse = asyncio.create_task(heartbeat())
    try:
        await MediaDownloader(
            FakeGateway([b"x" * 1024] * 4),
            FakeRepository(),
            paths,
            free_bytes=lambda _: 10**9,
            reserve_bytes=0,
            write_batch_bytes=1024,
        ).download(item(target, size=4096))
    finally:
        running = False
        await pulse
    assert ticks > 10
