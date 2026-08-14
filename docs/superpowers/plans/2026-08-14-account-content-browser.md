# Account Content Browser Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为已登录 Telegram 账号增加群组/频道自动识别、单会话关键词搜索、持久化结果浏览、逐项选择和复用现有队列的下载能力。

**Architecture:** 新增独立 `catalog.sqlite3`、内容领域模型、Telegram 内容接口、`ContentBrowserService` 和 PySide6 内容浏览页。搜索目录与 `tasks.sqlite3` 隔离；用户提交选择时才转换成现有 `TaskPlanner`、`TaskRepository` 和 `DownloadScheduler` 能处理的任务，所有数据和缓存继续受 `PortablePaths` 约束。

**Tech Stack:** Python 3.12、PySide6 6.11、qasync、Telethon 1.44、SQLite、pytest/pytest-asyncio/pytest-qt、Ruff、PyInstaller、Inno Setup。

---

## 文件结构

**新增文件**

- `src/telegram_downloader/content.py`：账号、会话、搜索查询、搜索会话和结果领域类型。
- `src/telegram_downloader/catalog.py`：内容目录 SQLite 架构、迁移、群组缓存和搜索历史持久化。
- `src/telegram_downloader/thumbnail_cache.py`：受根目录约束、带单文件和总容量限制的缩略图缓存。
- `src/telegram_downloader/content_browser.py`：同步、分页搜索、历史、选择、缩略图和任务预览编排。
- `src/telegram_downloader/ui/content_models.py`：会话、历史和媒体结果 Qt 模型。
- `src/telegram_downloader/ui/content_browser.py`：独立内容浏览页面及其信号。
- `tests/test_content.py`
- `tests/test_catalog.py`
- `tests/test_thumbnail_cache.py`
- `tests/test_content_browser.py`
- `tests/ui/test_content_models.py`
- `tests/ui/test_content_browser.py`
- `docs/releases/v0.3.0.md`

**修改文件**

- `src/telegram_downloader/paths.py`、`src/telegram_downloader/app.py`：注册目录数据库和缩略图路径，初始化服务并扩展自检。
- `src/telegram_downloader/gateway.py`：账号资料、会话枚举、分页媒体搜索、相册补齐和缩略图接口。
- `src/telegram_downloader/domain.py`、`src/telegram_downloader/repository.py`、`src/telegram_downloader/planner.py`：任务显示标题迁移和已选远程媒体计划入口。
- `src/telegram_downloader/ui/main.py`、`src/telegram_downloader/ui/theme.py`、`src/telegram_downloader/controller.py`：导航切页、内容页样式、账号自动同步、搜索生命周期和队列提交。
- `src/telegram_downloader/ui/settings.py`：清理缩略图缓存操作。
- 对应现有测试文件、版本文件、`README.md` 和安装脚本。

---

### Task 1: 内容领域类型与项目内路径

**Files:**
- Create: `src/telegram_downloader/content.py`
- Modify: `src/telegram_downloader/paths.py`
- Modify: `src/telegram_downloader/app.py`
- Create: `tests/test_content.py`
- Modify: `tests/test_paths.py`
- Modify: `tests/test_self_test.py`

- [ ] **Step 1: 写查询规范化和路径失败测试**

```python
# tests/test_content.py
from datetime import UTC, datetime

import pytest

from telegram_downloader.content import ContentSearchQuery
from telegram_downloader.domain import MediaKind, ScanFilters


def test_content_query_normalizes_keyword_and_has_stable_fingerprint() -> None:
    filters = ScanFilters(
        datetime(2026, 8, 1, tzinfo=UTC),
        datetime(2026, 8, 14, tzinfo=UTC),
        frozenset({MediaKind.PHOTO, MediaKind.VIDEO}),
        500,
    )
    left = ContentSearchQuery("  安装Ａ  ", filters)
    right = ContentSearchQuery("安装A", filters)

    assert left.keyword == "安装Ａ"
    assert left.normalized_keyword == "安装a"
    assert left.filters_fingerprint == right.filters_fingerprint


@pytest.mark.parametrize("keyword", ["", "   "])
def test_content_query_rejects_empty_keyword(keyword: str) -> None:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    with pytest.raises(ValueError, match="关键词"):
        ContentSearchQuery(keyword, ScanFilters(now, now, frozenset(MediaKind), 500))


def test_content_query_rejects_more_than_ten_thousand_results() -> None:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    with pytest.raises(ValueError, match="10000"):
        ContentSearchQuery(
            "教程",
            ScanFilters(now, now, frozenset(MediaKind), 10001),
        )


def test_content_query_rejects_invalid_dates_and_empty_media_kinds() -> None:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    with pytest.raises(ValueError, match="日期"):
        ContentSearchQuery(
            "教程",
            ScanFilters(
                now,
                datetime(2026, 8, 13, tzinfo=UTC),
                frozenset(MediaKind),
                1,
            ),
        )
    with pytest.raises(ValueError, match="媒体类型"):
        ContentSearchQuery("教程", ScanFilters(now, now, frozenset(), 1))
```

在 `tests/test_paths.py` 的布局测试中增加：

```python
assert paths.catalog_database == tmp_path / "data" / "database" / "catalog.sqlite3"
assert paths.thumbnail_cache == tmp_path / "data" / "cache" / "thumbnails"
assert paths.thumbnail_cache.is_dir()
```

在 `tests/test_self_test.py::test_self_test_includes_update_storage_and_database` 中增加：

```python
assert "catalog_database" in report["writable_paths"]
assert "thumbnail_cache" in report["writable_paths"]
assert (tmp_path / "data" / "database" / "catalog.sqlite3").is_file()
```

- [ ] **Step 2: 运行测试确认缺少类型和路径**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests\test_content.py tests\test_paths.py tests\test_self_test.py -q
```

Expected: collection fails because `telegram_downloader.content` and the two new path properties do not exist.

- [ ] **Step 3: 创建完整内容领域类型**

```python
# src/telegram_downloader/content.py
from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from telegram_downloader.domain import MediaKind, ScanFilters


class DialogKind(StrEnum):
    GROUP = "group"
    CHANNEL = "channel"


class SearchStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    INCOMPLETE = "incomplete"


@dataclass(frozen=True, slots=True)
class AccountProfile:
    account_id: str
    display_name: str


@dataclass(frozen=True, slots=True)
class ContentDialog:
    account_id: str
    peer_ref: str
    title: str
    username: str
    kind: DialogKind
    archived: bool
    available: bool
    last_synced_at: datetime


@dataclass(frozen=True, slots=True)
class ContentSearchQuery:
    keyword: str
    filters: ScanFilters

    def __post_init__(self) -> None:
        cleaned = self.keyword.strip()
        if not cleaned:
            raise ValueError("搜索关键词不能为空")
        if self.filters.date_from_utc > self.filters.date_to_utc:
            raise ValueError("开始日期不能晚于结束日期")
        if not self.filters.media_kinds:
            raise ValueError("请至少选择一种媒体类型")
        if not 1 <= self.filters.item_limit <= 10_000:
            raise ValueError("搜索结果上限必须在 1 到 10000 之间")
        object.__setattr__(self, "keyword", cleaned)

    @property
    def normalized_keyword(self) -> str:
        return unicodedata.normalize("NFKC", self.keyword).casefold()

    @property
    def filters_fingerprint(self) -> str:
        value = {
            "dateFrom": self.filters.date_from_utc.isoformat(),
            "dateTo": self.filters.date_to_utc.isoformat(),
            "itemLimit": self.filters.item_limit,
            "mediaKinds": sorted(kind.value for kind in self.filters.media_kinds),
        }
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class SearchCursor:
    offset_id: int = 0


@dataclass(frozen=True, slots=True)
class SearchSession:
    id: str
    account_id: str
    peer_ref: str
    dialog_title: str
    query: ContentSearchQuery
    status: SearchStatus
    generation: int
    cursor: SearchCursor | None
    exhausted: bool
    result_count: int
    created_at: datetime
    updated_at: datetime
    last_error: str | None = None


@dataclass(frozen=True, slots=True)
class SearchResult:
    id: str
    search_id: str
    account_id: str
    peer_ref: str
    message_id: int
    grouped_id: int | None
    media_id: str
    media_kind: MediaKind
    original_name: str
    expected_size: int | None
    message_date_utc: datetime
    excerpt: str
    thumbnail_key: str
    selected: bool = False
    available: bool = True
    queued: bool = False
```

- [ ] **Step 4: 增加路径并让自检初始化目录数据库**

在 `PortablePaths` 中加入：

```python
@property
def catalog_database(self) -> Path:
    return self.data / "database" / "catalog.sqlite3"

@property
def thumbnail_cache(self) -> Path:
    return self.cache / "thumbnails"
```

把 `self.catalog_database.parent` 和 `self.thumbnail_cache` 加入 `ensure_layout()` 的目录集合。在 `run_self_test()` 中初始化 `CatalogRepository(paths.catalog_database)`，并把下列键加入 `writable`：

```python
"catalog_database": paths.catalog_database,
"thumbnail_cache": paths.thumbnail_cache,
```

本步骤创建 `src/telegram_downloader/catalog.py` 的最小可运行入口；紧接着的 Task 2 会在下一次独立提交中把它替换为已通过目录仓库测试的 v1 架构：

```python
from pathlib import Path


class CatalogRepository:
    def __init__(self, database: Path) -> None:
        self.database = database.resolve()

    def initialize(self) -> None:
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self.database.touch(exist_ok=True)
```

- [ ] **Step 5: 运行领域和路径测试**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests\test_content.py tests\test_paths.py tests\test_self_test.py -q
```

Expected: all selected tests pass.

- [ ] **Step 6: 提交领域和路径契约**

```powershell
git add src/telegram_downloader/content.py src/telegram_downloader/catalog.py src/telegram_downloader/paths.py src/telegram_downloader/app.py tests/test_content.py tests/test_paths.py tests/test_self_test.py
git commit -m "feat: add content browser domain paths"
```

---

### Task 2: 目录数据库、账号隔离和会话缓存

**Files:**
- Modify: `src/telegram_downloader/catalog.py`
- Create: `tests/test_catalog.py`

- [ ] **Step 1: 写账号隔离和会话同步失败测试**

