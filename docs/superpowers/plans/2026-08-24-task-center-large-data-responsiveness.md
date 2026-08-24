# Task Center Large-Data Responsiveness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让任务中心在 10,000 个任务和单任务 50,000 个媒体项下仍保持快速筛选、增量刷新、稳定选择和可响应分页，并停止 GUI 事件循环内的全量同步任务/媒体读取。

**Architecture:** 在现有 `TaskRepository` 上增加批量快照、键集分页和只读索引；新增纯摘要构建器与 `TaskRefreshCoordinator`，将 500 ms dirty-ID 增量刷新和 5 秒完整校准统一管理。Qt 任务模型按稳定 ID 更新，媒体模型每页 500 条，控制器和应用只通过异步入口装配这些组件。

**Tech Stack:** Python 3.12, asyncio/qasync, SQLite, PySide6, pytest/pytest-qt, Ruff, PyInstaller, Inno Setup

---

## 实施约束

- 唯一需求来源是 `docs/superpowers/specs/2026-08-24-task-center-large-data-responsiveness-design.md`。
- 从设计提交 `5f37e33` 建立独立实施工作树；不得在 v0.18.3 候选分支直接修改代码。
- 严格 TDD：每个行为先得到目标明确的失败测试，再写最小实现。
- 不改变 Telegram 搜索、下载协议、全局媒体槽、带宽限制、订阅规则或下载持久化的 500 ms 固定窗口。
- 不增加任务或媒体业务字段；只允许增加可删除重建的 SQLite 索引 `idx_items_task_page`。
- 所有 SQLite 读取和索引创建必须离开 GUI 事件循环；同步兼容入口不能被生产 Qt 信号或启动路径调用。
- 性能和日志数据不得包含任务标题、来源、文件名、路径、消息、账号或凭据。
- 只形成 v0.18.4 本地候选。未获得新的“合并并发布更新”授权前，不合并 `main`、不打标签、不推送、不上传资产、不修改在线 stable 指针。

## 文件结构

### 新增

- `src/telegram_downloader/task_center.py`：纯任务摘要构建、进度样本和聚合仪表数据。
- `src/telegram_downloader/task_refresh.py`：dirty-ID 固定窗口、完整校准、代次和关闭生命周期。
- `tests/test_task_center.py`：纯摘要构建和聚合增量合同。
- `tests/test_task_refresh.py`：刷新窗口、立即刷新、校准、代次、故障和关闭合同。
- `tests/ui/test_task_center_responsiveness.py`：慢仓库下 Qt 心跳、筛选、分页和选择稳定门禁。
- `scripts/benchmark_task_center.py`：10,000 个合成任务和 50,000 个临时媒体的离线性能基准。
- `docs/releases/v0.18.4.md`：用户可见行为、兼容性和隐私边界。
- `docs/verification/v0.18.4-task-center-responsiveness.md`：三轮真实验证记录。

### 修改

- `src/telegram_downloader/repository.py`：批量任务快照、媒体批量读取、键集分页和后台索引准备。
- `src/telegram_downloader/ui/models.py`：稳定 ID 任务增量模型、单次筛选统计和媒体分页模型。
- `src/telegram_downloader/ui/main.py`：150 ms 筛选防抖、分页请求、局部摘要应用和选择/滚动锚点。
- `src/telegram_downloader/ui/async_actions.py`：任务详情 latest-wins 与媒体分页去重策略。
- `src/telegram_downloader/controller.py`：异步启动、增量任务刷新、媒体分页和可见媒体补丁。
- `src/telegram_downloader/app.py`：异步任务选择/分页信号和生命周期装配。
- `tests/test_repository.py`、`tests/test_ui_models.py`、`tests/ui/test_main_window.py`、`tests/test_controller.py`、`tests/test_app.py`：对应回归。
- `tests/test_packaging_contract.py`：v0.18.4 元数据和合成基准隐私合同。
- `pyproject.toml`、`src/telegram_downloader/__init__.py`、`installer/TelegramDownloader.iss`：本地候选版本 0.18.4。
- `README.md`：任务中心大数据行为与兼容性说明。

## Task 1: 增加按 ID 批量任务快照

**Files:**

- Modify: `src/telegram_downloader/repository.py`
- Modify: `tests/test_repository.py`

- [ ] **Step 1: 写入空输入、去重、缺失和固定顺序的失败测试**

在 `tests/test_repository.py` 复用 `records()`，创建三个任务并加入：

```python
def test_task_snapshots_by_ids_are_bulk_ordered_and_ignore_missing(tmp_path: Path) -> None:
    repo = TaskRepository(tmp_path / "tasks.sqlite3")
    repo.initialize()
    first, media = records(tmp_path)
    second = replace(
        first,
        id="task-2",
        created_at=first.created_at + timedelta(seconds=1),
        updated_at=first.updated_at + timedelta(seconds=1),
    )
    third = replace(
        first,
        id="task-3",
        created_at=first.created_at + timedelta(seconds=2),
        updated_at=first.updated_at + timedelta(seconds=2),
    )
    repo.create_task(first, [media])
    repo.create_task(second, [replace(media, id="item-2", task_id=second.id, message_id=8, media_id="m8")])
    repo.create_task(third, [replace(media, id="item-3", task_id=third.id, message_id=9, media_id="m9")])

    snapshots = repo.list_task_snapshots_by_ids(
        [first.id, "missing", third.id, first.id],
        include_archived=True,
    )

    assert [snapshot.task.id for snapshot in snapshots] == [third.id, first.id]
    assert repo.list_task_snapshots_by_ids([]) == []
```

再用包装连接统计 `SELECT` 数量，输入 401 个 ID 时要求最多三条分块聚合查询，不能调用 `get_task()` 或 `list_items()`。

- [ ] **Step 2: 运行失败测试**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_repository.py -k "snapshots_by_ids" -q
```

Expected: FAIL，`TaskRepository` 没有 `list_task_snapshots_by_ids`。

- [ ] **Step 3: 提取共享聚合 SQL 并实现批量接口**

在 `repository.py` 增加私有连接级方法，完整接口如下：

```python
def list_task_snapshots_by_ids(
    self,
    task_ids: Sequence[str],
    *,
    include_archived: bool = True,
) -> list[TaskSnapshot]:
    ordered = tuple(dict.fromkeys(task_ids))
    if not ordered:
        return []
    snapshots: list[TaskSnapshot] = []
    with self._connection() as connection:
        for selected in batched(ordered, 200):
            snapshots.extend(
                self._list_task_snapshots_on_connection(
                    connection,
                    tuple(selected),
                    include_archived=include_archived,
                )
            )
    return sorted(
        snapshots,
        key=lambda value: (-value.task.created_at.timestamp(), value.task.id),
    )
