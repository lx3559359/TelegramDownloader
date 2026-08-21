from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol


class Digest(Protocol):
    def update(self, data: bytes) -> None: ...

    def hexdigest(self) -> str: ...


BatchSubmit = Callable[[Path, Digest, bytes, bool], Awaitable[int]]


def append_batch(path: Path, digest: Digest, data: bytes, durable: bool) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab", buffering=0) as stream:
        if data:
            written = stream.write(data)
            if written != len(data):
                raise OSError(f"short file write: {written}/{len(data)}")
            digest.update(data)
        stream.flush()
        if durable:
            os.fsync(stream.fileno())
    return len(data)


async def submit_batch(path: Path, digest: Digest, data: bytes, durable: bool) -> int:
    return await asyncio.to_thread(append_batch, path, digest, data, durable)


class BufferedPartWriter:
    def __init__(
        self,
        path: Path,
        digest: Digest,
        *,
        offset: int,
        batch_bytes: int = 1024 * 1024,
        batch_interval: float = 0.5,
        clock: Callable[[], float] = time.monotonic,
        submit: BatchSubmit = submit_batch,
    ) -> None:
        if offset < 0 or batch_bytes <= 0 or batch_interval <= 0:
            raise ValueError("invalid buffered writer configuration")
        self.path = path
        self._digest = digest
        self._batch_bytes = batch_bytes
        self._batch_interval = batch_interval
        self._clock = clock
        self._submit = submit
        self._buffer = bytearray()
        self._last_flush = clock()
        self.received_bytes = offset
        self.persisted_bytes = offset
        self.durable_bytes = offset

    async def append(self, data: bytes) -> int:
        self._buffer.extend(data)
        self.received_bytes += len(data)
        now = self._clock()
        if (
            len(self._buffer) >= self._batch_bytes
            or now - self._last_flush >= self._batch_interval
        ):
            await self.flush()
        return self.persisted_bytes

    async def flush(self, *, durable: bool = False) -> int:
        if not self._buffer and not durable:
            return self.persisted_bytes
        data = bytes(self._buffer)
        operation = asyncio.ensure_future(
            self._submit(self.path, self._digest, data, durable)
        )
        cancellation: asyncio.CancelledError | None = None
        try:
            written = await asyncio.shield(operation)
        except asyncio.CancelledError as error:
            cancellation = error
            written = await operation
        if written != len(data):
            raise OSError(f"short batch submission: {written}/{len(data)}")
        self._buffer.clear()
        self.persisted_bytes += written
        if durable:
            self.durable_bytes = self.persisted_bytes
        self._last_flush = self._clock()
        if cancellation is not None:
            raise cancellation
        return self.persisted_bytes

    def hexdigest(self) -> str:
        if self._buffer:
            raise RuntimeError("digest requested before pending bytes were written")
        return self._digest.hexdigest()
