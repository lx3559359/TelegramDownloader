# Windows Telegram Downloader Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build, package, and formally publish a Simplified-Chinese Windows desktop application that downloads single Telegram posts or filtered channel/group media, ships as both a portable ZIP and a non-C-drive installer, performs signed rollback-safe online updates, and keeps every application-managed write under the project/application directory.

**Architecture:** A path bootstrap runs before any GUI or Telegram import, then composes a PySide6/qasync interface with isolated domain services, a Telethon gateway, an SQLite repository, a chunked download scheduler, and a dual-source update coordinator. A separately packaged helper applies the same signed runtime ZIP to portable and installed editions as a transaction, health-checking the replacement and rolling back on failure. Every external boundary is expressed as a protocol so login, scan, retry, resume, update, rollback, installer, and release flows can be tested without real Telegram credentials or network mutation.

**Tech Stack:** Python 3.12, PySide6 6.11.1, Telethon 1.44.0, qasync 0.28.0, python-socks 2.8.2, cryptography 49.0.0, SQLite, Windows DPAPI, pytest 9.1.1, pytest-asyncio 1.4.0, pytest-qt 4.5.0, Ruff 0.15.22, PyInstaller 6.21.0, Inno Setup 7 x64, GitHub CLI/API, ModelScope Hub

---

## File map

```text
pyproject.toml                         Project metadata, pytest and Ruff settings
requirements.txt                      Pinned runtime dependencies
requirements-dev.txt                  Pinned test/build dependencies
TelegramDownloader.spec               PyInstaller onedir recipe for app and update helper
installer/TelegramDownloader.iss      Current-user non-C-drive Inno Setup recipe
README.md                              Chinese user and build guide
scripts/setup-dev.ps1                  Project-local virtual environment bootstrap
scripts/test.ps1                       Local-cache test entry point
scripts/build.ps1                      Reproducible onedir build and ZIP creation
scripts/build-installer.ps1            Reproducible installer build and validation
scripts/release/generate_manifest.py   Canonical manifest, SHA-256, and Ed25519 signature
scripts/release/publish_github.py      Draft release upload, verification, and promotion
scripts/release/publish_modelscope.py  Candidate asset upload, verification, and promotion
scripts/release/release.ps1            Fail-closed two-platform formal release transaction
scripts/smoke.ps1                      Packaged executable path/self-test
src/telegram_downloader/bootstrap.py   Pre-import runtime-root and TEMP/TMP bootstrap
src/telegram_downloader/paths.py       Portable directory registry and containment guard
src/telegram_downloader/domain.py      Enums and immutable task/media value objects
src/telegram_downloader/links.py       Telegram URL parsing and normalization
src/telegram_downloader/files.py       Media classification and safe archive paths
src/telegram_downloader/repository.py  SQLite schema and persistence operations
src/telegram_downloader/security.py    DPAPI and encrypted secrets storage
src/telegram_downloader/settings.py    Atomic non-secret JSON settings
src/telegram_downloader/logging.py     Rotating project-local redacted logging
src/telegram_downloader/gateway.py     Telegram protocol, errors, and Telethon adapter
src/telegram_downloader/planner.py     Scan preview and task creation
src/telegram_downloader/downloader.py  Chunked .part writer and byte-offset resume
src/telegram_downloader/scheduler.py   Concurrency, retries, FloodWait, and recovery
src/telegram_downloader/update_contract.py Strict release manifest parsing and verification
src/telegram_downloader/update_sources.py  GitHub/ModelScope source adapters and reconciliation
src/telegram_downloader/update_download.py Resumable, hash-checked package download
src/telegram_downloader/update.py          Startup checks and update orchestration
src/telegram_downloader/update_helper.py   External replace, health-check, and rollback transaction
src/telegram_downloader/ui/theme.py    Dark workbench stylesheet
src/telegram_downloader/ui/models.py   Qt task table model
src/telegram_downloader/ui/main.py     Main workbench widgets and user intents
src/telegram_downloader/ui/login.py    API/phone/code/password login flow
src/telegram_downloader/ui/settings.py Proxy and concurrency settings dialog
src/telegram_downloader/controller.py  Async orchestration between UI and services
src/telegram_downloader/app.py         Composition root and qasync event loop
src/telegram_downloader/__main__.py    Bootstrap-first executable entry point
tests/                                 Unit, integration, GUI, update, installer, release, and packaging tests
```

### Task 1: Project-local toolchain and bootstrap-first entry

**Files:**
- Create: `pyproject.toml`
- Create: `requirements.txt`
- Create: `requirements-dev.txt`
- Create: `scripts/setup-dev.ps1`
- Create: `src/telegram_downloader/__init__.py`
- Create: `src/telegram_downloader/bootstrap.py`
- Test: `tests/test_bootstrap.py`

- [x] **Step 1: Write the failing bootstrap tests**

```python
# tests/test_bootstrap.py
from pathlib import Path

from telegram_downloader.bootstrap import configure_process, resolve_runtime_root


def test_source_runtime_root_is_repository_root(tmp_path: Path) -> None:
    module_file = tmp_path / "src" / "telegram_downloader" / "bootstrap.py"
    assert resolve_runtime_root(False, tmp_path / "ignored.exe", module_file) == tmp_path


def test_frozen_runtime_root_is_executable_parent(tmp_path: Path) -> None:
    exe = tmp_path / "portable" / "TelegramDownloader.exe"
    assert resolve_runtime_root(True, exe, tmp_path / "ignored.py") == exe.parent


def test_configure_process_redirects_temp_and_creates_only_local_dirs(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("TEMP", raising=False)
    monkeypatch.delenv("TMP", raising=False)
    configure_process(tmp_path)
    assert Path(str(__import__("os").environ["TEMP"])) == tmp_path / "data" / "temp"
    assert Path(str(__import__("os").environ["TMP"])) == tmp_path / "data" / "temp"
    assert Path(str(__import__("os").environ["APPDATA"])) == tmp_path / "data" / "user-profile" / "Roaming"
    assert Path(str(__import__("os").environ["LOCALAPPDATA"])) == tmp_path / "data" / "user-profile" / "Local"
    assert (tmp_path / "data" / "temp").is_dir()
    assert (tmp_path / "downloads").is_dir()
```

- [x] **Step 2: Run the focused test and confirm the expected failure**

Run: `python -m pytest tests/test_bootstrap.py -q`

Expected: collection fails with `ModuleNotFoundError: No module named 'telegram_downloader'`.

- [x] **Step 3: Add pinned dependencies, local setup, and bootstrap implementation**

```toml
# pyproject.toml
[build-system]
requires = ["setuptools>=75"]
build-backend = "setuptools.build_meta"

[project]
name = "telegram-portable-downloader"
version = "0.1.0"
requires-python = ">=3.12,<3.13"

[tool.pytest.ini_options]
pythonpath = ["src"]
testpaths = ["tests"]
asyncio_mode = "auto"

[tool.ruff]
target-version = "py312"
line-length = 100

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "SIM"]
```

```text
# requirements.txt
PySide6==6.11.1
Telethon==1.44.0
qasync==0.28.0
python-socks[asyncio]==2.8.2
cryptography==49.0.0
```

```text
# requirements-dev.txt
-r requirements.txt
pytest==9.1.1
pytest-asyncio==1.4.0
pytest-qt==4.5.0
ruff==0.15.22
pyinstaller==6.21.0
```

```powershell
# scripts/setup-dev.ps1
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$buildTemp = Join-Path $projectRoot '.build-temp'
$pipCache = Join-Path $projectRoot '.tool-cache\pip'
New-Item -ItemType Directory -Force -Path $buildTemp, $pipCache | Out-Null
$env:TEMP = $buildTemp
$env:TMP = $buildTemp
$env:PIP_CACHE_DIR = $pipCache
$venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPython)) {
    py -3.12 -m venv (Join-Path $projectRoot '.venv')
}
& $venvPython -m pip install --disable-pip-version-check -r (Join-Path $projectRoot 'requirements-dev.txt')
```

```python
# src/telegram_downloader/bootstrap.py
from __future__ import annotations

import os
import sys
from pathlib import Path


def resolve_runtime_root(frozen: bool, executable: Path, module_file: Path) -> Path:
    candidate = executable.parent if frozen else module_file.parents[2]
    return candidate.resolve()


def runtime_root() -> Path:
    return resolve_runtime_root(
        bool(getattr(sys, "frozen", False)), Path(sys.executable), Path(__file__)
    )


def configure_process(root: Path) -> Path:
    root = root.resolve()
    temp = root / "data" / "temp"
    roaming = root / "data" / "user-profile" / "Roaming"
    local = root / "data" / "user-profile" / "Local"
    for directory in (temp, roaming, local, root / "downloads"):
        directory.mkdir(parents=True, exist_ok=True)
    os.environ["TEMP"] = str(temp)
    os.environ["TMP"] = str(temp)
    os.environ["APPDATA"] = str(roaming)
    os.environ["LOCALAPPDATA"] = str(local)
    return root
```

Create an empty `src/telegram_downloader/__init__.py` containing only `__version__ = "0.1.0"`.

- [x] **Step 4: Install locally and run the bootstrap tests**

Run: `powershell -ExecutionPolicy Bypass -File scripts/setup-dev.ps1`

Expected: `.venv`, `.build-temp`, and `.tool-cache/pip` are created under the repository; dependency installation exits 0.

Run: `.venv\Scripts\python.exe -m pytest tests/test_bootstrap.py -q`

Expected: `3 passed`.

- [x] **Step 5: Commit the bootstrap slice**

```powershell
git add pyproject.toml requirements.txt requirements-dev.txt scripts/setup-dev.ps1 src/telegram_downloader tests/test_bootstrap.py
git commit -m "build: bootstrap project-local Python environment"
```

### Task 2: Portable path registry and containment guard

**Files:**
- Create: `src/telegram_downloader/paths.py`
- Test: `tests/test_paths.py`

- [x] **Step 1: Write containment and directory-layout tests**