```python
# tests/test_catalog.py
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from telegram_downloader.catalog import CatalogRepository
from telegram_downloader.content import AccountProfile, ContentDialog, DialogKind


def dialog(account: str, peer: str, title: str, now: datetime) -> ContentDialog:
    return ContentDialog(
        account, peer, title, "", DialogKind.GROUP, False, True, now
    )


def test_dialog_sync_is_account_scoped_and_marks_missing_unavailable(tmp_path: Path) -> None:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    repo = CatalogRepository(tmp_path / "catalog.sqlite3")
    repo.initialize()
    repo.upsert_account(AccountProfile("a1", "账号一"), now)
    repo.upsert_account(AccountProfile("a2", "账号二"), now)
    repo.replace_dialogs("a1", [dialog("a1", "-1001", "群一", now)], now)
    repo.replace_dialogs("a2", [dialog("a2", "-1002", "群二", now)], now)

    repo.replace_dialogs("a1", [], now)

    assert repo.list_dialogs("a1") == []
    stale = repo.list_dialogs("a1", include_unavailable=True)
    assert stale == [replace(dialog("a1", "-1001", "群一", now), available=False)]
    assert [item.title for item in repo.list_dialogs("a2")] == ["群二"]


def test_dialog_sync_updates_title_kind_and_archive_state(tmp_path: Path) -> None:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    repo = CatalogRepository(tmp_path / "catalog.sqlite3")
    repo.initialize()
    repo.upsert_account(AccountProfile("a1", "账号"), now)
    original = dialog("a1", "-1001", "旧标题", now)
    repo.replace_dialogs("a1", [original], now)
    changed = replace(
        original,
        title="新标题",
        username="new_name",
        kind=DialogKind.CHANNEL,
        archived=True,
    )

    repo.replace_dialogs("a1", [changed], now)

    assert repo.list_dialogs("a1") == [changed]


def test_most_recent_account_supports_offline_history(tmp_path: Path) -> None:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    repo = CatalogRepository(tmp_path / "catalog.sqlite3")
    repo.initialize()
    repo.upsert_account(AccountProfile("a1", "旧账号"), now)
    repo.upsert_account(AccountProfile("a2", "最近账号"), now.replace(second=1))

    assert repo.most_recent_account() == AccountProfile("a2", "最近账号")
```

- [ ] **Step 2: 运行测试确认仓库方法缺失**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests\test_catalog.py -q
```

Expected: fails because `upsert_account()`, `replace_dialogs()` and `list_dialogs()` do not exist.

- [ ] **Step 3: 用版本化 SQLite 架构替换临时实现**

在 `src/telegram_downloader/catalog.py` 中使用与 `TaskRepository` 相同的 WAL、外键、同步和 busy timeout 设置，并定义完整 v1 架构：

```python
_SCHEMA_V1 = """
CREATE TABLE accounts (
    account_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    last_used_at TEXT NOT NULL
);
CREATE TABLE dialogs (
    account_id TEXT NOT NULL REFERENCES accounts(account_id) ON DELETE CASCADE,
    peer_ref TEXT NOT NULL,
    title TEXT NOT NULL,
    username TEXT NOT NULL,
    kind TEXT NOT NULL,
    archived INTEGER NOT NULL CHECK(archived IN (0, 1)),
    available INTEGER NOT NULL CHECK(available IN (0, 1)),
    last_synced_at TEXT NOT NULL,
    PRIMARY KEY(account_id, peer_ref)
);
CREATE INDEX idx_dialogs_account_available_title
    ON dialogs(account_id, available, title COLLATE NOCASE);
CREATE TABLE search_sessions (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(account_id) ON DELETE CASCADE,
    peer_ref TEXT NOT NULL,
    dialog_title TEXT NOT NULL,
    keyword TEXT NOT NULL,
    normalized_keyword TEXT NOT NULL,
    date_from_utc TEXT NOT NULL,
    date_to_utc TEXT NOT NULL,
    media_kinds TEXT NOT NULL,
    item_limit INTEGER NOT NULL CHECK(item_limit BETWEEN 1 AND 10000),
    filters_fingerprint TEXT NOT NULL,
    status TEXT NOT NULL,
    generation INTEGER NOT NULL CHECK(generation > 0),
    next_offset_id INTEGER,
    exhausted INTEGER NOT NULL CHECK(exhausted IN (0, 1)),
    result_count INTEGER NOT NULL DEFAULT 0 CHECK(result_count >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_error TEXT,
    UNIQUE(account_id, peer_ref, normalized_keyword, filters_fingerprint)
);
CREATE TABLE search_results (
    id TEXT PRIMARY KEY,
    search_id TEXT NOT NULL REFERENCES search_sessions(id) ON DELETE CASCADE,
    account_id TEXT NOT NULL,
    peer_ref TEXT NOT NULL,
    message_id INTEGER NOT NULL CHECK(message_id > 0),
    grouped_id INTEGER,
    media_id TEXT NOT NULL,
    media_kind TEXT NOT NULL,
    original_name TEXT NOT NULL,
    expected_size INTEGER CHECK(expected_size IS NULL OR expected_size >= 0),
    message_date_utc TEXT NOT NULL,
    excerpt TEXT NOT NULL,
    thumbnail_key TEXT NOT NULL,
    selected INTEGER NOT NULL CHECK(selected IN (0, 1)),
    available INTEGER NOT NULL CHECK(available IN (0, 1)),
    queued INTEGER NOT NULL CHECK(queued IN (0, 1)),
    generation INTEGER NOT NULL CHECK(generation > 0),
    UNIQUE(search_id, peer_ref, message_id, media_id)
);
CREATE INDEX idx_results_search_generation_date
    ON search_results(search_id, generation, message_date_utc DESC, message_id DESC);
PRAGMA user_version=1;
"""
```

`initialize()` 必须拒绝未知的新版本，且只从版本 0 创建 v1：

```python
def initialize(self) -> None:
    self.database.parent.mkdir(parents=True, exist_ok=True)
    with self._connection() as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version == 0:
            connection.executescript(_SCHEMA_V1)
        elif version != 1:
            raise CatalogError(f"不支持的内容目录版本：{version}")
```

- [ ] **Step 4: 实现账号和会话方法**

```python
def upsert_account(self, profile: AccountProfile, used_at: datetime) -> None:
    with self._connection() as connection:
        connection.execute(
            "INSERT INTO accounts(account_id, display_name, last_used_at) VALUES(?, ?, ?) "
            "ON CONFLICT(account_id) DO UPDATE SET "
            "display_name=excluded.display_name, last_used_at=excluded.last_used_at",
            (profile.account_id, profile.display_name, used_at.isoformat()),
        )


def most_recent_account(self) -> AccountProfile | None:
    with self._connection() as connection:
        row = connection.execute(
            "SELECT account_id, display_name FROM accounts "
            "ORDER BY last_used_at DESC, account_id LIMIT 1"
        ).fetchone()
    if row is None:
        return None
    return AccountProfile(str(row["account_id"]), str(row["display_name"]))


def replace_dialogs(
    self,
    account_id: str,
    dialogs: list[ContentDialog],
    synced_at: datetime,
) -> None:
    if any(item.account_id != account_id for item in dialogs):
        raise ValueError("会话不属于当前账号")
    with self._connection() as connection:
        connection.execute(
            "UPDATE dialogs SET available=0, last_synced_at=? WHERE account_id=?",
            (synced_at.isoformat(), account_id),
        )
        for item in dialogs:
            connection.execute(
                "INSERT INTO dialogs(account_id, peer_ref, title, username, kind, "
                "archived, available, last_synced_at) VALUES(?, ?, ?, ?, ?, ?, 1, ?) "
                "ON CONFLICT(account_id, peer_ref) DO UPDATE SET "
                "title=excluded.title, username=excluded.username, kind=excluded.kind, "
                "archived=excluded.archived, available=1, "
                "last_synced_at=excluded.last_synced_at",
                (
                    account_id,
                    item.peer_ref,
                    item.title,
                    item.username,
                    item.kind.value,
                    int(item.archived),
                    synced_at.isoformat(),
                ),
            )


def list_dialogs(
    self, account_id: str, *, include_unavailable: bool = False
) -> list[ContentDialog]:
    where = "account_id=?" if include_unavailable else "account_id=? AND available=1"
    with self._connection() as connection:
        rows = connection.execute(
            "SELECT account_id, peer_ref, title, username, kind, archived, "
            f"available, last_synced_at FROM dialogs WHERE {where} "
            "ORDER BY title COLLATE NOCASE, peer_ref",
            (account_id,),
        ).fetchall()
    return [self._dialog_from_row(row) for row in rows]
```

`_dialog_from_row()` 使用以下确定性转换：

```python
@staticmethod
def _dialog_from_row(row: sqlite3.Row) -> ContentDialog:
    return ContentDialog(
        account_id=str(row["account_id"]),
        peer_ref=str(row["peer_ref"]),
        title=str(row["title"]),
        username=str(row["username"]),
        kind=DialogKind(str(row["kind"])),
        archived=bool(row["archived"]),
        available=bool(row["available"]),
        last_synced_at=datetime.fromisoformat(str(row["last_synced_at"])),
    )
```

- [ ] **Step 5: 运行目录仓库测试和 Ruff**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests\test_catalog.py -q
.venv\Scripts\python.exe -m ruff check src\telegram_downloader\catalog.py tests\test_catalog.py
```

Expected: both commands exit 0.

- [ ] **Step 6: 提交目录基础架构**

```powershell
git add src/telegram_downloader/catalog.py tests/test_catalog.py
git commit -m "feat: persist Telegram dialog catalog"
```

---

### Task 3: 搜索会话、分页结果和选择持久化

**Files:**
- Modify: `src/telegram_downloader/catalog.py`
- Modify: `tests/test_catalog.py`

- [ ] **Step 1: 写搜索刷新、选择保留和历史清理测试**

```python
from datetime import timedelta

from telegram_downloader.content import (
    ContentSearchQuery,
    SearchCursor,
    SearchResult,
    SearchStatus,
)
from telegram_downloader.domain import MediaKind, ScanFilters


def result(search_id: str, account_id: str, generation: int, now: datetime) -> SearchResult:
    del generation
    return SearchResult(
        "result-1",
        search_id,
        account_id,
        "-1001",
        7,
        None,
        "media-7",
        MediaKind.VIDEO,
        "x.mp4",
        12,
        now,
        "安装教程",
        "thumb-7",
    )


def test_search_refresh_preserves_selection_and_removes_stale_after_success(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    repo = CatalogRepository(tmp_path / "catalog.sqlite3")
    repo.initialize()
    repo.upsert_account(AccountProfile("a1", "账号"), now)
    repo.replace_dialogs("a1", [dialog("a1", "-1001", "群", now)], now)
    query = ContentSearchQuery(
        "安装",
        ScanFilters(now - timedelta(days=7), now, frozenset(MediaKind), 500),
    )
    first = repo.begin_search("search-1", "a1", "-1001", "群", query, now)
    repo.save_search_page("a1", first.id, first.generation, [result(first.id, "a1", 1, now)])
    repo.set_selected("a1", first.id, "result-1", True)
    repo.finish_search("a1", first.id, first.generation, SearchCursor(7), True, now)

    second = repo.begin_search("ignored", "a1", "-1001", "群", query, now)
    assert second.id == first.id
    assert second.generation == 2
    repo.save_search_page("a1", second.id, second.generation, [result(second.id, "a1", 2, now)])
    repo.finish_search("a1", second.id, second.generation, None, True, now)

    saved = repo.list_results("a1", second.id)
    assert len(saved) == 1
    assert saved[0].selected is True
    assert repo.list_sessions("a1")[0].status is SearchStatus.COMPLETED


def test_incomplete_search_keeps_current_generation_and_clear_is_account_scoped(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    repo = CatalogRepository(tmp_path / "catalog.sqlite3")
    repo.initialize()
    for account in ("a1", "a2"):
        repo.upsert_account(AccountProfile(account, account), now)
        repo.replace_dialogs(account, [dialog(account, f"-{account}", account, now)], now)
        query = ContentSearchQuery(
            "资料",
            ScanFilters(now, now, frozenset(MediaKind), 500),
        )
        session = repo.begin_search(f"s-{account}", account, f"-{account}", account, query, now)
        repo.finish_search(
            account,
            session.id,
            session.generation,
            SearchCursor(9),
            False,
            now,
            status=SearchStatus.INCOMPLETE,
            error="网络中断",
        )

    repo.clear_history("a1")

    assert repo.list_sessions("a1") == []
    assert len(repo.list_sessions("a2")) == 1
```