```

`_list_task_snapshots_on_connection` 必须复用 `list_task_snapshots` 的完整聚合列，只把 where 条件扩展为 `t.id IN (...)` 和可选的 `t.archived_at IS NULL`；`list_task_snapshots` 本身也调用该共享查询构造器，避免两份聚合 SQL 漂移。

- [ ] **Step 4: 运行仓库回归和 Ruff**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_repository.py -q
.\.venv\Scripts\python.exe -m ruff check src/telegram_downloader/repository.py tests/test_repository.py
```

Expected: PASS；401 个 ID 不产生 N+1，原完整快照结果不变。

- [ ] **Step 5: 提交批量快照**

```powershell
git add src/telegram_downloader/repository.py tests/test_repository.py
git commit -m "feat: query task snapshots by id"
```

## Task 2: 增加媒体键集分页、批量读取和兼容索引

**Files:**

- Modify: `src/telegram_downloader/repository.py`
- Modify: `tests/test_repository.py`

- [ ] **Step 1: 写入分页顺序、同键续页和索引幂等失败测试**

导入新类型并加入：

```python
def test_media_keyset_pages_are_complete_stable_and_counted(tmp_path: Path) -> None:
    repo = TaskRepository(tmp_path / "tasks.sqlite3")
    repo.initialize()
    task, first = records(tmp_path)
    items = [
        replace(
            first,
            id=f"item-{index}",
            message_id=10 - index // 2,
            media_id=f"media-{index}",
            target_path=tmp_path / f"{index}.bin",
        )
        for index in range(7)
    ]
    repo.create_task(task, items)

    first_page = repo.list_items_page(task.id, limit=3)
    second_page = repo.list_items_page(task.id, after=first_page.next_cursor, limit=3)
    third_page = repo.list_items_page(task.id, after=second_page.next_cursor, limit=3)

    combined = (*first_page.items, *second_page.items, *third_page.items)
    assert len({item.id for item in combined}) == 7
    assert [item.id for item in combined] == [item.id for item in repo.list_items(task.id)]
    assert first_page.total_count == second_page.total_count == third_page.total_count == 7
    assert third_page.next_cursor is None
```

再验证 `get_items(["item-3", "missing", "item-1", "item-3"])` 按首次输入顺序返回 `item-3, item-1`，以及 `ensure_task_center_indexes()` 连续调用两次后 `PRAGMA index_list(media_items)` 只含一个 `idx_items_task_page`。

- [ ] **Step 2: 运行失败测试**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_repository.py -k "keyset or task_center_indexes or get_items" -q
```

Expected: FAIL，分页类型和三个仓库方法尚不存在。

- [ ] **Step 3: 定义分页值对象**

在 `repository.py` 增加：

```python
@dataclass(frozen=True, slots=True)
class ItemPageCursor:
    message_date_utc: datetime
    message_id: int
    item_id: str


@dataclass(frozen=True, slots=True)
class ItemPage:
    items: tuple[MediaItem, ...]
    next_cursor: ItemPageCursor | None
    total_count: int
```

`list_items_page` 要求 `1 <= limit <= 1000`，用 `limit + 1` 判断是否还有下一页。游标 SQL 必须精确为：

```sql
AND (
    message_date_utc < ?
    OR (message_date_utc = ? AND message_id < ?)
    OR (message_date_utc = ? AND message_id = ? AND id > ?)
)
ORDER BY message_date_utc DESC, message_id DESC, id ASC
LIMIT ?
```

下一游标取实际返回页的最后一个媒体；没有额外行时为 `None`。同一连接另执行一次 `COUNT(*) WHERE task_id = ?`。

- [ ] **Step 4: 实现批量媒体读取和索引准备**

实现：

```python
def get_items(self, item_ids: Sequence[str]) -> list[MediaItem]:
    ordered = tuple(dict.fromkeys(item_ids))
    if not ordered:
        return []
    found: dict[str, MediaItem] = {}
    with self._connection() as connection:
        for selected in batched(ordered, 200):
            marks = ",".join("?" for _ in selected)
            rows = connection.execute(
                f"SELECT {_ITEM_COLUMNS} FROM media_items WHERE id IN ({marks})",
                tuple(selected),
            ).fetchall()
            found.update((str(row["id"]), self._item_from_row(row)) for row in rows)
    return [found[item_id] for item_id in ordered if item_id in found]

def ensure_task_center_indexes(self) -> None:
    with self._connection() as connection:
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_items_task_page "
            "ON media_items(task_id, message_date_utc DESC, message_id DESC, id ASC)"
        )
```

不要把新索引放进 `_SCHEMA`；其生产调用将在主窗口显示后、调度器恢复前异步执行。

- [ ] **Step 5: 运行完整仓库测试、查询计划断言和 Ruff**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_repository.py -q
.\.venv\Scripts\python.exe -m ruff check src/telegram_downloader/repository.py tests/test_repository.py
```

Expected: PASS；`EXPLAIN QUERY PLAN` 在索引准备后包含 `idx_items_task_page`，旧 `list_items` 顺序不变。

- [ ] **Step 6: 提交媒体读取边界**

```powershell
git add src/telegram_downloader/repository.py tests/test_repository.py
git commit -m "feat: page large task media"
```

## Task 3: 建立纯任务摘要与仪表聚合器

**Files:**

- Create: `src/telegram_downloader/task_center.py`
- Create: `tests/test_task_center.py`
- Modify: `src/telegram_downloader/controller.py`

- [ ] **Step 1: 写入纯构建、速度采样和增量聚合失败测试**

新测试构造两个 `TaskSnapshot`、`SchedulerSnapshot` 和旧进度样本，核心断言：

```python
result = build_task_view(
    snapshots,
    scheduler_state=scheduler,
    queue_positions={"queued": 2},
    sampled_at=11.0,
    previous_samples={"active": ProgressSample(10.0, 100)},
)

assert result.by_id["active"].speed_bps == 50.0
assert result.by_id["queued"].queue_position == 2
assert result.dashboard.completed_items == 3
assert result.progress_samples["active"] == ProgressSample(11.0, 150)
assert snapshots[0].task.id == "active"
```

再用 `build_task_patch(..., requested_ids=(...))` 和 `patch_task_view(previous, patch)` 断言只替换指定 ID、把请求后未返回的 ID 删除，并通过“减旧贡献、加新贡献”得到与完整重算相同的仪表值；补丁后的 `order_keys` 必须让新任务插入 `created_at DESC, id ASC` 的正确位置。