```python
# tests/test_paths.py
from pathlib import Path

import pytest

from telegram_downloader.paths import PathOutsideRootError, PortablePaths


def test_ensure_layout_creates_every_managed_directory(tmp_path: Path) -> None:
    paths = PortablePaths(tmp_path)
    paths.ensure_layout()
    assert paths.settings == tmp_path / "data" / "config" / "settings.json"
    assert paths.secrets == tmp_path / "data" / "config" / "secrets.dat"
    assert paths.database == tmp_path / "data" / "database" / "tasks.sqlite3"
    assert paths.log == tmp_path / "data" / "logs" / "app.log"
    assert paths.cache.is_dir()
    assert paths.temp.is_dir()
    assert paths.downloads.is_dir()


def test_guard_accepts_child_and_rejects_parent_escape(tmp_path: Path) -> None:
    paths = PortablePaths(tmp_path)
    assert paths.guard(tmp_path / "downloads" / "ok.bin") == (
        tmp_path / "downloads" / "ok.bin"
    ).resolve()
    with pytest.raises(PathOutsideRootError):
        paths.guard(tmp_path / ".." / "outside.bin")
```

- [x] **Step 2: Run the path tests and observe the missing module failure**

Run: `.venv\Scripts\python.exe -m pytest tests/test_paths.py -q`

Expected: collection fails with `ModuleNotFoundError: telegram_downloader.paths`.

- [x] **Step 3: Implement one immutable registry for every writable path**

```python
# src/telegram_downloader/paths.py
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class PathOutsideRootError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PortablePaths:
    root: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", self.root.resolve())

    @property
    def data(self) -> Path:
        return self.root / "data"

    @property
    def settings(self) -> Path:
        return self.data / "config" / "settings.json"

    @property
    def secrets(self) -> Path:
        return self.data / "config" / "secrets.dat"

    @property
    def database(self) -> Path:
        return self.data / "database" / "tasks.sqlite3"

    @property
    def log(self) -> Path:
        return self.data / "logs" / "app.log"

    @property
    def cache(self) -> Path:
        return self.data / "cache"

    @property
    def temp(self) -> Path:
        return self.data / "temp"

    @property
    def downloads(self) -> Path:
        return self.root / "downloads"

    def guard(self, candidate: Path) -> Path:
        resolved = candidate.resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise PathOutsideRootError(f"路径超出便携目录: {resolved}") from exc
        return resolved

    def ensure_layout(self) -> None:
        directories = {
            self.settings.parent,
            self.database.parent,
            self.log.parent,
            self.cache,
            self.temp,
            self.downloads,
        }
        for directory in directories:
            self.guard(directory).mkdir(parents=True, exist_ok=True)
```

- [x] **Step 4: Run the focused tests**

Run: `.venv\Scripts\python.exe -m pytest tests/test_paths.py -q`

Expected: `2 passed`.

- [x] **Step 5: Commit the portable path boundary**

```powershell
git add src/telegram_downloader/paths.py tests/test_paths.py
git commit -m "feat: enforce portable write paths"
```

### Task 3: Domain records, Telegram link parsing, and archive naming

**Files:**
- Create: `src/telegram_downloader/domain.py`
- Create: `src/telegram_downloader/links.py`
- Create: `src/telegram_downloader/files.py`
- Test: `tests/test_links.py`
- Test: `tests/test_files.py`

- [x] **Step 1: Write parsing, classification, and filename tests**

```python
# tests/test_links.py
import pytest

from telegram_downloader.domain import SourceKind
from telegram_downloader.links import InvalidTelegramLink, parse_telegram_link


@pytest.mark.parametrize(
    ("url", "kind", "message_id"),
    [
        ("https://t.me/example/42", SourceKind.SINGLE_MESSAGE, 42),
        ("https://t.me/c/123456/99", SourceKind.SINGLE_MESSAGE, 99),
        ("https://t.me/example", SourceKind.CHANNEL_OR_GROUP, None),
        ("https://t.me/+AbCdEf123", SourceKind.CHANNEL_OR_GROUP, None),
    ],
)
def test_parse_supported_links(url, kind, message_id) -> None:
    parsed = parse_telegram_link(url)
    assert parsed.kind is kind
    assert parsed.message_id == message_id


def test_rejects_non_telegram_host() -> None:
    with pytest.raises(InvalidTelegramLink):
        parse_telegram_link("https://example.com/channel/1")
```

```python
# tests/test_files.py
from datetime import datetime, timezone
from pathlib import Path

from telegram_downloader.domain import MediaKind
from telegram_downloader.files import archive_target, classify_media, disambiguate_target, sanitize_component


def test_sanitize_windows_reserved_and_illegal_names() -> None:
    assert sanitize_component("CON") == "_CON_"
    assert sanitize_component('bad<>:"/\\|?*name') == "bad_________name"


def test_classify_archive_before_generic_document() -> None:
    assert classify_media("application/zip", "backup.zip", False, False, False) is MediaKind.ARCHIVE
    assert classify_media("audio/ogg", "voice.ogg", False, True, False) is MediaKind.VOICE


def test_archive_target_uses_source_month_kind_and_message_id(tmp_path: Path) -> None:
    target = archive_target(
        tmp_path,
        "My:Channel",
        datetime(2026, 8, 13, tzinfo=timezone.utc),
        MediaKind.VIDEO,
        "clip?.mp4",
    )
    assert target == tmp_path / "My_Channel" / "2026-08" / "video" / "clip_.mp4"
    assert disambiguate_target(target, 42).name == "clip__42.mp4"
```

- [x] **Step 2: Run the tests and confirm missing domain modules**

Run: `.venv\Scripts\python.exe -m pytest tests/test_links.py tests/test_files.py -q`

Expected: collection fails because `telegram_downloader.domain` does not exist.

- [x] **Step 3: Implement stable enums, records, parsers, and Windows-safe paths**

```python
# src/telegram_downloader/domain.py
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path


class SourceKind(StrEnum):
    SINGLE_MESSAGE = "single_message"
    CHANNEL_OR_GROUP = "channel_or_group"


class MediaKind(StrEnum):
    PHOTO = "photo"
    VIDEO = "video"
    AUDIO = "audio"
    VOICE = "voice"
    DOCUMENT = "document"
    ARCHIVE = "archive"


class TaskStatus(StrEnum):
    DRAFT = "draft"
    SCANNING = "scanning"
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    PAUSED = "paused"
    WAITING_RETRY = "waiting_retry"
    COMPLETED = "completed"
    PARTIAL_FAILURE = "partial_failure"


class ItemStatus(StrEnum):
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    PAUSED = "paused"
    WAITING_RETRY = "waiting_retry"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ParsedLink:
    normalized_url: str
    entity_ref: str
    kind: SourceKind
    message_id: int | None


@dataclass(frozen=True, slots=True)
class ScanFilters:
    date_from_utc: datetime
    date_to_utc: datetime
    media_kinds: frozenset[MediaKind]
    item_limit: int


@dataclass(frozen=True, slots=True)
class TaskRecord:
    id: str
    source_kind: SourceKind
    source_ref: str
    source_title: str
    source_url: str
    filters: ScanFilters
    status: TaskStatus
    created_at: datetime
    updated_at: datetime
    last_error: str | None = None


@dataclass(frozen=True, slots=True)
class MediaItem:
    id: str
    task_id: str
    peer_ref: str
    message_id: int
    grouped_id: int | None
    media_id: str
    media_kind: MediaKind
    original_name: str
    target_path: Path
    expected_size: int | None
    message_date_utc: datetime
    downloaded_bytes: int = 0
    status: ItemStatus = ItemStatus.QUEUED
    retry_count: int = 0
    last_error: str | None = None
```

```python
# src/telegram_downloader/links.py
from __future__ import annotations

import re
from urllib.parse import urlparse

from telegram_downloader.domain import ParsedLink, SourceKind


class InvalidTelegramLink(ValueError):
    pass


_PUBLIC = re.compile(r"^/(?P<slug>[A-Za-z0-9_]{4,})(?:/(?P<message>\d+))?/?$")
_PRIVATE = re.compile(r"^/c/(?P<slug>\d+)(?:/(?P<message>\d+))?/?$")
_INVITE = re.compile(r"^/\+(?P<slug>[A-Za-z0-9_-]+)/?$")


def parse_telegram_link(value: str) -> ParsedLink:
    parsed = urlparse(value.strip())
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"t.me", "www.t.me"}:
        raise InvalidTelegramLink("请输入有效的 t.me 链接")
    match = _PRIVATE.match(parsed.path) or _PUBLIC.match(parsed.path) or _INVITE.match(parsed.path)
    if not match:
        raise InvalidTelegramLink("不支持此 Telegram 链接格式")
    message_text = match.groupdict().get("message")
    message_id = int(message_text) if message_text else None
    kind = SourceKind.SINGLE_MESSAGE if message_id is not None else SourceKind.CHANNEL_OR_GROUP
    normalized = f"https://t.me{parsed.path.rstrip('/')}"
    return ParsedLink(normalized, normalized, kind, message_id)
```

```python
# src/telegram_downloader/files.py
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from telegram_downloader.domain import MediaKind


_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
_ARCHIVE_EXTENSIONS = {".zip", ".7z", ".rar", ".tar", ".gz", ".bz2", ".xz"}


def sanitize_component(value: str, maximum: int = 120) -> str:
    cleaned = _ILLEGAL.sub("_", value).strip().rstrip(".") or "unnamed"
    stem = cleaned.split(".", 1)[0].upper()
    if stem in _RESERVED:
        cleaned = f"_{cleaned}_"
    return cleaned[:maximum].rstrip(" .") or "unnamed"


def classify_media(mime: str | None, name: str, photo: bool, voice: bool, video: bool) -> MediaKind:
    suffix = Path(name).suffix.lower()
    if suffix in _ARCHIVE_EXTENSIONS:
        return MediaKind.ARCHIVE
    if photo:
        return MediaKind.PHOTO
    if voice:
        return MediaKind.VOICE
    if video or (mime or "").startswith("video/"):
        return MediaKind.VIDEO
    if (mime or "").startswith("audio/"):
        return MediaKind.AUDIO
    return MediaKind.DOCUMENT


def archive_target(root: Path, source_title: str, date: datetime, kind: MediaKind, name: str) -> Path:
    safe_source = sanitize_component(source_title)
    original = Path(sanitize_component(name))
    return root / safe_source / date.strftime("%Y-%m") / kind.value / original.name


def disambiguate_target(target: Path, message_id: int) -> Path:
    return target.with_name(f"{target.stem}_{message_id}{target.suffix}")
```

- [x] **Step 4: Run domain tests and lint the new modules**

Run: `.venv\Scripts\python.exe -m pytest tests/test_links.py tests/test_files.py -q`

Expected: `7 passed`.