- [ ] **Step 2: 运行测试确认搜索 API 缺失**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests\test_catalog.py -q
```

Expected: new tests fail on `begin_search()` or `save_search_page()`.

- [ ] **Step 3: 实现搜索会话开始与读取**

`begin_search()` 通过唯一键原位刷新并递增 generation；新记录 generation 为 1：

```python
def begin_search(
    self,
    search_id: str,
    account_id: str,
    peer_ref: str,
    dialog_title: str,
    query: ContentSearchQuery,
    now: datetime,
) -> SearchSession:
    kinds = ",".join(sorted(kind.value for kind in query.filters.media_kinds))
    with self._connection() as connection:
        connection.execute(
            "INSERT INTO search_sessions(id, account_id, peer_ref, dialog_title, "
            "keyword, normalized_keyword, date_from_utc, date_to_utc, media_kinds, "
            "item_limit, filters_fingerprint, status, generation, next_offset_id, "
            "exhausted, result_count, created_at, updated_at, last_error) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, NULL, 0, 0, ?, ?, NULL) "
            "ON CONFLICT(account_id, peer_ref, normalized_keyword, filters_fingerprint) "
            "DO UPDATE SET dialog_title=excluded.dialog_title, keyword=excluded.keyword, "
            "status=excluded.status, generation=search_sessions.generation+1, "
            "next_offset_id=NULL, exhausted=0, result_count=0, "
            "updated_at=excluded.updated_at, last_error=NULL",
            (
                search_id, account_id, peer_ref, dialog_title, query.keyword,
                query.normalized_keyword, query.filters.date_from_utc.isoformat(),
                query.filters.date_to_utc.isoformat(), kinds, query.filters.item_limit,
                query.filters_fingerprint, SearchStatus.RUNNING.value,
                now.isoformat(), now.isoformat(),
            ),
        )
        row = connection.execute(
            "SELECT * FROM search_sessions WHERE account_id=? AND peer_ref=? "
            "AND normalized_keyword=? AND filters_fingerprint=?",
            (account_id, peer_ref, query.normalized_keyword, query.filters_fingerprint),
        ).fetchone()
    return self._session_from_row(row)
```

实现 `get_session(account_id, search_id)` 和 `list_sessions(account_id)`；两者的 SQL 都显式带 `account_id=?`，后者按 `updated_at DESC, id` 排序并调用 `_session_from_row()` 重建 `ContentSearchQuery`、`SearchCursor` 和枚举。

```python
@staticmethod
def _session_from_row(row: sqlite3.Row) -> SearchSession:
    filters = ScanFilters(
        datetime.fromisoformat(str(row["date_from_utc"])),
        datetime.fromisoformat(str(row["date_to_utc"])),
        frozenset(
            MediaKind(value)
            for value in str(row["media_kinds"]).split(",")
            if value
        ),
        int(row["item_limit"]),
    )
    cursor_value = row["next_offset_id"]
    return SearchSession(
        id=str(row["id"]),
        account_id=str(row["account_id"]),
        peer_ref=str(row["peer_ref"]),
        dialog_title=str(row["dialog_title"]),
        query=ContentSearchQuery(str(row["keyword"]), filters),
        status=SearchStatus(str(row["status"])),
        generation=int(row["generation"]),
        cursor=SearchCursor(int(cursor_value)) if cursor_value is not None else None,
        exhausted=bool(row["exhausted"]),
        result_count=int(row["result_count"]),
        created_at=datetime.fromisoformat(str(row["created_at"])),
        updated_at=datetime.fromisoformat(str(row["updated_at"])),
        last_error=str(row["last_error"]) if row["last_error"] is not None else None,
    )
```

- [ ] **Step 4: 实现分页 upsert、完成、选择和删除**

```python
def save_search_page(
    self,
    account_id: str,
    search_id: str,
    generation: int,
    results: list[SearchResult],
) -> None:
    if any(
        item.search_id != search_id or item.account_id != account_id
        for item in results
    ):
        raise ValueError("搜索结果不属于当前搜索")
    with self._connection() as connection:
        session = connection.execute(
            "SELECT peer_ref, generation FROM search_sessions "
            "WHERE account_id=? AND id=?",
            (account_id, search_id),
        ).fetchone()
        if session is None or int(session["generation"]) != generation:
            raise StaleSearchError("搜索结果已被更新的搜索代次取代")
        if any(item.peer_ref != str(session["peer_ref"]) for item in results):
            raise ValueError("搜索结果不属于当前会话")
        for item in results:
            connection.execute(
                "INSERT INTO search_results(id, search_id, account_id, peer_ref, "
                "message_id, grouped_id, media_id, media_kind, original_name, "
                "expected_size, message_date_utc, excerpt, thumbnail_key, selected, "
                "available, queued, generation) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(search_id, peer_ref, message_id, media_id) DO UPDATE SET "
                "original_name=excluded.original_name, expected_size=excluded.expected_size, "
                "message_date_utc=excluded.message_date_utc, excerpt=excluded.excerpt, "
                "thumbnail_key=excluded.thumbnail_key, available=excluded.available, "
                "generation=excluded.generation",
                (
                    item.id, item.search_id, item.account_id, item.peer_ref,
                    item.message_id, item.grouped_id, item.media_id,
                    item.media_kind.value, item.original_name, item.expected_size,
                    item.message_date_utc.isoformat(), item.excerpt,
                    item.thumbnail_key, int(item.selected), int(item.available),
                    int(item.queued), generation,
                ),
            )


def finish_search(
    self,
    account_id: str,
    search_id: str,
    generation: int,
    cursor: SearchCursor | None,
    exhausted: bool,
    now: datetime,
    *,
    status: SearchStatus = SearchStatus.COMPLETED,
    error: str | None = None,
) -> None:
    with self._connection() as connection:
        session = connection.execute(
            "SELECT 1 FROM search_sessions "
            "WHERE account_id=? AND id=? AND generation=?",
            (account_id, search_id, generation),
        ).fetchone()
        if session is None:
            raise StaleSearchError("搜索结果已被更新的搜索代次取代")
        count = connection.execute(
            "SELECT COUNT(*) FROM search_results "
            "WHERE account_id=? AND search_id=? AND generation=?",
            (account_id, search_id, generation),
        ).fetchone()[0]
        connection.execute(
            "UPDATE search_sessions SET status=?, next_offset_id=?, exhausted=?, "
            "result_count=?, updated_at=?, last_error=? "
            "WHERE account_id=? AND id=? AND generation=?",
            (
                status.value,
                cursor.offset_id if cursor else None,
                int(exhausted),
                count,
                now.isoformat(),
                error,
                account_id,
                search_id,
                generation,
            ),
        )
        if status is SearchStatus.COMPLETED:
            connection.execute(
                "DELETE FROM search_results "
                "WHERE account_id=? AND search_id=? AND generation<>?",
                (account_id, search_id, generation),
            )
```

定义 `StaleSearchError(CatalogError)`，让旧代次的迟到分页不能覆盖新刷新结果。`list_results(account_id, search_id)` 连接 `search_sessions`，显式限定账号且只返回当前 generation；`set_selected(account_id, search_id, result_id, selected)` 必须在同一账号和 `search_id` 下更新一行并在 rowcount 不为 1 时抛 `KeyError`。实现 `mark_queued(account_id, result_ids)`、`delete_session(account_id, search_id)` 和 `clear_history(account_id)`，删除必须依赖外键级联且不能跨账号。

`_result_from_row()` 按以下字段映射，避免 SQLite 整数和字符串泄漏到领域层：

```python
@staticmethod
def _result_from_row(row: sqlite3.Row) -> SearchResult:
    grouped = row["grouped_id"]
    expected = row["expected_size"]
    return SearchResult(
        id=str(row["id"]),
        search_id=str(row["search_id"]),
        account_id=str(row["account_id"]),
        peer_ref=str(row["peer_ref"]),
        message_id=int(row["message_id"]),
        grouped_id=int(grouped) if grouped is not None else None,
        media_id=str(row["media_id"]),
        media_kind=MediaKind(str(row["media_kind"])),
        original_name=str(row["original_name"]),
        expected_size=int(expected) if expected is not None else None,
        message_date_utc=datetime.fromisoformat(str(row["message_date_utc"])),
        excerpt=str(row["excerpt"]),
        thumbnail_key=str(row["thumbnail_key"]),
        selected=bool(row["selected"]),
        available=bool(row["available"]),
        queued=bool(row["queued"]),
    )
```

- [ ] **Step 5: 运行仓库测试**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests\test_catalog.py -q
```

Expected: all catalog tests pass.

- [ ] **Step 6: 提交搜索持久化**

```powershell
git add src/telegram_downloader/catalog.py tests/test_catalog.py
git commit -m "feat: persist content search history"
```

---

### Task 4: Telegram 账号资料与群组/频道枚举

**Files:**
- Modify: `src/telegram_downloader/gateway.py`
- Modify: `tests/test_gateway.py`

- [ ] **Step 1: 写账号稳定 ID、会话过滤和归档枚举失败测试**

在 `tests/test_gateway.py` 增加：

```python
from telegram_downloader.content import AccountProfile, DialogKind


@pytest.mark.asyncio
async def test_account_profile_uses_stable_id_and_display_name() -> None:
    class Client:
        async def get_me(self):
            return SimpleNamespace(
                id=42,
                first_name="张",
                last_name="三",
                username="zhangsan",
            )

    gateway = TelethonGateway.from_client_for_test(Client())

    assert await gateway.account_profile() == AccountProfile("42", "张 三")


@pytest.mark.asyncio
async def test_content_dialogs_include_active_and_archived_but_not_users_or_bots() -> None:
    group = SimpleNamespace(id=101, title="普通群", username="group")
    channel = SimpleNamespace(id=102, title="资料频道", username="docs")
    user = SimpleNamespace(id=103, first_name="某人", bot=False)
    bot = SimpleNamespace(id=104, first_name="机器人", bot=True)

    class Client:
        def iter_dialogs(self, *, archived=False):
            values = (
                [
                    SimpleNamespace(entity=group, name="普通群", is_group=True, is_channel=False),
                    SimpleNamespace(entity=user, name="某人", is_group=False, is_channel=False),
                    SimpleNamespace(entity=bot, name="机器人", is_group=False, is_channel=False),
                ]
                if not archived
                else [
                    SimpleNamespace(
                        entity=channel,
                        name="资料频道",
                        is_group=False,
                        is_channel=True,
                    )
                ]
            )

            async def generate():
                for value in values:
                    yield value

            return generate()

    gateway = TelethonGateway.from_client_for_test(
        Client(), peer_id_getter=lambda entity: -1_000_000_000_000 - entity.id
    )

    found = [item async for item in gateway.iter_content_dialogs("42")]

    assert [(item.title, item.kind, item.archived) for item in found] == [
        ("普通群", DialogKind.GROUP, False),
        ("资料频道", DialogKind.CHANNEL, True),
    ]
    assert all(item.account_id == "42" for item in found)
    assert [item.peer_ref for item in found] == ["-1000000000101", "-1000000000102"]
```