- [ ] **Step 2: 运行失败测试**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_task_center.py -q
```

Expected: FAIL，`telegram_downloader.task_center` 不存在。

- [ ] **Step 3: 实现不可变视图结果**

模块公开以下类型：

```python
@dataclass(frozen=True, slots=True)
class ProgressSample:
    sampled_at: float
    downloaded_bytes: int


@dataclass(frozen=True, slots=True)
class TaskDashboard:
    total_speed_bps: float
    completed_items: int
    remaining_items: int
    current_task_id: str | None


@dataclass(frozen=True, slots=True)
class TaskView:
    ordered: tuple[TaskSummary, ...]
    by_id: Mapping[str, TaskSummary]
    order_keys: Mapping[str, tuple[float, str]]
    progress_samples: Mapping[str, ProgressSample]
    dashboard: TaskDashboard


@dataclass(frozen=True, slots=True)
class TaskViewPatch:
    replacements: Mapping[str, TaskSummary]
    order_keys: Mapping[str, tuple[float, str]]
    progress_samples: Mapping[str, ProgressSample]
    removed_ids: frozenset[str]
```

`build_task_view` 从现有控制器 `_summaries_from_snapshots` 搬移完整格式化和速度计算逻辑，输入与输出均不可变，不访问 Qt、仓库或调度器。排序键固定为 `(-created_at.timestamp(), task_id)`。`build_task_patch` 接收请求 ID 和实际返回快照，把未返回的请求 ID 放入 `removed_ids`；`patch_task_view(previous, patch)` 使用显式 `order_keys` 合并，不能依赖不含创建时间的 `TaskSummary` 猜测顺序。不得在后台线程修改控制器字段。

- [ ] **Step 4: 让旧控制器完整刷新复用纯构建器**

保留当前公开行为，但把 `refresh_tasks` 和 `_refresh_task_views` 的摘要转换改为调用 `build_task_view`；控制器只在 Qt 线程替换 `_progress_samples` 和应用结果。此步不改变刷新频率，先确保纯逻辑与旧显示一致。

- [ ] **Step 5: 运行聚焦回归与 Ruff**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_task_center.py tests/test_controller.py -k "refresh_tasks or speed or remaining or queue_position" -q
.\.venv\Scripts\python.exe -m ruff check src/telegram_downloader/task_center.py src/telegram_downloader/controller.py tests/test_task_center.py
```

Expected: PASS；速度、剩余时间、队列位置和仪表统计与基线一致。

- [ ] **Step 6: 提交纯视图构建器**

```powershell
git add src/telegram_downloader/task_center.py src/telegram_downloader/controller.py tests/test_task_center.py
git commit -m "refactor: isolate task center view state"
```

## Task 4: 将任务表改为稳定 ID 增量模型

**Files:**

- Modify: `src/telegram_downloader/ui/models.py`
- Modify: `tests/test_ui_models.py`
- Modify: `tests/ui/test_main_window.py`

- [ ] **Step 1: 写入无重置更新、顺序变化和单次计数失败测试**

加入 10,000 条合成摘要和 `QSignalSpy`：

```python
model = TaskTableModel()
model.set_tasks(make_task_summaries(10_000))
reset_spy = QSignalSpy(model.modelReset)
reset_count = reset_spy.count()
changed = QSignalSpy(model.dataChanged)

model.apply_tasks(
    [replace(model.task_by_id("task-5000"), progress_text="1 / 2")],
    {"task-5000": (5000.0, "task-5000")},
)

assert reset_spy.count() == reset_count
assert changed.count() == 1
assert model.row_for_task_id("task-5000") is not None
```

分别验证新增、删除、移动和筛选切换均不新增 `modelReset`；持久索引仍指向原任务 ID。给 `_matches_filter` 加计数包装，调用 `filter_counts()` 后要求每个任务只经过一次状态分类，而不是每个 `TaskFilter` 一次。

- [ ] **Step 2: 运行失败测试**

Run:

```powershell
$env:QT_QPA_PLATFORM='offscreen'
.\.venv\Scripts\python.exe -m pytest tests/test_ui_models.py tests/ui/test_main_window.py -k "incremental or persistent or filter_counts" -q
```

Expected: FAIL，模型没有 `apply_tasks` / `task_by_id`，筛选仍重置并重复遍历。

- [ ] **Step 3: 实现任务索引和单次分类**

`TaskTableModel` 增加：

```python
self._all_by_id: dict[str, TaskSummary] = {}
self._row_by_id: dict[str, int] = {}
self._normalized_titles: dict[str, str] = {}
self._filter_counts = {selected: 0 for selected in TaskFilter}
```

公开最小接口：

```python
def apply_snapshot(
    self,
    tasks: Sequence[TaskSummary],
    order_keys: Mapping[str, tuple[float, str]],
) -> None: ...
def apply_tasks(
    self,
    tasks: Sequence[TaskSummary],
    order_keys: Mapping[str, tuple[float, str]],
    removed_ids: Collection[str] = (),
) -> None: ...
def task_by_id(self, task_id: str) -> TaskSummary | None: ...
def all_tasks(self) -> tuple[TaskSummary, ...]: ...
```

模型保存每个 ID 的显式 `order_keys`。算法固定为：删除缺失行时倒序 `beginRemoveRows`；新 ID 用排序键二分目标位置并 `beginInsertRows`；已有 ID 排序键变化时用 `beginMoveRows`；内容变化把连续行合并后发 `dataChanged`。`apply_snapshot` 首次允许一次 reset，之后不得常规 reset。每次结构变化后重建 `_row_by_id`，先验证目标 ID 唯一，重复 ID 抛出 `ValueError("任务视图包含重复 ID")`。旧 `set_tasks(tasks)` 兼容入口按输入行生成 `(float(row), task_id)` 排序键并委托 `apply_snapshot`；生产控制器只传真实时间排序键。

筛选函数一次遍历同时计算 ALL、ACTIVE、PAUSED、FAILED、COMPLETED、ARCHIVED，并生成可见 ID。筛选集合变化使用布局变化和稳定 ID 持久索引映射，不调用 `beginResetModel`。

- [ ] **Step 4: 运行模型和窗口回归**

Run:

```powershell
$env:QT_QPA_PLATFORM='offscreen'
.\.venv\Scripts\python.exe -m pytest tests/test_ui_models.py tests/ui/test_main_window.py -q
.\.venv\Scripts\python.exe -m ruff check src/telegram_downloader/ui/models.py tests/test_ui_models.py tests/ui/test_main_window.py
```

Expected: PASS；原状态文本、筛选语义、多选和当前行行为保持不变。

- [ ] **Step 5: 提交任务增量模型**

