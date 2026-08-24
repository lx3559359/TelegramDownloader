# TG 快取搜索结果响应性能优化实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 10,000 条搜索结果的模型更新降为线性复杂度，并把历史读取、选择持久化和入队本地计算移出 Qt 主线程，同时保持滚动、勾选、缩略图、generation 隔离和任务幂等语义。

**Architecture:** `SearchResultTableModel` 使用稳定 ID 行索引和有界结构信号应用累计快照；`ContentBrowserPage` 合并同 generation 的短周期批次并维护可见锚点。`ContentBrowserService` 通过可注入后台执行器访问带线程锁的短连接 `CatalogRepository`，Controller 串行处理选择意图、异步历史切换和入队提交，并在每个线程结果返回后检查搜索 ID、generation 与操作代次。

**Tech Stack:** Python 3.12、PySide6、qasync/asyncio、SQLite、pytest、pytest-asyncio、pytest-qt、Ruff、PowerShell、PyInstaller、Inno Setup

---

## 文件职责

- `src/telegram_downloader/content.py`：新增选择意图、选择模式、搜索快照和选择提交值对象。
- `src/telegram_downloader/catalog.py`：仓库线程串行化、原子搜索快照和批量选择 SQL。
- `src/telegram_downloader/content_browser.py`：后台仓库边界、累计渐进快照、历史快照与选择持久化服务。
- `src/telegram_downloader/ui/content_models.py`：O(n) 搜索结果模型、稳定 ID 行索引和批量本地选择。
- `src/telegram_downloader/ui/content_browser.py`：33 ms 搜索批次合并、50 ms 选择意图合并、滚动锚点和加载状态。
- `src/telegram_downloader/controller.py`：选择写入循环、最新历史请求、后台入队与失败校正。
- `src/telegram_downloader/app.py`：新信号、历史/入队动作桥和 hooks 装配。
- `src/telegram_downloader/ui/async_actions.py`：为历史打开和内容入队声明动作策略。
- `scripts/benchmark_search_results.py`：固定合成数据模型基准，不读取真实 Telegram 内容。
- `tests/ui/test_content_models.py`：模型复杂度合同、信号与稳定索引测试。
- `tests/ui/test_content_browser.py`：批次合并、可见锚点、选择意图和加载反馈测试。
- `tests/test_catalog.py`：批量选择、generation、单事务和线程串行化测试。
- `tests/test_content_browser.py`：后台执行、累计快照、取消和历史快照测试。
- `tests/test_controller.py`：选择写入、历史 latest-wins、入队心跳与幂等恢复测试。
- `tests/test_app.py`：动作策略和信号装配合同。
- `tests/ui/test_async_actions.py`：history replace-latest 与 queue deduplicate 生命周期合同。
- `tests/test_packaging_contract.py`：v0.18.2 版本和基准脚本打包边界合同。
- `docs/releases/v0.18.2.md`：用户可见发布说明。
- `docs/verification/v0.18.2-search-response-performance.md`：三轮验证与产物证据。

## Task 1：把搜索结果模型改为 O(n)

**Files:**
- Modify: `src/telegram_downloader/ui/content_models.py:241`
- Modify: `tests/ui/test_content_models.py:355`

- [ ] **Step 1：写模型复杂度与稳定 ID 的失败测试**

在 `tests/ui/test_content_models.py` 增加 `pytest`、`QPersistentModelIndex` 导入和以下测试。`many_results()` 复用现有 `search_results()` 的首项，只生成合成 ID，不使用真实内容：

```python
import pytest

from PySide6.QtCore import QPersistentModelIndex, Qt


def many_results(count: int) -> list[SearchResult]:
    first = search_results(datetime(2026, 8, 24, tzinfo=UTC))[0]
    return [
        replace(
            first,
            id=f"result-{index}",
            message_id=count - index,
            media_id=f"media-{index}",
            thumbnail_key=f"thumb-{index}",
        )
        for index in range(count)
    ]


def test_result_model_indexes_ten_thousand_rows_without_reset(qtbot) -> None:
    model = SearchResultTableModel()
    values = many_results(10_000)
    resets = QSignalSpy(model.modelReset)
    changed = QSignalSpy(model.dataChanged)

    model.apply_results(values)
    updated = list(values)
    updated[7_777] = replace(updated[7_777], selected=True)
    model.apply_results(updated)

    assert model.row_for_result_id("result-7777") == 7_777
    assert model.result_at(7_777).selected is True
    assert resets.count() == 0
    assert changed.count() == 1
    assert changed.at(0)[0].row() == 7_777
    assert changed.at(0)[1].row() == 7_777


def test_result_model_reorders_by_stable_id_and_preserves_persistent_index(qtbot) -> None:
    model = SearchResultTableModel()
    values = many_results(6)
    model.apply_results(values)
    retained = QPersistentModelIndex(model.index(1, 4))

    target = [values[5], values[0], values[1], values[2], values[3], values[4]]
    model.apply_results(target)

    assert retained.isValid()
    assert model.data(retained, Qt.ItemDataRole.UserRole) == "result-1"
    assert retained.row() == 2


def test_result_model_rejects_duplicate_ids_without_changing_rows() -> None:
    model = SearchResultTableModel()
    values = many_results(2)
    model.apply_results(values)

    with pytest.raises(ValueError, match="搜索结果 ID 重复"):
        model.apply_results([values[0], replace(values[1], id=values[0].id)])

    assert [model.result_at(row).id for row in range(model.rowCount())] == [
        "result-0",
        "result-1",
    ]
```

- [ ] **Step 2：运行模型测试，确认 RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\ui\test_content_models.py -q
```

Expected: FAIL，至少报告 `SearchResultTableModel` 没有 `row_for_result_id`，且当前 10,000 行路径无法满足信号合同。

- [ ] **Step 3：实现稳定 ID 索引和线性快照应用**

在 `SearchResultTableModel` 中把 `_results` 改为列表，增加固定碎片阈值、索引和辅助方法：

```python
_FRAGMENT_RESET_THRESHOLD = 64


class SearchResultTableModel(QAbstractTableModel):
    # 保留 HEADERS 与 selection_changed

    def __init__(self) -> None:
        super().__init__()
        self._results: list[SearchResult] = []
        self._row_by_id: dict[str, int] = {}
        self._thumbnails: dict[str, Path] = {}
        self._fallback_icons: dict[MediaKind, QIcon] = {}

    def row_for_result_id(self, result_id: str) -> int | None:
        return self._row_by_id.get(result_id)

    def results(self) -> tuple[SearchResult, ...]:
        return tuple(self._results)

    def _reindex(self) -> None:
        self._row_by_id = {
            result.id: row for row, result in enumerate(self._results)
        }

    @staticmethod
    def _ranges(rows: list[int]) -> list[tuple[int, int]]:
        if not rows:
            return []
        ranges: list[tuple[int, int]] = []
        first = previous = rows[0]
        for row in rows[1:]:
            if row == previous + 1:
                previous = row
                continue
            ranges.append((first, previous))
            first = previous = row
        ranges.append((first, previous))
        return ranges

    def _validate_target(self, target: list[SearchResult]) -> None:
        ids = [result.id for result in target]
        if len(ids) != len(set(ids)):
            raise ValueError("搜索结果 ID 重复")

    def _prune_thumbnails(self, target_ids: set[str]) -> None:
        self._thumbnails = {
            result_id: path
            for result_id, path in self._thumbnails.items()
            if result_id in target_ids
        }

    def _reset_results(self, target: list[SearchResult]) -> None:
        self.beginResetModel()
        self._results = list(target)
        self._reindex()
        self._prune_thumbnails(set(self._row_by_id))
        self.endResetModel()
