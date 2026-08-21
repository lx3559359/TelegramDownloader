import hashlib
from pathlib import Path

import pytest

from telegram_downloader.download_io import BufferedPartWriter


class Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


@pytest.mark.asyncio
async def test_writer_batches_by_size_and_durable_flush(tmp_path: Path) -> None:
    calls: list[tuple[bytes, bool]] = []

    async def submit(path, digest, data, durable):
        calls.append((data, durable))
        with path.open("ab", buffering=0) as stream:
            stream.write(data)
        digest.update(data)
        return len(data)

    path = tmp_path / "video.part"
    writer = BufferedPartWriter(
        path,
        hashlib.sha256(),
        offset=0,
        batch_bytes=4,
        batch_interval=10.0,
        submit=submit,
    )
    await writer.append(b"ab")
    assert calls == []
    await writer.append(b"cd")
    assert calls == [(b"abcd", False)]
    await writer.append(b"ef")
    await writer.flush(durable=True)
    assert calls == [(b"abcd", False), (b"ef", True)]
    assert writer.received_bytes == writer.persisted_bytes == 6
    assert writer.durable_bytes == 6
    assert writer.hexdigest() == hashlib.sha256(b"abcdef").hexdigest()
    assert path.read_bytes() == b"abcdef"


@pytest.mark.asyncio
async def test_writer_batches_by_elapsed_time(tmp_path: Path) -> None:
    clock = Clock()
    calls: list[bytes] = []

    async def submit(path, digest, data, durable):
        calls.append(data)
        with path.open("ab", buffering=0) as stream:
            stream.write(data)
        digest.update(data)
        return len(data)

    writer = BufferedPartWriter(
        tmp_path / "video.part",
        hashlib.sha256(),
        offset=0,
        batch_bytes=1024,
        batch_interval=0.5,
        clock=clock,
        submit=submit,
    )
    await writer.append(b"a")
    clock.value = 0.5
    await writer.append(b"b")
    assert calls == [b"ab"]


@pytest.mark.asyncio
async def test_empty_durable_flush_reaches_submitter(tmp_path: Path) -> None:
    durable_values: list[bool] = []

    async def submit(_path, _digest, data, durable):
        assert data == b""
        durable_values.append(durable)
        return 0

    writer = BufferedPartWriter(
        tmp_path / "video.part",
        hashlib.sha256(),
        offset=7,
        submit=submit,
    )
    await writer.flush(durable=True)
    assert durable_values == [True]
    assert writer.persisted_bytes == 7
    assert writer.durable_bytes == 7