```powershell
git add src/telegram_downloader/ui/models.py tests/test_ui_models.py tests/ui/test_main_window.py
git commit -m "perf: update task rows incrementally"
```

## Task 5: 将媒体表改为分页追加和局部补丁模型

**Files:**

- Modify: `src/telegram_downloader/ui/models.py`
- Modify: `tests/ui/test_main_window.py`

- [ ] **Step 1: 写入首批、追加、补丁和重复拒绝失败测试**

核心测试：

```python
model = TaskItemTableModel()
model.begin_task("task", total_count=50_000)
model.append_page("task", make_item_summaries(0, 500))
reset_spy = QSignalSpy(model.modelReset)
reset_count = reset_spy.count()
inserted = QSignalSpy(model.rowsInserted)

model.append_page("task", make_item_summaries(500, 500))
model.apply_items(
    "task",
    [replace(model.item_by_id("item-20"), downloaded_bytes=8)],
)

assert model.rowCount() == 1000
assert model.loaded_count == 1000
assert model.total_count == 50_000
assert reset_spy.count() == reset_count
assert inserted.count() == 1
assert model.item_by_id("item-20").downloaded_bytes == 8
```

验证错误任务 ID、页内重复 ID、与已加载 ID 重复都抛出 `ValueError` 且不留下部分追加；`visible_item_ids(first_row, last_row)` 返回有界稳定 ID。

- [ ] **Step 2: 运行失败测试**

Run:

```powershell
$env:QT_QPA_PLATFORM='offscreen'
.\.venv\Scripts\python.exe -m pytest tests/ui/test_main_window.py -k "item_model and (page or patch or duplicate)" -q
```

Expected: FAIL，分页模型接口不存在。

- [ ] **Step 3: 实现媒体分页状态**

公开接口固定为：

```python
def begin_task(self, task_id: str, *, total_count: int) -> None: ...
def append_page(self, task_id: str, items: Sequence[TaskItemSummary]) -> None: ...
def apply_items(self, task_id: str, items: Sequence[TaskItemSummary]) -> None: ...
def item_by_id(self, item_id: str) -> TaskItemSummary | None: ...
def loaded_ids(self) -> tuple[str, ...]: ...
def visible_item_ids(self, first_row: int, last_row: int) -> tuple[str, ...]: ...
```

切换任务的 `begin_task` 可以执行一次 reset；同任务追加和更新只能使用 `beginInsertRows` / `dataChanged`。模型维护 `task_id`、`total_count`、`_row_by_id`，并提供 `has_more = loaded_count < total_count`。

保留现有 `set_items(items)` 作为一版兼容包装：它以当前或空任务 ID 调用 `begin_task` 后追加整批，仅供旧测试 double 和非生产调用过渡；新增打包合同确保生产控制器不再调用该全量入口。

- [ ] **Step 4: 运行媒体模型完整回归和 Ruff**

Run:

```powershell
$env:QT_QPA_PLATFORM='offscreen'
.\.venv\Scripts\python.exe -m pytest tests/ui/test_main_window.py -k "task_item or media" -q
.\.venv\Scripts\python.exe -m ruff check src/telegram_downloader/ui/models.py tests/ui/test_main_window.py
```

Expected: PASS；现有格式、完整性颜色、工具提示和媒体操作选择不变。

- [ ] **Step 5: 提交媒体分页模型**

```powershell
git add src/telegram_downloader/ui/models.py tests/ui/test_main_window.py
git commit -m "perf: virtualize task media rows"
```

## Task 6: 增加筛选防抖、分页触发和稳定锚点

**Files:**

- Modify: `src/telegram_downloader/ui/main.py`
- Modify: `tests/ui/test_main_window.py`

- [ ] **Step 1: 写入 150 ms latest-wins 和立即应用失败测试**

窗口测试记录 `task_model.set_filter` 调用，并直接断言计时器间隔为 150 ms。每次输入间隔 25 ms 连续形成 `a`、`ab`、`abc`，在累计 75 ms 时仍无调用，随后用条件等待确认只调用最终 `abc`；不在 149/150 ms 边界写易抖动断言。另验证 Return、文本清空和状态下拉切换会停止计时器并同步应用。

```python
assert window._task_filter_timer.interval() == 150
for character in "abc":
    qtbot.keyClick(window.task_search, character)
    qtbot.wait(25)
assert calls == []
qtbot.waitUntil(lambda: calls == [(TaskFilter.ALL, "abc")], timeout=500)

window.task_search.returnPressed.emit()
assert calls[-1] == (TaskFilter.ALL, "abc")
window.task_search.clear()
assert calls[-1] == (TaskFilter.ALL, "")
```

- [ ] **Step 2: 写入接近末尾分页和锚点保持失败测试**

新增 `task_items_page_requested = Signal(str)`。装入 500/50,000 行后滚动到末 100 行内只发出一次当前 task ID；追加完成前重复滚动不重复发射。任务补丁、筛选和顺序变化后，选中 ID、当前 ID 和首个可见任务 ID 不变。

- [ ] **Step 3: 运行失败测试**

Run:

```powershell
$env:QT_QPA_PLATFORM='offscreen'
.\.venv\Scripts\python.exe -m pytest tests/ui/test_main_window.py -k "debounce or page_requested or scroll_anchor" -q
```

Expected: FAIL，输入仍立即筛选，分页信号和锚点接口不存在。

- [ ] **Step 4: 实现防抖和分页状态**

在 `MainWindow.__init__` 创建：

```python
self._task_filter_timer = QTimer(self)
self._task_filter_timer.setSingleShot(True)
self._task_filter_timer.setInterval(150)
self._task_filter_timer.timeout.connect(self._apply_task_filter_now)
self._task_item_page_pending = False
```

`textChanged` 连接 `_schedule_task_filter`；空文本立即应用，否则启动计时器。`returnPressed` 和状态切换调用 `_apply_task_filter_now`。媒体滚动处理器计算最后可见行；当 `last_visible >= rowCount - 100`、模型 `has_more` 且无 pending 时发出任务 ID。新增 `set_task_items_page_busy(bool)` 和 `show_task_items_page_error(message)`，错误只影响底部提示。

任务更新前记录选中 ID、当前 ID 和顶部可见 ID；应用后按 ID 恢复，顶部 ID 不存在时锚定最接近原行的可见任务。

窗口向控制器提供以下明确边界：