```

将 `setData()` 的修改改为 O(1) 行替换，不再复制完整元组：

```python
result = self._results[index.row()]
if result.selected == requested:
    return True
self._results[index.row()] = replace(result, selected=requested)
self.dataChanged.emit(index, index, [Qt.ItemDataRole.CheckStateRole])
self.selection_changed.emit(result.id, requested)
return True
```

用下列完整控制流替换 `set_results()` 与 `apply_results()`；结构删除范围始终倒序，新增统一追加，最终排序只发一次 layout change：

```python
def set_results(self, results: list[SearchResult]) -> None:
    target = list(results)
    self._validate_target(target)
    self._reset_results(target)

def apply_results(self, results: list[SearchResult]) -> None:
    target = list(results)
    self._validate_target(target)
    target_ids = [item.id for item in target]
    target_id_set = set(target_ids)
    current_ids = [item.id for item in self._results]

    if current_ids == target_ids:
        changed_rows = [
            row
            for row, (before, after) in enumerate(zip(self._results, target, strict=True))
            if before != after
        ]
        self._results = target
        self._reindex()
        self._prune_thumbnails(target_id_set)
        for first, last in self._ranges(changed_rows):
            self.dataChanged.emit(
                self.index(first, 0),
                self.index(last, self.columnCount() - 1),
            )
        return

    removed_rows = [
        row for row, result in enumerate(self._results) if result.id not in target_id_set
    ]
    removed_ranges = self._ranges(removed_rows)
    if len(removed_ranges) > _FRAGMENT_RESET_THRESHOLD:
        self._reset_results(target)
        return
    for first, last in reversed(removed_ranges):
        self.beginRemoveRows(_INVALID_INDEX, first, last)
        del self._results[first : last + 1]
        self.endRemoveRows()

    surviving_ids = {item.id for item in self._results}
    additions = [item for item in target if item.id not in surviving_ids]
    if additions:
        first = len(self._results)
        last = first + len(additions) - 1
        self.beginInsertRows(_INVALID_INDEX, first, last)
        self._results.extend(additions)
        self.endInsertRows()

    existing_order = [item.id for item in self._results]
    if existing_order != target_ids:
        persistent = self.persistentIndexList()
        persistent_ids = [
            self._results[index.row()].id if index.isValid() else ""
            for index in persistent
        ]
        self.layoutAboutToBeChanged.emit()
        self._results = target
        self._reindex()
        remapped = [
            self.index(self._row_by_id[result_id], index.column())
            if result_id in self._row_by_id
            else QModelIndex()
            for index, result_id in zip(persistent, persistent_ids, strict=True)
        ]
        self.changePersistentIndexList(persistent, remapped)
        self.layoutChanged.emit()
    else:
        before_by_id = {item.id: item for item in self._results}
        changed_rows = [
            row for row, item in enumerate(target) if before_by_id[item.id] != item
        ]
        self._results = target
        self._reindex()
        for first, last in self._ranges(changed_rows):
            self.dataChanged.emit(
                self.index(first, 0),
                self.index(last, self.columnCount() - 1),
            )
    self._prune_thumbnails(target_id_set)
```

保留 `result_at()`、`selected_results()`、`set_thumbnail()` 的公开语义，并让 `set_thumbnail()` 使用 `_row_by_id` 直接定位行。

- [ ] **Step 4：运行模型聚焦测试，确认 GREEN**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\ui\test_content_models.py -q
.\.venv\Scripts\ruff.exe check src\telegram_downloader\ui\content_models.py tests\ui\test_content_models.py
```

Expected: 全部 PASS，Ruff 输出 `All checks passed!`。

- [ ] **Step 5：提交模型切片**

```powershell
git add src/telegram_downloader/ui/content_models.py tests/ui/test_content_models.py
git commit -m "perf: make search result updates linear"
```

## Task 2：合并渐进批次并保持视口锚点

**Files:**
- Modify: `src/telegram_downloader/ui/content_browser.py:58-80, 470-590`
- Modify: `tests/ui/test_content_browser.py:600`

- [ ] **Step 1：写首批立即、后续合并、稳定批次和锚点测试**

在 `tests/ui/test_content_browser.py` 增加：

```python
def test_progressive_batches_show_first_immediately_and_coalesce_latest(qtbot) -> None:
    now = datetime(2026, 8, 24, tzinfo=UTC)
    page = ContentBrowserPage()
    qtbot.addWidget(page)
    page.set_active_search(session(now))
    first = result(now, "r1", 3)
    second = result(now, "r2", 2)
    third = result(now, "r3", 1)

    page.apply_search_batch(SearchResultBatch("search-1", 1, (first,), False))
    assert page.result_model.rowCount() == 1

    page.apply_search_batch(SearchResultBatch("search-1", 1, (first, second), False))
    page.apply_search_batch(
        SearchResultBatch("search-1", 1, (first, second, third), False)
    )
    assert page.result_model.rowCount() == 1
    qtbot.waitUntil(lambda: page.result_model.rowCount() == 3, timeout=500)

    page.apply_search_batch(
        SearchResultBatch("search-1", 1, (first, second), stable=True)
    )
    assert page.result_model.rowCount() == 2
    assert page.queue_button.isEnabled() is False  # 未登录的既有合同仍生效


def test_older_generation_batch_cannot_replace_newer_results(qtbot) -> None:
    now = datetime(2026, 8, 24, tzinfo=UTC)
    page = ContentBrowserPage()
    qtbot.addWidget(page)
    newer = replace(session(now), generation=2)
    page.set_active_search(newer)
    page.apply_search_batch(
        SearchResultBatch("search-1", 2, (result(now, "new", 2),), False)
    )
    page.apply_search_batch(
        SearchResultBatch("search-1", 1, (result(now, "old", 1),), True)
    )
    assert page.result_model.result_at(0).id == "new"


def test_result_update_restores_top_visible_and_current_ids(qtbot) -> None:
    now = datetime(2026, 8, 24, tzinfo=UTC)
    page = ContentBrowserPage()
    page.resize(1_000, 650)
    qtbot.addWidget(page)
    page.show()
    values = [result(now, f"r{index}", 200 - index) for index in range(120)]
    page.set_results(values)
    page.result_table.scrollTo(page.result_model.index(60, 0))
    page.result_table.setCurrentIndex(page.result_model.index(65, 4))
    qtbot.wait(20)
    top_id = page.result_model.result_at(page._visible_result_rows().start).id

    page.apply_search_batch(
        SearchResultBatch(
            "search-1",
            1,
            tuple([values[0], result(now, "inserted", 199), *values[1:]]),
            True,
        )
    )

    assert page.result_model.data(
        page.result_table.currentIndex(), Qt.ItemDataRole.UserRole
    ) == "r65"
    assert page.result_model.result_at(page._visible_result_rows().start).id == top_id
```

- [ ] **Step 2：运行页面测试，确认 RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\ui\test_content_browser.py -q
```

Expected: FAIL，因为后续批次当前会立即应用、旧 generation 未拒绝、页面未按 ID 恢复锚点。

- [ ] **Step 3：实现 33 ms 合并器与锚点恢复**

在 `ContentBrowserPage.__init__()` 增加：

```python
self._pending_result_batch: SearchResultBatch | None = None
self._result_batch_timer = QTimer(self)
self._result_batch_timer.setSingleShot(True)
self._result_batch_timer.setInterval(33)
self._result_batch_timer.timeout.connect(self._flush_result_batch)
```

将 `apply_search_batch()` 拆成调度与应用两层：

```python
def apply_search_batch(self, batch: SearchResultBatch) -> None:
    if (
        self._batch_search_id == batch.search_id
        and self._batch_generation is not None
        and batch.generation < self._batch_generation
    ):
        return
    key_changed = (
        self._batch_search_id,
        self._batch_generation,
    ) != (batch.search_id, batch.generation)
    if key_changed:
        self._result_batch_timer.stop()
        self._pending_result_batch = None
    self._batch_search_id = batch.search_id
    self._batch_generation = batch.generation
    if self.result_model.rowCount() == 0 or batch.stable:
        self._result_batch_timer.stop()
        self._pending_result_batch = None
        self._apply_result_batch(batch)
        return
    self._pending_result_batch = batch
    if not self._result_batch_timer.isActive():
        self._result_batch_timer.start()