Run: `.venv\Scripts\ruff.exe check src/telegram_downloader/domain.py src/telegram_downloader/links.py src/telegram_downloader/files.py tests/test_links.py tests/test_files.py`

Expected: `All checks passed!`.

- [x] **Step 5: Commit the domain slice**

```powershell
git add src/telegram_downloader/domain.py src/telegram_downloader/links.py src/telegram_downloader/files.py tests/test_links.py tests/test_files.py
git commit -m "feat: parse Telegram sources and build archive paths"
```

### Task 4: SQLite task repository and crash recovery

**Files:**
- Create: `src/telegram_downloader/repository.py`
- Test: `tests/test_repository.py`

- [ ] **Step 1: Write repository round-trip and recovery tests**

```python
# tests/test_repository.py
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from telegram_downloader.domain import ItemStatus, MediaItem, MediaKind, ScanFilters, SourceKind, TaskRecord, TaskStatus
from telegram_downloader.repository import TaskRepository


def records(tmp_path: Path) -> tuple[TaskRecord, MediaItem]:
    now = datetime(2026, 8, 13, tzinfo=timezone.utc)
    filters = ScanFilters(now, now, frozenset({MediaKind.VIDEO}), 10)
    task = TaskRecord("task-1", SourceKind.CHANNEL_OR_GROUP, "peer", "频道", "https://t.me/peer", filters, TaskStatus.QUEUED, now, now)
    item = MediaItem("item-1", task.id, "peer", 7, None, "media-7", MediaKind.VIDEO, "x.mp4", tmp_path / "x.mp4", 8, now)
    return task, item


def test_round_trip_and_unique_source_item(tmp_path: Path) -> None:
    repo = TaskRepository(tmp_path / "tasks.sqlite3")
    repo.initialize()
    task, item = records(tmp_path)
    repo.create_task(task, [item])
    repo.update_item_progress(item.id, 4, ItemStatus.DOWNLOADING)
    assert repo.get_task(task.id) == task
    assert repo.list_items(task.id)[0].downloaded_bytes == 4
    assert repo.insert_item_if_new(replace(item, id="duplicate")) is False


def test_recover_interrupted_work(tmp_path: Path) -> None:
    repo = TaskRepository(tmp_path / "tasks.sqlite3")
    repo.initialize()
    task, item = records(tmp_path)
    repo.create_task(replace(task, status=TaskStatus.DOWNLOADING), [replace(item, status=ItemStatus.DOWNLOADING)])
    repo.recover_interrupted()
    assert repo.get_task(task.id).status is TaskStatus.QUEUED
    assert repo.list_items(task.id)[0].status is ItemStatus.QUEUED
```

- [ ] **Step 2: Run repository tests and verify the missing implementation**

Run: `.venv\Scripts\python.exe -m pytest tests/test_repository.py -q`

Expected: collection fails with `ModuleNotFoundError: telegram_downloader.repository`.

- [ ] **Step 3: Implement schema, typed mapping, atomic updates, and recovery**

Implement `TaskRepository` with one short-lived connection per public operation. `initialize()` must execute this schema and set `PRAGMA journal_mode=WAL`, `PRAGMA foreign_keys=ON`, and `PRAGMA synchronous=NORMAL`:

```sql
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    source_kind TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    source_title TEXT NOT NULL,
    source_url TEXT NOT NULL,
    date_from_utc TEXT NOT NULL,
    date_to_utc TEXT NOT NULL,
    media_kinds TEXT NOT NULL,
    item_limit INTEGER NOT NULL CHECK(item_limit > 0),
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_error TEXT
);
CREATE TABLE IF NOT EXISTS media_items (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    peer_ref TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    grouped_id INTEGER,
    media_id TEXT NOT NULL,
    media_kind TEXT NOT NULL,
    original_name TEXT NOT NULL,
    target_path TEXT NOT NULL,
    expected_size INTEGER,
    message_date_utc TEXT NOT NULL,
    downloaded_bytes INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    retry_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    UNIQUE(peer_ref, message_id, media_id)
);
CREATE INDEX IF NOT EXISTS idx_items_task_status ON media_items(task_id, status);
```

The public API must be exactly:

```python
class TaskRepository:
    def __init__(self, database: Path) -> None: ...
    def initialize(self) -> None: ...
    def create_task(self, task: TaskRecord, items: list[MediaItem]) -> None: ...
    def insert_item_if_new(self, item: MediaItem) -> bool: ...
    def get_task(self, task_id: str) -> TaskRecord: ...
    def list_tasks(self) -> list[TaskRecord]: ...
    def list_items(self, task_id: str, statuses: set[ItemStatus] | None = None) -> list[MediaItem]: ...
    def update_task_status(self, task_id: str, status: TaskStatus, error: str | None = None) -> None: ...
    def update_item_progress(self, item_id: str, downloaded_bytes: int, status: ItemStatus, error: str | None = None, retry_count: int | None = None) -> None: ...
    def recover_interrupted(self) -> None: ...
```

Serialize datetimes with `datetime.isoformat()`, media kinds as a sorted comma-separated string, and paths with `str(path)`. `recover_interrupted()` must update task states `scanning`, `downloading`, and `waiting_retry` to `queued`, and item states `downloading` and `waiting_retry` to `queued`, inside one transaction.

- [ ] **Step 4: Run repository and existing tests**

Run: `.venv\Scripts\python.exe -m pytest tests/test_repository.py tests/test_paths.py tests/test_files.py -q`

Expected: all tests pass.

- [ ] **Step 5: Commit persistence**

```powershell
git add src/telegram_downloader/repository.py tests/test_repository.py
git commit -m "feat: persist and recover download tasks"
```

### Task 5: Atomic settings, DPAPI secrets, and redacted logging

**Files:**
- Create: `src/telegram_downloader/settings.py`
- Create: `src/telegram_downloader/security.py`
- Create: `src/telegram_downloader/logging.py`
- Test: `tests/test_settings.py`
- Test: `tests/test_security.py`
- Test: `tests/test_logging.py`

- [ ] **Step 1: Write tests for atomic settings, injectable encryption, and log redaction**

```python
# tests/test_settings.py
from telegram_downloader.settings import AppSettings, ProxySettings, SettingsStore


def test_settings_round_trip(tmp_path) -> None:
    path = tmp_path / "config" / "settings.json"
    store = SettingsStore(path)
    settings = AppSettings(api_id=123, concurrency=3, proxy=ProxySettings("socks5", "127.0.0.1", 1080, "u"))
    store.save(settings)
    assert store.load() == settings
    assert not path.with_suffix(".json.tmp").exists()
```

```python
# tests/test_security.py
from telegram_downloader.security import SecretsVault


class ReverseProtector:
    def protect(self, value: bytes) -> bytes:
        return value[::-1]

    def unprotect(self, value: bytes) -> bytes:
        return value[::-1]


def test_secrets_are_not_stored_as_plaintext(tmp_path) -> None:
    path = tmp_path / "secrets.dat"
    vault = SecretsVault(path, ReverseProtector())
    vault.save({"api_hash": "secret-hash", "session": "session-value"})
    assert b"secret-hash" not in path.read_bytes()
    assert vault.load()["session"] == "session-value"
```

```python
# tests/test_logging.py
import logging

from telegram_downloader.logging import SecretRedactionFilter


def test_redaction_removes_registered_secrets() -> None:
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "hash=abc123", (), None)
    SecretRedactionFilter({"abc123"}).filter(record)
    assert record.getMessage() == "hash=***"
```

- [ ] **Step 2: Run tests and observe missing modules**

Run: `.venv\Scripts\python.exe -m pytest tests/test_settings.py tests/test_security.py tests/test_logging.py -q`

Expected: collection fails because the three modules do not exist.

- [ ] **Step 3: Implement the three storage/security boundaries**

```python
# src/telegram_downloader/settings.py
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ProxySettings:
    kind: str = "none"
    host: str = ""
    port: int = 0
    username: str = ""


@dataclass(frozen=True, slots=True)
class AppSettings:
    api_id: int = 0
    concurrency: int = 3
    proxy: ProxySettings = ProxySettings()


class SettingsStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> AppSettings:
        if not self.path.exists():
            return AppSettings()
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        raw["proxy"] = ProxySettings(**raw.get("proxy", {}))
        return AppSettings(**raw)

    def save(self, settings: AppSettings) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text(json.dumps(asdict(settings), ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, self.path)
```

`src/telegram_downloader/security.py` must define a `Protector` protocol, `DpapiProtector`, and `SecretsVault`. `DpapiProtector` calls `CryptProtectData` and `CryptUnprotectData` through `ctypes.windll.crypt32` with `CRYPTPROTECT_UI_FORBIDDEN = 0x1`, always releases output memory with `kernel32.LocalFree`, and raises `OSError(ctypes.get_last_error())` on failure. `SecretsVault.save()` serializes compact UTF-8 JSON, encrypts it, writes `<name>.tmp`, and commits with `os.replace`; `load()` returns `{}` when absent and otherwise decrypts and parses the JSON.

```python
# src/telegram_downloader/logging.py
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


class SecretRedactionFilter(logging.Filter):
    def __init__(self, secrets: set[str]) -> None:
        super().__init__()
        self.secrets = {value for value in secrets if value}

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        for secret in self.secrets:
            message = message.replace(secret, "***")
        record.msg, record.args = message, ()
        return True


def configure_logging(path: Path, secrets: set[str]) -> logging.Logger:
    path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("telegram_downloader")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    handler = RotatingFileHandler(path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    handler.addFilter(SecretRedactionFilter(secrets))
    logger.addHandler(handler)
    return logger
```

- [ ] **Step 4: Run security tests and a real Windows DPAPI round trip**

Run: `.venv\Scripts\python.exe -m pytest tests/test_settings.py tests/test_security.py tests/test_logging.py -q`

Expected: `3 passed`.

Run: `.venv\Scripts\python.exe -c "from telegram_downloader.security import DpapiProtector; p=DpapiProtector(); assert p.unprotect(p.protect(b'probe')) == b'probe'; print('DPAPI OK')"`

Expected: `DPAPI OK`.

- [ ] **Step 5: Commit configuration and credential safety**

```powershell
git add src/telegram_downloader/settings.py src/telegram_downloader/security.py src/telegram_downloader/logging.py tests/test_settings.py tests/test_security.py tests/test_logging.py
git commit -m "feat: store portable settings and encrypted secrets"
```

### Task 6: Telegram gateway contract, login, media scan, and proxy mapping