```python
def set_task_snapshot(
    self,
    tasks: Sequence[TaskSummary],
    order_keys: Mapping[str, tuple[float, str]],
    dashboard: TaskDashboard,
) -> None: ...

def apply_task_patch(
    self,
    tasks: Sequence[TaskSummary],
    order_keys: Mapping[str, tuple[float, str]],
    removed_ids: Collection[str],
    dashboard: TaskDashboard,
) -> None: ...

def begin_task_items(self, task_id: str, *, total_count: int) -> None: ...
def append_task_items(
    self,
    task_id: str,
    items: Sequence[TaskItemSummary],
    *,
    total_count: int,
) -> None: ...
def apply_task_items(self, task_id: str, items: Sequence[TaskItemSummary]) -> None: ...
def visible_task_item_ids(self) -> tuple[str, ...]: ...
```

`set_task_snapshot` 和 `apply_task_patch` 从传入的 `TaskDashboard` 更新速度、已完成数、剩余数和当前任务卡片，不能每 500 ms 再遍历全部任务。旧 `set_task_summaries` / `set_task_items` 只保留一版测试兼容包装；打包合同确保生产控制器不调用它们。

- [ ] **Step 5: 运行窗口完整测试和 Ruff**

Run:

```powershell
$env:QT_QPA_PLATFORM='offscreen'
.\.venv\Scripts\python.exe -m pytest tests/ui/test_main_window.py -q
.\.venv\Scripts\python.exe -m ruff check src/telegram_downloader/ui/main.py tests/ui/test_main_window.py
```

Expected: PASS；所有按钮、选择、导航和现有任务操作信号保持兼容。

- [ ] **Step 6: 提交窗口交互优化**

```powershell
git add src/telegram_downloader/ui/main.py tests/ui/test_main_window.py
git commit -m "perf: debounce and page task center views"
```

## Task 7: 实现增量刷新协调器

**Files:**

- Create: `src/telegram_downloader/task_refresh.py`
- Create: `tests/test_task_refresh.py`

- [ ] **Step 1: 写入固定 500 ms dirty 合并测试**

使用 20 ms 测试间隔和记录 loader：

```python
coordinator = TaskRefreshCoordinator(
    load_full=load_full,
    load_ids=load_ids,
    apply_full=apply_full,
    apply_patch=apply_patch,
    progress_interval=0.02,
    reconcile_interval=0.2,
)
await coordinator.activate()
applied.clear()
for _ in range(500):
    coordinator.mark_progress(["a"])
await asyncio.wait_for(applied.wait(), timeout=1)

assert id_batches == [("a",)]
assert full_calls == 1  # activate 的初始校准
```

记录第一条事件到应用的时间，要求后续 `mark_progress` 不把截止时间推迟超过原 20 ms 窗口。

- [ ] **Step 2: 写入立即刷新、5 秒校准和代次失败测试**

覆盖：

- `await refresh_now(["a", "b"])` 在 apply 完成前不返回。
- `await reconcile_now()` 读取完整快照并替换状态。
- 页面隐藏调用 deactivate 后不执行周期完整读取；再次进入调用 activate 并立即完整读取。
- `replace_generation()` 后旧 load 结果被取回但不 apply。
- load/apply 第一次异常返回给等待方、调用一次脱敏错误处理器，后续周期可重试。
- `close()` 停止 worker、取回异常、二次调用幂等并拒绝新工作。

- [ ] **Step 3: 运行失败测试**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_task_refresh.py -q
```

Expected: FAIL，模块不存在。

- [ ] **Step 4: 定义协议和协调器 API**

模块接口固定为：

```python
FullT = TypeVar("FullT")
PatchT = TypeVar("PatchT")

class TaskRefreshCoordinator(Generic[FullT, PatchT]):
    def __init__(
        self,
        *,
        load_full: Callable[[], Awaitable[FullT]],
        load_ids: Callable[[tuple[str, ...]], Awaitable[PatchT]],
        apply_full: Callable[[FullT], None],
        apply_patch: Callable[[PatchT], None],
        progress_interval: float = 0.5,
        reconcile_interval: float = 5.0,
        on_error: Callable[[BaseException], None] | None = None,
    ) -> None: ...

    async def activate(self) -> None: ...
    def deactivate(self) -> None: ...
    def mark_progress(self, task_ids: Collection[str]) -> None: ...
    async def refresh_now(self, task_ids: Collection[str]) -> None: ...
    async def reconcile_now(self) -> None: ...
    def replace_generation(self) -> None: ...
    async def close(self) -> None: ...
```

worker 使用 `asyncio.Event` 唤醒并根据 `loop.time()` 计算固定 deadline。完整读取、立即读取和进度读取由同一个 worker 串行执行，不能并发应用两个 `TaskView`；立即等待方绑定目标修订号，只有包含其 ID 的 apply 完成后才返回。`dirty_task_ids` 是 latest-wins 集合；从集合提取快照后到达的新 ID 留在下一窗口。完整校准期间到达的 dirty ID 不被清除。deactivate 和 `replace_generation()` 都使已启动结果失效。所有 task 有 done callback 取回异常；旧代次只禁止 apply，不取消已进入线程的 loader。

- [ ] **Step 5: 运行协调器测试和 Ruff**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_task_refresh.py -q
.\.venv\Scripts\python.exe -m ruff check src/telegram_downloader/task_refresh.py tests/test_task_refresh.py
```

Expected: PASS；无 pending task、未取回异常或不稳定固定 sleep 断言。

- [ ] **Step 6: 提交刷新协调器**

```powershell
git add src/telegram_downloader/task_refresh.py tests/test_task_refresh.py
git commit -m "feat: coordinate incremental task refreshes"
```

## Task 8: 将控制器迁移到异步增量任务和媒体分页

**Files:**

- Modify: `src/telegram_downloader/controller.py`
- Modify: `tests/test_controller.py`

- [ ] **Step 1: 把同步启动和详情读取测试改为失败的异步合同**

将 `test_task_detail_selection_loads_only_one_selected_task` 改为 async，仓库的 `list_items_page` 用 `threading.Event` 阻塞。启动选择后 5 ms 心跳必须推进，释放仓库后窗口收到 500 条第一页。

```python
operation = asyncio.create_task(controller.select_task_details(["task-1"]))
assert await asyncio.to_thread(started.wait, 1) is True
await heartbeat_reached(5)
release.set()
await operation
assert window.pages[-1].task_id == "task-1"
assert len(window.pages[-1].items) == 500
```

另断言 `controller.start()` 不调用同步 `refresh_tasks`；完整快照在线程释放前主事件循环持续运行。

- [ ] **Step 2: 写入 dirty-ID、终态立即刷新和可见媒体失败测试**

注入假 `TaskRefreshCoordinator`，验证：