def _flush_result_batch(self) -> None:
    batch = self._pending_result_batch
    self._pending_result_batch = None
    if batch is not None:
        self._apply_result_batch(batch)

def _visible_anchor(self) -> tuple[str | None, str | None, int]:
    visible = self._visible_result_rows()
    top_id = (
        self.result_model.result_at(visible.start).id
        if visible.start < self.result_model.rowCount()
        else None
    )
    current = self.result_table.currentIndex()
    current_id = (
        str(self.result_model.data(current, Qt.ItemDataRole.UserRole))
        if current.isValid()
        else None
    )
    return top_id, current_id, self.result_table.horizontalScrollBar().value()

def _restore_visible_anchor(
    self,
    top_id: str | None,
    current_id: str | None,
    horizontal: int,
) -> None:
    if top_id is not None:
        row = self.result_model.row_for_result_id(top_id)
        if row is not None:
            self.result_table.scrollTo(
                self.result_model.index(row, 0),
                QAbstractItemView.ScrollHint.PositionAtTop,
            )
    if current_id is not None:
        row = self.result_model.row_for_result_id(current_id)
        if row is not None:
            self.result_table.setCurrentIndex(self.result_model.index(row, 4))
    self.result_table.horizontalScrollBar().setValue(horizontal)

def _apply_result_batch(self, batch: SearchResultBatch) -> None:
    anchor = self._visible_anchor()
    self._results_stable = batch.stable
    self.results = list(batch.results)
    self.result_model.apply_results(self.results)
    self._restore_visible_anchor(*anchor)
    self._thumbnail_requested_ids.intersection_update(
        item.id for item in self.results
    )
    self._update_selection_summary()
    self._refresh_actions()
    QTimer.singleShot(0, self.request_visible_thumbnails)
```

`set_active_search()` 在 key 改变时停止 `_result_batch_timer` 并清空 `_pending_result_batch`。`set_results()` 也先清空待处理批次，避免历史快照后又应用旧搜索结果。

- [ ] **Step 4：运行页面与模型测试，确认 GREEN**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\ui\test_content_browser.py tests\ui\test_content_models.py -q
.\.venv\Scripts\ruff.exe check src\telegram_downloader\ui tests\ui
```

Expected: 全部 PASS，Ruff 通过。

- [ ] **Step 5：提交页面批次切片**

```powershell
git add src/telegram_downloader/ui/content_browser.py tests/ui/test_content_browser.py
git commit -m "perf: coalesce progressive search rendering"
```

## Task 3：增加线程安全搜索快照与批量选择事务

**Files:**
- Modify: `src/telegram_downloader/content.py:157`
- Modify: `src/telegram_downloader/catalog.py:207, 668-780`
- Modify: `tests/test_catalog.py:680-885`

- [ ] **Step 1：写批量选择、generation 和仓库线程串行化失败测试**

在 `src/telegram_downloader/content.py` 的测试导入准备使用以下新值对象；先在 `tests/test_catalog.py` 写测试：

```python
import threading
import time

from telegram_downloader.content import (
    SearchSelectionIntent,
    SelectionMode,
)


def test_catalog_applies_patch_select_all_and_invert_in_one_transaction(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 24, tzinfo=UTC)
    repo = CatalogRepository(tmp_path / "catalog.sqlite3")
    repo.initialize()
    repo.upsert_account(AccountProfile("a1", "账号"), now)
    query = ContentSearchQuery("资料", ScanFilters(now, now, frozenset(MediaKind), 10))
    session = repo.begin_search("s1", "a1", "-1001", "群", query, now)
    values = [
        result(session.id, "a1", now, result_id=f"r{index}", message_id=index)
        for index in range(4)
    ]
    values[3] = replace(values[3], available=False)
    repo.save_search_page("a1", session.id, session.generation, values)

    patch = SearchSelectionIntent(
        session.id,
        session.generation,
        1,
        SelectionMode.PATCH,
        (("r0", True), ("r1", True), ("r0", False)),
    )
    assert repo.apply_selection("a1", patch).changed_count == 2
    select_all = replace(patch, revision=2, mode=SelectionMode.SELECT_ALL, changes=())
    repo.apply_selection("a1", select_all)
    invert = replace(patch, revision=3, mode=SelectionMode.INVERT, changes=())
    repo.apply_selection("a1", invert)

    saved = {item.id: item for item in repo.list_results("a1", session.id)}
    assert all(saved[result_id].selected is False for result_id in ("r0", "r1", "r2"))
    assert saved["r3"].selected is False


def test_catalog_rejects_stale_selection_generation(tmp_path: Path) -> None:
    repo, session = prepared_selection_catalog(tmp_path)
    stale = SearchSelectionIntent(
        session.id,
        session.generation + 1,
        1,
        SelectionMode.SELECT_ALL,
        (),
    )
    with pytest.raises(StaleSearchError):
        repo.apply_selection("a1", stale)


def test_catalog_connection_boundaries_are_serialized(tmp_path: Path) -> None:
    repo = CatalogRepository(tmp_path / "catalog.sqlite3")
    repo.initialize()
    active = 0
    peak = 0
    gate = threading.Barrier(3)

    def use_connection() -> None:
        nonlocal active, peak
        gate.wait()
        with repo._connection():
            active += 1
            peak = max(peak, active)
            time.sleep(0.05)
            active -= 1

    threads = [threading.Thread(target=use_connection) for _ in range(2)]
    for thread in threads:
        thread.start()
    gate.wait()
    for thread in threads:
        thread.join()
    assert peak == 1
```

同时在这些测试前加入完整辅助器；它只使用 `tmp_path` 和合成结果：

```python
def prepared_selection_catalog(tmp_path: Path):
    now = datetime(2026, 8, 24, tzinfo=UTC)
    repo = CatalogRepository(tmp_path / "catalog.sqlite3")
    repo.initialize()
    repo.upsert_account(AccountProfile("a1", "账号"), now)
    query = ContentSearchQuery(
        "资料",
        ScanFilters(now, now, frozenset(MediaKind), 10),
    )
    session = repo.begin_search("s1", "a1", "-1001", "群", query, now)
    repo.save_search_page(
        "a1",
        session.id,
        session.generation,
        [
            result(session.id, "a1", now, result_id="r0", message_id=2),
            result(session.id, "a1", now, result_id="r1", message_id=1),
        ],
    )
    return repo, session
```

- [ ] **Step 2：运行 catalog 测试，确认 RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_catalog.py -q
```

Expected: collection FAIL，因为 `SearchSelectionIntent` 与 `SelectionMode` 尚不存在。

- [ ] **Step 3：实现选择值对象**

在 `SearchResult` 后增加：

```python
class SelectionMode(StrEnum):
    PATCH = "patch"
    SELECT_ALL = "select_all"
    INVERT = "invert"


@dataclass(frozen=True, slots=True)
class SearchSelectionIntent:
    search_id: str
    generation: int
    revision: int
    mode: SelectionMode
    changes: tuple[tuple[str, bool], ...] = ()

    def __post_init__(self) -> None:
        if not self.search_id or self.generation <= 0 or self.revision <= 0:
            raise ValueError("选择意图缺少有效搜索代次")
        if self.mode is SelectionMode.PATCH and not self.changes:
            raise ValueError("选择补丁不能为空")
        if self.mode is not SelectionMode.PATCH and self.changes:
            raise ValueError("批量选择模式不能携带逐项补丁")

    @property
    def final_changes(self) -> tuple[tuple[str, bool], ...]:
        latest: dict[str, bool] = {}
        for result_id, selected in self.changes:
            if not result_id:
                raise ValueError("选择补丁包含空结果 ID")
            latest[result_id] = bool(selected)
        return tuple(latest.items())