再增加一个测试：让第二次 `iter_dialogs(archived=True)` 抛出测试用访问异常，断言 `iter_content_dialogs()` 通过现有 `_raise_mapped()` 转换为 `AccessDeniedError`，且不泄漏原始异常文本。

- [ ] **Step 2: 运行网关测试确认新协议缺失**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests\test_gateway.py -q
```

Expected: new tests fail because `account_profile()` and `iter_content_dialogs()` do not exist.

- [ ] **Step 3: 扩展协议并实现账号资料**

在 `TelegramGateway` 中增加：

```python
async def account_profile(self) -> AccountProfile:
    raise NotImplementedError

def iter_content_dialogs(
    self,
    account_id: str,
) -> AsyncIterator[ContentDialog]:
    raise NotImplementedError
```

在 `TelethonGateway` 中实现 `account_profile()`；账号不存在或没有整数 `id` 时抛 `GatewayError("Telegram 账号尚未登录")`。抽取 `_account_display_name(account)`，让现有 `account_name()` 调用 `account_profile()` 并返回 `display_name`，避免两套名称规则漂移。

```python
async def account_profile(self) -> AccountProfile:
    try:
        account = await self._client.get_me()
    except Exception as exc:
        self._raise_mapped(exc)
    account_id = getattr(account, "id", None) if account is not None else None
    if not isinstance(account_id, int):
        raise GatewayError("Telegram 账号尚未登录")
    return AccountProfile(str(account_id), self._account_display_name(account))
```

- [ ] **Step 4: 实现只含群组/频道的双目录枚举**

```python
async def iter_content_dialogs(
    self,
    account_id: str,
) -> AsyncIterator[ContentDialog]:
    seen: set[str] = set()
    try:
        for archived in (False, True):
            async for dialog in self._client.iter_dialogs(archived=archived):
                is_group = bool(getattr(dialog, "is_group", False))
                is_channel = bool(getattr(dialog, "is_channel", False))
                if not (is_group or is_channel):
                    continue
                entity = getattr(dialog, "entity", None)
                if entity is None:
                    continue
                peer_ref = str(self._peer_id_getter(entity))
                if peer_ref in seen:
                    continue
                seen.add(peer_ref)
                title = str(
                    getattr(dialog, "name", "")
                    or getattr(entity, "title", "")
                    or peer_ref
                )
                yield ContentDialog(
                    account_id,
                    peer_ref,
                    title,
                    str(getattr(entity, "username", "") or ""),
                    DialogKind.GROUP if is_group else DialogKind.CHANNEL,
                    archived,
                    True,
                    datetime.now(UTC),
                )
    except Exception as exc:
        self._raise_mapped(exc)
```

会话时间由后续服务同步时统一替换为同一个时钟值，网关中的时间只满足纯对象完整性；不要返回 Telethon entity。

- [ ] **Step 5: 运行网关回归和 Ruff**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests\test_gateway.py -q
.venv\Scripts\python.exe -m ruff check src\telegram_downloader\gateway.py tests\test_gateway.py
```

Expected: both commands exit 0.

- [ ] **Step 6: 提交会话枚举能力**

```powershell
git add src/telegram_downloader/gateway.py tests/test_gateway.py
git commit -m "feat: enumerate account groups and channels"
```

---

### Task 5: 服务端媒体分页搜索、相册补齐与缩略图读取

**Files:**
- Modify: `src/telegram_downloader/gateway.py`
- Modify: `tests/test_gateway.py`

- [ ] **Step 1: 写 100 项分页、过滤和安全摘要失败测试**

先在网关模块定义测试将使用的纯对象接口：`RemoteSearchHit(remote, excerpt, thumbnail_key)` 与 `RemoteSearchPage(items, next_cursor, exhausted)`。随后在 `tests/test_gateway.py` 增加一个假客户端，它记录 `search`、`offset_id`、`limit` 和最后实际遍历的消息 ID，并从 101 条混合日期/媒体类型消息中按 limit 返回；断言：

```python
query = ContentSearchQuery(
    "安装教程",
    ScanFilters(
        datetime(2026, 8, 1, tzinfo=UTC),
        datetime(2026, 8, 14, 23, 59, tzinfo=UTC),
        frozenset({MediaKind.VIDEO}),
        500,
    ),
)
page = await gateway.search_media_page("-1001", query, SearchCursor())

assert client.calls[0] == {
    "search": "安装教程",
    "offset_id": 0,
    "limit": 100,
}
assert len(page.items) <= 100
assert all(item.remote.kind is MediaKind.VIDEO for item in page.items)
assert all(len(item.excerpt) <= 500 for item in page.items)
assert page.next_cursor == SearchCursor(client.last_scanned_id)
assert page.exhausted is False
```

加入含控制字符和 600 个字符正文的消息，断言摘要移除不可显示字符并截断到 500 个 Unicode 字符。再测无更多消息时 `next_cursor is None` 且 `exhausted is True`。

- [ ] **Step 2: 写相册补齐与缩略图失败测试**

增加测试：命中消息 `message_id=50, grouped_id=900`，周边消息 49、50、51 属于同一相册，48 属于另一相册；断言 `expand_album("-1001", 50, 900)` 按消息 ID 升序返回 49、50、51。增加缩略图测试：

```python
assert await gateway.load_thumbnail("-1001", 50, "m50") == b"jpeg"
assert await gateway.load_thumbnail("-1001", 404, "missing") is None
```

假客户端的 `download_media(message.media, file=bytes, thumb=-1)` 返回 `b"jpeg"`；消息不存在、无媒体或 Telethon 返回非字节值时返回 `None`。访问、网络和限流异常仍必须进入 `_raise_mapped()`。

- [ ] **Step 3: 运行测试确认分页接口缺失**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests\test_gateway.py -q
```

Expected: fails on missing `RemoteSearchPage` or `search_media_page()`.

- [ ] **Step 4: 增加分页与相册协议**

```python
@dataclass(frozen=True, slots=True)
class RemoteSearchHit:
    remote: RemoteMedia
    excerpt: str
    thumbnail_key: str


@dataclass(frozen=True, slots=True)
class RemoteSearchPage:
    items: tuple[RemoteSearchHit, ...]
    next_cursor: SearchCursor | None
    exhausted: bool
```

在 `TelegramGateway` 增加以下方法，参数和返回类型保持一致：

```python
async def search_media_page(
    self,
    peer_ref: str,
    query: ContentSearchQuery,
    cursor: SearchCursor | None,
) -> RemoteSearchPage:
    raise NotImplementedError

async def expand_album(
    self,
    peer_ref: str,
    message_id: int,
    grouped_id: int,
) -> tuple[RemoteSearchHit, ...]:
    raise NotImplementedError

async def load_thumbnail(
    self,
    peer_ref: str,
    message_id: int,
    media_id: str,
) -> bytes | None:
    raise NotImplementedError
```

- [ ] **Step 5: 实现服务端搜索和分页游标**

`search_media_page()` 必须使用 `iter_messages(entity, search=query.keyword, offset_id=cursor.offset_id if cursor else 0, limit=100)`，逐条调用现有 `remote_media_from_message()`，再应用 UTC 日期和 `media_kinds` 过滤。保存最后检查过的消息 ID 作为游标，不能只保存最后一个命中的媒体 ID，否则过滤密集时会重复页面。读取满 100 条且未越过开始日期时 `exhausted=False`；否则到末尾。`RemoteSearchHit.thumbnail_key` 固定为 `"{peer_ref}:{message_id}:{media_id}"`。

增加确定性摘要工具：

```python
@staticmethod
def _message_excerpt(message: object) -> str:
    raw = str(getattr(message, "message", "") or "")
    visible = "".join(char for char in raw if char.isprintable() or char in "\n\t")
    return " ".join(visible.split())[:500]
```

`expand_album()` 复用 `_ALBUM_RADIUS` 和 `_resolve_entity()`，只保留相同 `grouped_id` 且可转换为 `RemoteMedia` 的成员，按 `message_id` 升序去重。`load_thumbnail()` 必须重新按 peer/message 定位媒体，绝不信任 `media_id` 作为 Telethon 实体；`media_id` 仅用于校验当前 `RemoteMedia.media_id` 与调用值一致，不一致时返回 `None`。

- [ ] **Step 6: 运行网关定向与全量测试**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests\test_gateway.py -q
.venv\Scripts\python.exe -m pytest tests\test_planner.py tests\test_downloader.py -q
.venv\Scripts\python.exe -m ruff check src\telegram_downloader\gateway.py tests\test_gateway.py
```

Expected: all commands exit 0.

- [ ] **Step 7: 提交内容读取网关**

```powershell
git add src/telegram_downloader/gateway.py tests/test_gateway.py
git commit -m "feat: search Telegram media pages"
```

---

### Task 6: 任务显示标题迁移与已选媒体计划入口

**Files:**
- Modify: `src/telegram_downloader/domain.py`
- Modify: `src/telegram_downloader/repository.py`
- Modify: `src/telegram_downloader/planner.py`
- Modify: `src/telegram_downloader/controller.py`
- Modify: `tests/test_repository.py`
- Modify: `tests/test_planner.py`
- Modify: `tests/test_controller.py`

- [ ] **Step 1: 写旧库迁移和显示标题回归测试**

在 `tests/test_repository.py` 用 `sqlite3` 创建当前 v0.2.3 的 `tasks` 表（没有 `display_title`），插入一条旧任务后调用 `TaskRepository.initialize()`，断言旧任务 `display_title is None`。再保存一条 `display_title="资料群（搜索：安装）"` 的新任务，关闭并重新打开仓库，断言完整 round trip。

在 `tests/test_controller.py` 构造 `source_title="资料群"`、`display_title="资料群（搜索：安装）"` 的任务，断言任务表摘要使用 `display_title`；调用 `open_task_directory()` 时仍打开 `downloads/资料群`，不创建含关键词的目录。

- [ ] **Step 2: 写选择项去重与归档路径测试**

在 `tests/test_planner.py` 增加：

```python
def test_plan_selected_uses_search_title_but_archives_under_source(tmp_path: Path) -> None:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    existing = {("-1001", 8, "m8")}
    repo = FakeRepository()
    repo.existing = existing
    planner = TaskPlanner(
        FakeGateway([]),
        repo,
        tmp_path,
        uuid_factory=iter(["task", "item-9"]).__next__,
        clock=lambda: now,
    )
    query = ContentSearchQuery(
        "安装",
        ScanFilters(now, now, frozenset({MediaKind.VIDEO}), 500),
    )
    selected = [
        RemoteMedia("-1001", "资料群", 9, None, "m9", MediaKind.VIDEO, "a.mp4", 10, now),
        RemoteMedia("-1001", "资料群", 8, None, "m8", MediaKind.VIDEO, "b.mp4", 20, now),
    ]

    preview = planner.plan_selected("-1001", "资料群", query, selected)

    assert preview.task.display_title == "资料群（搜索：安装）"
    assert [item.message_id for item in preview.items] == [9]
    assert preview.items[0].target_path.is_relative_to(tmp_path / "资料群")
```

在 `FakeRepository.__init__()` 中增加 `self.existing = set()`，并增加 `existing_media_keys(self, keys)` 返回 `keys & self.existing`；这样原链接扫描测试仍使用同一仓库协议。再测输入为空和全部已存在时均抛 `EmptyScanError("所选媒体已全部存在于下载队列")`，且不创建任务。增加提交竞态测试：预览形成后先由另一任务占用其中一个媒体键，`commit_selected()` 应原子写入仍可用的项并返回 `skipped_count=1`；若全部键都被占用则回滚新任务行并抛 `EmptyScanError`。