- `_refresh_tasks_if_due` 只把 scheduler 的 active task IDs 交给 `mark_progress`。
- 暂停、继续、重试、归档、恢复等待 `refresh_now(accepted_ids)`。
- 优先级变化刷新全部 queued IDs，因为队列位置会联动。
- 终态事件调用立即刷新，不等待 500 ms。
- 当前任务只把窗口返回的可见媒体 ID 和已选媒体 ID 传给 `repository.get_items`。

- [ ] **Step 3: 运行失败测试**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_controller.py -k "task_refresh or detail_page or visible_media or async_start" -q
```

Expected: FAIL，控制器仍全量刷新且详情读取同步。

- [ ] **Step 4: 装配协调器和后台 view loader**

控制器构造时创建或注入 `TaskRefreshCoordinator[TaskView, TaskViewPatch]`。loader 必须完整离开事件循环：

```python
async def _load_full_task_view(self) -> TaskView:
    snapshots = await asyncio.to_thread(
        self.repository.list_task_snapshots,
        include_archived=True,
    )
    scheduler_state, queue_positions = self._task_scheduler_state()
    return await asyncio.to_thread(
        build_task_view,
        snapshots,
        scheduler_state=scheduler_state,
        queue_positions=queue_positions,
        sampled_at=monotonic_clock(),
        previous_samples=dict(self._progress_samples),
)
```

按 ID loader 使用 `list_task_snapshots_by_ids` 并构造补丁：

```python
async def _load_task_patch(self, task_ids: tuple[str, ...]) -> TaskViewPatch:
    snapshots = await asyncio.to_thread(
        self.repository.list_task_snapshots_by_ids,
        task_ids,
        include_archived=True,
    )
    scheduler_state, queue_positions = self._task_scheduler_state()
    return await asyncio.to_thread(
        build_task_patch,
        snapshots,
        requested_ids=task_ids,
        scheduler_state=scheduler_state,
        queue_positions=queue_positions,
        sampled_at=monotonic_clock(),
        previous_samples=dict(self._progress_samples),
    )
```

full apply 调用 `window.set_task_snapshot(view.ordered, view.order_keys, view.dashboard)`；patch apply 先执行 `updated = patch_task_view(current, patch)`，再调用 `window.apply_task_patch(tuple(patch.replacements.values()), patch.order_keys, patch.removed_ids, updated.dashboard)`。两者都只在 Qt 线程替换当前 `TaskView` 和 `_progress_samples`。生产 `start()` 先 `await asyncio.to_thread(repository.ensure_task_center_indexes)`；捕获错误只显示脱敏警告，然后 `await task_refresh.activate()`。删除生产启动中的同步 `refresh_tasks()`。

索引失败时设置 `_task_center_index_ready = False`。每次 5 秒完整校准前检查 scheduler snapshot；仅当 active 和 queued 都为空时才重试一次后台 `ensure_task_center_indexes`，成功后停止重试，下载活动期间不得尝试创建索引。

- [ ] **Step 5: 实现媒体页生命周期**

`select_task_details` 改为 async：0 或多选时递增 detail generation 并清空；单选时调用 `list_items_page(..., limit=500)`，转换为 `TaskItemSummary` 后依次调用 `window.begin_task_items` 和 `window.append_task_items`。新增：

```python
async def load_more_task_items(self, task_id: str) -> None: ...
async def refresh_visible_task_items(self) -> None: ...
```

每个任务保存下一 `ItemPageCursor`，同代次同一时刻一个分页 task。加载更多调用 `window.append_task_items`；失败保留当前模型并调用窗口错误提示。可见刷新通过 `repository.get_items` 一次读取 `window.visible_task_item_ids()` 与 `window.selected_media_ids()` 的并集，再调用 `window.apply_task_items`；旧代次丢弃。

- [ ] **Step 6: 迁移用户动作和关闭生命周期**

用户动作使用 accepted IDs 调用 `await task_refresh.refresh_now(...)`；优先级动作使用 scheduler snapshot 的 queued IDs 联合目标 ID。`_refresh_tasks_if_due` 只 mark active IDs 并刷新当前可见媒体。账号服务替换调用 `replace_generation()`；`shutdown()` 的 finally 中 `await task_refresh.close()`，无活动下载也必须关闭。

- [ ] **Step 7: 运行控制器、下载和完整性回归**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_controller.py tests/test_download_queue_e2e.py tests/test_file_integrity_e2e.py -q
.\.venv\Scripts\python.exe -m ruff check src/telegram_downloader/controller.py tests/test_controller.py
```

Expected: PASS；无同步生产刷新、N+1、pending task 或旧页覆盖。

- [ ] **Step 8: 提交控制器迁移**

```powershell
git add src/telegram_downloader/controller.py tests/test_controller.py
git commit -m "perf: refresh task center incrementally"
```

## Task 9: 更新应用信号、索引启动顺序和页面可见性

**Files:**

- Modify: `src/telegram_downloader/app.py`
- Modify: `src/telegram_downloader/ui/async_actions.py`
- Modify: `src/telegram_downloader/ui/main.py`
- Modify: `tests/test_app.py`
- Modify: `tests/ui/test_async_actions.py`
- Modify: `tests/ui/test_main_window.py`

- [ ] **Step 1: 写入异步选择、分页和页面激活失败测试**

扩展 app 信号路由测试，控制器 double 使用 `AsyncMock`：

```python
details_seen = asyncio.Event()
page_seen = asyncio.Event()
controller.select_task_details = AsyncMock(side_effect=lambda *_: details_seen.set())
controller.load_more_task_items = AsyncMock(side_effect=lambda *_: page_seen.set())
controller.set_task_center_visible = Mock()

window.task_selection_changed.emit(["task"])
window.task_items_page_requested.emit("task")
window.task_center_visibility_changed.emit(True)
await asyncio.wait_for(
    asyncio.gather(details_seen.wait(), page_seen.wait()),
    timeout=1,
)

controller.select_task_details.assert_awaited_once_with(["task"])
controller.load_more_task_items.assert_awaited_once_with("task")
controller.set_task_center_visible.assert_called_once_with(True)
```

验证重复选择采用 `REPLACE_LATEST`，重复分页采用 `DEDUPLICATE`，关闭应用会等待控制器协调器关闭。

- [ ] **Step 2: 运行失败测试**

Run:

```powershell
$env:QT_QPA_PLATFORM='offscreen'
.\.venv\Scripts\python.exe -m pytest tests/test_app.py tests/ui/test_async_actions.py tests/ui/test_main_window.py -k "task_selection or task_items_page or task_center_visibility or task_details" -q
```

Expected: FAIL，新信号和异步路由不存在。

- [ ] **Step 3: 增加页面可见性信号和异步路由**

