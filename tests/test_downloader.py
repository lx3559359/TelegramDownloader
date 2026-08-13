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

    def update_item_progress(
        self,
        item_id,
        downloaded_bytes,
        status,
        error=None,
        retry_count=None,
    ):
        self.updates.append((item_id, downloaded_bytes, status))


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
    assert repo.updates[-1] == ("i", 6, ItemStatus.COMPLETED)


@pytest.mark.asyncio
async def test_refuses_download_when_space_is_below_remaining_plus_reserve(
    tmp_path: Path,
) -> None:
    paths = PortablePaths(tmp_path)
    paths.ensure_layout()
    media_downloader = MediaDownloader(
        FakeGateway([b"abcdef"]),
        FakeRepository(),
        paths,
        free_bytes=lambda _: 7,
        reserve_bytes=2,
    )

    with pytest.raises(InsufficientSpaceError):
        await media_downloader.download(item(paths.downloads / "x.mp4"))


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
    checks = iter([False, True])

    with pytest.raises(DownloadPaused):
        await downloader(paths, gateway, repo).download(
            item(target),
            should_pause=lambda: next(checks),
        )

    assert target.with_suffix(".mp4.part").read_bytes() == b"ab"
    assert repo.updates[-1] == ("i", 2, ItemStatus.PAUSED)


@pytest.mark.asyncio
async def test_size_mismatch_keeps_partial_and_never_creates_final(tmp_path: Path) -> None:
    paths = PortablePaths(tmp_path)
    paths.ensure_layout()
    target = paths.downloads / "x.mp4"

    with pytest.raises(SizeMismatchError):
        await downloader(paths, FakeGateway([b"abc"]), FakeRepository()).download(
            item(target)
        )

    assert target.with_suffix(".mp4.part").read_bytes() == b"abc"
    assert not target.exists()


@pytest.mark.asyncio
async def test_existing_verified_final_skips_network(tmp_path: Path) -> None:
    paths = PortablePaths(tmp_path)
    paths.ensure_layout()
    target = paths.downloads / "x.mp4"
    target.write_bytes(b"abcdef")
    gateway, repo = FakeGateway([]), FakeRepository()

    await downloader(paths, gateway, repo).download(item(target))

    assert gateway.calls == 0
    assert repo.updates[-1] == ("i", 6, ItemStatus.COMPLETED)


@pytest.mark.asyncio
async def test_rejects_target_outside_portable_root(tmp_path: Path) -> None:
    paths = PortablePaths(tmp_path / "root")
    paths.ensure_layout()

    with pytest.raises(PathOutsideRootError):
        await downloader(paths, FakeGateway([]), FakeRepository()).download(
            item(tmp_path / "outside.mp4")
        )