- [ ] **Step 3: 运行测试确认迁移和计划入口缺失**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests\test_repository.py tests\test_planner.py tests\test_controller.py -q
```

Expected: new tests fail because `TaskRecord.display_title` and `plan_selected()` do not exist.

- [ ] **Step 4: 增加兼容旧数据的任务标题字段**

把 `display_title: str | None = None` 放在 `TaskRecord` 最后，保持现有位置参数构造兼容。新建表的 `tasks` 增加 `display_title TEXT`，并在 `initialize()` 中执行幂等迁移：

```python
columns = {
    str(row[1])
    for row in connection.execute("PRAGMA table_info(tasks)").fetchall()
}
if "display_title" not in columns:
    connection.execute("ALTER TABLE tasks ADD COLUMN display_title TEXT")
```

把 `display_title` 追加到 `_TASK_COLUMNS`、`_task_values()` 和 `_task_from_row()`，不要改变已有列顺序。控制器生成 `TaskSummary` 时使用 `task.display_title or task.source_title`，`open_task_directory()` 保持使用 `task.source_title`。

- [ ] **Step 5: 增加数据库级媒体键查询**

在仓库增加：

```python
def existing_media_keys(
    self,
    keys: set[tuple[str, int, str]],
) -> set[tuple[str, int, str]]:
    found: set[tuple[str, int, str]] = set()
    ordered = sorted(keys)
    with self._connection() as connection:
        for chunk in batched(ordered, 200):
            where = " OR ".join(
                "(peer_ref=? AND message_id=? AND media_id=?)" for _ in chunk
            )
            parameters = [value for key in chunk for value in key]
            rows = connection.execute(
                "SELECT peer_ref, message_id, media_id FROM media_items WHERE " + where,
                parameters,
            ).fetchall()
            found.update((str(row[0]), int(row[1]), str(row[2])) for row in rows)
    return found
```

空集合直接返回空集合。保留 `media_items` 的唯一约束作为提交瞬间的最终防线。

- [ ] **Step 6: 抽取共同预览构建并实现 `plan_selected()`**

让 `scan()` 只负责从网关取回媒体，然后调用新的私有 `_build_preview`。增加同步入口：

```python
def plan_selected(
    self,
    source_ref: str,
    source_title: str,
    query: ContentSearchQuery,
    selected: list[RemoteMedia],
) -> ScanPreview:
    return self._build_preview(
        source_kind=SourceKind.CHANNEL_OR_GROUP,
        source_ref=source_ref,
        source_title=source_title,
        source_url=f"telegram://peer/{source_ref}",
        filters=query.filters,
        remote=selected,
        display_title=f"{source_title}（搜索：{query.keyword}）",
        empty_message="所选媒体已全部存在于下载队列",
    )
```

`_build_preview()` 接受 `skip_existing: bool`：先用现有 `_deduplicate()`，仅在该值为真时用 `repository.existing_media_keys()` 去掉已入任务库的键，然后执行现有目标路径、大小统计和 UUID 逻辑。增加公开只读代理 `TaskPlanner.existing_media_keys(keys)`，让内容服务在预览前形成重复统计；`plan_selected()` 内仍再次检查，防止检查与计划之间的竞态。`plan_selected()` 传 `skip_existing=True`；链接扫描传 `skip_existing=False`、`display_title=None`，保持原行为和“筛选范围内没有找到可下载媒体”错误。把 `existing_media_keys()` 加入 `TaskWriter` 协议。

`TaskWriter` 协议新增 `create_task_deduplicating(task, items) -> list[MediaItem]`。`TaskRepository` 在一个事务中先插入任务，再逐项调用现有 `_insert_item()`；捕获 `sqlite3.IntegrityError` 后只在同一 `(peer_ref, message_id, media_id)` 已存在时计为重复，其他约束错误继续抛出。如果最终一项都未插入，抛出仓库内 `AllMediaAlreadyExists`，让整个事务回滚，不能留下空任务。

在 planner 定义：

```python
@dataclass(frozen=True, slots=True)
class SelectedCommit:
    task: TaskRecord
    accepted_keys: frozenset[tuple[str, int, str]]
    skipped_count: int
```

`commit_selected(preview)` 把任务状态改为 `QUEUED`，调用 `create_task_deduplicating()`，把实际插入项转换为 `accepted_keys` 并计算 skipped；捕获 `AllMediaAlreadyExists` 后抛 `EmptyScanError("所选媒体已全部存在于下载队列")`。现有 `commit()` 和链接扫描继续使用严格 `create_task()`。

- [ ] **Step 7: 运行迁移、计划与控制器回归**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests\test_repository.py tests\test_planner.py tests\test_controller.py -q
.venv\Scripts\python.exe -m ruff check src\telegram_downloader\domain.py src\telegram_downloader\repository.py src\telegram_downloader\planner.py src\telegram_downloader\controller.py tests\test_repository.py tests\test_planner.py tests\test_controller.py
```

Expected: both commands exit 0.

- [ ] **Step 8: 提交任务队列复用边界**

```powershell
git add src/telegram_downloader/domain.py src/telegram_downloader/repository.py src/telegram_downloader/planner.py src/telegram_downloader/controller.py tests/test_repository.py tests/test_planner.py tests/test_controller.py
git commit -m "feat: plan selected catalog media"
```

---

### Task 7: 有边界、可清理的项目内缩略图缓存

**Files:**
- Create: `src/telegram_downloader/thumbnail_cache.py`
- Create: `tests/test_thumbnail_cache.py`

- [ ] **Step 1: 写键隔离、单项上限和 LRU 失败测试**

```python
# tests/test_thumbnail_cache.py
import os
from pathlib import Path

from telegram_downloader.thumbnail_cache import ThumbnailCache


def test_thumbnail_keys_are_hashed_and_account_scoped(tmp_path: Path) -> None:
    cache = ThumbnailCache(tmp_path / "thumbnails", max_total_bytes=1024)

    first = cache.put("a1:-1001:7:m7", b"one")
    second = cache.put("a2:-1001:7:m7", b"two")

    assert first is not None and first.parent == (tmp_path / "thumbnails").resolve()
    assert second is not None and first != second
    assert "-1001" not in first.name
    assert cache.get("a1:-1001:7:m7") == first


def test_oversized_thumbnail_is_not_written(tmp_path: Path) -> None:
    cache = ThumbnailCache(
        tmp_path / "thumbnails",
        max_item_bytes=4,
        max_total_bytes=1024,
    )

    assert cache.put("key", b"12345") is None
    assert list((tmp_path / "thumbnails").iterdir()) == []


def test_total_limit_evicts_least_recently_used_file(tmp_path: Path) -> None:
    cache = ThumbnailCache(tmp_path / "thumbnails", max_total_bytes=6)
    old = cache.put("old", b"111")
    recent = cache.put("recent", b"222")
    assert old is not None and recent is not None
    os.utime(old, ns=(1, 1))
    os.utime(recent, ns=(2, 2))

    newest = cache.put("newest", b"333")

    assert newest is not None
    assert old.exists() is False
    assert recent.exists() is True
    assert cache.total_bytes() == 6


def test_clear_returns_removed_count_and_bytes(tmp_path: Path) -> None:
    cache = ThumbnailCache(tmp_path / "thumbnails", max_total_bytes=1024)
    cache.put("a", b"12")
    cache.put("b", b"345")

    assert cache.clear() == (2, 5)
    assert cache.total_bytes() == 0


def test_delete_removes_only_the_requested_key(tmp_path: Path) -> None:
    cache = ThumbnailCache(tmp_path / "thumbnails", max_total_bytes=1024)
    cache.put("a", b"12")
    kept = cache.put("b", b"345")

    assert cache.delete("a") is True
    assert kept is not None and kept.exists()
```

- [ ] **Step 2: 运行测试确认缓存模块缺失**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests\test_thumbnail_cache.py -q
```

Expected: collection fails because `telegram_downloader.thumbnail_cache` does not exist.

- [ ] **Step 3: 实现哈希路径、原子写入和 LRU 清理**

```python
# src/telegram_downloader/thumbnail_cache.py
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from uuid import uuid4


class ThumbnailCache:
    def __init__(
        self,
        root: Path,
        *,
        max_item_bytes: int = 256 * 1024,
        max_total_bytes: int = 512 * 1024 * 1024,
    ) -> None:
        if max_item_bytes < 1 or max_total_bytes < 1:
            raise ValueError("缩略图缓存上限必须大于零")
        self.root = root.resolve()
        self.max_item_bytes = max_item_bytes
        self.max_total_bytes = max_total_bytes
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        path = (self.root / f"{digest}.thumb").resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("缩略图路径越出应用目录")
        return path

    def get(self, key: str) -> Path | None:
        path = self.path_for(key)
        if not path.is_file():
            return None
        os.utime(path, None)
        return path

    def put(self, key: str, content: bytes) -> Path | None:
        if not content or len(content) > self.max_item_bytes:
            return None
        path = self.path_for(key)
        temporary = self.root / f"{path.name}.{uuid4().hex}.tmp"
        try:
            temporary.write_bytes(content)
            os.replace(temporary, path)
            self._prune()
            return path if path.exists() else None
        finally:
            temporary.unlink(missing_ok=True)
```

完成 `delete(key)`、`total_bytes()`、`clear()` 和 `_prune()`：只遍历 `*.thumb`；`delete` 只删除 `path_for(key)`；按 `(st_mtime_ns, name)` 从旧到新删除，直到总量不超过上限；文件在并发间消失时捕获 `FileNotFoundError`。不得遍历或删除 `root` 之外的路径。原子写入失败时在 `finally` 中清理同名 `.tmp`。

- [ ] **Step 4: 运行缓存测试和 Ruff**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests\test_thumbnail_cache.py -q
.venv\Scripts\python.exe -m ruff check src\telegram_downloader\thumbnail_cache.py tests\test_thumbnail_cache.py
```

Expected: both commands exit 0.

- [ ] **Step 5: 提交缩略图缓存**

```powershell
git add src/telegram_downloader/thumbnail_cache.py tests/test_thumbnail_cache.py
git commit -m "feat: cache thumbnails inside application data"
```

---

### Task 8: 内容浏览服务、分页生命周期与选择下载准备

**Files:**
- Create: `src/telegram_downloader/content_browser.py`
- Modify: `src/telegram_downloader/catalog.py`
- Create: `tests/test_content_browser.py`
- Modify: `tests/test_catalog.py`

- [ ] **Step 1: 写缓存先显示、同步串行化和账号切换测试**

使用内存假网关、真实临时 `CatalogRepository`、假 `TaskPlanner` 和真实小容量 `ThumbnailCache`。先在 catalog 放入最近账号，断网且尚未调用网关时执行 `activate_cached_account()`，断言可以返回该账号的会话、历史和结果快照，但 `online=False`。随后恢复网关，测试顺序固定为：

```python
profile, cached = await service.activate_account()
assert profile == AccountProfile("a1", "账号一")
assert [item.title for item in cached] == ["旧缓存群"]

fresh = await service.sync_dialogs()
assert [item.title for item in fresh] == ["新同步群"]
assert catalog.list_dialogs("a1") == fresh
```