**Files:**
- Create: `src/telegram_downloader/gateway.py`
- Test: `tests/test_gateway.py`

- [ ] **Step 1: Write adapter tests against a fake Telethon client**

```python
# tests/test_gateway.py
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from telegram_downloader.domain import MediaKind
from telegram_downloader.gateway import AuthState, TelethonGateway, proxy_dict
from telegram_downloader.settings import ProxySettings


def test_proxy_dict_supports_socks5_and_http() -> None:
    socks = proxy_dict(ProxySettings("socks5", "127.0.0.1", 1080, "u"), "p")
    assert socks == {"proxy_type": "socks5", "addr": "127.0.0.1", "port": 1080, "username": "u", "password": "p", "rdns": True}
    assert proxy_dict(ProxySettings(), "") is None


@pytest.mark.asyncio
async def test_login_reports_password_requirement() -> None:
    class PasswordNeeded(Exception):
        pass

    class Client:
        async def sign_in(self, **kwargs):
            raise PasswordNeeded()

    gateway = TelethonGateway.from_client_for_test(Client(), password_needed_error=PasswordNeeded)
    assert await gateway.sign_in("+8613800000000", "12345", "hash") is AuthState.PASSWORD_REQUIRED


def test_message_metadata_classifies_voice_and_name() -> None:
    now = datetime(2026, 8, 13, tzinfo=timezone.utc)
    message = SimpleNamespace(id=7, date=now, grouped_id=None, photo=None, video=None, audio=None, voice=object(), document=object(), file=SimpleNamespace(name="voice.ogg", mime_type="audio/ogg", size=12, id="m7"))
    media = TelethonGateway.remote_media_from_message("peer", message)
    assert media.kind is MediaKind.VOICE
    assert media.original_name == "voice.ogg"
    assert media.expected_size == 12
```

- [ ] **Step 2: Run the gateway tests and confirm the module is absent**

Run: `.venv\Scripts\python.exe -m pytest tests/test_gateway.py -q`

Expected: collection fails with `ModuleNotFoundError: telegram_downloader.gateway`.

- [ ] **Step 3: Implement the protocol and Telethon boundary**

Define these stable public types in `gateway.py`:

```python
class AuthState(StrEnum):
    READY = "ready"
    CODE_SENT = "code_sent"
    PASSWORD_REQUIRED = "password_required"


@dataclass(frozen=True, slots=True)
class RemoteMedia:
    peer_ref: str
    source_title: str
    message_id: int
    grouped_id: int | None
    media_id: str
    kind: MediaKind
    original_name: str
    expected_size: int | None
    message_date_utc: datetime


class GatewayError(RuntimeError): ...
class AccessDeniedError(GatewayError): ...
class EmptyMediaError(GatewayError): ...
class TransientNetworkError(GatewayError): ...
class MediaReferenceExpired(GatewayError): ...


class FloodWaitError(GatewayError):
    def __init__(self, seconds: int) -> None:
        super().__init__(f"Telegram 要求等待 {seconds} 秒")
        self.seconds = seconds


class TelegramGateway(Protocol):
    async def connect(self) -> None: ...
    async def request_code(self, phone: str) -> str: ...
    async def sign_in(self, phone: str, code: str, phone_code_hash: str) -> AuthState: ...
    async def check_password(self, password: str) -> AuthState: ...
    def export_session(self) -> str: ...
    async def scan(self, source: ParsedLink, filters: ScanFilters) -> AsyncIterator[RemoteMedia]: ...
    async def stream_media(self, peer_ref: str, message_id: int, offset: int) -> AsyncIterator[bytes]: ...
    async def test_connection(self) -> None: ...
    async def disconnect(self) -> None: ...
```

`TelethonGateway` must create `TelegramClient(StringSession(session), api_id, api_hash, proxy=proxy_dict(proxy, proxy_password), flood_sleep_threshold=0)`. It also provides `from_client_for_test(client, password_needed_error)` so tests can inject a client without importing Telegram credentials. Map Telethon `FloodWaitError.seconds` to the local `FloodWaitError`; map invalid/private entity errors to `AccessDeniedError`; map connection/timeouts to `TransientNetworkError`.

For a single link, fetch the exact message. When it has `grouped_id`, iterate a bounded window of messages around its ID and yield all matching grouped messages in ascending message ID order. For channel/group scans, iterate newest to oldest, skip messages newer than `date_to_utc`, stop below `date_from_utc`, apply `media_kinds`, and stop at `item_limit`. Use `remote_media_from_message()` for all metadata classification. `stream_media()` refetches the message and yields `client.iter_download(message.media, offset=offset)` chunks.

- [ ] **Step 4: Run the adapter tests and import check**

Run: `.venv\Scripts\python.exe -m pytest tests/test_gateway.py -q`

Expected: `3 passed`.

Run: `.venv\Scripts\python.exe -c "from telegram_downloader.gateway import TelethonGateway; print('gateway import OK')"`

Expected: `gateway import OK` without a network connection.

- [ ] **Step 5: Commit the Telegram boundary**

```powershell
git add src/telegram_downloader/gateway.py tests/test_gateway.py
git commit -m "feat: add Telegram login and media gateway"
```

### Task 7: Scan preview and persistent task planning

**Files:**
- Create: `src/telegram_downloader/planner.py`
- Test: `tests/test_planner.py`

- [ ] **Step 1: Write scan ordering, summary, and commit tests**

```python
# tests/test_planner.py
from datetime import datetime, timezone
from pathlib import Path

import pytest

from telegram_downloader.domain import MediaKind, ScanFilters, TaskStatus
from telegram_downloader.gateway import RemoteMedia
from telegram_downloader.links import parse_telegram_link
from telegram_downloader.planner import EmptyScanError, TaskPlanner


class FakeGateway:
    def __init__(self, media):
        self.media = media

    async def scan(self, source, filters):
        for item in self.media:
            yield item


class FakeRepository:
    def __init__(self):
        self.saved = None

    def create_task(self, task, items):
        self.saved = (task, items)


@pytest.mark.asyncio
async def test_preview_summarizes_known_and_unknown_sizes(tmp_path: Path) -> None:
    now = datetime(2026, 8, 13, tzinfo=timezone.utc)
    media = [
        RemoteMedia("peer", "频道", 9, None, "m9", MediaKind.VIDEO, "a.mp4", 100, now),
        RemoteMedia("peer", "频道", 8, None, "m8", MediaKind.DOCUMENT, "b.pdf", None, now),
    ]
    repo = FakeRepository()
    planner = TaskPlanner(FakeGateway(media), repo, tmp_path, uuid_factory=iter(["task", "i1", "i2"]).__next__, clock=lambda: now)
    filters = ScanFilters(now, now, frozenset(MediaKind), 20)
    preview = await planner.scan(parse_telegram_link("https://t.me/channel"), filters)
    assert preview.known_bytes == 100
    assert preview.unknown_size_count == 1
    planner.commit(preview)
    assert repo.saved[0].status is TaskStatus.QUEUED
    assert [item.message_id for item in repo.saved[1]] == [9, 8]


@pytest.mark.asyncio
async def test_empty_scan_is_rejected(tmp_path: Path) -> None:
    now = datetime(2026, 8, 13, tzinfo=timezone.utc)
    planner = TaskPlanner(FakeGateway([]), FakeRepository(), tmp_path)
    with pytest.raises(EmptyScanError, match="没有找到"):
        await planner.scan(parse_telegram_link("https://t.me/channel"), ScanFilters(now, now, frozenset(MediaKind), 20))
```

- [ ] **Step 2: Run planner tests and confirm the missing planner module**

Run: `.venv\Scripts\python.exe -m pytest tests/test_planner.py -q`

Expected: collection fails with `ModuleNotFoundError: telegram_downloader.planner`.

- [ ] **Step 3: Implement preview-first task planning**

```python
# src/telegram_downloader/planner.py
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from uuid import uuid4

from telegram_downloader.domain import MediaItem, ParsedLink, ScanFilters, TaskRecord, TaskStatus
from telegram_downloader.files import archive_target, disambiguate_target


class EmptyScanError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ScanPreview:
    task: TaskRecord
    items: tuple[MediaItem, ...]
    known_bytes: int
    unknown_size_count: int


class TaskPlanner:
    def __init__(self, gateway, repository, downloads: Path, uuid_factory: Callable[[], str] | None = None, clock=None) -> None:
        self.gateway = gateway
        self.repository = repository
        self.downloads = downloads
        self.uuid_factory = uuid_factory or (lambda: str(uuid4()))
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    async def scan(self, source: ParsedLink, filters: ScanFilters) -> ScanPreview:
        task_id = self.uuid_factory()
        now = self.clock()
        remote = [item async for item in self.gateway.scan(source, filters)]
        if not remote:
            raise EmptyScanError("筛选范围内没有找到可下载媒体")
        task = TaskRecord(task_id, source.kind, source.entity_ref, remote[0].source_title, source.normalized_url, filters, TaskStatus.DRAFT, now, now)
        planned, used = [], set()
        for item in remote:
            target = archive_target(self.downloads, item.source_title, item.message_date_utc, item.kind, item.original_name)
            if target in used or target.exists():
                target = disambiguate_target(target, item.message_id)
            used.add(target)
            planned.append(MediaItem(
                self.uuid_factory(), task_id, item.peer_ref, item.message_id, item.grouped_id,
                item.media_id, item.kind, item.original_name, target,
                item.expected_size, item.message_date_utc,
            ))
        items = tuple(planned)
        return ScanPreview(task, items, sum(item.expected_size or 0 for item in remote), sum(item.expected_size is None for item in remote))

    def commit(self, preview: ScanPreview) -> TaskRecord:
        queued = replace(preview.task, status=TaskStatus.QUEUED, updated_at=self.clock())
        self.repository.create_task(queued, list(preview.items))
        return queued
```

- [ ] **Step 4: Run planner and gateway tests**

Run: `.venv\Scripts\python.exe -m pytest tests/test_planner.py tests/test_gateway.py -q`

Expected: all tests pass.

- [ ] **Step 5: Commit scan planning**

```powershell
git add src/telegram_downloader/planner.py tests/test_planner.py
git commit -m "feat: preview and persist filtered scans"
```

### Task 8: Chunked downloader, disk guard, and byte-offset resume

**Files:**
- Create: `src/telegram_downloader/downloader.py`
- Test: `tests/test_downloader.py`

- [ ] **Step 1: Write resume, completion, and disk-space tests**

