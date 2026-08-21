# Buffered Download I/O Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace per-Telegram-chunk file flushing with bounded background batch writes while preserving exact pause, resume, size, and SHA-256 behavior.

**Architecture:** Add a focused `BufferedPartWriter` that owns one `.part` buffer and serializes writes through an injectable async submitter. `MediaDownloader` continues to own network, bandwidth, disk-space, and repository state, but reports only confirmed bytes. Durable flushes remain mandatory for pause, cancellation, and completion.

**Tech Stack:** Python 3.12, asyncio, `asyncio.to_thread`, pathlib, hashlib, pytest, pytest-asyncio, Ruff.

---

## File map

- Create `src/telegram_downloader/download_io.py`: background append/fsync helper, batch thresholds, counters, and digest ownership.
- Create `tests/test_download_io.py`: deterministic size/time batching and durability tests.
- Modify `src/telegram_downloader/downloader.py`: replace direct per-chunk writes with the writer.
- Modify `tests/test_downloader.py`: add write-count, heartbeat, pause, cancellation, and recovery coverage.
- Modify `tests/test_download_queue_e2e.py`: verify scheduler completion through the new writer.

### Task 1: Implement deterministic buffered part writing

**Files:**
- Create: `src/telegram_downloader/download_io.py`
- Create: `tests/test_download_io.py`

- [ ] **Step 1: Write the failing size, time, and durability tests**

```python
# tests/test_download_io.py
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
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_download_io.py -q
```

Expected: collection fails because `telegram_downloader.download_io` does not exist.

- [ ] **Step 3: Add the focused writer implementation**

```python
# src/telegram_downloader/download_io.py
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
        if len(self._buffer) >= self._batch_bytes or now - self._last_flush >= self._batch_interval:
            await self.flush()
        return self.persisted_bytes

    async def flush(self, *, durable: bool = False) -> int:
        if not self._buffer and not durable:
            return self.persisted_bytes
        data = bytes(self._buffer)
        written = await self._submit(self.path, self._digest, data, durable)
        if written != len(data):
            raise OSError(f"short batch submission: {written}/{len(data)}")
        self._buffer.clear()
        self.persisted_bytes += written
        if durable:
            self.durable_bytes = self.persisted_bytes
        self._last_flush = self._clock()
        return self.persisted_bytes

    def hexdigest(self) -> str:
        if self._buffer:
            raise RuntimeError("digest requested before pending bytes were written")
        return self._digest.hexdigest()
```

- [ ] **Step 4: Run writer tests and Ruff**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_download_io.py -q
.\.venv\Scripts\python.exe -m ruff check src\telegram_downloader\download_io.py tests\test_download_io.py
```

Expected: all writer tests pass and Ruff exits 0.

- [ ] **Step 5: Commit Task 1**

```powershell
git add src/telegram_downloader/download_io.py tests/test_download_io.py
git commit -m "feat: add buffered part writer"
```

### Task 2: Integrate confirmed bytes into `MediaDownloader`

**Files:**
- Modify: `src/telegram_downloader/downloader.py:64-223`
- Modify: `tests/test_downloader.py`

- [ ] **Step 1: Add failing batching and exact-progress tests**

```python
# append to tests/test_downloader.py
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
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_downloader.py::test_download_batches_8_mib_of_64_kib_chunks tests\test_downloader.py::test_progress_never_leads_confirmed_part_size -q
```

Expected: `MediaDownloader.__init__` rejects the writer arguments.

- [ ] **Step 3: Replace direct writes with `BufferedPartWriter`**

Add these constructor fields:

```python
write_batch_bytes: int = 1024 * 1024,
write_batch_interval: float = 0.5,
batch_submit: BatchSubmit | None = None,
```

After hashing an existing partial, construct the writer with the guarded `.part` path, digest, offset, configured thresholds, and optional injected submitter. In the network loop, keep bandwidth acquisition before `await writer.append(bytes(chunk))`; use `writer.received_bytes` for overflow and free-space calculations and `writer.persisted_bytes` for repository progress. Remove direct `stream.write` and per-chunk `stream.flush`.

Use this exact durable helper in pause, cancellation, and final paths:

```python
async def persist_writer(writer: BufferedPartWriter) -> int:
    operation = asyncio.create_task(writer.flush(durable=True))
    try:
        return await asyncio.shield(operation)
    except asyncio.CancelledError:
        await operation
        raise
```

On normal completion, call `await writer.flush(durable=True)`, validate `writer.persisted_bytes`, read `writer.hexdigest()`, atomically replace the `.part`, and call `complete_item`. On pause/cancel, record the durable returned offset as `PAUSED` before raising.

- [ ] **Step 4: Run all downloader tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_download_io.py tests\test_downloader.py -q
```

Expected: all existing and new downloader tests pass.

- [ ] **Step 5: Commit Task 2**

```powershell
git add src/telegram_downloader/downloader.py tests/test_downloader.py
git commit -m "perf: batch media file writes"
```

### Task 3: Prove slow disk work does not block the event loop

**Files:**
- Modify: `tests/test_downloader.py`
- Modify: `tests/test_download_queue_e2e.py`

- [ ] **Step 1: Add a failing heartbeat regression**

```python
# append to tests/test_downloader.py
@pytest.mark.asyncio
async def test_slow_batch_write_allows_event_loop_heartbeat(tmp_path, monkeypatch) -> None:
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
```

- [ ] **Step 2: Run the heartbeat and verify GREEN against Task 2**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_downloader.py::test_slow_batch_write_allows_event_loop_heartbeat -q
```

Expected: PASS with heartbeat count above 10. If it fails, verify `submit_batch` resolves `append_batch` inside the worker call instead of binding a stale function default.

- [ ] **Step 3: Extend the queue E2E assertions**

After the existing queue reaches idle in `tests/test_download_queue_e2e.py`, assert every completed item has a final file, exact expected size, non-empty SHA-256, and no remaining `.part`.

- [ ] **Step 4: Run download and scheduler regression**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_download_io.py tests\test_downloader.py tests\test_scheduler.py tests\test_download_queue_e2e.py tests\test_download_queue_stress.py -q
.\.venv\Scripts\python.exe -m ruff check src\telegram_downloader\download_io.py src\telegram_downloader\downloader.py tests\test_download_io.py tests\test_downloader.py tests\test_download_queue_e2e.py
```

Expected: all selected tests pass and Ruff exits 0.

- [ ] **Step 5: Commit Task 3**

```powershell
git add tests/test_downloader.py tests/test_download_queue_e2e.py
git commit -m "test: verify responsive buffered downloads"
```

### Task 4: Record download performance evidence

**Files:**
- Create: `docs/verification/2026-08-21-buffered-download-io.md`

- [ ] **Step 1: Run fresh verification**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_download_io.py tests\test_downloader.py tests\test_scheduler.py tests\test_download_queue_e2e.py tests\test_download_queue_stress.py -q --durations=10
.\.venv\Scripts\python.exe -m ruff check src tests
git status --short
```

Expected: tests and Ruff pass; only the verification record is untracked while it is being written.

- [ ] **Step 2: Write exact observed evidence**

Record the commands, pass count, duration, eight data-batch writes for the deterministic 8 MiB case, heartbeat result, commit SHA, and worktree status. Record no Telegram identifiers, real filenames, credentials, or absolute user paths.

- [ ] **Step 3: Commit the verification record**

```powershell
git add docs/verification/2026-08-21-buffered-download-io.md
git commit -m "docs: verify buffered download io"
```