@dataclass(frozen=True, slots=True)
class SearchSnapshot:
    session: SearchSession
    results: tuple[SearchResult, ...]


@dataclass(frozen=True, slots=True)
class SelectionCommit:
    search_id: str
    generation: int
    revision: int
    changed_count: int
```

- [ ] **Step 4：实现仓库锁、原子快照和批量选择**

在 `catalog.py` 导入 `threading.RLock`、四个新类型，并修改构造器和连接边界：

```python
def __init__(self, database: Path) -> None:
    self.database = database.resolve()
    self._connection_lock = RLock()

@contextmanager
def _connection(self) -> Iterator[sqlite3.Connection]:
    with self._connection_lock:
        connection = sqlite3.connect(self.database, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute("PRAGMA busy_timeout=5000")
            with connection:
                yield connection
        finally:
            connection.close()
```

新增原子快照：

```python
def load_search_snapshot(self, account_id: str, search_id: str) -> SearchSnapshot:
    with self._connection() as connection:
        row = connection.execute(
            "SELECT * FROM search_sessions WHERE account_id=? AND id=?",
            (account_id, search_id),
        ).fetchone()
        if row is None:
            raise KeyError(search_id)
        rows = self._select_result_rows(connection, account_id, search_id)
    return SearchSnapshot(
        self._session_from_row(row),
        tuple(self._result_from_row(item) for item in rows),
    )
```

新增 `apply_selection()`；PATCH 先按 ID 合并最终值并在同一连接逐条更新，SELECT_ALL 与 INVERT 各执行一条 SQL。所有 WHERE 都包含当前 session generation：

```python
def apply_selection(
    self,
    account_id: str,
    intent: SearchSelectionIntent,
) -> SelectionCommit:
    with self._connection() as connection:
        row = connection.execute(
            "SELECT generation FROM search_sessions WHERE account_id=? AND id=?",
            (account_id, intent.search_id),
        ).fetchone()
        if row is None or int(row["generation"]) != intent.generation:
            raise StaleSearchError("选择操作已被更新的搜索代次取代")
        changed = 0
        if intent.mode is SelectionMode.PATCH:
            for result_id, selected in intent.final_changes:
                cursor = connection.execute(
                    "UPDATE search_results SET selected=? WHERE account_id=? "
                    "AND search_id=? AND id=? AND generation=? "
                    "AND (?=0 OR (available=1 AND queued=0))",
                    (
                        int(selected),
                        account_id,
                        intent.search_id,
                        result_id,
                        intent.generation,
                        int(selected),
                    ),
                )
                if cursor.rowcount != 1:
                    raise ValueError("该媒体当前不可选择")
                changed += 1
        elif intent.mode is SelectionMode.SELECT_ALL:
            cursor = connection.execute(
                "UPDATE search_results SET selected=1 WHERE account_id=? "
                "AND search_id=? AND generation=? AND available=1 AND queued=0",
                (account_id, intent.search_id, intent.generation),
            )
            changed = max(0, cursor.rowcount)
        else:
            cursor = connection.execute(
                "UPDATE search_results SET selected=CASE selected WHEN 1 THEN 0 ELSE 1 END "
                "WHERE account_id=? AND search_id=? AND generation=? "
                "AND available=1 AND queued=0",
                (account_id, intent.search_id, intent.generation),
            )
            changed = max(0, cursor.rowcount)
    return SelectionCommit(
        intent.search_id,
        intent.generation,
        intent.revision,
        changed,
    )
```

保留旧 `set_selected()` 作为兼容包装：构造 generation 来自 `get_session()` 的 PATCH intent 并调用 `apply_selection()`。现有调用方迁移完后仍保留该公开方法，避免破坏旧测试与外部恢复代码。

- [ ] **Step 5：运行 catalog 与内容类型测试，确认 GREEN**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_catalog.py tests\test_content.py -q
.\.venv\Scripts\ruff.exe check src\telegram_downloader\content.py src\telegram_downloader\catalog.py tests\test_catalog.py
```

Expected: 全部 PASS，schema version 不变，Ruff 通过。

- [ ] **Step 6：提交仓库切片**

```powershell
git add src/telegram_downloader/content.py src/telegram_downloader/catalog.py tests/test_catalog.py
git commit -m "perf: batch search selection transactions"
```

## Task 4：把搜索与历史 SQLite 工作移出事件循环

**Files:**
- Modify: `src/telegram_downloader/content_browser.py:90-590`
- Modify: `tests/test_content_browser.py:240-550, 1560-1645`

- [ ] **Step 1：写后台心跳、累计快照与取消保护失败测试**

在 `tests/test_content_browser.py` 增加：

```python
@pytest.mark.asyncio
async def test_search_catalog_work_does_not_block_event_loop(
    tmp_path: Path,
    monkeypatch,
) -> None:
    now = datetime(2026, 8, 24, tzinfo=UTC)
    gateway = FakeGateway(AccountProfile("a1", "账号"))
    service = await prepared_online_service(tmp_path, now, gateway)
    gateway.pages = [RemoteSearchPage((make_hit(1, now),), None, True)]
    entered = threading.Event()
    original = service.catalog.commit_search_page

    def slow_commit(*args, **kwargs):
        entered.set()
        time.sleep(0.30)
        return original(*args, **kwargs)

    monkeypatch.setattr(service.catalog, "commit_search_page", slow_commit)
    heartbeat = 0

    async def beat() -> None:
        nonlocal heartbeat
        while not entered.is_set():
            await asyncio.sleep(0)
        deadline = asyncio.get_running_loop().time() + 0.20
        while asyncio.get_running_loop().time() < deadline:
            heartbeat += 1
            await asyncio.sleep(0.01)

    await asyncio.gather(service.start_search("-1001", make_query(now)), beat())
    assert heartbeat >= 5


@pytest.mark.asyncio
async def test_global_provisional_batches_are_cumulative(tmp_path: Path) -> None:
    now = datetime(2026, 8, 24, tzinfo=UTC)
    gateway = FakeGateway(AccountProfile("a1", "账号"))
    service = await prepared_online_service(tmp_path, now, gateway)
    gateway.all_pages = [
        RemoteSearchPage((make_hit(20, now),), SearchCursor(19, 1, "-1001"), False),
        RemoteSearchPage((make_hit(18, now),), None, True),
    ]
    batches: list[SearchResultBatch] = []

    await service.start_search(
        ALL_DIALOGS_SCOPE_REF,
        make_query(now),
        scope=SearchScope.ALL_DIALOGS,
        on_results=batches.append,
    )

    provisional = [batch for batch in batches if not batch.stable]
    assert [len(batch.results) for batch in provisional] == [1, 2]
    assert {item.message_id for item in provisional[-1].results} == {20, 18}


@pytest.mark.asyncio
async def test_load_search_snapshot_runs_through_background_boundary(tmp_path: Path) -> None:
    now = datetime(2026, 8, 24, tzinfo=UTC)
    gateway = FakeGateway(AccountProfile("a1", "账号"))
    service = await prepared_online_service(tmp_path, now, gateway)
    session = service.catalog.begin_search(
        "search-1",
        "a1",
        "-1001",
        "资料群",
        make_query(now),
        now,
    )
    service.catalog.save_search_page(
        "a1",
        session.id,
        session.generation,
        [make_saved_result(session.id, now, "result-1", 1)],
    )
    calls = 0
    original = service._run_blocking

    async def counted(operation):
        nonlocal calls
        calls += 1
        return await original(operation)

    service._run_blocking = counted
    snapshot = await service.load_search_snapshot("search-1")
    assert snapshot.session.id == "search-1"
    assert calls == 1
```

补充 `threading`、`time` 和 `SearchResultBatch` 导入。测试辅助器只写 `tmp_path` 内合成 SQLite。

- [ ] **Step 2：运行服务测试，确认 RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_content_browser.py -q
```

Expected: FAIL；当前同步 `commit_search_page()` 会阻塞心跳，临时批次不是累计快照，且没有异步 `load_search_snapshot()`。

- [ ] **Step 3：增加可注入后台执行边界**

在模块顶部增加：

```python
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

_T = TypeVar("_T")
BlockingRunner = Callable[[Callable[[], _T]], Awaitable[_T]]


async def _to_thread(operation: Callable[[], _T]) -> _T:
    return await asyncio.to_thread(operation)
```

构造器增加 `run_blocking: BlockingRunner = _to_thread` 并保存为 `self._run_blocking`。所有后台调用都传入闭包，闭包不得访问 Qt。

- [ ] **Step 4：迁移搜索数据库边界并发送累计快照**

将 `start_search()` 的 dialog 校验与 `begin_search()` 包在一个后台闭包；`load_more()` 的 session 读取也通过 `_run_blocking`。在 `_fetch_page()` 中：

```python
stable_results = list(
    await self._run_blocking(
        lambda: self.catalog.list_results(account.account_id, session.id)
    )
)
```

发送直接命中前构建累计快照：

```python
direct = [
    result_for(hit)
    for hit in self._deduplicate_hits(list(page.items))
]
provisional_by_key = {
    self._result_key(item): item for item in stable_results
}
for item in direct:
    provisional_by_key[self._result_key(item)] = item
provisional = sorted(
    provisional_by_key.values(),
    key=lambda item: (
        -item.message_date_utc.timestamp(),
        item.peer_ref,
        -item.message_id,
        item.media_id,
    ),
)
if on_results is not None and provisional:
    on_results(
        SearchResultBatch(
            session.id,
            session.generation,
            tuple(provisional),
            stable=False,
        )
    )
```

将 `planner.existing_media_keys()` 与 `catalog.commit_search_page()` 放进同一个后台本地阶段，返回 `(queued_keys, commit)`；Controller 回调仍发生在 await 返回后的事件循环线程。取消和 Gateway 失败路径使用 `await asyncio.shield(self._run_blocking(...))` 完成短暂的不完整状态提交，然后重新抛出或返回。

- [ ] **Step 5：增加异步快照与选择服务方法**

在 `ContentBrowserService` 增加：

```python
async def list_sessions_async(self) -> list[SearchSession]:
    account = self._require_account()
    return await self._run_blocking(
        lambda: self.catalog.list_sessions(account.account_id)
    )

async def load_search_snapshot(self, search_id: str) -> SearchSnapshot:
    account = self._require_account()
    return await self._run_blocking(
        lambda: self.catalog.load_search_snapshot(account.account_id, search_id)
    )

async def persist_selection(
    self,
    intent: SearchSelectionIntent,
) -> SelectionCommit:
    account = self._require_account()
    return await self._run_blocking(
        lambda: self.catalog.apply_selection(account.account_id, intent)
    )
```

保留同步 `list_sessions()` 与 `list_results()` 给启动缓存和兼容测试使用，但新的搜索完成、历史打开、选择失败校正不得在主线程调用它们。

- [ ] **Step 6：运行内容服务测试，确认 GREEN**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_content_browser.py tests\test_account_wide_search_e2e.py -q
.\.venv\Scripts\ruff.exe check src\telegram_downloader\content_browser.py tests\test_content_browser.py
```

Expected: 全部 PASS；300 ms 延迟期间心跳至少推进 5 次。

- [ ] **Step 7：提交服务后台化切片**

```powershell
git add src/telegram_downloader/content_browser.py tests/test_content_browser.py tests/test_account_wide_search_e2e.py
git commit -m "perf: move search catalog work off UI loop"
```

## Task 5：批量选择意图与异步历史切换

**Files:**
- Modify: `src/telegram_downloader/ui/content_models.py:300-390`
- Modify: `src/telegram_downloader/ui/content_browser.py:58-80, 390-810`
- Modify: `src/telegram_downloader/controller.py:600-640, 1476-1680, 2980-3030`
- Modify: `tests/ui/test_content_models.py`
- Modify: `tests/ui/test_content_browser.py`
- Modify: `tests/test_controller.py:3260-3590, 4050-4170`

在模型测试导入 `SelectionMode`；页面测试导入 `SearchSelectionIntent` 与 `SelectionMode`；Controller 测试导入 `SearchSelectionIntent`、`SearchSnapshot` 与 `SelectionMode`。现有 `SearchSession`、`SearchStatus`、`ContentSearchQuery`、`MediaKind` 和 `ScanFilters` 导入继续复用。

- [ ] **Step 1：写 O(1) 单选、一次全选意图和选择合并测试**

模型测试增加：

```python
def test_bulk_selection_updates_model_once_without_per_row_signal(qtbot) -> None:
    model = SearchResultTableModel()
    model.apply_results(many_results(10_000))
    changes = QSignalSpy(model.dataChanged)
    intents = QSignalSpy(model.selection_changed)

    changed = model.apply_selection_mode(SelectionMode.SELECT_ALL)

    assert changed == 10_000
    assert changes.count() == 1
    assert intents.count() == 0
    assert len(model.selected_results()) == 10_000
```

页面测试增加：

```python
def prepared_selectable_page(qtbot, now: datetime) -> ContentBrowserPage:
    page = ContentBrowserPage()
    qtbot.addWidget(page)
    page.set_logged_in(True)
    page.set_dialogs([dialog(now)])
    page.dialog_list.setCurrentIndex(page.dialog_model.index(0, 0))
    page.set_active_search(
        replace(
            session(now),
            status=SearchStatus.COMPLETED,
            exhausted=True,
        )
    )
    page.set_results(
        [
            result(now, "r1", 2),
            result(now, "r2", 1),
        ]
    )
    return page


def test_single_selection_intents_merge_by_result_id(qtbot) -> None:
    now = datetime(2026, 8, 24, tzinfo=UTC)
    page = prepared_selectable_page(qtbot, now)
    intents: list[SearchSelectionIntent] = []
    page.selection_intent_requested.connect(intents.append)

    page._selection_changed("r1", True)
    page._selection_changed("r1", False)
    page._selection_changed("r2", True)
    qtbot.waitUntil(lambda: len(intents) == 1, timeout=500)

    assert intents[0].mode is SelectionMode.PATCH
    assert dict(intents[0].final_changes) == {"r1": False, "r2": True}


def test_select_all_emits_one_bulk_intent(qtbot) -> None:
    now = datetime(2026, 8, 24, tzinfo=UTC)
    page = prepared_selectable_page(qtbot, now)
    intents: list[SearchSelectionIntent] = []
    page.selection_intent_requested.connect(intents.append)
    qtbot.mouseClick(page.select_all_button, Qt.MouseButton.LeftButton)
    assert len(intents) == 1
    assert intents[0].mode is SelectionMode.SELECT_ALL
```

- [ ] **Step 2：写 Controller 选择串行化与历史 latest-wins 失败测试**

在 `tests/test_controller.py` 增加：

```python
@pytest.mark.asyncio
async def test_selection_writer_preserves_intent_order_and_reloads_only_on_failure() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    calls: list[int] = []

    class Browser:
        async def persist_selection(self, intent):
            calls.append(intent.revision)
            if intent.revision == 1:
                entered.set()
                await release.wait()
            return SimpleNamespace(revision=intent.revision)

    controller = AppController.for_test(
        content_browser=Browser(),
        window=ContentWindowFake(),
    )
    first = SearchSelectionIntent("s1", 1, 1, SelectionMode.SELECT_ALL)
    second = SearchSelectionIntent("s1", 1, 2, SelectionMode.INVERT)
    controller.submit_content_selection(first)
    await entered.wait()
    controller.submit_content_selection(second)
    release.set()
    await controller._selection_persist_task
    assert calls == [1, 2]


@pytest.mark.asyncio
async def test_history_open_latest_request_wins() -> None:
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    now = datetime(2026, 8, 24, tzinfo=UTC)
    query = ContentSearchQuery(
        "资料",
        ScanFilters(now, now, frozenset({MediaKind.VIDEO}), 10),
    )

    class Browser:
        async def load_search_snapshot(self, search_id):
            if search_id == "first":
                first_started.set()
                await release_first.wait()
            return SearchSnapshot(
                SearchSession(
                    search_id,
                    "a1",
                    "-1001",
                    "资料群",
                    query,
                    SearchStatus.COMPLETED,
                    1,
                    None,
                    True,
                    0,
                    now,
                    now,
                ),
                (),
            )

        async def list_sessions_async(self):
            return []

    window = ContentWindowFake()
    controller = AppController.for_test(content_browser=Browser(), window=window)
    first = asyncio.create_task(controller.open_content_history("first"))
    await first_started.wait()
    await controller.open_content_history("second")
    release_first.set()
    await first
    assert window.content_page.active_search_id == "second"
```

- [ ] **Step 3：运行聚焦测试，确认 RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\ui\test_content_models.py tests\ui\test_content_browser.py tests\test_controller.py -q
```

Expected: FAIL，因为批量选择方法、选择意图信号、Controller writer 和异步历史方法尚不存在。

- [ ] **Step 4：实现模型批量本地选择和页面 50 ms 合并**

模型增加：

```python
def apply_selection_mode(self, mode: SelectionMode) -> int:
    if mode not in (SelectionMode.SELECT_ALL, SelectionMode.INVERT):
        raise ValueError("批量选择模式无效")
    changed_rows: list[int] = []
    for row, item in enumerate(self._results):
        if not item.available or item.queued:
            continue
        selected = True if mode is SelectionMode.SELECT_ALL else not item.selected
        if item.selected == selected:
            continue
        self._results[row] = replace(item, selected=selected)
        changed_rows.append(row)
    for first, last in self._ranges(changed_rows):
        self.dataChanged.emit(
            self.index(first, 0),
            self.index(last, 0),
            [Qt.ItemDataRole.CheckStateRole],
        )
    return len(changed_rows)
```

页面把 `selection_changed` 替换为 `selection_intent_requested = Signal(object)`，保存 `_selection_changes`、`_selection_revision` 和 50 ms 单次 `QTimer`。单项变化只更新字典；timer 到期创建 PATCH intent。全选/反选先 flush 当前 PATCH，再调用 `apply_selection_mode()`，增加 revision 并立即发出对应模式 intent。每次发出后用 `result_model.results()` 更新 `self.results`、选择摘要和动作状态。

- [ ] **Step 5：实现 Controller 选择写入循环和异步历史**

构造器新增：

```python
self._selection_intents: deque[SearchSelectionIntent] = deque()
self._selection_persist_task: asyncio.Task[None] | None = None
self._history_generation = 0
```

新增选择入口与 writer：

```python
def submit_content_selection(self, intent: SearchSelectionIntent) -> None:
    self._selection_intents.append(intent)
    if self._selection_persist_task is None or self._selection_persist_task.done():
        self._selection_persist_task = self._spawn_background(
            self._drain_content_selection()
        )

async def _drain_content_selection(self) -> None:
    page = self._content_page()
    try:
        while self._selection_intents:
            intent = self._selection_intents.popleft()
            if self.content_browser is None:
                return
            try:
                await self.content_browser.persist_selection(intent)
            except Exception as error:
                snapshot = await self.content_browser.load_search_snapshot(
                    intent.search_id
                )
                if (
                    page.active_search_id == intent.search_id
                    and page._batch_generation == intent.generation
                ):
                    page.apply_search_batch(
                        SearchResultBatch(
                            snapshot.session.id,
                            snapshot.session.generation,
                            snapshot.results,
                            stable=True,
                        )
                    )
                    page.show_error(self._safe_error(error))
    finally:
        self._selection_persist_task = None
```

新增 `open_content_history()`：先递增 `_history_generation` 并设置页面历史忙碌；并行 await `load_search_snapshot(search_id)` 与 `list_sessions_async()`，返回后仅当本地 generation 等于当前值时设置 sessions、active search 和 results。`finally` 也只允许最新请求清除忙碌状态。把搜索失败和入队后的同步 `_reload_content_search()` 改为 await 同一个异步快照方法。

选择 writer 维护每个 `(search_id, generation)` 已提交的最新 revision。失败校正快照返回后，必须同时确认账号未变化、页面搜索 ID/generation 仍匹配，且页面当前 selection revision 仍等于失败 intent 的 revision；任一不匹配就只记录固定动作名、计数和耗时，不覆盖较新的乐观选择。成功返回也校验 `SelectionCommit` 的 search ID、generation、revision 与原 intent 完全一致。

- [ ] **Step 6：把选择任务加入账号切换与关闭清理**

`_cancel_content_operations()` 把 `_selection_persist_task` 纳入 tracked tasks，先清空 `_selection_intents`，再取消等待；线程内已开始的 SQLite 事务由仓库锁自然结束，返回值因页面 key 不匹配被丢弃。

- [ ] **Step 7：运行选择、历史和控制器测试，确认 GREEN**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\ui\test_content_models.py tests\ui\test_content_browser.py tests\test_controller.py -q
.\.venv\Scripts\ruff.exe check src\telegram_downloader\ui\content_models.py src\telegram_downloader\ui\content_browser.py src\telegram_downloader\controller.py tests\ui tests\test_controller.py
```

Expected: 全部 PASS；单项选择成功路径不调用全量 `list_results()`。

- [ ] **Step 8：提交选择与历史切片**

```powershell
git add src/telegram_downloader/ui/content_models.py src/telegram_downloader/ui/content_browser.py src/telegram_downloader/controller.py tests/ui/test_content_models.py tests/ui/test_content_browser.py tests/test_controller.py
git commit -m "perf: persist search interactions asynchronously"
```

## Task 6：把入队预检与提交移出主线程并保证幂等恢复

**Files:**
- Modify: `src/telegram_downloader/content_browser.py:639-724`
- Modify: `src/telegram_downloader/controller.py:1644-1680`
- Modify: `tests/test_content_browser.py:1330-1490`
- Modify: `tests/test_controller.py:4050-4225`

- [ ] **Step 1：写 300 ms 入队心跳和回写失败恢复测试**

```python
@pytest.mark.asyncio
async def test_queue_preflight_does_not_block_event_loop() -> None:
    entered = threading.Event()

    class ContentService:
        def prepare_download(self, _search_id):
            entered.set()
            time.sleep(0.30)
            return SimpleNamespace(preview="preview")

    controller = AppController.for_test(
        content_browser=ContentService(),
        planner=object(),
        confirm_preview=lambda _preview: False,
        window=ContentWindowFake(),
    )
    heartbeat = 0

    async def beat():
        nonlocal heartbeat
        while not entered.is_set():
            await asyncio.sleep(0)
        for _ in range(10):
            heartbeat += 1
            await asyncio.sleep(0.01)

    await asyncio.gather(controller.queue_content_selection("s1"), beat())
    assert heartbeat == 10


@pytest.mark.asyncio
async def test_queue_commit_starts_once_when_catalog_reconciliation_fails() -> None:
    committed = SimpleNamespace(
        task=SimpleNamespace(id="task-1"),
        accepted_keys=frozenset({("peer", 1, "media")}),
    )

    class ContentService:
        def prepare_download(self, _search_id):
            return SimpleNamespace(preview="preview")

        def finalize_queue(self, _search_id, _joined_count):
            raise OSError("catalog unavailable")

        def reconcile_queue(self, _search_id):
            return SimpleNamespace(results=())

    planner = SimpleNamespace(commit_selected=Mock(return_value=committed))
    controller = AppController.for_test(
        content_browser=ContentService(),
        planner=planner,
        confirm_preview=lambda _preview: True,
        window=ContentWindowFake(),
    )
    controller._start_task = Mock()
    controller.refresh_tasks_async = AsyncMock()

    await controller.queue_content_selection("s1")

    planner.commit_selected.assert_called_once_with("preview")
    controller._start_task.assert_called_once_with("task-1")
```

- [ ] **Step 2：运行入队测试，确认 RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_controller.py -q -k "queue"
```

Expected: FAIL；同步 `prepare_download()` 阻塞心跳，回写异常当前会阻止任务启动。

- [ ] **Step 3：增加内容目录幂等校正方法**

在 `ContentBrowserService` 增加同步本地方法，由 Controller 放入线程：

```python
def reconcile_queue(self, search_id: str) -> SearchSnapshot:
    account = self._require_account()
    planner = self._require_planner()
    snapshot = self.catalog.load_search_snapshot(account.account_id, search_id)
    eligible = [item for item in snapshot.results if item.available]
    existing = planner.existing_media_keys(
        {self._result_key(item) for item in eligible}
    )
    queued_ids = tuple(
        item.id for item in eligible if self._result_key(item) in existing
    )
    self.catalog.mark_queued(account.account_id, queued_ids)
    return self.catalog.load_search_snapshot(account.account_id, search_id)
```

- [ ] **Step 4：重排 Controller 入队阶段**

`queue_content_selection()` 固定执行：

```python
preparation = await asyncio.to_thread(
    self.content_browser.prepare_download,
    search_id,
)
if not await self._confirm_download_preview(preparation.preview):
    self._show_status("已取消创建任务")
    return
committed = await asyncio.to_thread(
    self.planner.commit_selected,
    preparation.preview,
)
self._start_task(committed.task.id)
joined_count = len(committed.accepted_keys)
try:
    report = await asyncio.to_thread(
        self.content_browser.finalize_queue,
        search_id,
        joined_count,
    )
except Exception:
    snapshot = await asyncio.to_thread(
        self.content_browser.reconcile_queue,
        search_id,
    )
    page.apply_search_batch(
        SearchResultBatch(
            snapshot.session.id,
            snapshot.session.generation,
            snapshot.results,
            stable=True,
        )
    )
    raise
else:
    await self._reload_content_search_async(search_id)
await self.refresh_tasks_async()
```

确保 `_start_task()` 只在 `commit_selected()` 成功后调用一次。回写失败时任务已存在且开始运行；`reconcile_queue()` 只按任务仓库媒体键修正 catalog，不创建任务。

- [ ] **Step 5：运行入队、planner 和仓库回归，确认 GREEN**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_controller.py tests\test_content_browser.py tests\test_planner.py tests\test_repository.py -q
.\.venv\Scripts\ruff.exe check src\telegram_downloader\content_browser.py src\telegram_downloader\controller.py tests\test_content_browser.py tests\test_controller.py
```

Expected: 全部 PASS；300 ms 预检期间 10 次心跳全部执行。

- [ ] **Step 6：提交入队切片**

```powershell
git add src/telegram_downloader/content_browser.py src/telegram_downloader/controller.py tests/test_content_browser.py tests/test_controller.py
git commit -m "perf: run queue preparation off UI loop"
```

## Task 7：装配动作策略与生命周期合同

**Files:**
- Modify: `src/telegram_downloader/app.py:1032-1068, 1190-1285`
- Modify: `src/telegram_downloader/ui/async_actions.py:36-80`
- Modify: `tests/test_app.py:46-80, 820-870`
- Modify: `tests/ui/test_async_actions.py`

- [ ] **Step 1：写动作策略与装配失败测试**

在 `tests/test_app.py` 的 `EXPECTED_POLICIES` 增加：

```python
"content.history.open": ActionPolicy.REPLACE_LATEST,
"content.queue": ActionPolicy.DEDUPLICATE,
```

增加源装配合同：

```python
def test_content_history_queue_and_selection_use_responsive_wiring() -> None:
    source = getsource(app.create_application)
    assert '"content.history.open"' in source
    assert '"content.queue"' in source
    assert "selection_intent_requested.connect(controller.submit_content_selection)" in source
    assert "history_open_requested.connect(controller._reload_content_search)" not in source
    assert "selection_changed.connect(controller.set_content_selected)" not in source
```

- [ ] **Step 2：运行 app 与动作桥测试，确认 RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_app.py tests\ui\test_async_actions.py -q
```

Expected: FAIL，因为两个策略和新装配尚不存在。

- [ ] **Step 3：更新动作策略与信号连接**

`ACTION_POLICIES` 增加两个固定键。`create_application()` 删除旧的同步 history/selection 连接，改为：

```python
window.content_page.selection_intent_requested.connect(
    controller.submit_content_selection
)
async_actions.connect_payload(
    window.content_page.history_open_requested,
    "content.history.open",
    controller.open_content_history,
    hooks=ActionHooks(failed=content_failure),
)
async_actions.connect_payload(
    window.content_page.queue_requested,
    "content.queue",
    controller.queue_content_selection,
    hooks=ActionHooks(
        started=lambda: window.content_page.set_queue_busy(True),
        failed=content_failure,
        finished=lambda: window.content_page.set_queue_busy(False),
    ),
)
```

删除 `content_queue_requested` 的 qasync slot，避免同一信号被连接两次。页面 `_emit_queue()` 只发信号，不自行重复设置 busy；busy 由 started hook 在任务创建前同步设置。

同时从 `AppController.queue_content_selection()` 删除入口和 `finally` 中的 `page.set_queue_busy(...)`；Controller 只负责业务状态与错误，按钮忙碌状态唯一归 `ActionHooks`。把原来直接调用 Controller 并断言 `[True, False]` 的测试迁到动作桥测试；Controller 单元测试只断言预检、确认、提交、校正、刷新和启动顺序。

- [ ] **Step 4：验证关闭清理**

扩展 `tests/test_controller.py` 关闭测试：选择 writer、搜索、历史 replace-latest 和缩略图任务全部结束后，`_selection_intents` 为空，`_selection_persist_task` 为 `None`。扩展 `tests/ui/test_async_actions.py` 验证 history replace-latest 会取消旧等待，queue deduplicate 不创建第二个任务。

- [ ] **Step 5：运行装配回归，确认 GREEN**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_app.py tests\ui\test_async_actions.py tests\test_controller.py -q
.\.venv\Scripts\ruff.exe check src\telegram_downloader\app.py src\telegram_downloader\ui\async_actions.py tests\test_app.py tests\ui\test_async_actions.py
```

Expected: 全部 PASS，动作策略映射与源装配合同一致。

- [ ] **Step 6：提交装配切片**

```powershell
git add src/telegram_downloader/app.py src/telegram_downloader/ui/async_actions.py tests/test_app.py tests/ui/test_async_actions.py tests/test_controller.py
git commit -m "perf: wire responsive content actions"
```

## Task 8：基准、版本、发布说明与三轮验证

**Files:**
- Create: `scripts/benchmark_search_results.py`
- Modify: `pyproject.toml:7`
- Modify: `src/telegram_downloader/__init__.py:1`
- Modify: `installer/TelegramDownloader.iss:2`
- Modify: `tests/test_packaging_contract.py`
- Create: `docs/releases/v0.18.2.md`
- Create: `docs/verification/v0.18.2-search-response-performance.md`

- [ ] **Step 1：创建固定合成模型基准**

`scripts/benchmark_search_results.py` 必须只打印数量、毫秒和比例，不能读取真实数据库或消息：

```python
from __future__ import annotations

import argparse
import statistics
from dataclasses import replace
from datetime import UTC, datetime
from time import perf_counter

from telegram_downloader.content import SearchResult
from telegram_downloader.domain import MediaKind
from telegram_downloader.ui.content_models import SearchResultTableModel


def make_results(count: int) -> list[SearchResult]:
    now = datetime(2026, 8, 24, tzinfo=UTC)
    base = SearchResult(
        "r0", "s1", "a1", "peer", count, None, "m0", MediaKind.PHOTO,
        "image.jpg", 1024, now, "synthetic", "synthetic-thumb",
    )
    return [
        replace(
            base,
            id=f"r{index}",
            message_id=count - index,
            media_id=f"m{index}",
            thumbnail_key=f"t{index}",
        )
        for index in range(count)
    ]


def median_ms(operation, repeats: int) -> float:
    values = []
    for _ in range(repeats):
        started = perf_counter()
        operation()
        values.append((perf_counter() - started) * 1000)
    return statistics.median(values)


def measure(count: int, repeats: int) -> tuple[float, float]:
    values = make_results(count)
    initial = median_ms(
        lambda: SearchResultTableModel().apply_results(values),
        repeats,
    )
    model = SearchResultTableModel()
    model.apply_results(values)
    updated = list(values)
    updated[-1] = replace(updated[-1], selected=True)
    changed = median_ms(lambda: model.apply_results(updated), repeats)
    return initial, changed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args()
    if args.repeats <= 0:
        raise SystemExit("repeats must be positive")
    measurements = {}
    for count in (100, 500, 1_000, 2_000, 10_000):
        initial, changed = measure(count, args.repeats)
        measurements[count] = (initial, changed)
        print(
            f"ROWS={count} INITIAL_MEDIAN_MS={initial:.2f} "
            f"ONE_ROW_MEDIAN_MS={changed:.2f}"
        )
    ratio = measurements[10_000][1] / max(measurements[2_000][1], 0.001)
    print(f"UPDATE_SCALE_10000_OVER_2000={ratio:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2：运行基准并记录结果**

```powershell
$env:QT_QPA_PLATFORM = 'offscreen'
.\.venv\Scripts\python.exe scripts\benchmark_search_results.py --repeats 7
```

Expected: 10,000 行首次装载和单行更新中位数分别不超过约 100 ms，`UPDATE_SCALE_10000_OVER_2000` 不超过 8.00。把实际输出原样写入验证文档，不把绝对时间加入 pytest 硬断言。

- [ ] **Step 3：增加版本合同并确认 RED**

把 `tests/test_packaging_contract.py` 的版本期望改为 `0.18.2`，并断言 `scripts/benchmark_search_results.py` 不读取 `data`、`catalog.sqlite3`、环境凭据或 Telegram URL。

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_packaging_contract.py -q
```

Expected: FAIL，当前源版本仍为 0.18.1。

- [ ] **Step 4：更新版本与发布说明**

将以下三个位置统一改为 `0.18.2`：

- `pyproject.toml` 的 `project.version`
- `src/telegram_downloader/__init__.py` 的 `__version__`
- `installer/TelegramDownloader.iss` 的 `AppVersion`

`docs/releases/v0.18.2.md` 固定包含：线性结果模型、10,000 条基准、后台 SQLite、选择批处理、历史 latest-wins、入队幂等恢复、兼容性和隐私边界。

- [ ] **Step 5：运行实现级聚焦回归**

```powershell
.\.venv\Scripts\python.exe -m pytest tests\ui\test_content_models.py tests\ui\test_content_browser.py tests\test_catalog.py tests\test_content_browser.py tests\test_controller.py tests\test_app.py tests\ui\test_async_actions.py tests\test_account_wide_search_e2e.py tests\test_packaging_contract.py -q
.\.venv\Scripts\ruff.exe check src tests scripts
git diff --check
```

Expected: 全部 PASS，Ruff 输出 `All checks passed!`，`git diff --check` 无输出。

- [ ] **Step 6：执行第 1 轮完整自检**

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\test.ps1
$env:QT_QPA_PLATFORM = 'offscreen'
.\.venv\Scripts\python.exe scripts\benchmark_search_results.py --repeats 7
```

Expected: 完整 pytest 与 Ruff PASS；记录测试数量、耗时、五档基准和比例。

- [ ] **Step 7：执行第 2 轮便携包自检**

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build.ps1
```

Expected: 构建内置 pytest/Ruff PASS，输出 `PACKAGED_SMOKE_OK`，生成 `dist\TelegramDownloader-0.18.2-win-x64-portable.zip`。记录文件大小与 SHA-256。

- [ ] **Step 8：执行第 3 轮安装器与 GUI 自检**

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build-installer.ps1
```

Expected: 构建内置 pytest/Ruff PASS，输出 `PACKAGED_SMOKE_OK` 和 `INSTALLER_SMOKE_OK`，生成 `dist\release\TelegramDownloader-0.18.2-win-x64-setup.exe`。

随后在 Windows GUI 使用只含合成数据的测试目录检查：搜索首批显示、10,000 条稳定快照、历史连续切换、单项连续勾选、全选、反选、滚动锚点、缩略图保留、取消搜索和入队确认。不得连接或输出真实账号内容。

- [ ] **Step 9：写验证文档并做隐私检查**

`docs/verification/v0.18.2-search-response-performance.md` 记录：

- 实现提交 SHA。
- 三轮命令与实际通过数量。
- 优化前基线和优化后七次运行中位数。
- 心跳、SQL 事务次数、Qt 信号范围与 generation 测试结果。
- 便携包和安装器大小、SHA-256、冒烟标志。
- GUI 合成数据检查结论。

运行：

```powershell
rg -n "tg://login\?token=|api_hash|session=|phone=|peer_ref=|message_id=" docs\verification\v0.18.2-search-response-performance.md scripts\benchmark_search_results.py
git status --short
git diff --check
```

Expected: `rg` 无敏感命中；Git 只显示本任务预期文件；diff check 无输出。

- [ ] **Step 10：提交发布候选切片**

```powershell
git add scripts/benchmark_search_results.py pyproject.toml src/telegram_downloader/__init__.py installer/TelegramDownloader.iss tests/test_packaging_contract.py docs/releases/v0.18.2.md docs/verification/v0.18.2-search-response-performance.md
git commit -m "release: prepare TG Quick Fetch 0.18.2"
```

提交后再次运行：

```powershell
git status --short --branch
git diff --check v0.18.1..HEAD
```

Expected: 工作区干净，整个 v0.18.1..HEAD 范围无空白错误。

## 计划完成标准

- 所有 Task 均按 RED → GREEN 顺序执行并独立提交。
- 10,000 行模型路径满足中位数和伸缩比例目标。
- 注入 300 ms 本地阻塞时事件循环心跳持续推进。
- 搜索第一批、累计快照、稳定锁、滚动锚点、选择与缩略图状态全部保持。
- 单项选择不再全量重载；全选/反选为单事务；历史为 latest-wins；入队预检和提交不阻塞 UI。
- 三轮自检全部通过，验证文档不含账号、关键词、消息、文件路径或凭据。
- 未经用户明确选择“合并并发布”，不得推送、打标签或切换在线更新指针。