```python
# tests/test_downloader.py
from datetime import datetime, timezone
from pathlib import Path

import pytest

from telegram_downloader.domain import ItemStatus, MediaItem, MediaKind
from telegram_downloader.downloader import InsufficientSpaceError, MediaDownloader
from telegram_downloader.paths import PortablePaths


class FakeGateway:
    def __init__(self, chunks):
        self.chunks = chunks
        self.offset = None

    async def stream_media(self, peer_ref, message_id, offset):
        self.offset = offset
        for chunk in self.chunks:
            yield chunk


class FakeRepository:
    def __init__(self):
        self.updates = []

    def update_item_progress(self, item_id, downloaded_bytes, status, error=None, retry_count=None):
        self.updates.append((item_id, downloaded_bytes, status))


def item(target: Path, size: int = 6) -> MediaItem:
    return MediaItem("i", "t", "peer", 7, None, "m", MediaKind.VIDEO, "x.mp4", target, size, datetime(2026, 8, 13, tzinfo=timezone.utc))


@pytest.mark.asyncio
async def test_resumes_from_part_size_and_atomically_finishes(tmp_path: Path) -> None:
    paths = PortablePaths(tmp_path)
    paths.ensure_layout()
    target = paths.downloads / "x.mp4"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.with_suffix(".mp4.part").write_bytes(b"ab")
    gateway, repo = FakeGateway([b"cd", b"ef"]), FakeRepository()
    await MediaDownloader(gateway, repo, paths, free_bytes=lambda _: 10**9, reserve_bytes=0).download(item(target))
    assert gateway.offset == 2
    assert target.read_bytes() == b"abcdef"
    assert repo.updates[-1] == ("i", 6, ItemStatus.COMPLETED)


@pytest.mark.asyncio
async def test_refuses_download_when_space_is_below_remaining_plus_reserve(tmp_path: Path) -> None:
    paths = PortablePaths(tmp_path)
    paths.ensure_layout()
    downloader = MediaDownloader(FakeGateway([b"abcdef"]), FakeRepository(), paths, free_bytes=lambda _: 5, reserve_bytes=2)
    with pytest.raises(InsufficientSpaceError):
        await downloader.download(item(paths.downloads / "x.mp4"))
```

- [ ] **Step 2: Run downloader tests and verify the expected failure**

Run: `.venv\Scripts\python.exe -m pytest tests/test_downloader.py -q`

Expected: collection fails with `ModuleNotFoundError: telegram_downloader.downloader`.

- [ ] **Step 3: Implement guarded streaming and recovery**

`MediaDownloader` must use this public API:

```python
class DownloadPaused(RuntimeError): ...
class InsufficientSpaceError(RuntimeError): ...
class SizeMismatchError(RuntimeError): ...


class MediaDownloader:
    def __init__(self, gateway, repository, paths: PortablePaths, free_bytes=None, reserve_bytes: int = 512 * 1024 * 1024, progress_interval: float = 0.5) -> None: ...
    async def download(self, item: MediaItem, should_pause=lambda: False) -> Path: ...
```

The implementation sequence is exact:

1. Guard `item.target_path` with `PortablePaths.guard()` and create its parent.
2. If the final target exists and its size equals `expected_size`, mark the item completed and return without opening Telegram.
3. Set `part = target.with_suffix(target.suffix + ".part")`; use its length as `offset`.
4. If `offset > expected_size`, atomically rename it to `.part.corrupt` and reset the offset to zero.
5. Require free space of at least `(expected_size - offset) + max(reserve_bytes, int(expected_size * 0.05))`; when size is unknown, require `reserve_bytes`.
6. Mark the item downloading, append chunks from `gateway.stream_media(..., offset)`, and flush repository progress no more than twice per second unless the test sets `progress_interval=0`.
7. When `should_pause()` becomes true, flush progress, mark paused, and raise `DownloadPaused` while retaining `.part`.
8. Verify exact size when known, `os.replace(part, target)`, then mark completed.

Use `shutil.disk_usage(path.anchor).free` as the production `free_bytes` function and call it again after each 16 MiB written so a filling disk pauses safely.

- [ ] **Step 4: Run downloader and path tests**

Run: `.venv\Scripts\python.exe -m pytest tests/test_downloader.py tests/test_paths.py -q`

Expected: all tests pass.

- [ ] **Step 5: Commit resumable downloads**

```powershell
git add src/telegram_downloader/downloader.py tests/test_downloader.py
git commit -m "feat: resume chunked downloads from partial files"
```

### Task 9: Concurrent scheduler, retries, FloodWait, and shutdown recovery

**Files:**
- Create: `src/telegram_downloader/scheduler.py`
- Test: `tests/test_scheduler.py`

- [ ] **Step 1: Write deterministic scheduler tests**

```python
# tests/test_scheduler.py
import asyncio
from types import SimpleNamespace

import pytest

from telegram_downloader.domain import ItemStatus, TaskStatus
from telegram_downloader.gateway import FloodWaitError, TransientNetworkError
from telegram_downloader.scheduler import DownloadScheduler, RetryPolicy


class Repo:
    def __init__(self):
        self.items = [SimpleNamespace(id="i", retry_count=0)]
        self.item_updates = []
        self.task_updates = []

    def list_items(self, task_id, statuses=None): return self.items
    def update_item_progress(self, item_id, downloaded_bytes, status, error=None, retry_count=None): self.item_updates.append((status, retry_count, error))
    def update_task_status(self, task_id, status, error=None): self.task_updates.append(status)
    def recover_interrupted(self): self.recovered = True


@pytest.mark.asyncio
async def test_flood_wait_sleeps_exact_seconds_then_retries() -> None:
    attempts, sleeps = 0, []
    class Downloader:
        async def download(self, item, should_pause):
            nonlocal attempts
            attempts += 1
            if attempts == 1: raise FloodWaitError(4)
    async def fake_sleep(seconds): sleeps.append(seconds)
    repo = Repo()
    scheduler = DownloadScheduler(repo, Downloader(), concurrency=1, retry=RetryPolicy(3, 1), sleep=fake_sleep)
    await scheduler.run_task("t")
    assert attempts == 2
    assert sleeps == [4]
    assert repo.task_updates[-1] is TaskStatus.COMPLETED


@pytest.mark.asyncio
async def test_transient_error_uses_exponential_backoff_then_marks_failed() -> None:
    class Downloader:
        async def download(self, item, should_pause): raise TransientNetworkError("offline")
    sleeps = []
    repo = Repo()
    scheduler = DownloadScheduler(repo, Downloader(), concurrency=1, retry=RetryPolicy(3, 2), sleep=lambda value: _record_sleep(sleeps, value))
    await scheduler.run_task("t")
    assert sleeps == [2, 4]
    assert repo.item_updates[-1][0] is ItemStatus.FAILED
    assert repo.task_updates[-1] is TaskStatus.PARTIAL_FAILURE


async def _record_sleep(values, value):
    values.append(value)
    await asyncio.sleep(0)
```

- [ ] **Step 2: Run scheduler tests and confirm the missing module**

Run: `.venv\Scripts\python.exe -m pytest tests/test_scheduler.py -q`

Expected: collection fails with `ModuleNotFoundError: telegram_downloader.scheduler`.

- [ ] **Step 3: Implement bounded concurrency and explicit retry policy**

```python
# src/telegram_downloader/scheduler.py public surface
@dataclass(frozen=True, slots=True)
class RetryPolicy:
    attempts: int = 3
    base_delay: int = 2


class DownloadScheduler:
    def __init__(self, repository, downloader, concurrency: int = 3, retry: RetryPolicy = RetryPolicy(), sleep=asyncio.sleep) -> None: ...
    def recover(self) -> None: ...
    def pause_task(self, task_id: str) -> None: ...
    async def resume_task(self, task_id: str) -> None: ...
    async def run_task(self, task_id: str) -> None: ...
    async def shutdown(self) -> None: ...
```

Clamp concurrency to 1–5. `run_task()` marks the task downloading, selects queued/paused/waiting-retry/failed items, and executes `_run_item()` under an `asyncio.Semaphore`. Flood waits set item and task to `WAITING_RETRY`, sleep exactly `seconds`, and do not consume the three transient attempts. `TransientNetworkError` retries at `base_delay * 2**attempt_index`; after the final attempt it marks the item failed. `InsufficientSpaceError` and `DownloadPaused` mark the item and task paused without retrying. Aggregate all item results: all completed becomes task completed; any failed becomes partial failure; any paused remains paused. `shutdown()` sets every task pause flag and awaits active tasks with a five-second timeout.

- [ ] **Step 4: Run scheduler, downloader, and repository tests**

Run: `.venv\Scripts\python.exe -m pytest tests/test_scheduler.py tests/test_downloader.py tests/test_repository.py -q`

Expected: all tests pass.

- [ ] **Step 5: Commit scheduling and recovery**

```powershell
git add src/telegram_downloader/scheduler.py tests/test_scheduler.py
git commit -m "feat: schedule retries and recover interrupted work"
```

### Task 10: Professional workbench window and task table model

**Files:**
- Create: `src/telegram_downloader/ui/__init__.py`
- Create: `src/telegram_downloader/ui/theme.py`
- Create: `src/telegram_downloader/ui/models.py`
- Create: `src/telegram_downloader/ui/main.py`
- Test: `tests/ui/test_main_window.py`

- [ ] **Step 1: Write GUI structure and signal tests**

```python
# tests/ui/test_main_window.py
from PySide6.QtCore import Qt

from telegram_downloader.ui.main import MainWindow


def test_workbench_contains_required_controls(qtbot) -> None:
    window = MainWindow()
    qtbot.addWidget(window)
    assert window.windowTitle() == "Telegram 下载器"
    assert window.link_input.placeholderText().startswith("粘贴")
    assert window.limit_input.minimum() == 1
    assert window.limit_input.maximum() == 100000
    assert window.task_table.model().columnCount() == 6
    assert window.account_badge.text() == "未登录"


def test_scan_button_emits_link(qtbot) -> None:
    window = MainWindow()
    qtbot.addWidget(window)
    window.link_input.setText("https://t.me/example/42")
    with qtbot.waitSignal(window.scan_requested, timeout=500) as signal:
        qtbot.mouseClick(window.scan_button, Qt.MouseButton.LeftButton)
    assert signal.args == ["https://t.me/example/42"]
```

- [ ] **Step 2: Run the GUI tests headlessly and confirm the missing UI**