假网关让第一次 `sync_dialogs()` 停在 `asyncio.Event`，同时启动第二次，断言网关枚举峰值为 1。随后把网关账号切为 `a2` 并调用 `activate_account()`，断言服务当前账号、会话和搜索读取全部切到 `a2`，不能返回 `a1` 数据。

- [ ] **Step 2: 写首批、加载更多、相册去重与数量上限测试**

假网关第一页返回 100 项及 `SearchCursor(700)`，其中两项属于同一 `grouped_id`；`expand_album()` 返回重叠成员。断言：

```python
session, first_page = await service.start_search("-1001", query)
assert session.status is SearchStatus.RUNNING
assert len(first_page) <= 100
assert catalog.get_session("a1", session.id).cursor == SearchCursor(700)

session, all_results = await service.load_more(session.id)
assert session.status is SearchStatus.COMPLETED
assert session.exhausted is True
assert len({(r.peer_ref, r.message_id, r.media_id) for r in all_results}) == len(all_results)
assert len(all_results) <= query.filters.item_limit
```

验证相册成员按消息 ID 排序、可逐项选择，且相册补齐和下一页重叠不会创建重复结果。把其中一个媒体键预先写入任务库，断言新搜索首次落库时该结果已经 `queued=True, selected=False`。把上限设为 101 且第二页使用普通非相册结果，断言第二页只保存剩余 1 项并将会话标记完成。另加边界测试：一个相册不能拆成两批；当前批容纳不下时整组延后到下一批，总上限容纳不下时整组跳过并明确标记“达到数量上限”。

- [ ] **Step 3: 写取消、网络中断和进程恢复测试**

让第二页网关分别抛 `asyncio.CancelledError` 和 `TransientNetworkError`。两种情况下均断言第一页仍在数据库，搜索状态为 `INCOMPLETE`，游标保留为最后成功页；取消必须继续向上传播，网络错误必须转换为安全摘要且不能记录关键词或消息正文。

在 `tests/test_catalog.py` 增加：初始化一个 `RUNNING` 会话后重新打开仓库并调用 `recover_interrupted_searches("a1", now)`，断言状态变成 `INCOMPLETE`、错误为“上次搜索未正常结束”，已有结果和选择不变。

- [ ] **Step 4: 写选择准备、重复报告和缩略图回退测试**

勾选 4 项：2 项有效、1 项 `available=False`、1 项已存在任务库。断言 `prepare_download()` 返回：

```python
assert preparation.selected_count == 4
assert len(preparation.preview.items) == 2
assert preparation.duplicate_count == 1
assert preparation.unavailable_count == 1
assert len(preparation.preview_result_ids) == 2
```

模拟 `commit_selected()` 成功后调用 `finalize_queue(search_id, joined_count=2)`，重开目录库并断言新加入的两项和已存在的一项都显示 `queued=True` 且不可再次选择；返回报告中的加入数为 2、重复数为 1。全重复或全不可用时不产生空任务。缩略图测试先命中本地缓存，不调用网关；未命中时下载并写入以 `account_id:peer_ref:message_id:media_id` 为键的缓存；网关缩略图异常返回 `None`，不改变搜索结果可用状态。

再建立两个引用同一缩略图键的搜索记录：删除第一条历史后缓存仍存在；删除第二条后缓存被移除。给另一个账号建立独立缩略图，清空 `a1` 历史后 `a2` 文件和记录必须保留。

- [ ] **Step 5: 运行测试确认服务和仓库辅助方法缺失**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests\test_content_browser.py tests\test_catalog.py -q
```

Expected: fails on missing `ContentBrowserService` and catalog lifecycle methods.

- [ ] **Step 6: 补齐目录仓库的服务边界方法**

在 `CatalogRepository` 增加下列精确签名，并逐一保持账号限制：`get_dialog(account_id: str, peer_ref: str) -> ContentDialog`、`get_session(account_id: str, search_id: str) -> SearchSession`、`list_sessions(account_id: str) -> list[SearchSession]`、`list_results(account_id: str, search_id: str, *, selected_only: bool = False) -> list[SearchResult]`、`set_selected(account_id: str, search_id: str, result_id: str, selected: bool) -> None`、`mark_queued(account_id: str, result_ids: tuple[str, ...]) -> None`、`mark_unavailable(account_id: str, result_ids: tuple[str, ...]) -> None`、`list_thumbnail_keys(account_id: str, search_id: str | None = None) -> set[str]`、`referenced_thumbnail_keys(account_id: str, keys: set[str]) -> set[str]` 和 `recover_interrupted_searches(account_id: str, now: datetime) -> int`。

`set_selected(account_id, search_id, result_id, True)` 的 SQL 必须连接 `search_sessions` 并附带 `account_id=? AND available=1 AND queued=0`；不满足时抛 `ValueError("该媒体当前不可选择")`。`mark_queued()` 同一事务内只更新属于该账号的结果并设置 `queued=1, selected=0`。`recover_interrupted_searches()` 只更新当前账号且 `status='running'` 的会话并返回影响行数。把 Task 3 中的行转换器完整实现为：字符串枚举转换、ISO datetime 转换、逗号媒体类型还原为 `frozenset[MediaKind]`，空 `next_offset_id` 还原为 `None`；缺行统一抛 `KeyError`。

- [ ] **Step 7: 实现内容服务状态对象与账号激活**

```python
@dataclass(frozen=True, slots=True)
class DownloadPreparation:
    preview: ScanPreview
    selected_count: int
    preview_result_ids: tuple[str, ...]
    duplicate_count: int
    unavailable_count: int