`MainWindow.show_page("tasks")` 进入时发 `task_center_visibility_changed(True)`，离开时发 False；相同页面重复调用不重复发射。在 `ACTION_POLICIES` 注册 `task.details = REPLACE_LATEST` 和 `task.page = DEDUPLICATE`，`app.py` 用现有 `AsyncActionBridge.connect_payload` 注册：

```python
async_actions.connect_payload(
    window.task_selection_changed,
    "task.details",
    controller.select_task_details,
    policy=ActionPolicy.REPLACE_LATEST,
)
async_actions.connect_payload(
    window.task_items_page_requested,
    "task.page",
    controller.load_more_task_items,
    policy=ActionPolicy.DEDUPLICATE,
)
```

页面可见性是同步内存调用；不能直接访问仓库。

- [ ] **Step 4: 运行应用、窗口和生命周期回归**

Run:

```powershell
$env:QT_QPA_PLATFORM='offscreen'
.\.venv\Scripts\python.exe -m pytest tests/test_app.py tests/ui/test_async_actions.py tests/ui/test_main_window.py tests/test_background_runtime_e2e.py -q
.\.venv\Scripts\python.exe -m ruff check src/telegram_downloader/app.py src/telegram_downloader/ui/async_actions.py src/telegram_downloader/ui/main.py tests/test_app.py tests/ui/test_async_actions.py tests/ui/test_main_window.py
```

Expected: PASS；账号切换、托盘后台、单实例恢复和应用关闭语义不变。

- [ ] **Step 5: 提交应用装配**

```powershell
git add src/telegram_downloader/app.py src/telegram_downloader/ui/async_actions.py src/telegram_downloader/ui/main.py tests/test_app.py tests/ui/test_async_actions.py tests/ui/test_main_window.py
git commit -m "feat: wire paged task center lifecycle"
```

## Task 10: 增加端到端大数据响应和隐私基准

**Files:**

- Create: `tests/ui/test_task_center_responsiveness.py`
- Create: `scripts/benchmark_task_center.py`
- Modify: `tests/test_packaging_contract.py`

- [ ] **Step 1: 写入基准隐私合同失败测试**

在 `tests/test_packaging_contract.py` 加入：

```python
def test_task_center_benchmark_is_synthetic_and_private() -> None:
    root = Path(__file__).parents[1]
    source = (root / "scripts/benchmark_task_center.py").read_text(encoding="utf-8").casefold()
    assert "temporarydirectory" in source
    assert "10_000" in source
    assert "50_000" in source
    for forbidden in (
        "telethongateway",
        "secrets.dat",
        "runtime_root(",
        "t.me/",
        "api_hash",
        "catalog.sqlite3",
    ):
        assert forbidden not in source

    controller = (root / "src/telegram_downloader/controller.py").read_text(encoding="utf-8")
    assert ".set_task_summaries(" not in controller
    assert ".set_task_items(" not in controller
```

- [ ] **Step 2: 运行失败合同**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_packaging_contract.py -k "task_center_benchmark" -q
```

Expected: FAIL，脚本不存在。

- [ ] **Step 3: 实现可独立执行的性能基准**

脚本使用离屏 Qt、固定合成摘要和 `TemporaryDirectory` 下的 `TaskRepository`。50,000 条媒体用一条事务 `executemany` 写入固定无隐私记录，再调用正式分页接口。输出：

```text
TASKS=10000
TASK_INITIAL_MEDIAN_MS=12.34
TASK_FILTER_MEDIAN_MS=8.76
TASK_ONE_ROW_MEDIAN_MS=0.45
MEDIA_ITEMS=50000
MEDIA_FIRST_PAGE_MEDIAN_MS=18.90
MAX_EVENT_LOOP_GAP_MS=7.50
MODEL_RESETS_AFTER_INITIAL=0
DIRTY_BATCHES_FOR_500_EVENTS=1
TASK_CENTER_BENCHMARK_OK
```

命令行只允许 `--repeats`、`--repository-delay-ms`、`--max-model-ms`、`--max-gap-ms`；默认 7、50、100、20。任一中位数超过 100 ms、gap 超过 20 ms、reset 非 0 或 dirty batch 非 1 时退出非零。

- [ ] **Step 4: 增加 Qt 慢仓库端到端门禁**

`tests/ui/test_task_center_responsiveness.py` 使用 `create_application(tmp_path)`、5 ms `QTimer` 和每次查询阻塞 50 ms 的合成仓库，验证：

- 初始完整任务读取期间窗口心跳推进。
- 500 个进度事件只更新一个任务行。
- 选择 50,000 媒体任务只显示 500 行。
- 滚动追加到 1,000 行不 reset，并保留任务/媒体选择和顶部锚点。
- 旧代次分页释放后不覆盖新任务。

测试不创建 gateway、不启动登录、不连接网络。

- [ ] **Step 5: 执行内置七轮基准和聚焦测试**

Run:

```powershell
$env:QT_QPA_PLATFORM='offscreen'
.\.venv\Scripts\python.exe -m pytest tests/ui/test_task_center_responsiveness.py tests/test_packaging_contract.py -k "task_center" -q
.\.venv\Scripts\python.exe scripts\benchmark_task_center.py --repeats 7
```

Expected: 测试 PASS；脚本打印七轮原始值、约定中位数和 `TASK_CENTER_BENCHMARK_OK`。

- [ ] **Step 6: 静态检查并提交性能门禁**

Run:

```powershell
.\.venv\Scripts\python.exe -m ruff check scripts/benchmark_task_center.py tests/ui/test_task_center_responsiveness.py tests/test_packaging_contract.py
git diff --check
```

Expected: PASS，无隐私入口或空白错误。

```powershell
git add scripts/benchmark_task_center.py tests/ui/test_task_center_responsiveness.py tests/test_packaging_contract.py
git commit -m "test: gate task center responsiveness"
```

## Task 11: 完整审查、缺陷修复和 v0.18.4 本地候选

**Files:**

- Modify only when a failing regression has a focused reproduction test.
- Modify: `pyproject.toml`
- Modify: `src/telegram_downloader/__init__.py`
- Modify: `installer/TelegramDownloader.iss`
- Modify: `README.md`
- Modify: `tests/test_packaging_contract.py`
- Create: `docs/releases/v0.18.4.md`
- Create: `docs/verification/v0.18.4-task-center-responsiveness.md`

- [ ] **Step 1: 运行任务中心联合回归**

Run:

```powershell
$env:QT_QPA_PLATFORM='offscreen'
.\.venv\Scripts\python.exe -m pytest tests/test_repository.py tests/test_task_center.py tests/test_task_refresh.py tests/test_ui_models.py tests/test_controller.py tests/test_app.py tests/ui/test_main_window.py tests/ui/test_task_center_responsiveness.py -q
```

Expected: PASS，无 RuntimeWarning、未取回 task 异常、pending task 或 Qt 模型告警。

- [ ] **Step 2: 运行完整自动化**

Run:

```powershell
.\scripts\test.ps1
```

Expected: 完整 Pytest 与 Ruff 全部通过。

- [ ] **Step 3: 使用代码审查技能检查实现**

REQUIRED SUB-SKILL: `superpowers:requesting-code-review`。

逐项审查：

- 键集分页混合 DESC/ASC 条件是否无重复、无遗漏。
- 401+ ID 分块是否仍为批量连接而非 N+1。
- 任务模型插入、删除、移动和筛选后 persistent index 是否正确。
- 500 ms deadline 是否不会被新进度延期。
- 5 秒校准与同时到达 dirty IDs 是否不会互相覆盖。
- 旧账号、旧任务和旧媒体代次是否只丢弃结果且取回异常。
- 生产启动、任务选择、分页、可见媒体刷新是否仍有同步 SQLite。
- 索引是否在下载恢复前后台创建，失败重试是否只发生在调度器空闲。
- 账号切换、托盘隐藏、无 runner 关闭和应用退出是否关闭协调器。
- 基准和日志是否不含真实数据入口。

每个有效问题先写聚焦失败测试，再修复并单独验证；无问题时不制造改动。

- [ ] **Step 4: 更新版本合同到 0.18.4 并先确认失败**

把打包合同改为要求 `pyproject.toml`、`__version__`、Inno Setup 三处均为 `0.18.4`，并要求 release notes 含：`10,000`、`50,000`、`150 ms`、`500 ms`、`5 秒`、`键集分页`、`兼容性`、`隐私边界`。

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_packaging_contract.py -k "v0184" -q
```