Run: `$env:QT_QPA_PLATFORM='offscreen'; .venv\Scripts\python.exe -m pytest tests/ui/test_main_window.py -q`

Expected: collection fails with `ModuleNotFoundError: telegram_downloader.ui`.

- [ ] **Step 3: Implement the dark three-column workbench**

`ui/models.py` defines the exact presentation record below. `TaskTableModel(QAbstractTableModel)` exposes columns `任务`, `状态`, `进度`, `大小`, `速度`, `剩余时间`, accepts `set_tasks(list[TaskSummary])`, returns Chinese state labels, and uses `Qt.UserRole` for task IDs.

```python
@dataclass(frozen=True, slots=True)
class TaskSummary:
    id: str
    title: str
    status: TaskStatus
    progress_text: str
    size_text: str
    speed_text: str
    remaining_text: str
```

`MainWindow(QMainWindow)` must define these signals and public widgets so the controller and tests do not reach into layout internals:

```python
class MainWindow(QMainWindow):
    scan_requested = Signal(str)
    pause_requested = Signal(str)
    resume_requested = Signal(str)
    retry_failed_requested = Signal(str)
    settings_requested = Signal()

    link_input: QLineEdit
    scan_button: QPushButton
    date_from: QDateEdit
    date_to: QDateEdit
    media_checks: dict[MediaKind, QCheckBox]
    limit_input: QSpinBox
    task_table: QTableView
    task_model: TaskTableModel
    account_badge: QLabel
    speed_value: QLabel
    completed_value: QLabel
    remaining_value: QLabel
```

Build a 1180×720 window with a 180-pixel navigation rail, expanding center panel, and 220-pixel statistics rail. The center panel contains link input and scan button, inclusive date pickers, all six media checkboxes checked by default, limit default 500, the task table, and pause/resume/retry/open-directory buttons. `theme.py` exports one `DARK_STYLESHEET` string using slate backgrounds, cyan primary actions, teal connected state, orange warnings, and visible keyboard focus outlines.

- [ ] **Step 4: Run GUI tests and manually render offscreen**

Run: `$env:QT_QPA_PLATFORM='offscreen'; .venv\Scripts\python.exe -m pytest tests/ui/test_main_window.py -q`

Expected: `2 passed`.

Run: `.venv\Scripts\ruff.exe check src/telegram_downloader/ui tests/ui`

Expected: `All checks passed!`.

- [ ] **Step 5: Commit the workbench UI**

```powershell
git add src/telegram_downloader/ui tests/ui/test_main_window.py
git commit -m "feat: add professional download workbench"
```

### Task 11: Login wizard and proxy/settings dialog

**Files:**
- Create: `src/telegram_downloader/ui/login.py`
- Create: `src/telegram_downloader/ui/settings.py`
- Test: `tests/ui/test_login_dialog.py`
- Test: `tests/ui/test_settings_dialog.py`

- [ ] **Step 1: Write wizard state and settings validation tests**

```python
# tests/ui/test_login_dialog.py
from PySide6.QtWidgets import QLineEdit

from telegram_downloader.ui.login import LoginDialog, LoginPage


def test_login_pages_mask_sensitive_fields(qtbot) -> None:
    dialog = LoginDialog()
    qtbot.addWidget(dialog)
    assert dialog.api_hash.echoMode() is QLineEdit.EchoMode.Password
    assert dialog.password.echoMode() is QLineEdit.EchoMode.Password
    dialog.show_page(LoginPage.CODE)
    assert dialog.stack.currentWidget() is dialog.code_page


def test_ready_state_updates_account_label(qtbot) -> None:
    dialog = LoginDialog()
    qtbot.addWidget(dialog)
    dialog.show_ready("Test User")
    assert "Test User" in dialog.ready_label.text()
```

```python
# tests/ui/test_settings_dialog.py
from telegram_downloader.settings import AppSettings, ProxySettings
from telegram_downloader.ui.settings import SettingsDialog


def test_round_trip_manual_proxy_form(qtbot) -> None:
    settings = AppSettings(123, 4, ProxySettings("http", "127.0.0.1", 8080, "u"))
    dialog = SettingsDialog(settings)
    qtbot.addWidget(dialog)
    assert dialog.values() == settings
    assert dialog.concurrency.minimum() == 1
    assert dialog.concurrency.maximum() == 5
```

- [ ] **Step 2: Run dialog tests and observe missing classes**

Run: `$env:QT_QPA_PLATFORM='offscreen'; .venv\Scripts\python.exe -m pytest tests/ui/test_login_dialog.py tests/ui/test_settings_dialog.py -q`

Expected: collection fails because `ui.login` and `ui.settings` do not exist.

- [ ] **Step 3: Implement explicit login states and validated proxy fields**

`LoginDialog` uses `QStackedWidget` pages represented by this enum:

```python
class LoginPage(IntEnum):
    CREDENTIALS = 0
    PHONE = 1
    CODE = 2
    PASSWORD = 3
    READY = 4
```

It exposes password-masked `api_hash`, `proxy_password`, and `password` inputs; emits `credentials_submitted(int, str, ProxySettings, str)`, `phone_submitted(str)`, `code_submitted(str)`, and `password_submitted(str)`; validates positive API ID, non-empty API Hash, phone beginning with `+`, and non-empty codes before emitting. `show_error(text)` presents a non-secret Chinese inline error without changing the current page.

`SettingsDialog` loads and returns `AppSettings`, supports proxy kinds `none`, `socks5`, and `http`, validates host and port 1–65535 when proxy is enabled, keeps the proxy password in a separate masked field, and emits `test_proxy_requested(ProxySettings, str)` without persisting until the user accepts.

- [ ] **Step 4: Run all GUI tests**

Run: `$env:QT_QPA_PLATFORM='offscreen'; .venv\Scripts\python.exe -m pytest tests/ui -q`

Expected: all GUI tests pass.

- [ ] **Step 5: Commit authentication and settings UI**

```powershell
git add src/telegram_downloader/ui/login.py src/telegram_downloader/ui/settings.py tests/ui/test_login_dialog.py tests/ui/test_settings_dialog.py
git commit -m "feat: add account login and proxy dialogs"
```

### Task 12: Async controller, composition root, and clean shutdown

**Files:**
- Create: `src/telegram_downloader/controller.py`
- Create: `src/telegram_downloader/app.py`
- Create: `src/telegram_downloader/__main__.py`
- Test: `tests/test_controller.py`
- Test: `tests/test_self_test.py`

- [ ] **Step 1: Write controller login/scan tests and portable self-test**

```python
# tests/test_controller.py
from datetime import datetime, timezone

import pytest

from telegram_downloader.controller import AppController
from telegram_downloader.gateway import AuthState


@pytest.mark.asyncio
async def test_code_login_saves_exported_session() -> None:
    class Gateway:
        async def sign_in(self, phone, code, phone_code_hash): return AuthState.READY
        def export_session(self): return "portable-session"
    class Vault:
        def __init__(self): self.value = {}
        def save(self, value): self.value = value
        def load(self): return {}
    vault = Vault()
    controller = AppController.for_test(gateway=Gateway(), vault=vault)
    controller.phone, controller.phone_code_hash = "+8613800000000", "hash"
    await controller.submit_code("12345")
    assert vault.value["session"] == "portable-session"


@pytest.mark.asyncio
async def test_scan_requires_user_confirmation_before_commit() -> None:
    class Planner:
        committed = False
        async def scan(self, source, filters): return "preview"
        def commit(self, preview): self.committed = True
    planner = Planner()
    controller = AppController.for_test(planner=planner, confirm_preview=lambda preview: False)
    await controller.scan_link("https://t.me/example/42", controller.default_filters(datetime(2026, 8, 13, tzinfo=timezone.utc)))
    assert planner.committed is False


def test_local_dates_become_inclusive_utc_boundaries() -> None:
    from datetime import date, timedelta, timezone
    filters = AppController.filters_from_dates(
        date(2026, 8, 1), date(2026, 8, 2), frozenset(), 500, timezone(timedelta(hours=8))
    )
    assert filters.date_from_utc.isoformat() == "2026-07-31T16:00:00+00:00"
    assert filters.date_to_utc.isoformat() == "2026-08-02T15:59:59.999999+00:00"
```

```python
# tests/test_self_test.py
import json

from telegram_downloader.app import run_self_test


def test_self_test_reports_only_paths_under_root(tmp_path) -> None:
    report = run_self_test(tmp_path)
    assert report["ok"] is True
    assert all(str(value).startswith(str(tmp_path)) for value in report["writable_paths"].values())
    disk_report = json.loads((tmp_path / "data" / "logs" / "self-test.json").read_text(encoding="utf-8"))
    assert disk_report == report
```

- [ ] **Step 2: Run integration tests and confirm missing composition modules**

Run: `.venv\Scripts\python.exe -m pytest tests/test_controller.py tests/test_self_test.py -q`

Expected: collection fails because `controller.py` and `app.py` do not exist.

- [ ] **Step 3: Implement orchestration without leaking secrets into the UI or logs**

`AppController` owns gateway, planner, scheduler, settings store, secrets vault, main window, and login dialog. Its public async methods are:

```python
async def submit_credentials(self, api_id: int, api_hash: str, proxy: ProxySettings, proxy_password: str) -> None: ...
async def submit_phone(self, phone: str) -> None: ...
async def submit_code(self, code: str) -> None: ...
async def submit_password(self, password: str) -> None: ...
async def scan_link(self, link: str, filters: ScanFilters) -> None: ...
async def test_proxy(self, proxy: ProxySettings, password: str) -> None: ...
async def shutdown(self) -> None: ...
```

`for_test(**dependencies)` supplies inert defaults for omitted UI/storage dependencies so controller tests can inject only the boundary under test. `filters_from_dates(date_from, date_to, media_kinds, item_limit, local_timezone)` combines the first date with local `00:00:00`, the second with local `23:59:59.999999`, then converts both to UTC. `default_filters(now)` returns a one-day inclusive UTC filter with every `MediaKind` and limit 500 for controller-only tests. Persist non-secret settings separately from `api_hash`, `proxy_password`, and exported `session`. After a ready login, clear code/password fields in memory, update the account badge, and close the login dialog. `scan_link()` parses the link, shows a preview containing count/known size/unknown-size count, commits only after confirmation, refreshes the task model, and starts `scheduler.run_task(task.id)` with `asyncio.create_task`. The open-directory action resolves the selected task target through `PortablePaths.guard()` before calling `os.startfile()`.