class ContentBrowserService:
    def __init__(
        self,
        catalog: CatalogRepository,
        thumbnails: ThumbnailCache,
        *,
        gateway: TelegramGateway | None = None,
        planner: TaskPlanner | None = None,
        uuid_factory: Callable[[], str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.gateway = gateway
        self.catalog = catalog
        self.planner = planner
        self.thumbnails = thumbnails
        self.uuid_factory = uuid_factory or (lambda: str(uuid4()))
        self.clock = clock or (lambda: datetime.now(UTC))
        self.account: AccountProfile | None = None
        self._sync_lock = asyncio.Lock()
        self._search_lock = asyncio.Lock()
```

构造器把可选依赖分别保存为 `self.gateway` 和 `self.planner`。增加 `bind_online(gateway, planner)` 与 `go_offline()`；前者只在控制器已取消旧后台任务后替换两个引用，后者清空引用但保留账号和缓存读取能力。`activate_cached_account()` 只调用 `catalog.most_recent_account()` 并返回该账号缓存快照，绝不触网；没有账号时返回空值。`activate_account()` 要求在线依赖存在，调网关取得 profile、upsert 账号、恢复 RUNNING 搜索并立刻返回缓存列表。`sync_dialogs()` 在 `_sync_lock` 内枚举，使用 `dataclasses.replace(item, account_id=profile.account_id, last_synced_at=now)` 统一时间后一次 `replace_dialogs()`；失败时不清空旧缓存。

- [ ] **Step 8: 实现每次最多一页的搜索状态机**

`start_search()` 在 `_search_lock` 内校验当前账号和可用会话、调用 `begin_search()`，然后只取首批一页。`load_more()` 读取同账号会话及保存的 cursor，只取下一页。共同 `_fetch_page()` 必须：

1. 调 `gateway.search_media_page()`；
2. 对本页首次出现的 `grouped_id` 调 `expand_album()`；
3. 以 `(peer_ref, message_id, media_id)` 去重并按 `message_date_utc DESC, message_id DESC, media_id` 排序；
4. 每次最多保留 `min(100, 会话剩余 item_limit)` 项；相册作为不可拆分单元，当前批容纳不下时整组延后并把下一游标设为触发消息 ID 加 1，总上限容纳不下时整组跳过，绝不保存残缺相册；
5. 用稳定结果 ID 保存：`uuid5(NAMESPACE_URL, f"{search_id}:{peer}:{message}:{media}")`，并把持久化 `thumbnail_key` 设为 `f"{account_id}:{peer}:{message}:{media}"`；保存前批量调用 `planner.existing_media_keys()`，已存在项直接设 `queued=True, selected=False`；
6. 保存页后，以“Telegram 已耗尽或达到 item_limit”为完成条件，否则保持 `RUNNING` 并保存下一游标；
7. 每次返回 `catalog.get_session(account_id, search_id)` 和 `catalog.list_results(account_id, search_id)` 的数据库真值。

捕获 `asyncio.CancelledError` 时写 `INCOMPLETE` 后重新抛出；捕获 `GatewayError` 时写安全错误摘要后重新抛出。若第一页失败且没有结果，也保留该历史记录。不要在普通日志写 `query.keyword`、`excerpt` 或完整 `SearchResult`。

- [ ] **Step 9: 实现选择、下载准备和按需缩略图**

`set_selected()`、`select_all()`、`invert_selection()` 委托目录仓库并始终跳过 `available=False` 或 `queued=True` 的行。`prepare_download(search_id)` 把可用且未入队的选择转换为 `RemoteMedia`，先调用 `planner.existing_media_keys()` 形成初始重复集合，再把剩余项交给 Task 6 的 `planner.plan_selected()` 做第二次数据库防重复；最后根据 preview 中的媒体键计算接收、重复和不可用数量。全重复时抛出携带三个跳过计数的 `NothingToQueueError`，控制器只报告结果，不弹空预览。

`finalize_queue(search_id, joined_count)` 在任务提交后重新读取本次仍选中的结果并查询 `planner.existing_media_keys()`，把此刻确实存在于任务库的结果全部标记 queued；返回的最终重复数为“有效选择数减 joined_count”，因此覆盖预览与提交之间的竞态。`load_thumbnail(result_id)` 先查缓存，再调用网关；除 `CancelledError` 外的缩略图错误返回 `None`。

`delete_history(search_id)` 和 `clear_history()` 必须先记录当前账号受影响的缩略图键，提交数据库删除后再查询仍被当前账号其他搜索引用的键，只对无引用差集调用 `ThumbnailCache.delete()`；缓存清理失败不回滚已经提交的数据库删除，只返回安全警告。

- [ ] **Step 10: 运行服务、目录与相关回归测试**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests\test_content_browser.py tests\test_catalog.py tests\test_planner.py tests\test_repository.py -q
.venv\Scripts\python.exe -m ruff check src\telegram_downloader\content_browser.py src\telegram_downloader\catalog.py tests\test_content_browser.py tests\test_catalog.py
```

Expected: both commands exit 0.

- [ ] **Step 11: 提交内容服务**

```powershell
git add src/telegram_downloader/content_browser.py src/telegram_downloader/catalog.py tests/test_content_browser.py tests/test_catalog.py
git commit -m "feat: orchestrate persisted content searches"
```

---

### Task 9: 群组、搜索历史和媒体结果 Qt 模型

**Files:**
- Create: `src/telegram_downloader/ui/content_models.py`
- Create: `tests/ui/test_content_models.py`

- [ ] **Step 1: 写会话过滤和角色数据失败测试**

```python
# tests/ui/test_content_models.py
from PySide6.QtCore import Qt

from telegram_downloader.ui.content_models import DialogListModel


def test_dialog_model_filters_by_title_or_username(dialogs) -> None:
    model = DialogListModel()
    model.set_dialogs(dialogs)
    model.set_filter("docs")

    assert model.rowCount() == 1
    index = model.index(0, 0)
    assert model.data(index, Qt.ItemDataRole.DisplayRole) == "资料频道"
    assert model.data(index, Qt.ItemDataRole.UserRole) == "-1002"
    assert model.dialog_at(0).username == "docs"
```

测试 helper 中同时放入群组、频道、归档和不可用项；断言显示文本含“已归档”或“不可用”，筛选使用 Unicode `casefold()`，排序为可用优先、标题、peer ID。

- [ ] **Step 2: 写历史状态和结果勾选模型失败测试**

`SearchHistoryTableModel` 固定列为“群组/频道、关键词、筛选、状态、结果数、更新时间”，`UserRole` 返回搜索 ID，`ToolTipRole` 显示安全错误。`SearchResultTableModel` 固定列为“选择、预览、日期、摘要、类型、大小、状态”，并测试：

```python
changed = []
model.selection_changed.connect(lambda result_id, selected: changed.append((result_id, selected)))
model.set_results(results)
select_index = model.index(0, 0)

assert model.flags(select_index) & Qt.ItemFlag.ItemIsUserCheckable
assert model.setData(select_index, Qt.CheckState.Checked, Qt.ItemDataRole.CheckStateRole)
assert changed == [(results[0].id, True)]
assert model.data(select_index, Qt.ItemDataRole.UserRole) == results[0].id
```

对 `available=False` 和 `queued=True` 行断言没有 `ItemIsUserCheckable`；大小未知显示“未知”；缩略图路径存在时 `DecorationRole` 返回图像，缺失时返回按媒体类型生成的图标。

- [ ] **Step 3: 运行模型测试确认模块缺失**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests\ui\test_content_models.py -q
```

Expected: collection fails because `telegram_downloader.ui.content_models` does not exist.

- [ ] **Step 4: 实现三个模型并保持模型无数据库依赖**

实现 `DialogListModel(QAbstractListModel)`、`SearchHistoryTableModel(QAbstractTableModel)` 和 `SearchResultTableModel(QAbstractTableModel)` 三个类；结果模型定义 `selection_changed = Signal(str, bool)`。

三个模型只持有不可变领域对象快照，不直接访问 `CatalogRepository` 或网关。`set_dialogs()`、`set_sessions()`、`set_results()` 使用 `beginResetModel()/endResetModel()`。结果模型用 `set_thumbnail(result_id, path)` 发射单行 `dataChanged`；`setData()` 先在内存中执行 `replace(result, selected=requested_state)` 再发信号，若控制器持久化失败则通过 `set_results()` 回滚为数据库快照。

- [ ] **Step 5: 运行模型测试和 Ruff**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests\ui\test_content_models.py -q
.venv\Scripts\python.exe -m ruff check src\telegram_downloader\ui\content_models.py tests\ui\test_content_models.py
```

Expected: both commands exit 0.

- [ ] **Step 6: 提交内容模型**

```powershell
git add src/telegram_downloader/ui/content_models.py tests/ui/test_content_models.py
git commit -m "feat: add content browser Qt models"
```

---

### Task 10: 独立内容浏览页面与主窗口导航

**Files:**
- Create: `src/telegram_downloader/ui/content_browser.py`
- Modify: `src/telegram_downloader/ui/main.py`
- Modify: `src/telegram_downloader/ui/theme.py`
- Create: `tests/ui/test_content_browser.py`
- Modify: `tests/ui/test_main_window.py`

- [ ] **Step 1: 写页面结构、未登录状态和搜索参数失败测试**

在 `tests/ui/test_content_browser.py` 创建页面并断言：左列有会话名称筛选框、同步状态和刷新按钮；右列有当前会话、关键词、起止日期、六种媒体类型、1–10,000 上限（默认 500）、搜索/取消按钮；中部有“搜索结果”和“搜索记录”两个标签页；底部有选择统计、全选、反选和加入队列按钮。

```python
def test_logged_out_page_keeps_history_visible_but_disables_online_actions(qtbot) -> None:
    page = ContentBrowserPage()
    qtbot.addWidget(page)
    page.set_logged_in(False)

    assert page.refresh_button.isEnabled() is False
    assert page.search_button.isEnabled() is False
    assert page.queue_button.isEnabled() is False
    assert page.history_table.isEnabled() is True
    assert "登录" in page.empty_hint.text()
```

选择一个可用会话、输入带首尾空白的关键词并点击搜索，断言 `search_requested` 发出 peer ID、去空白关键词、两个 `date`、媒体类型集合和上限；关键词为空、日期倒置或媒体类型全不选时只显示本地校验错误，不发信号。

- [ ] **Step 2: 写选择统计、历史操作和可见缩略图测试**

装载包含已知/未知大小、不可用和已入队状态的结果，断言只有可选行参与全选/反选，底部显示“已选 2 项 · 已知 3.0 MB · 1 项大小未知”。点击加入队列发出当前搜索 ID。历史页双击行发出 `history_open_requested(search_id)`；删除单条和清空历史分别发出独立信号并携带当前 ID。

把 30 行结果装入固定高度表格，仅让前几行可见；页面显示或滚动后，`thumbnail_requested(result_id)` 只为 `visualRect(index).intersects(viewport.rect())` 的行发出，并用集合避免同一结果重复请求。

- [ ] **Step 3: 写主导航切页和统计栏显示测试**

在 `tests/ui/test_main_window.py` 增加：

```python
def test_content_navigation_switches_page_and_hides_statistics(qtbot) -> None:
    window = MainWindow()
    qtbot.addWidget(window)
    window.show()

    qtbot.mouseClick(window.content_nav_button, Qt.MouseButton.LeftButton)

    assert window.page_stack.currentWidget() is window.content_page
    assert window.statistics_panel.isHidden() is True
    assert window.content_nav_button.property("active") is True

    qtbot.mouseClick(window.tasks_nav_button, Qt.MouseButton.LeftButton)
    assert window.page_stack.currentWidget() is window.task_page
    assert window.statistics_panel.isHidden() is False
```

同时断言账号登录和设置按钮仍发原有信号，任务中心原控件及键盘选择行为没有变化。

- [ ] **Step 4: 运行 UI 测试确认页面缺失**

Run:

```powershell
$env:QT_QPA_PLATFORM='offscreen'
.venv\Scripts\python.exe -m pytest tests\ui\test_content_browser.py tests\ui\test_main_window.py -q
```

Expected: collection or assertions fail because content page and stacked navigation do not exist.

- [ ] **Step 5: 实现 `ContentBrowserPage` 的稳定信号接口**

页面定义：

```python
class ContentBrowserPage(QWidget):
    refresh_requested = Signal()
    search_requested = Signal(str, str, object, object, object, int)
    cancel_search_requested = Signal()
    load_more_requested = Signal(str)
    history_open_requested = Signal(str)
    history_delete_requested = Signal(str)
    history_clear_requested = Signal()
    selection_changed = Signal(str, bool)
    queue_requested = Signal(str)
    thumbnail_requested = Signal(str)
```

组合 Task 9 的三个模型。提供 `set_logged_in()`、`set_dialogs()`、`set_sync_state()`、`set_sessions()`、`set_active_search()`、`set_results()`、`set_search_busy()`、`set_thumbnail()` 和 `show_error()`；所有按钮可用状态集中在 `_refresh_actions()`，避免信号处理器各自修改导致状态矛盾。`set_results()` 根据领域对象重新计算选择统计，并安排一次可见行缩略图检查。

“加载更多”仅在当前会话 `status=RUNNING`、`exhausted=False`、未搜索中时显示。取消按钮只在搜索中显示。不可用历史仍可打开查看，但不能搜索、选择或加入队列。

- [ ] **Step 6: 将主工作区改成堆叠页面**

在 `MainWindow` 中保留 `_build_workspace()` 作为任务页内容，将其赋给 `self.task_page`；创建 `self.content_page = ContentBrowserPage()` 与 `self.page_stack = QStackedWidget()`。把 `_build_statistics()` 的返回值保存为 `self.statistics_panel`。导航按钮保存为字段并统一通过：

```python
def show_page(self, name: str) -> None:
    content = name == "content"
    self.page_stack.setCurrentWidget(self.content_page if content else self.task_page)
    self.statistics_panel.setVisible(not content)
    self._set_nav_active(self.content_nav_button if content else self.tasks_nav_button)
```

切页只影响显示，不取消下载或搜索。更新样式属性后调用 `style().unpolish()/polish()`，确保动态 active 状态生效。在 `DARK_STYLESHEET` 增加内容页左右分栏、紧凑表格、历史标签、选择栏和禁用/不可用行状态；复用现有青色主操作色、深色边框和 CJK 字体，不引入外部图片或字体资源。

- [ ] **Step 7: 运行 UI 全量回归和 Ruff**

Run:

```powershell
$env:QT_QPA_PLATFORM='offscreen'
.venv\Scripts\python.exe -m pytest tests\ui -q
.venv\Scripts\python.exe -m ruff check src\telegram_downloader\ui\content_browser.py src\telegram_downloader\ui\main.py src\telegram_downloader\ui\theme.py tests\ui\test_content_browser.py tests\ui\test_main_window.py
```

Expected: both commands exit 0.

- [ ] **Step 8: 提交内容浏览界面**

```powershell
git add src/telegram_downloader/ui/content_browser.py src/telegram_downloader/ui/main.py src/telegram_downloader/ui/theme.py tests/ui/test_content_browser.py tests/ui/test_main_window.py
git commit -m "feat: add account content browser page"
```

---

### Task 11: 应用装配、控制器生命周期与设置页缓存清理

**Files:**
- Modify: `src/telegram_downloader/app.py`
- Modify: `src/telegram_downloader/controller.py`
- Modify: `src/telegram_downloader/ui/settings.py`
- Modify: `tests/test_app.py`
- Modify: `tests/test_controller.py`
- Modify: `tests/ui/test_settings_dialog.py`

- [ ] **Step 1: 写启动缓存先显示、后台自动同步和同步失败测试**

在 `tests/test_controller.py` 使用阻塞同步假服务。`activate_cached_account()` 在任何网络调用前返回最近账号缓存；即使 `gateway.connect()` 失败，也断言内容页仍显示历史且在线搜索/入队禁用。在线路径中 `activate_account()` 返回 profile 和缓存会话，`sync_dialogs()` 等待事件。调用 `await controller.start()` 后断言缓存已交给内容页、主窗口账号已显示、自动同步已经启动但没有阻塞启动；释放事件后断言新列表替换缓存。同步抛异常时旧列表保留，状态栏只显示安全错误类型。

再测 `_finish_login()` 同样激活内容账号并自动同步一次；提交新 API 凭据切换 gateway 前，活动搜索和同步被取消并等待完成，旧账号结果不再写入新账号页面。

- [ ] **Step 2: 写搜索、加载更多、选择与入队控制器测试**

覆盖以下可观察流程：

```python
await controller.search_content("-1001", query)
assert window.content_page.active_search_id == "search-1"
assert window.content_page.results == first_page

await controller.load_more_content("search-1")
controller.set_content_selected("search-1", "result-1", True)
await controller.queue_content_selection("search-1")
```

断言用户确认后依次发生 `planner.commit_selected(preview)`、`content_browser.finalize_queue("search-1", joined_count=2)`、`refresh_tasks()` 和 `_start_task(task.id)`；取消确认时不 commit、不标记 queued。状态栏报告“选择 4 项，加入 2 项，跳过重复 1 项，不可用 1 项”。搜索期间调用 `cancel_content_search()`，断言协程取消、页面恢复可操作且部分结果仍显示。

- [ ] **Step 3: 写设置页清缓存和关闭生命周期测试**

`SettingsDialog` 增加“缩略图缓存”只读大小标签和“清理”按钮。点击按钮发 `thumbnail_cache_clear_requested`，不自动关闭设置页。控制器调用 `ThumbnailCache.clear()` 后更新标签并显示移除文件数/大小；该操作不得调用 `CatalogRepository.clear_history()`。

关闭测试同时启动同步、搜索和缩略图任务；`shutdown()` 必须先取消并等待这些任务，再关闭 scheduler，最后断开 gateway，断言没有 pending task 警告。

- [ ] **Step 4: 运行测试确认装配与控制器入口缺失**

Run:

```powershell
$env:QT_QPA_PLATFORM='offscreen'
.venv\Scripts\python.exe -m pytest tests\test_app.py tests\test_controller.py tests\ui\test_settings_dialog.py -q
```

Expected: new tests fail because content service dependencies and signal handlers are not wired.

- [ ] **Step 5: 在应用根中初始化目录功能且保持数据库隔离**

`run_self_test()` 和 `create_application()` 都创建并初始化：

```python
catalog = CatalogRepository(paths.catalog_database)
catalog.initialize()
thumbnails = ThumbnailCache(paths.thumbnail_cache)
```

账号级 `recover_interrupted_searches(profile.account_id, now)` 由 `ContentBrowserService.activate_account()` 在取得稳定账号 ID 后调用。若 catalog 初始化失败，生产应用捕获该错误并把内容页设为只读错误状态，但继续初始化 `TaskRepository`、恢复并运行已有下载。应用无论是否已有 Telegram 凭据都创建离线可读的 `ContentBrowserService(catalog, thumbnails)`；`build_services()` 创建新 planner/scheduler，调用 `content_browser.bind_online(gateway, planner)`，并返回 `(planner, scheduler, content_browser)`。新 gateway 登录成功前先取消旧内容任务，再绑定新依赖，不能让内容服务继续引用旧 gateway。

- [ ] **Step 6: 实现控制器内容生命周期**

给 `AppController` 增加可选 `content_browser`，并把 `service_builder` 类型改为三元组。增加受跟踪任务字段 `_dialog_sync_task`、`_content_search_task`、`_thumbnail_tasks: dict[str, asyncio.Task[None]]`。核心方法固定为 `activate_cached_content_account() -> None`、`activate_content_account() -> None`、`refresh_content_dialogs() -> None`、`search_content(peer_ref: str, query: ContentSearchQuery) -> None`、`load_more_content(search_id: str) -> None`、`cancel_content_search() -> None`、`set_content_selected(search_id: str, result_id: str, selected: bool) -> None`、`queue_content_selection(search_id: str) -> None`、`request_thumbnail(result_id: str) -> None`、`delete_content_history(search_id: str) -> None` 和 `clear_content_history() -> None`；前五个和 `queue_content_selection` 为异步方法，其余为同步方法。

`start()` 在连接 Telegram 前先 await `activate_cached_content_account()` 展示最近账号的离线缓存；连接并确认已登录后再 await `activate_content_account()` 校正真实账号。`_finish_login()` 直接走在线激活。在线激活先展示该账号缓存，再用 `_spawn_background()` 触发一次同步。手动同步与自动同步复用同一方法和 service lock。搜索开始时保存当前 task，以便取消；`finally` 从数据库重载 session/results，保证失败或取消后的部分结果可见。

入队流程调用 `prepare_download()`、现有 `confirm_preview()`、`planner.commit_selected()`、`finalize_queue()`、`refresh_tasks()` 和 `_start_task()`。`commit_selected()` 在同一事务中跳过提交瞬间的新重复项；若全部变成重复则回滚空任务。`finalize_queue()` 重新读取数据库真值、刷新结果状态并给出最终跳过报告。

- [ ] **Step 7: 连接内容页和设置页信号**

在 `create_application()` 使用 `qasync.asyncSlot` 为刷新、搜索、加载更多、加入队列和缩略图建立异步槽；同步槽连接取消、选择、历史删除/清空。搜索槽通过 `AppController.filters_from_dates()` 构建 filters 后再创建 `ContentSearchQuery`，但内容浏览数量范围使用 10,000；任务中心原链接扫描仍保留 100,000 上限。

设置页构造时传入 `thumbnails.total_bytes()`，清理信号连接 `controller.clear_thumbnail_cache()`；清理后只刷新当前设置对话框的缓存标签。把所有动态槽保存在 `_ui_slots`，避免被垃圾回收。

- [ ] **Step 8: 运行控制器、应用和 UI 集成测试**

Run:

```powershell
$env:QT_QPA_PLATFORM='offscreen'
.venv\Scripts\python.exe -m pytest tests\test_app.py tests\test_controller.py tests\ui\test_settings_dialog.py tests\ui\test_content_browser.py tests\ui\test_main_window.py -q
.venv\Scripts\python.exe -m ruff check src\telegram_downloader\app.py src\telegram_downloader\controller.py src\telegram_downloader\ui\settings.py tests\test_app.py tests\test_controller.py tests\ui\test_settings_dialog.py
```

Expected: both commands exit 0.

- [ ] **Step 9: 提交完整应用接入**

```powershell
git add src/telegram_downloader/app.py src/telegram_downloader/controller.py src/telegram_downloader/ui/settings.py tests/test_app.py tests/test_controller.py tests/ui/test_settings_dialog.py
git commit -m "feat: wire content browser lifecycle"
```

---

### Task 12: 0.3.0 文档、全量验证、便携包与安装包候选构建

**Files:**
- Modify: `pyproject.toml`
- Modify: `src/telegram_downloader/__init__.py`
- Modify: `installer/TelegramDownloader.iss`
- Modify: `README.md`
- Create: `docs/releases/v0.3.0.md`
- Modify: `tests/test_packaging_contract.py`
- Modify: `tests/test_installer_contract.py`
- Modify: `scripts/smoke.ps1`
- Modify: `scripts/smoke-installer.ps1`

- [ ] **Step 1: 写 0.3.0 版本和用户数据排除合同失败测试**

把 `test_v023_version_and_qr_runtime_contract_are_consistent` 重命名为 `test_v030_version_and_content_runtime_contract_are_consistent`，断言 `pyproject.toml`、`__init__.py` 和 Inno 默认版本均为 `0.3.0`，同时检查 `app.py` 直接导入并装配 `ContentBrowserService`、`CatalogRepository` 与 `ThumbnailCache`，使 PyInstaller 能从真实导入图发现新增模块。增加 ZIP/安装源合同：便携包构建在压缩前删除 `data`、`downloads`；安装脚本仍排除两者，因此真实 `catalog.sqlite3`、缩略图、搜索历史和 session 不进入产物。

在安装器合同中要求 smoke 脚本明确创建并哈希：

```text
data\database\catalog.sqlite3
data\cache\thumbnails\preserve.thumb
data\sentinel.keep
```

并在原位升级和普通卸载后验证三个哈希不变。

- [ ] **Step 2: 运行合同测试确认版本与 smoke 保护缺失**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests\test_packaging_contract.py tests\test_installer_contract.py -q
```

Expected: fails on 0.2.3 version strings and missing catalog/thumbnail preservation assertions.

- [ ] **Step 3: 更新版本、中文说明和候选发布说明**

统一更新到 `0.3.0`，在 `README.md` 写明：内容浏览入口、只包含已加入群组/频道、关键词必填、首批/加载更多、相册逐项选择、历史与缩略图位置、清理行为、离线可查看但不可搜索、重复跳过、所有数据均在应用目录。`docs/releases/v0.3.0.md` 记录功能、兼容性、数据迁移、已知边界和验证命令；不得写入账号 ID、群组名、关键词或真实本地路径。

本任务只准备候选版本，不运行 `scripts/release/release.ps1`，不创建 GitHub Release、不推送魔搭、不更新远端 stable 指针。

- [ ] **Step 4: 强化直接启动和安装升级的数据保持 smoke**

`scripts/smoke.ps1` 检查 self-test 的 `catalog_database` 与 `thumbnail_cache` 均解析在 `dist\TelegramDownloader` 下。`scripts/smoke-installer.ps1` 在首次安装后创建有效最小 catalog 数据库、缩略图和 sentinel，记录 SHA-256；原位安装同一候选包后和普通卸载后分别重算并比较。测试数据库只包含合成值，所有路径继续经过 `Assert-ProjectChild()`。

便携构建验证现有运行目录 `data` 时，在运行 `scripts/build.ps1` 前对上述三类文件记录哈希，构建后确认恢复目录中文件及哈希不变；生成的 portable ZIP 内不得有 `data/` 或 `downloads/` 项。

- [ ] **Step 5: 运行全部 Python 测试与静态检查**

Run:

```powershell
$env:QT_QPA_PLATFORM='offscreen'
.\scripts\test.ps1
```

Expected: pytest, Ruff and all checks exit 0; no existing login,链接扫描, scheduler, update signature or release contract regression.

- [ ] **Step 6: 构建并验证直接启动目录和便携 ZIP**

Run:

```powershell
.\scripts\build.ps1
```

Expected:

- `dist\TelegramDownloader\TelegramDownloader.exe --self-test` 已由脚本执行成功；报告版本为 `0.3.0`。
- `dist\TelegramDownloader-0.3.0-win-x64-portable.zip` 存在。
- self-test 中 `catalog_database`、`thumbnail_cache` 及全部其他 writable path 都位于应用目录。
- 构建前已有的项目内 catalog、缩略图和 downloads 哈希不变；ZIP 不携带用户数据。

- [ ] **Step 7: 构建并验证安装包**

Run:

```powershell
.\scripts\build-installer.ps1 -SkipAppBuild
```

Expected:

- `dist\release\TelegramDownloader-0.3.0-win-x64-setup.exe` 存在。
- C 盘安装目标被拒绝。
- 项目内测试目录安装及 `--self-test` 成功。
- 原位升级和普通卸载均保留 catalog、缩略图和 downloads/sentinel 数据。

- [ ] **Step 8: 做最终工作区和隐私审计**

Run:

```powershell
git status --short
git diff --check
git grep -n -I -E "api_hash|tg://login\?token=|phone_code_hash" -- . ':!src/telegram_downloader/gateway.py' ':!tests'
```

Expected: diff check clean；git grep 不发现真实凭据或二维码令牌；`data/`、`downloads/`、`.build-temp/`、`.tool-cache/` 和 `dist/` 均未被暂存。

- [ ] **Step 9: 提交 0.3.0 候选版本**

```powershell
git add pyproject.toml src/telegram_downloader/__init__.py installer/TelegramDownloader.iss README.md docs/releases/v0.3.0.md tests/test_packaging_contract.py tests/test_installer_contract.py scripts/smoke.ps1 scripts/smoke-installer.ps1
git commit -m "release: prepare TelegramDownloader 0.3.0"
```

提交后再次运行 `git status --short`，Expected: clean。停在本地候选版本，等待用户明确授权合并与正式发布。

---