Expected: FAIL，权威版本仍为 0.18.3，说明文件不存在。

- [ ] **Step 5: 更新版本、README 和发布说明**

把三个权威版本源改为 0.18.4。README 说明任务中心增量刷新、150 ms 筛选防抖、500 条媒体分页、5 秒校准和兼容索引。`docs/releases/v0.18.4.md` 明确：无业务字段迁移、无新权限、无真实数据基准、强制终止不影响下载持久化语义。

建立验证文档框架，只记录已执行事实；尚未构建的制品明确写“未执行”，不预填测试数、耗时或哈希。

- [ ] **Step 6: 运行版本、文档和完整回归**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_packaging_contract.py tests/test_installer_contract.py tests/test_self_test.py -q
.\scripts\test.ps1
git diff --check
```

Expected: PASS；版本一致为 0.18.4，完整自动化和 Ruff 通过。

- [ ] **Step 7: 提交审查修复和本地候选元数据**

若审查产生代码修复，先独立提交：

```powershell
git add src tests scripts
git commit -m "fix: harden task center responsiveness"
```

再提交候选元数据：

```powershell
git add pyproject.toml src/telegram_downloader/__init__.py installer/TelegramDownloader.iss README.md tests/test_packaging_contract.py docs/releases/v0.18.4.md docs/verification/v0.18.4-task-center-responsiveness.md
git commit -m "docs: prepare v0.18.4 local candidate"
```

## Task 12: 按三轮执行最终验收并停在本地候选

**Files:**

- Modify: `docs/verification/v0.18.4-task-center-responsiveness.md`

- [ ] **Step 1: 第一轮——源码、完整测试、静态检查和七次基准**

Run:

```powershell
$env:QT_QPA_PLATFORM='offscreen'
.\.venv\Scripts\python.exe -m pytest tests/test_repository.py tests/test_task_center.py tests/test_task_refresh.py tests/test_controller.py tests/ui/test_main_window.py tests/ui/test_task_center_responsiveness.py -q
.\scripts\test.ps1
.\.venv\Scripts\python.exe scripts\benchmark_task_center.py --repeats 7
git diff --check
```

Expected: 全部 PASS；脚本内七轮任务/媒体中位数不超过 100 ms、Qt gap 不超过 20 ms、初始后 reset 为 0、500 事件 dirty batch 为 1。

- [ ] **Step 2: 第二轮——便携包、隐私和冻结大数据 GUI**

先按绝对路径确认项目内冻结 EXE 未运行，再执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\smoke.ps1
```

Expected: 构建内完整测试、Ruff、`PACKAGED_SMOKE_OK` 通过；生成 `dist/TelegramDownloader-0.18.4-win-x64-portable.zip`。ZIP 禁止出现 `data/`、`downloads/`、数据库、凭据、日志、`.part`、`.corrupt*` 和性能证据中的用户内容。

在工作树内隔离 D 盘目录解压并运行冻结 `--self-test`。随后重新执行 `tests/ui/test_task_center_responsiveness.py` 的固定合成 10,000/50,000 离屏 Qt 冒烟；不向冻结应用注入测试数据库。记录 ZIP/EXE 字节数和 SHA-256。

- [ ] **Step 3: 第三轮——安装器、保留语义和人工 GUI**

Run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build-installer.ps1 -SkipAppBuild
```

Expected: `INSTALLER_SMOKE_OK`；非系统盘安装、同版本原位升级和普通卸载后，合成 `data`、`downloads`、缩略图和哨兵哈希保持，运行文件被移除。

使用隔离安装实例人工检查：主窗口启动可交互、任务筛选、任务切换、媒体滚动入口、选择保持、页面切换、关闭到托盘和单实例恢复。10,000/50,000 数据量、分页失败重试和滚动锚点由本轮合成 Qt 冒烟提供证据，不修改隔离安装实例的业务数据库。通知区域右键若桌面工具无法定位，必须如实记录并用现有 Qt 自动化证据补充，不得写成手工通过。

- [ ] **Step 4: 使用完成前验证技能记录证据**

REQUIRED SUB-SKILL: `superpowers:verification-before-completion`。

把三轮实际命令、通过数量、耗时、七次原始基准、模型信号、查询次数、smoke 标记、隐私条目数、制品大小和 SHA-256 写入验证文档。然后重新运行：

```powershell
git status --short
git diff --check
.\scripts\test.ps1
```

Expected: 只有预期验证文档改动；最终完整测试和 Ruff 再次通过。

- [ ] **Step 5: 提交验证证据并停止**

```powershell
git add docs/verification/v0.18.4-task-center-responsiveness.md
git commit -m "docs: record v0.18.4 task center verification"
git status --short
```

Expected: 工作树干净。向用户报告实施分支、提交、三轮结果、制品路径和 SHA-256，并明确未合并 `main`、未打标签、未推送、未发布在线更新；等待单独授权。