`app.py` exposes:

```python
def run_self_test(root: Path) -> dict[str, object]: ...
def create_application(root: Path) -> tuple[QApplication, qasync.QEventLoop, AppController]: ...
def run(root: Path) -> int: ...
```

`run_self_test()` constructs `PortablePaths`, creates the layout, initializes SQLite, verifies every writable path with `guard()`, writes JSON to `data/logs/self-test.json`, and returns the same dictionary. `create_application()` loads settings and secrets, configures redacted logging, initializes repository recovery, creates the Telethon gateway only when credentials exist, then wires every UI signal through `qasync.asyncSlot`. Connect `QApplication.aboutToQuit` to schedule `controller.shutdown()`.

`__main__.py` must import only standard library and `bootstrap` before calling `configure_process(runtime_root())`. Parse `--self-test`; import `run_self_test` or `run` only after bootstrap. Return exit code 0 when self-test reports `ok`, otherwise 1.

- [ ] **Step 4: Run controller, self-test, and full test suite**

Run: `.venv\Scripts\python.exe -m pytest tests/test_controller.py tests/test_self_test.py -q`

Expected: all focused tests pass.

Run: `$env:QT_QPA_PLATFORM='offscreen'; .venv\Scripts\python.exe -m pytest -q`

Expected: the complete suite passes.

- [ ] **Step 5: Commit the runnable application**

```powershell
git add src/telegram_downloader/controller.py src/telegram_downloader/app.py src/telegram_downloader/__main__.py tests/test_controller.py tests/test_self_test.py
git commit -m "feat: wire the runnable desktop application"
```

### Task 13: Portable build, packaged smoke test, and Chinese guide

**Files:**
- Create: `TelegramDownloader.spec`
- Create: `scripts/test.ps1`
- Create: `scripts/smoke.ps1`
- Create: `scripts/build.ps1`
- Create: `README.md`
- Test: `tests/test_packaging_contract.py`

- [ ] **Step 1: Write a packaging contract test before the build files exist**

```python
# tests/test_packaging_contract.py
from pathlib import Path


def test_build_contract_uses_onedir_and_project_local_workpaths() -> None:
    root = Path(__file__).parents[1]
    spec = (root / "TelegramDownloader.spec").read_text(encoding="utf-8")
    build = (root / "scripts" / "build.ps1").read_text(encoding="utf-8")
    assert "COLLECT(" in spec
    assert "--onefile" not in build
    assert ".build-temp" in build
    assert ".tool-cache" in build
    assert "smoke.ps1" in build
```

- [ ] **Step 2: Run the contract test and confirm missing packaging files**

Run: `.venv\Scripts\python.exe -m pytest tests/test_packaging_contract.py -q`

Expected: fails with `FileNotFoundError` for `TelegramDownloader.spec`.

- [ ] **Step 3: Add the exact onedir recipe and isolated PowerShell automation**

```python
# TelegramDownloader.spec
from pathlib import Path

root = Path(SPECPATH)
a = Analysis(
    [str(root / "src" / "telegram_downloader" / "__main__.py")],
    pathex=[str(root / "src")],
    binaries=[],
    datas=[],
    hiddenimports=["qasync", "python_socks", "telethon.sessions.string"],
    hookspath=[],
    runtime_hooks=[],
    excludes=["PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="TelegramDownloader",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="TelegramDownloader",
)
```

```powershell
# scripts/test.ps1
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$env:TEMP = Join-Path $projectRoot '.build-temp'
$env:TMP = $env:TEMP
$env:PIP_CACHE_DIR = Join-Path $projectRoot '.tool-cache\pip'
$env:PYINSTALLER_CONFIG_DIR = Join-Path $projectRoot '.tool-cache\pyinstaller'
$env:QT_QPA_PLATFORM = 'offscreen'
& (Join-Path $projectRoot '.venv\Scripts\python.exe') -m pytest -q
& (Join-Path $projectRoot '.venv\Scripts\ruff.exe') check src tests
```

```powershell
# scripts/smoke.ps1
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$appDir = (Resolve-Path (Join-Path $projectRoot 'dist\TelegramDownloader')).Path
$exe = Join-Path $appDir 'TelegramDownloader.exe'
$process = Start-Process -FilePath $exe -ArgumentList '--self-test' -WorkingDirectory $appDir -Wait -PassThru -WindowStyle Hidden
if ($process.ExitCode -ne 0) { throw "Self-test exited $($process.ExitCode)" }
$reportPath = Join-Path $appDir 'data\logs\self-test.json'
$report = Get-Content -Raw $reportPath | ConvertFrom-Json
if (-not $report.ok) { throw 'Packaged path self-test failed' }
foreach ($entry in $report.writable_paths.PSObject.Properties) {
    $resolved = [IO.Path]::GetFullPath([string]$entry.Value)
    if (-not $resolved.StartsWith($appDir, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Path escaped package: $resolved"
    }
}
Write-Output 'PACKAGED_SMOKE_OK'
```

```powershell
# scripts/build.ps1
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$env:TEMP = Join-Path $projectRoot '.build-temp'
$env:TMP = $env:TEMP
$env:PIP_CACHE_DIR = Join-Path $projectRoot '.tool-cache\pip'
$env:PYINSTALLER_CONFIG_DIR = Join-Path $projectRoot '.tool-cache\pyinstaller'
$work = Join-Path $projectRoot 'build'
$dist = Join-Path $projectRoot 'dist'
New-Item -ItemType Directory -Force -Path $env:TEMP, $env:PIP_CACHE_DIR, $work, $dist | Out-Null
& (Join-Path $PSScriptRoot 'setup-dev.ps1')
& (Join-Path $PSScriptRoot 'test.ps1')
& (Join-Path $projectRoot '.venv\Scripts\pyinstaller.exe') --noconfirm --clean --workpath $work --distpath $dist (Join-Path $projectRoot 'TelegramDownloader.spec')
& (Join-Path $PSScriptRoot 'smoke.ps1')
$packageData = Join-Path $dist 'TelegramDownloader\data'
$packageDownloads = Join-Path $dist 'TelegramDownloader\downloads'
if (Test-Path $packageData) { Remove-Item -LiteralPath $packageData -Recurse -Force }
if (Test-Path $packageDownloads) { Remove-Item -LiteralPath $packageDownloads -Recurse -Force }
$zip = Join-Path $dist 'TelegramDownloader-portable-win-x64.zip'
if (Test-Path $zip) { Remove-Item -LiteralPath $zip -Force }
Compress-Archive -Path (Join-Path $dist 'TelegramDownloader\*') -DestinationPath $zip
Write-Output $zip
```

`README.md` must document, in Chinese: Windows 10/11 x64 support; green ZIP usage; how to obtain API ID/Hash from `my.telegram.org`; phone/code/2FA login; single-message and filtered batch workflows; SOCKS5/HTTP fields; the complete portable directory tree; `.part` recovery behavior; DPAPI portability limitation; content-access restrictions; development setup; test/build commands; and the final ZIP path.

- [ ] **Step 4: Run the contract, build the application, and smoke test the EXE**

Run: `.venv\Scripts\python.exe -m pytest tests/test_packaging_contract.py -q`

Expected: `1 passed`.

Run: `powershell -ExecutionPolicy Bypass -File scripts/build.ps1`

Expected: tests and Ruff pass, PyInstaller creates `dist/TelegramDownloader/TelegramDownloader.exe`, smoke output contains `PACKAGED_SMOKE_OK`, and `dist/TelegramDownloader-portable-win-x64.zip` exists.

- [ ] **Step 5: Commit packaging and documentation**

```powershell
git add TelegramDownloader.spec scripts/test.ps1 scripts/smoke.ps1 scripts/build.ps1 README.md tests/test_packaging_contract.py
git commit -m "build: package portable Windows downloader"
```

### Task 14: Signed update contract and dual-source reconciliation

**Files:**
- Create: `src/telegram_downloader/update_contract.py`
- Create: `src/telegram_downloader/update_sources.py`
- Create: `src/telegram_downloader/trusted_update_keys.json`
- Test: `tests/update/test_update_contract.py`
- Test: `tests/update/test_update_sources.py`

- [ ] **Step 1: Write failing strict-contract and source-reconciliation tests**

Cover canonical JSON serialization; Ed25519 valid/invalid signatures; unknown key IDs; malformed, oversized, duplicate-key, BOM, extra-field, downgrade, prerelease, byte-size, and SHA-256 validation; both sources agreeing; either source being temporarily unavailable; one source being stale; and same-version content conflicts failing closed.

- [ ] **Step 2: Run focused tests and confirm update modules are missing**

Run: `.venv\Scripts\python.exe -m pytest tests/update/test_update_contract.py tests/update/test_update_sources.py -q`

Expected: collection fails because the update contract modules do not exist.

- [ ] **Step 3: Implement immutable contracts and source protocols**

`update_contract.py` exposes immutable manifest/asset models, strict semantic-version parsing, canonical JSON bytes, `verify_manifest(manifest_bytes, signature_bytes, trusted_keys)`, and `verify_asset(path, expected_size, expected_sha256)`. Reject any field not defined by schema version 1. Store only public SPKI DER keys as Base64 in `trusted_update_keys.json`.

`update_sources.py` defines async source adapters returning `latest.json`, versioned manifest, signature, and ranged asset streams. Implement GitHub and ModelScope HTTPS URL builders plus `reconcile_sources(results, current_version)`. A valid single source may proceed when the other is unavailable; same-version disagreement is a hard error.

- [ ] **Step 4: Run focused tests and lint**

Run: `.venv\Scripts\python.exe -m pytest tests/update/test_update_contract.py tests/update/test_update_sources.py -q`

Run: `.venv\Scripts\ruff.exe check src/telegram_downloader/update_contract.py src/telegram_downloader/update_sources.py tests/update`

Expected: all pass.

- [ ] **Step 5: Commit the signed update contract**

```powershell
git add src/telegram_downloader/update_contract.py src/telegram_downloader/update_sources.py src/telegram_downloader/trusted_update_keys.json tests/update
git commit -m "feat: verify signed updates from dual sources"
```

### Task 15: Resumable online update, external replacement, and rollback

**Files:**
- Create: `src/telegram_downloader/update_download.py`
- Create: `src/telegram_downloader/update.py`
- Create: `src/telegram_downloader/update_helper.py`
- Create: `src/telegram_downloader/ui/update_dialog.py`
- Modify: `src/telegram_downloader/paths.py`
- Modify: `src/telegram_downloader/controller.py`
- Modify: `src/telegram_downloader/app.py`
- Modify: `src/telegram_downloader/__main__.py`
- Modify: `TelegramDownloader.spec`
- Test: `tests/update/test_resumable_update.py`
- Test: `tests/update/test_update_transaction.py`
- Test: `tests/update/test_update_coordinator.py`
- Test: `tests/ui/test_update_dialog.py`

- [ ] **Step 1: Write failing download, transaction, and UI tests**

Cover HTTP Range resume, server ignoring Range, source failover, corrupted partial files, final size/hash mismatch, insufficient disk space, managed-file inventory validation, preservation of `data`/`downloads`/unknown root files, locked-file failure, process-exit wait, healthy replacement commit, health timeout, replacement crash, automatic rollback, rollback journal recovery after helper interruption, startup non-blocking behavior, user decline, and accepted-update shutdown.

- [ ] **Step 2: Run focused tests and confirm the update workflow is absent**

Run: `$env:QT_QPA_PLATFORM='offscreen'; .venv\Scripts\python.exe -m pytest tests/update tests/ui/test_update_dialog.py -q`

Expected: collection fails for the missing downloader/coordinator/helper/dialog modules.

- [ ] **Step 3: Implement project-local resumable download and coordinator**

Write partial packages and metadata under `data/update/staging`; fsync before promoting; always verify the signed manifest, asset size, and SHA-256. Check both sources at startup after local recovery, display version/release notes/size, and begin download only after explicit confirmation. Copy the helper into `data/update/helper/<version>` before asking the main process to exit.

- [ ] **Step 4: Implement journaled replacement and health-check rollback**

The helper accepts only absolute paths guarded below the application root, waits for the parent PID, validates a versioned managed-file inventory, moves old managed files to `data/update/backup/<old-version>`, installs the new runtime, and launches `--update-health-check`. It commits only after a transaction-specific confirmation file appears. On any error or timeout it removes only newly managed files, restores the backup, records a redacted result, and starts the old executable. A subsequent helper launch resumes or rolls back an interrupted journal idempotently.

- [ ] **Step 5: Package both executables and run fault-injection tests**

Run: `$env:QT_QPA_PLATFORM='offscreen'; .venv\Scripts\python.exe -m pytest tests/update tests/ui/test_update_dialog.py -q`

Run: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build.ps1`

Expected: `TelegramDownloader.exe` and `UpdateHelper.exe` are both onedir executables; portable smoke and every injected rollback case pass.

- [ ] **Step 6: Commit online update support**

```powershell
git add src/telegram_downloader TelegramDownloader.spec tests/update tests/ui/test_update_dialog.py
git commit -m "feat: add rollback-safe online updates"
```

### Task 16: Non-C-drive Windows installer

**Files:**
- Create: `installer/TelegramDownloader.iss`
- Create: `scripts/build-installer.ps1`
- Create: `scripts/smoke-installer.ps1`
- Test: `tests/test_installer_contract.py`

- [ ] **Step 1: Write a failing installer contract test**

Assert current-user privileges, x64 architecture, an explicit target-volume guard rejecting both drive C and the Windows system volume, application-root data paths, preserved `data`/`downloads` on normal uninstall, no MSI dependency, project-local compiler output/log/temp paths, and portable/installer version equality.

- [ ] **Step 2: Run the contract test and confirm installer files are missing**

Run: `.venv\Scripts\python.exe -m pytest tests/test_installer_contract.py -q`

Expected: fails with `FileNotFoundError` for `installer/TelegramDownloader.iss`.

- [ ] **Step 3: Implement and compile the installer**

Use Inno Setup 7 x64 with `PrivilegesRequired=lowest`. Select a fixed non-system drive by default and block Next/silent installation when the target is on C or the Windows system volume. Install the exact tested onedir runtime, create current-user shortcuts and uninstall metadata, preserve user-created data by default, and offer a separately confirmed uninstall option to remove `data` and `downloads`.

- [ ] **Step 4: Smoke test install, launch, update compatibility, and uninstall**

Build into `dist/release`; silently install under the worktree's D-drive `.build-temp/installed-smoke`, run `TelegramDownloader.exe --self-test`, verify all reported writable paths are under that install directory, seed sentinel data, uninstall normally, and verify the sentinel remains. Separately assert a C-drive target is rejected before copying application files.

- [ ] **Step 5: Commit installer delivery**

```powershell
git add installer scripts/build-installer.ps1 scripts/smoke-installer.ps1 tests/test_installer_contract.py README.md
git commit -m "build: add non-C-drive Windows installer"
```

### Task 17: Reproducible signed release and GitHub/ModelScope synchronization

**Files:**
- Create: `scripts/release/generate_manifest.py`
- Create: `scripts/release/publish_github.py`
- Create: `scripts/release/publish_modelscope.py`
- Create: `scripts/release/release.ps1`
- Create: `scripts/release/verify_remote_release.py`
- Create: `.github/workflows/verify.yml`
- Modify: `.gitignore`
- Modify: `README.md`
- Test: `tests/release/test_generate_manifest.py`
- Test: `tests/release/test_publish_contract.py`

- [ ] **Step 1: Write failing offline release tests**

Cover deterministic manifests, key ID/public key matching, no private key material in tracked files or logs, exact release asset sets, source/package version agreement, GitHub draft-before-publish behavior, ModelScope candidate-before-latest behavior, remote byte/hash comparison, failure before either latest pointer advances, idempotent retry, and all ModelScope/temp/cache paths resolving under the workspace.

- [ ] **Step 2: Run focused tests and confirm release scripts are missing**

Run: `.venv\Scripts\python.exe -m pytest tests/release -q`

Expected: fails because release scripts do not exist.

- [ ] **Step 3: Implement signing and fail-closed publication**

Generate an Ed25519 release key once, commit only its public key, and save the private key under ignored `.release-secrets` plus the GitHub Actions secret. `release.ps1` requires a clean `main`, a strict `X.Y.Z` version, passing tests, fresh packages, and matching local hashes. It pushes the same commit/tag to `github` and `modelscope`, uploads a GitHub draft Release and a versioned ModelScope candidate, downloads/compares both remote asset sets, then publishes/promotes both latest pointers. Secrets are read from environment or project-local ignored files and never echoed.

- [ ] **Step 4: Validate scripts offline and against disposable mocked adapters**

Run: `.venv\Scripts\python.exe -m pytest tests/release -q`

Run: `.venv\Scripts\ruff.exe check scripts/release tests/release`

Expected: all tests pass and injected failures leave both latest pointers unchanged.

- [ ] **Step 5: Create public repositories and publish the first formal release**

Create `lx3559359/TelegramDownloader` on GitHub and ModelScope if absent, configure remotes without embedding tokens, publish `main`, tag `v0.1.0`, source archive, portable ZIP, installer EXE, notes, manifest, and signature to both, then download each remote asset and compare SHA-256 with the local release set.

- [ ] **Step 6: Commit release automation**

```powershell
git add .github .gitignore scripts/release tests/release README.md src/telegram_downloader/trusted_update_keys.json
git commit -m "ci: publish signed releases to github and modelscope"
```

### Task 18: Acceptance audit and release candidate verification

**Files:**
- Modify: `README.md`
- Create: `docs/verification/2026-08-13-release-checklist.md`

- [ ] **Step 1: Run the complete automated quality gate from a clean process environment**

Run: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/test.ps1`

Expected: every pytest test passes and Ruff reports `All checks passed!`.

- [ ] **Step 2: Rebuild and verify package contents**

Run: `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build.ps1`

Expected: build exits 0, packaged self-test reports `PACKAGED_SMOKE_OK`, and both the portable ZIP and installer EXE exist.

Run: `Get-ChildItem -Recurse dist\TelegramDownloader | Select-Object FullName,Length`

Expected: the package contains the EXE and runtime libraries, but no pre-created `data`, `downloads`, `.venv`, source tests, or user credentials.

- [ ] **Step 3: Launch the packaged GUI and exercise the credential-free paths**

Run: `Start-Process -FilePath (Resolve-Path 'dist\TelegramDownloader\TelegramDownloader.exe') -WorkingDirectory (Resolve-Path 'dist\TelegramDownloader')`

Expected: the professional workbench opens, the account badge says `未登录`, the login wizard accepts API/proxy fields, invalid links show Chinese validation, and closing the window leaves no running `TelegramDownloader` process.

- [ ] **Step 4: Record evidence and the only credential-dependent checks**

Create `docs/verification/2026-08-13-release-checklist.md` with exact commands, timestamps, test counts, portable/installer sizes and hashes, self-test JSON, update fault-injection results, remote publication verification, and manual GUI observations. Mark these two checks as user-performed after delivery because secrets are intentionally never requested in chat:

```text
- [ ] 使用用户自己的 API ID/API Hash 完成手机号、验证码和可选两步验证登录。
- [ ] 使用账号有权访问的消息和频道验证真实单条下载、筛选批量下载及代理连接。
```

All non-secret acceptance items must be checked as passed before handoff; the two credential-dependent checks remain clearly labeled as post-delivery user verification, not silently claimed as tested.

- [ ] **Step 5: Commit the release evidence**

```powershell
git add README.md docs/verification/2026-08-13-release-checklist.md
git commit -m "docs: record signed release verification"
```

## Final implementation handoff checklist

- [ ] `git status --short` is empty except intentionally ignored runtime/build directories.
- [ ] `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/test.ps1` passes.
- [ ] `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build.ps1` passes.
- [ ] `dist/TelegramDownloader/TelegramDownloader.exe` launches without system Python.
- [ ] `dist/release/TelegramDownloader-<version>-win-x64-portable.zip` exists and contains no credentials or runtime data.
- [ ] `dist/release/TelegramDownloader-<version>-win-x64-setup.exe` installs only to a non-C drive and launches without system Python.
- [ ] Signed startup update, helper health check, and injected rollback failures pass for both delivery modes.
- [ ] GitHub and ModelScope public repositories contain the same `main`, `v<version>` tag, release assets, hashes, manifest, signature, and latest pointer.
- [ ] Every path in packaged `data/logs/self-test.json` resolves below the package root before the clean ZIP is produced.
- [ ] The final response links the EXE directory, ZIP, README, design, plan, and verification report using absolute paths.
