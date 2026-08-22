# TelegramDownloader 存储空间管理实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不自动触碰正式下载、下载分片和损坏留档的前提下，为维护中心增加白名单空间统计、用户选择启用的空闲自动清理、双确认手动清理和脱敏历史。

**Architecture:** 用只读 `StorageInventoryService`、不可变 `StorageCleanupPlanner` 和逐项重验的 `StorageCleanupExecutor` 分离扫描、决策与删除；由 `StorageMaintenanceService` 和可注入时钟的调度器编排，并通过应用级活动登记器与下载、搜索、订阅、诊断和更新互斥。现有健康诊断被包装进新的维护中心，存储状态使用独立 schema-1 原子 JSON，正式媒体和私人路径永不进入清理历史或通知。

**Tech Stack:** Python 3.12、asyncio、sqlite3、pathlib/os、PySide6、qasync、pytest/pytest-asyncio/pytest-qt、Ruff、PyInstaller、Inno Setup。

---

## 实施前约束

- 基线提交：`e027bb7`（存储维护设计已确认）。
- 在执行阶段先用 `superpowers:using-git-worktrees` 创建 `codex/storage-maintenance-v015` 隔离工作树。
- 每个任务严格执行 RED → GREEN → 回归 → 提交；不得把多个任务压成一个大提交。
- 所有删除测试只能使用 pytest 临时目录，不得读取或删除真实 `data`、`downloads` 或用户目录。
- 本计划生成 v0.15.0 本地候选版，不推送远端、不创建标签、不发布 GitHub/ModelScope Release；正式发布需要最终验收后的明确授权。

## 文件职责映射

### 新建文件

- `src/telegram_downloader/maintenance_activity.py`：活动类型、引用计数令牌、变更等待与连续空闲判定。
- `src/telegram_downloader/storage_models.py`：类别、固定结果代码、清单、计划、执行结果、历史和状态数据结构。
- `src/telegram_downloader/storage_state.py`：schema-1 严格解析、隐私校验、最近 20 次历史和原子保存。
- `src/telegram_downloader/update_protection.py`：把更新日志和活动事务转换成只读保护快照。
- `src/telegram_downloader/storage_inventory.py`：白名单文件遍历、自动类别统计及手动分片/留档分类。
- `src/telegram_downloader/storage_cleanup.py`：保留策略计划、执行前重验、逐文件删除和空目录修剪。
- `src/telegram_downloader/storage_maintenance.py`：扫描/清理编排、确认会话、取消、状态保存和通知决策。
- `src/telegram_downloader/storage_scheduler.py`：启动延迟、连续空闲、周期、忙碌重试和关闭收敛。
- `src/telegram_downloader/ui/storage.py`：存储分类模型、存储页、手动候选模型和双确认对话框。
- `src/telegram_downloader/ui/maintenance.py`：健康检查与存储空间两个真实标签的维护中心容器。
- `tests/test_maintenance_activity.py`
- `tests/test_storage_models.py`
- `tests/test_storage_state.py`
- `tests/test_update_protection.py`
- `tests/test_storage_inventory.py`
- `tests/test_storage_cleanup.py`
- `tests/test_storage_maintenance.py`
- `tests/test_storage_scheduler.py`
- `tests/test_storage_maintenance_e2e.py`
- `tests/ui/test_storage.py`
- `tests/ui/test_maintenance.py`

### 修改文件

- `src/telegram_downloader/settings.py:18-147`：增加固定、安全、默认关闭的嵌套存储设置并兼容旧 JSON。
- `src/telegram_downloader/paths.py:15-114`：增加维护目录和状态文件路径。
- `src/telegram_downloader/repository.py:251-457`：增加按目标路径批量读取维护状态的单查询接口。
- `src/telegram_downloader/update_helper.py:212-244`：复用严格更新日志读取器。
- `src/telegram_downloader/thumbnail_cache.py:14-89`：让即时缓存淘汰与 1 GiB/900 MiB 策略一致。
- `src/telegram_downloader/notifications.py:9-174`：增加维护通知类型、字节聚合和维护中心路由。
- `src/telegram_downloader/scheduler.py:150-430`：下载操作登记活动令牌。
- `src/telegram_downloader/subscription_scheduler.py:35-160`：订阅执行登记活动令牌。
- `src/telegram_downloader/update.py:91-174`：实际更新下载/启动阶段登记活动令牌。
- `src/telegram_downloader/controller.py:430-2100`：高负载前台操作和诊断登记活动令牌，保持异常/取消释放。
- `src/telegram_downloader/ui/diagnostics.py`：只做可嵌入维护中心所需的对象名/尺寸兼容，不改变诊断语义。
- `src/telegram_downloader/ui/main.py:75-180,183-233,1017-1063`：健康诊断导航升级为维护中心并保留兼容别名。
- `src/telegram_downloader/ui/settings.py:35-405`：旧缩略图清理按钮改成前往存储空间的快捷入口。
- `src/telegram_downloader/ui/async_actions.py:15-180`：登记存储操作策略键。
- `src/telegram_downloader/app.py:340-1380`：构造、连接、启动和关闭维护服务。
- `src/telegram_downloader/diagnostic_probes.py:40-65`、`src/telegram_downloader/app.py:231-281`：把维护路径纳入项目内写入保护和 `run_self_test`。
- `tests/test_settings.py`、`tests/test_paths.py`、`tests/test_repository.py`、`tests/test_thumbnail_cache.py`、`tests/test_notifications.py`、`tests/test_scheduler.py`、`tests/test_subscription_scheduler.py`、`tests/update/test_update_coordinator.py`、`tests/test_controller.py`、`tests/test_app.py`、`tests/test_self_test.py`、`tests/ui/test_main_window.py`、`tests/ui/test_settings_dialog.py`：扩展现有契约。
- `README.md`、`docs/releases/v0.15.0.md`、`docs/verification/v0.15.0-storage-maintenance.md`：用户说明、候选版说明和三轮验收证据。
- `pyproject.toml`、`src/telegram_downloader/__init__.py`、`installer/TelegramDownloader.iss`、`scripts/build.ps1`、`tests/test_packaging_contract.py`：v0.15.0 一致性与打包隐私契约。

## Task 1：固定配置与项目内路径

**Files:**
- Modify: `src/telegram_downloader/settings.py:18-147`
- Modify: `src/telegram_downloader/paths.py:15-114`
- Test: `tests/test_settings.py`
- Test: `tests/test_paths.py`

- [ ] **Step 1：先写设置默认值、旧 JSON 和路径失败测试**

在 `tests/test_settings.py` 增加：

```python
from telegram_downloader.settings import StorageMaintenanceSettings


def test_storage_maintenance_defaults_are_fixed_and_opt_in() -> None:
    value = StorageMaintenanceSettings()
    assert value.automatic_enabled is False
    assert value.temp_retention_days == 7
    assert value.log_retention_days == 30
    assert value.thumbnail_limit_bytes == 1024**3
    assert value.thumbnail_target_bytes == 900 * 1024**2
    assert value.update_staging_retention_days == 7
    assert value.update_backup_keep_count == 1
    assert value.check_interval_seconds == 86400
    assert value.startup_delay_seconds == 300
    assert value.idle_required_seconds == 60
    assert value.busy_retry_seconds == 900


def test_old_settings_default_storage_maintenance_to_disabled(tmp_path) -> None:
    path = tmp_path / "settings.json"
    path.write_text('{"api_id": 7, "concurrency": 2}', encoding="utf-8")
    loaded = SettingsStore(path).load()
    assert loaded.api_id == 7
    assert loaded.concurrency == 2
    assert loaded.storage_maintenance == StorageMaintenanceSettings()
```

在 `tests/test_paths.py::test_ensure_layout_creates_every_managed_directory` 增加：

```python
assert paths.maintenance == tmp_path / "data" / "maintenance"
assert paths.maintenance.is_dir()
assert paths.storage_maintenance_state == (
    tmp_path / "data" / "maintenance" / "storage-state.json"
)
```

- [ ] **Step 2：运行测试并确认 RED**

Run: `.venv\Scripts\python.exe -m pytest tests/test_settings.py tests/test_paths.py -q`

Expected: collection fails because `StorageMaintenanceSettings` and the two `PortablePaths` properties do not exist.

- [ ] **Step 3：实现固定嵌套设置与路径**

在 `ProxySettings` 前增加：

```python
@dataclass(frozen=True, slots=True)
class StorageMaintenanceSettings:
    automatic_enabled: bool = False
    temp_retention_days: int = 7
    log_retention_days: int = 30
    thumbnail_limit_bytes: int = 1024**3
    thumbnail_target_bytes: int = 900 * 1024**2
    update_staging_retention_days: int = 7
    update_backup_keep_count: int = 1
    check_interval_seconds: int = 86400
    startup_delay_seconds: int = 300
    idle_required_seconds: int = 60
    busy_retry_seconds: int = 900

    def __post_init__(self) -> None:
        if not isinstance(self.automatic_enabled, bool):
            raise SettingsError("自动清理开关必须是布尔值")
        expected = {
            "temp_retention_days": 7,
            "log_retention_days": 30,
            "thumbnail_limit_bytes": 1024**3,
            "thumbnail_target_bytes": 900 * 1024**2,
            "update_staging_retention_days": 7,
            "update_backup_keep_count": 1,
            "check_interval_seconds": 86400,
            "startup_delay_seconds": 300,
            "idle_required_seconds": 60,
            "busy_retry_seconds": 900,
        }
        if any(getattr(self, name) != value for name, value in expected.items()):
            raise SettingsError("存储维护策略不是受支持的固定策略")
```

给 `AppSettings` 增加：

```python
storage_maintenance: StorageMaintenanceSettings = StorageMaintenanceSettings()
```

在 `AppSettings.__post_init__` 增加严格类型检查；在 `SettingsStore.load` 中读取 `storage_maintenance` 对象并构造 `StorageMaintenanceSettings(**maintenance_raw)`。在 `PortablePaths` 增加：

```python
@property
def maintenance(self) -> Path:
    return self.data / "maintenance"

@property
def storage_maintenance_state(self) -> Path:
    return self.maintenance / "storage-state.json"
```

并把 `self.maintenance` 加入 `ensure_layout()` 的目录集合。

- [ ] **Step 4：运行设置和路径回归**

Run: `.venv\Scripts\python.exe -m pytest tests/test_settings.py tests/test_paths.py -q`

Expected: all tests pass.

- [ ] **Step 5：提交配置合同**

```powershell
git add src/telegram_downloader/settings.py src/telegram_downloader/paths.py tests/test_settings.py tests/test_paths.py
git commit -m "feat: define safe storage maintenance settings"
```

## Task 2：领域模型与脱敏状态文件

**Files:**
- Create: `src/telegram_downloader/storage_models.py`
- Create: `src/telegram_downloader/storage_state.py`
- Create: `tests/test_storage_models.py`
- Create: `tests/test_storage_state.py`

- [ ] **Step 1：写不可变模型和隐私状态 RED 测试**

测试必须构造七种固定类别、不可变计划、执行结果和 21 条历史，并断言历史只保留最后 20 条。关键断言：

```python
def test_state_history_is_bounded_and_contains_no_paths(tmp_path) -> None:
    store = StorageStateStore(tmp_path / "storage-state.json")
    history = tuple(
        StorageRunHistory(
            occurred_at=datetime(2026, 8, 22, 0, index, tzinfo=UTC),
            trigger=StorageTrigger.MANUAL_SAFE,
            result_code=StorageResultCode.COMPLETED,
            deleted_count=index,
            skipped_count=0,
            failed_count=0,
            cancelled_count=0,
            released_bytes=index * 100,
            categories=(
                StorageCategoryCount(
                    category=StorageCategory.TEMP,
                    deleted_count=index,
                    skipped_count=0,
                    failed_count=0,
                    cancelled_count=0,
                    released_bytes=index * 100,
                ),
            ),
        )
        for index in range(21)
    )
    store.save(StorageMaintenanceState(history=history))
    loaded = store.load()
    assert len(loaded.history) == 20
    payload = store.path.read_text(encoding="utf-8")
    assert "downloads" not in payload
    assert "private.mp4" not in payload
```

另写无效 schema、额外字段、重复 JSON 字段、非 UTC 时间、负数字节和未知结果代码拒绝测试。

- [ ] **Step 2：运行模型/状态测试并确认 RED**

Run: `.venv\Scripts\python.exe -m pytest tests/test_storage_models.py tests/test_storage_state.py -q`

Expected: collection fails because both modules are missing.

- [ ] **Step 3：实现固定枚举与不可变数据结构**

`storage_models.py` 固定定义：

```python
class StorageCategory(StrEnum):
    THUMBNAILS = "thumbnails"
    TEMP = "temp"
    ROTATED_LOGS = "rotated-logs"
    UPDATE_STAGING = "update-staging"
    UPDATE_BACKUP = "update-backup"
    DOWNLOAD_PART = "download-part"
    CORRUPT_ARCHIVE = "corrupt-archive"


class StorageTrigger(StrEnum):
    AUTOMATIC = "automatic"
    MANUAL_SAFE = "manual-safe"
    MANUAL_DOWNLOAD = "manual-download"


class StorageResultCode(StrEnum):
    COMPLETED = "completed"
    NOTHING_TO_CLEAN = "nothing-to-clean"
    CANCELLED = "cancelled"
    BUSY_DEFERRED = "busy-deferred"
    FILE_IN_USE = "file-in-use"
    PERMISSION_DENIED = "permission-denied"
    STATE_CHANGED = "state-changed"
    UNSAFE_PATH = "unsafe-path"
    PROTECTED_BY_TASK = "protected-by-task"
    PROTECTED_BY_UPDATE = "protected-by-update"
    STATE_SAVE_FAILED = "state-save-failed"
    LOCAL_ERROR = "local-error"


@dataclass(frozen=True, slots=True)
class StorageEntry:
    id: str
    relative_path: PurePosixPath
    category: StorageCategory
    size: int
    mtime_ns: int
    selectable: bool
    reason: StorageResultCode | None = None
    task_id: str | None = None
    display_name: str | None = None


@dataclass(frozen=True, slots=True)
class StorageCleanupPlan:
    id: str
    generated_at: datetime
    trigger: StorageTrigger
    entries: tuple[StorageEntry, ...]

    @property
    def expected_bytes(self) -> int:
        return sum(entry.size for entry in self.entries)


@dataclass(frozen=True, slots=True)
class StorageCategorySummary:
    category: StorageCategory
    scanned_at: datetime
    total_count: int
    total_bytes: int
    reclaimable_count: int
    reclaimable_bytes: int


@dataclass(frozen=True, slots=True)
class StoragePolicy:
    temp_retention_days: int = 7
    log_retention_days: int = 30
    thumbnail_limit_bytes: int = 1024**3
    thumbnail_target_bytes: int = 900 * 1024**2
    update_staging_retention_days: int = 7
    update_backup_keep_count: int = 1


@dataclass(frozen=True, slots=True)
class StorageInventory:
    scanned_at: datetime
    disk_free_bytes: int
    entries: tuple[StorageEntry, ...]
    summaries: tuple[StorageCategorySummary, ...]


@dataclass(frozen=True, slots=True)
class StorageExecutionItem:
    entry_id: str
    category: StorageCategory
    code: StorageResultCode
    released_bytes: int


@dataclass(frozen=True, slots=True)
class StorageExecutionResult:
    plan_id: str
    trigger: StorageTrigger
    started_at: datetime
    completed_at: datetime
    result_code: StorageResultCode
    items: tuple[StorageExecutionItem, ...]

    @property
    def released_bytes(self) -> int:
        return sum(item.released_bytes for item in self.items)

    @property
    def deleted_count(self) -> int:
        return sum(item.code is StorageResultCode.COMPLETED for item in self.items)

    @property
    def failed_count(self) -> int:
        failures = {
            StorageResultCode.FILE_IN_USE,
            StorageResultCode.PERMISSION_DENIED,
            StorageResultCode.LOCAL_ERROR,
        }
        return sum(item.code in failures for item in self.items)

    @property
    def cancelled_count(self) -> int:
        return sum(item.code is StorageResultCode.CANCELLED for item in self.items)

    @property
    def skipped_count(self) -> int:
        return len(self.items) - self.deleted_count - self.failed_count - self.cancelled_count


@dataclass(frozen=True, slots=True)
class StorageCategoryCount:
    category: StorageCategory
    deleted_count: int
    skipped_count: int
    failed_count: int
    cancelled_count: int
    released_bytes: int


@dataclass(frozen=True, slots=True)
class StorageRunHistory:
    occurred_at: datetime
    trigger: StorageTrigger
    categories: tuple[StorageCategoryCount, ...]
    deleted_count: int
    skipped_count: int
    failed_count: int
    cancelled_count: int
    released_bytes: int
    result_code: StorageResultCode


@dataclass(frozen=True, slots=True)
class StorageMaintenanceState:
    schema_version: int = 1
    last_scan_at: datetime | None = None
    last_automatic_check_at: datetime | None = None
    last_cleanup_at: datetime | None = None
    next_due_at: datetime | None = None
    summaries: tuple[StorageCategorySummary, ...] = ()
    history: tuple[StorageRunHistory, ...] = ()
```

在各 `__post_init__` 中拒绝负数量、非 UTC 时间、重复条目 ID、重复分类、`schema_version != 1`、`thumbnail_target_bytes > thumbnail_limit_bytes` 和路径字符串出现在历史结构中。`StorageEntry.relative_path` 必须非空、非绝对、不含 `.`/`..` 段或反斜杠；`selectable=True` 时 reason 必须为空，受保护条目必须有固定 reason。`StoragePolicy.from_settings(settings)` 复制固定策略值，测试可直接构造较小但仍满足正数与 target ≤ limit 的策略。`StorageStateStore.save()` 在序列化前使用 `dataclasses.replace(state, history=state.history[-20:])` 截断历史；状态 JSON 只写上述聚合字段，绝不序列化 `StorageEntry` 或 `StorageExecutionItem.entry_id`。

- [ ] **Step 4：实现 schema-1 严格状态存储**

`StorageStateStore` 必须先把文件读入 `text`，再调用 `json.loads(text, object_pairs_hook=_pairs_without_duplicates)`，验证精确字段集合，并使用：

```python
def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        with temporary.open("wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
```

`load()` 对不存在文件返回空 schema-1 状态；损坏或未知 schema 抛出固定 `StorageStateError("存储维护记录不可用")`，不得把 JSON 异常文本向上传递。

- [ ] **Step 5：运行测试并提交**

Run: `.venv\Scripts\python.exe -m pytest tests/test_storage_models.py tests/test_storage_state.py -q`

Expected: all tests pass.

```powershell
git add src/telegram_downloader/storage_models.py src/telegram_downloader/storage_state.py tests/test_storage_models.py tests/test_storage_state.py
git commit -m "feat: persist private storage maintenance state"
```

## Task 3：应用活动登记与连续空闲

**Files:**
- Create: `src/telegram_downloader/maintenance_activity.py`
- Create: `tests/test_maintenance_activity.py`

- [ ] **Step 1：写引用计数、异常释放和连续空闲 RED 测试**

```python
class FakeMonotonicClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.timeouts: list[tuple[float, asyncio.Future[None]]] = []

    def __call__(self) -> float:
        return self.now

    async def wait_for(self, awaitable, timeout: float):
        task = asyncio.ensure_future(awaitable)
        marker = asyncio.get_running_loop().create_future()
        self.timeouts.append((self.now + timeout, marker))
        done, _pending = await asyncio.wait(
            (task, marker),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if marker in done:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise TimeoutError
        marker.cancel()
        return task.result()

    async def advance(self, seconds: float) -> None:
        self.now += seconds
        for deadline, marker in tuple(self.timeouts):
            if deadline <= self.now and not marker.done():
                marker.set_result(None)
        self.timeouts = [
            (deadline, marker)
            for deadline, marker in self.timeouts
            if not marker.done()
        ]
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_activity_tokens_are_reference_counted_and_release_on_error() -> None:
    registry = OperationActivityRegistry()
    with pytest.raises(RuntimeError):
        with registry.track(ActivityKind.SEARCH):
            with registry.track(ActivityKind.SEARCH):
                assert registry.active_count == 2
                raise RuntimeError("stop")
    assert registry.active_count == 0
    assert registry.is_idle is True


@pytest.mark.asyncio
async def test_wait_for_continuous_idle_restarts_after_activity(monkeypatch) -> None:
    clock = FakeMonotonicClock()
    monkeypatch.setattr(asyncio, "wait_for", clock.wait_for)
    registry = OperationActivityRegistry(clock=clock)
    waiter = asyncio.create_task(registry.wait_for_continuous_idle(60))
    await clock.advance(30)
    with registry.track(ActivityKind.DOWNLOAD):
        await clock.advance(1)
    await clock.advance(59)
    assert waiter.done() is False
    await clock.advance(1)
    assert await waiter is True


def test_maintenance_token_is_exclusive_and_business_has_priority() -> None:
    registry = OperationActivityRegistry()
    maintenance = registry.try_track_maintenance(ActivityKind.STORAGE_CLEANUP)
    assert maintenance is not None
    with pytest.raises(MaintenanceBusyError):
        registry.track(ActivityKind.DOWNLOAD)
    with pytest.raises(RuntimeError, match="未释放"):
        registry.close()
    maintenance.release()

    with registry.track(ActivityKind.SEARCH):
        assert registry.try_track_maintenance(ActivityKind.STORAGE_SCAN) is None
```

- [ ] **Step 2：运行并确认模块缺失**

Run: `.venv\Scripts\python.exe -m pytest tests/test_maintenance_activity.py -q`

Expected: FAIL during import.

- [ ] **Step 3：实现活动登记器**

定义固定类型：`DOWNLOAD`、`SCAN`、`SEARCH`、`SUBSCRIPTION`、`INTEGRITY`、`DIAGNOSTICS`、`UPDATE`、`STORAGE_SCAN`、`STORAGE_CLEANUP`。普通业务使用 `track()`；维护服务只能使用 `try_track_maintenance()`。维护令牌仅在当前计数为零时原子取得；持有维护令牌期间，新的普通业务得到固定 `MaintenanceBusyError("存储维护正在收尾，请稍后重试")`，从而保证两类操作不重叠。普通业务已经活动时维护返回 `None`，业务优先。每次 begin/release 增加 generation 并 `Event.set()`。`wait_for_continuous_idle(seconds)` 使用 generation + 可注入单调时钟，在任何新活动发生时重新计算起点，关闭事件返回 `False`。

核心令牌和计数实现固定为：

```python
class MaintenanceBusyError(RuntimeError):
    """Raised when a business action reaches an active maintenance window."""


@dataclass(slots=True)
class ActivityToken:
    registry: "OperationActivityRegistry"
    kind: ActivityKind
    released: bool = False

    def __enter__(self) -> "ActivityToken":
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        self.release()

    def release(self) -> None:
        if not self.released:
            self.released = True
            self.registry._release(self.kind)


class OperationActivityRegistry:
    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self._counts: Counter[ActivityKind] = Counter()
        self._generation = 0
        self._changed = asyncio.Event()
        self._closed = False

    @property
    def active_count(self) -> int:
        return sum(self._counts.values())

    @property
    def is_idle(self) -> bool:
        return self.active_count == 0

    def active(self, kind: ActivityKind) -> int:
        return self._counts[kind]

    def track(self, kind: ActivityKind) -> ActivityToken:
        if self._closed:
            raise RuntimeError("活动登记器已经关闭")
        if kind in {ActivityKind.STORAGE_SCAN, ActivityKind.STORAGE_CLEANUP}:
            raise ValueError("维护活动必须取得独占令牌")
        if self.active(ActivityKind.STORAGE_SCAN) or self.active(
            ActivityKind.STORAGE_CLEANUP
        ):
            raise MaintenanceBusyError("存储维护正在收尾，请稍后重试")
        return self._begin(kind)

    def try_track_maintenance(self, kind: ActivityKind) -> ActivityToken | None:
        if self._closed:
            return None
        if kind not in {ActivityKind.STORAGE_SCAN, ActivityKind.STORAGE_CLEANUP}:
            raise ValueError("不是维护活动类型")
        if self.active_count:
            return None
        return self._begin(kind)

    def _begin(self, kind: ActivityKind) -> ActivityToken:
        self._counts[kind] += 1
        self._notify()
        return ActivityToken(self, kind)

    def _release(self, kind: ActivityKind) -> None:
        if self._counts[kind] <= 0:
            raise RuntimeError("活动令牌重复释放")
        self._counts[kind] -= 1
        if self._counts[kind] == 0:
            del self._counts[kind]
        self._notify()

    def _notify(self) -> None:
        event = self._changed
        self._changed = asyncio.Event()
        self._generation += 1
        event.set()

    def close(self) -> None:
        if self.active_count:
            raise RuntimeError("仍有未释放的活动令牌")
        self._closed = True
        self._notify()

    async def wait_for_change(self, generation: int, timeout: float) -> int:
        if generation != self._generation or self._closed:
            return self._generation
        event = self._changed
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except TimeoutError:
            return self._generation
        return self._generation

    async def wait_for_continuous_idle(self, seconds: float) -> bool:
        if seconds <= 0:
            raise ValueError("连续空闲时间必须大于零")
        idle_since: float | None = None
        while not self._closed:
            if self.is_idle:
                if idle_since is None:
                    idle_since = self.clock()
                remaining = seconds - (self.clock() - idle_since)
                if remaining <= 0:
                    return True
            else:
                idle_since = None
                remaining = 3600.0
            generation = self._generation
            await self.wait_for_change(generation, remaining)
        return False
```

测试通过 monkeypatch `asyncio.wait_for` 驱动假时钟，不发生真实等待。业务入口捕获 `MaintenanceBusyError` 并显示固定可重试提示，不把它记录成下载、搜索或订阅失败；维护入口把 `None` 映射为 `BUSY_DEFERRED`。

- [ ] **Step 4：运行并提交**

Run: `.venv\Scripts\python.exe -m pytest tests/test_maintenance_activity.py -q`

Expected: all tests pass without real sleeps.

```powershell
git add src/telegram_downloader/maintenance_activity.py tests/test_maintenance_activity.py
git commit -m "feat: track maintenance-safe application activity"
```

## Task 4：任务批量保护与更新保护快照

**Files:**
- Modify: `src/telegram_downloader/repository.py:251-457`
- Create: `src/telegram_downloader/update_protection.py`
- Modify: `src/telegram_downloader/update_helper.py:212-244`
- Test: `tests/test_repository.py`
- Create: `tests/test_update_protection.py`
- Test: `tests/update/test_update_transaction.py`

- [ ] **Step 1：写单查询任务保护和更新 fail-closed 测试**

仓库测试创建完成、暂停、失败和已验证媒体，调用一次 `maintenance_media_by_targets`，断言返回规范目标路径、任务标题、媒体状态和完整性状态。用 sqlite trace callback 断言批量调用只有一个媒体 JOIN 查询。

更新保护测试覆盖：无日志返回空保护；有效日志保护 backup/extraction；损坏日志返回 `fail_closed=True` 并保护整个 staging/backup；绝对/越界路径拒绝。

- [ ] **Step 2：运行并确认 RED**

Run: `.venv\Scripts\python.exe -m pytest tests/test_repository.py tests/test_update_protection.py tests/update/test_update_transaction.py -q`

Expected: new API and module are missing.

- [ ] **Step 3：增加维护媒体批量 DTO 和查询**

在 `repository.py` 定义：

```python
@dataclass(frozen=True, slots=True)
class MaintenanceMediaRecord:
    item_id: str
    task_id: str
    task_title: str
    target_path: Path
    task_status: TaskStatus
    item_status: ItemStatus
    integrity_status: IntegrityStatus
```

增加 `maintenance_media_by_targets(self, targets: Sequence[Path]) -> dict[Path, MaintenanceMediaRecord]`。去重后按每批最多 400 个路径构造 `marks = ",".join("?" for _ in batch)`，再执行：

```sql
SELECT media_items.id, media_items.task_id,
       COALESCE(tasks.display_title, tasks.source_title) AS task_title,
       media_items.target_path, tasks.status AS task_status,
       media_items.status AS item_status, media_items.integrity_status
FROM media_items
JOIN tasks ON tasks.id = media_items.task_id
WHERE media_items.target_path IN ({marks})
```

结果 key 使用 `Path(row["target_path"]).resolve()`；空输入不得连接数据库。

- [ ] **Step 4：实现更新保护提供器并让更新助手复用解析**

`UpdateProtectionSnapshot` 包含 `protected: frozenset[Path]` 与 `fail_closed: bool`，`protects(path)` 在 path 等于或位于任一受保护目录下时返回 True。`UpdateProtectionProvider.snapshot()`：

- 日志不存在：空集合、`False`。
- 严格读取成功：保护日志中的 backup、extraction、journal 本身及 staging 内同 transaction ID 的健康标记。
- 任意读取/结构/路径错误：保护 `update_staging`、`update_backup` 和 `update_journal`，返回 `fail_closed=True`。

把更新日志精确字段校验抽成 `load_update_journal(paths)`，`UpdateTransaction._read_journal()` 直接调用它，确保清理器和更新器没有两套解析规则。

- [ ] **Step 5：回归并提交**

Run: `.venv\Scripts\python.exe -m pytest tests/test_repository.py tests/test_update_protection.py tests/update/test_update_transaction.py -q`

Expected: all tests pass.

```powershell
git add src/telegram_downloader/repository.py src/telegram_downloader/update_protection.py src/telegram_downloader/update_helper.py tests/test_repository.py tests/test_update_protection.py tests/update/test_update_transaction.py
git commit -m "feat: expose maintenance protection snapshots"
```

## Task 5：自动白名单清单

**Files:**
- Create: `src/telegram_downloader/storage_inventory.py`
- Create: `tests/test_storage_inventory.py`
- Modify: `src/telegram_downloader/thumbnail_cache.py:14-89`
- Test: `tests/test_thumbnail_cache.py`

- [ ] **Step 1：写自动类别、边界和重解析点 RED 测试**

测试用可注入 `StoragePolicy` 将缩略图阈值设为 100、目标设为 90，创建固定 mtime 文件并断言：

- 99 字节不清理；101 字节按最旧顺序选择到不高于 90。
- 7/30 天边界保留，严格更旧才候选，未来 mtime 保留。
- 只识别 `app.log.N`，当前 `app.log` 和未知日志保护。
- 活动更新路径与诊断临时路径保护。
- 文件/目录符号链接不跟随；Windows 下目录联接标记为不安全。
- 相同 mtime 用相对路径确定排序。

- [ ] **Step 2：运行并确认 RED**

Run: `.venv\Scripts\python.exe -m pytest tests/test_storage_inventory.py tests/test_thumbnail_cache.py -q`

Expected: inventory module is missing and old thumbnail cache stops at 512 MiB rather than the new policy.

- [ ] **Step 3：实现不跟随链接的白名单遍历**

使用 `os.scandir`、`DirEntry.stat(follow_symlinks=False)` 和 Windows `FILE_ATTRIBUTE_REPARSE_POINT = 0x400`。遍历函数只接受预定义类别根目录，遇到 symlink/reparse point 生成不可选择的安全结果，不递归进入。普通文件条目 ID 使用：

```python
def storage_entry_id(category: StorageCategory, relative: PurePosixPath) -> str:
    payload = f"{category.value}\0{relative.as_posix()}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
```

`scan_automatic(now, update_snapshot, active_paths)` 同时记录 `shutil.disk_usage(paths.root).free`，并按以下固定矩阵返回前五类汇总与条目；它不调用任何 `downloads` 遍历：

| 类别 | 根与识别规则 | `selectable=True` 条件 |
|---|---|---|
| `THUMBNAILS` | `paths.thumbnail_cache` 下普通文件 | 总量严格超过 limit 后，按 `(mtime_ns, relative_path)` 从旧到新选到预计剩余量不高于 target |
| `TEMP` | `paths.temp` 下普通文件，但整棵 `paths.diagnostic_temp` 永久排除 | `mtime_ns` 严格早于 `now - 7 days`，不在 `active_paths` |
| `ROTATED_LOGS` | 只接受 `paths.log.parent / app.log.N`，其中 N 为正整数 | 严格早于 `now - 30 days`；`app.log` 与其他名称不创建条目 |
| `UPDATE_STAGING` | `paths.update_staging` 下普通文件 | 严格早于 `now - 7 days`，且 `update_snapshot.protects(path)` 为 False |
| `UPDATE_BACKUP` | 顶层目录名以 8 位小写十六进制事务后缀结尾；目录内 `runtime-manifest.json` 可由 `read_installed_inventory(directory)` 严格读取，且所有普通文件都属于该 manifest | 在全部结构有效且未受保护的备份中，按 `(directory_mtime_ns, name)` 保留最新 1 个；仅较旧目录的已验证普通文件可选 |

未来时间、无法 stat、未知日志、无效备份、链接/重解析点和 `update_snapshot.fail_closed=True` 覆盖的更新根都保留并以固定保护代码计入扫描状态，不进入自动计划。分类汇总分别记录总文件数/字节和可释放文件数/字节；缺少某个类别根视为空，不创建目录。

- [ ] **Step 4：统一缩略图即时淘汰水位**

把 `ThumbnailCache` 默认 `max_total_bytes` 攏为 `1024**3`，新增 `target_total_bytes=900 * 1024**2`，校验 `0 < target <= max`；`_prune()` 超过上限时删到 `target_total_bytes`。继续用 `get()` 的 `os.utime` 表示最近访问，排序键保持 `(mtime_ns, name)`。

- [ ] **Step 5：回归并提交**

Run: `.venv\Scripts\python.exe -m pytest tests/test_storage_inventory.py tests/test_thumbnail_cache.py -q`

Expected: all tests pass.

```powershell
git add src/telegram_downloader/storage_inventory.py src/telegram_downloader/thumbnail_cache.py tests/test_storage_inventory.py tests/test_thumbnail_cache.py
git commit -m "feat: inventory automatic storage categories"
```

## Task 6：手动分片与损坏留档清单

**Files:**
- Modify: `src/telegram_downloader/storage_inventory.py`
- Modify: `tests/test_storage_inventory.py`
- Create: `tests/test_storage_maintenance_e2e.py`

- [ ] **Step 1：写任务关联和保护 RED 测试**

为 `.part`、`.part.corrupt`、`.corrupt`、`.corrupt.2`、普通 `.part` 用户文件和正式媒体构造真实仓库。断言：

```python
assert by_name["done.mp4.part"].selectable is True
assert by_name["paused.mp4.part"].selectable is False
assert by_name["retry.mp4.part"].reason is StorageResultCode.PROTECTED_BY_TASK
assert by_name["repair.mp4.corrupt"].selectable is False
assert by_name["verified.mp4.corrupt.2"].selectable is True
assert by_name["unknown.bin.part"].selectable is False
assert "verified.mp4" not in by_name
```

可选择残留分片必须同时满足媒体 `COMPLETED`、完整性 `VERIFIED`、正式文件存在，并且 service 已取得全局 `STORAGE_SCAN` 独占令牌；表格仍显示关联任务与媒体两种状态。损坏留档使用同一条件，任何无法关联仓库记录的文件都不可选择。

- [ ] **Step 2：运行并确认 RED**

Run: `.venv\Scripts\python.exe -m pytest tests/test_storage_inventory.py tests/test_storage_maintenance_e2e.py -q`

Expected: manual scan API is missing.

- [ ] **Step 3：实现按需 downloads 扫描与单批仓库查询**

增加 `scan_download_candidates(repository, active_paths, progress, cancelled)`：

- 先只枚举匹配 `\.part$` 或 `\.corrupt(?:\.\d+)?$` 的普通文件。
- 从候选名推导正式目标：先移除 corrupt 后缀，再移除可选 part 后缀。
- 一次把所有推导目标传给 `maintenance_media_by_targets`。
- 无记录、正式目标缺失、媒体非完成、非 VERIFIED 或活动路径均返回不可选择条目和固定 `PROTECTED_BY_TASK` 代码。
- 每 256 个文件回传一次扫描进度并检查取消；不在扫描线程触碰 Qt 对象。

- [ ] **Step 4：运行 E2E 并提交**

Run: `.venv\Scripts\python.exe -m pytest tests/test_storage_inventory.py tests/test_storage_maintenance_e2e.py -q`

Expected: all tests pass and query-count assertion proves no N+1.

```powershell
git add src/telegram_downloader/storage_inventory.py tests/test_storage_inventory.py tests/test_storage_maintenance_e2e.py
git commit -m "feat: classify protected download leftovers"
```

## Task 7：计划器、执行前重验与逐项删除

**Files:**
- Create: `src/telegram_downloader/storage_cleanup.py`
- Create: `tests/test_storage_cleanup.py`
- Modify: `tests/test_storage_maintenance_e2e.py`

- [ ] **Step 1：写计划确定性和 TOCTOU RED 测试**

覆盖自动/手动计划隔离、重复 ID 拒绝、大小变化、mtime 变化、普通文件变链接、任务状态变化、更新保护变化、占用、权限不足、取消、部分成功和未知父目录文件。关键测试：

```python
def test_executor_skips_file_changed_after_plan(tmp_path) -> None:
    entry, plan, executor = prepared_safe_file(tmp_path, content=b"old")
    target = tmp_path / entry.relative_path
    target.write_bytes(b"changed")
    result = executor.execute(plan)
    assert target.read_bytes() == b"changed"
    assert result.items[0].code is StorageResultCode.STATE_CHANGED


def test_executor_never_removes_parent_with_unknown_file(tmp_path) -> None:
    entry, plan, executor = prepared_safe_file(tmp_path, content=b"old")
    parent = (tmp_path / entry.relative_path).parent
    (parent / "unknown.keep").write_bytes(b"keep")
    result = executor.execute(plan)
    assert result.deleted_count == 1
    assert (parent / "unknown.keep").is_file()
    assert parent.is_dir()
```

- [ ] **Step 2：运行并确认 RED**

Run: `.venv\Scripts\python.exe -m pytest tests/test_storage_cleanup.py tests/test_storage_maintenance_e2e.py -q`

Expected: cleanup module is missing.

- [ ] **Step 3：实现计划器**

`StorageCleanupPlanner.automatic(inventory, now)` 只接受前五类中 `selectable=True` 条目；`manual_download(inventory, selected_ids, now)` 要求 ID 集合非空、无重复、全部存在、属于后两类且 `selectable=True`，任一不满足就拒绝整个请求，不能静默缩小用户选择。计划 ID 用随机 UUID，条目按 `(category.value, relative_path.as_posix())` 固定排序，条目 tuple 与扫描对象分离。

- [ ] **Step 4：实现逐项重验执行器**

执行器依次：

1. 重新把 `root / relative_path` 交给 `PortablePaths.guard`。
2. 逐级拒绝 symlink/reparse point。
3. `lstat` 要求普通文件、大小和 `mtime_ns` 与计划一致。
4. 手动类别重新获取任务保护；更新类别重新获取更新保护。
5. 只用 `Path.unlink()` 永久删除，不调用回收站 API；`PermissionError` 映射 `PERMISSION_DENIED`，Windows sharing violation/文件占用映射 `FILE_IN_USE`，其他 `OSError` 映射 `LOCAL_ERROR`，不存在或任一 metadata 变化映射 `STATE_CHANGED`。
6. 只向类别根回溯 `rmdir()` 空目录，遇到非空或根目录立即停止。

取消回调在每个条目前检查；取消后剩余条目标为 `CANCELLED`，已经删除的条目保持成功。aggregate code 优先级固定为：空计划 `NOTHING_TO_CLEAN`；任一取消 `CANCELLED`；否则任一逐项失败 `LOCAL_ERROR`；其余 `COMPLETED`。具体原因保留在各 `StorageExecutionItem.code`，service 保存失败再把 aggregate code 覆盖为 `STATE_SAVE_FAILED`。

- [ ] **Step 5：运行路径安全回归并提交**

Run: `.venv\Scripts\python.exe -m pytest tests/test_storage_cleanup.py tests/test_storage_maintenance_e2e.py tests/test_paths.py -q`

Expected: all tests pass.

```powershell
git add src/telegram_downloader/storage_cleanup.py tests/test_storage_cleanup.py tests/test_storage_maintenance_e2e.py
git commit -m "feat: execute revalidated storage cleanup plans"
```

## Task 8：维护服务、确认会话与调度器

**Files:**
- Create: `src/telegram_downloader/storage_maintenance.py`
- Create: `src/telegram_downloader/storage_scheduler.py`
- Create: `tests/test_storage_maintenance.py`
- Create: `tests/test_storage_scheduler.py`
- Modify: `tests/test_storage_maintenance_e2e.py`

- [ ] **Step 1：写服务互斥、状态保存和假时钟调度 RED 测试**

测试要求：

- 重复扫描共享同一任务或得到固定 busy 结果，不能并发遍历。
- “立即清理安全项目”先返回分类数量和预计字节，只在一次界面确认后执行；确认 ID 过期或消费后不可复用。
- 手动执行必须使用服务刚生成的确认 ID；未知、过期或已消费确认 ID 拒绝。
- 清理成功但状态保存失败时返回 `STATE_SAVE_FAILED`，已删除文件不伪回滚。
- 历史只记录聚合字段。
- executor 抛出含绝对路径/文件名/秘密的底层异常时，caplog、页面错误、历史和通知都只出现类别、固定代码与聚合数量。
- 自动关闭不扫描；开启后 300 秒、60 秒连续空闲、86400 秒周期和 busy 后 900 秒重试准确。
- 关闭自动清理只阻止下一周期，不取消已经进入逐项执行的本轮；取消按钮仍可停止剩余条目。
- 状态文件损坏或 schema 未知时自动周期发布固定失败结果但不扫描/删除；用户主动重新扫描成功后用新 schema-1 状态替换损坏文件。
- 关闭取消等待和剩余执行。

- [ ] **Step 2：运行并确认 RED**

Run: `.venv\Scripts\python.exe -m pytest tests/test_storage_maintenance.py tests/test_storage_scheduler.py tests/test_storage_maintenance_e2e.py -q`

Expected: service and scheduler modules are missing.

- [ ] **Step 3：实现维护服务 API**

固定公开方法名与返回类型为 `load_state() -> StorageMaintenanceState`、`scan_automatic(progress=None) -> StorageInventory`、`scan_downloads(progress=None) -> StorageInventory`、`prepare_safe() -> SafeCleanupConfirmation`、`execute_safe(confirmation_id) -> StorageExecutionResult`、`clean_safe(trigger) -> StorageExecutionResult`、`prepare_manual(selected_ids) -> ManualCleanupConfirmation`、`execute_manual(confirmation_id) -> StorageExecutionResult`、`cancel() -> None` 和 `shutdown() -> None`。除 `load_state` 与 `prepare_manual` 外均为 async；`clean_safe` 只供已经明确启用的自动调度器调用，界面必须走 `prepare_safe`/`execute_safe` 的一次确认合同。

一次性确认结构和 `prepare_manual` 的核心实现为：

```python
class StorageMaintenanceError(RuntimeError):
    """Fixed user-safe storage maintenance failure."""


@dataclass(frozen=True, slots=True)
class StoragePreviewCategory:
    category: StorageCategory
    item_count: int
    expected_bytes: int


@dataclass(frozen=True, slots=True)
class SafeCleanupConfirmation:
    id: str
    categories: tuple[StoragePreviewCategory, ...]
    item_count: int
    expected_bytes: int
    expires_at: float


@dataclass(frozen=True, slots=True)
class ManualCleanupConfirmation:
    id: str
    item_count: int
    expected_bytes: int
    expires_at: float


def prepare_manual(
    self,
    selected_ids: Sequence[str],
) -> ManualCleanupConfirmation:
    if self._download_inventory is None:
        raise StorageMaintenanceError("请先重新扫描分片与留档")
    now = self.utc_clock()
    if now - self._download_inventory.scanned_at > timedelta(minutes=5):
        raise StorageMaintenanceError("分片与留档清单已过期，请重新扫描")
    plan = self.planner.manual_download(
        self._download_inventory,
        selected_ids,
        now,
    )
    confirmation = ManualCleanupConfirmation(
        id=uuid4().hex,
        item_count=len(plan.entries),
        expected_bytes=plan.expected_bytes,
        expires_at=self.monotonic_clock() + 300,
    )
    self._confirmations = {confirmation.id: (confirmation, plan)}
    return confirmation
```

扫描和执行使用 `asyncio.to_thread`；跨线程取消对象固定为 `threading.Event`。服务持有一个 asyncio 锁、当前 worker task、最近清单和分开的安全/下载一次性确认字典。`prepare_safe` 取得 `STORAGE_SCAN` 独占令牌，重新扫描自动白名单并生成五分钟有效计划；页面只得到聚合类别计数，不得到路径。`execute_safe` 与 `execute_manual` 在服务锁内先 peek 并验证确认，再尝试取得 `STORAGE_CLEANUP` 独占令牌；取得后才 `pop` 并逐项重验。未知、过期或重复确认抛出固定 `StorageMaintenanceError`；无法取得独占令牌返回 `BUSY_DEFERRED`，不消费尚未过期的确认。`clean_safe(AUTOMATIC)` 原子取得 `STORAGE_CLEANUP` 令牌后在同一令牌内完成扫描、计划和执行；无法取得时返回并持久化 `BUSY_DEFERRED`。`cancel()` 只 set 当前 event，`shutdown()` set 后 await worker 收敛；所有令牌在成功、异常和取消时释放。

状态写入规则固定为：成功扫描合并本次类别汇总并更新 `last_scan_at`；自动尝试更新 `last_automatic_check_at`；`COMPLETED` 与 `NOTHING_TO_CLEAN` 更新 `last_cleanup_at` 和 `next_due_at=completed_at+86400s`；`BUSY_DEFERRED` 追加历史但不推进正式 due；执行结果均追加聚合 history 并截断 20 条。状态加载失败时 `clean_safe(AUTOMATIC)` 不调用 inventory/executor；用户触发的 `scan_automatic` 或 `scan_downloads` 可从空内存状态重新生成并原子覆盖文件。日志调用只传 `category.value`、`result_code.value`、计数和字节，不传 exception 文本、entry ID、任务标题、display name 或路径。

- [ ] **Step 4：实现可注入调度器**

`StorageMaintenanceScheduler.start()` 创建单一 runner；runner 读取当前设置，关闭时等待设置变化。启动时已开启或运行中由关闭切为开启都先等待 300 秒；随后等待 `next_due_at`（无值视为立即到期）和连续 idle，调用前再次确认 `automatic_enabled`，然后调用 `clean_safe(AUTOMATIC)`。`BUSY_DEFERRED` 固定等待 900 秒再重试；完成或无项目使用状态中的正式 due，且不补跑错过周期。service 自己原子取得维护令牌，避免调度器预先持有 cleanup 令牌后无法满足空闲条件。`reconfigure(settings)` 唤醒等待中的 runner；如果 service 已经执行，则只记录新设置并在本轮结束后生效，不调用 `cancel()`。`shutdown()` 才取消自身 runner、调用 service shutdown，并等待执行收敛；它不关闭由应用拥有、其他业务共享的 activity registry。

- [ ] **Step 5：运行并提交**

Run: `.venv\Scripts\python.exe -m pytest tests/test_storage_maintenance.py tests/test_storage_scheduler.py tests/test_storage_maintenance_e2e.py -q`

Expected: all tests pass without real time delays.

```powershell
git add src/telegram_downloader/storage_maintenance.py src/telegram_downloader/storage_scheduler.py tests/test_storage_maintenance.py tests/test_storage_scheduler.py tests/test_storage_maintenance_e2e.py
git commit -m "feat: schedule opt-in idle storage cleanup"
```

## Task 9：存储空间页面与双确认对话框

**Files:**
- Create: `src/telegram_downloader/ui/storage.py`
- Create: `tests/ui/test_storage.py`

- [ ] **Step 1：写界面结构、信号和确认 RED 测试**

测试页面有四个概览值、七行固定类别、上次统计空状态、扫描/取消/启用/安全清理/管理分片按钮。开启自动清理时必须先显示固定策略与预计自动范围；用户拒绝时 checkbox 保持关闭，用户接受但设置保存失败时回滚关闭。手动表格测试受保护项没有 checkable flag，可删除项默认 unchecked。

安全清理测试先收到 `SafeCleanupConfirmation`，一次 Yes 后才发出 `safe_execute_requested(confirmation_id)`。下载残留测试分别模拟第一次 Yes/第二次 No 和第一次 Yes/第二次 Yes，断言只有两次 Yes 才发出 `manual_execute_requested(confirmation_id)`。

- [ ] **Step 2：运行并确认 RED**

Run: `.venv\Scripts\python.exe -m pytest tests/ui/test_storage.py -q`

Expected: storage UI module is missing.

- [ ] **Step 3：实现存储模型和页面**

`StorageCategoryModel.HEADERS` 固定为 `("类别", "当前大小", "可释放", "保留策略", "最近扫描", "状态")`；顺序严格使用七个 `StorageCategory`。`StoragePage` 信号固定为：

```python
activated = Signal()
scan_requested = Signal()
cancel_requested = Signal()
automatic_changed = Signal(bool)
safe_prepare_requested = Signal()
safe_execute_requested = Signal(str)
download_scan_requested = Signal()
manual_prepare_requested = Signal(object)
manual_execute_requested = Signal(str)
```

页面提供 `set_state`、`set_inventory`、`set_progress`、`set_busy`、`present_safe_confirmation`、`present_manual_confirmation`、`show_error` 和 `show_result`；busy 时禁用冲突按钮但保留取消。四个概览固定计算为 inventory 的 `disk_free_bytes`、现有 summaries 的 `total_bytes` 合计、前五类 `reclaimable_bytes` 合计、后两类 `reclaimable_bytes` 合计；对应数据从未扫描时显示“尚未扫描”，不能显示伪造的 0 B。`show_result` 必须分别显示删除、跳过、失败、取消与实际释放字节；`STATE_SAVE_FAILED` 固定显示“清理完成，记录保存失败”，不得显示“已回滚”。`present_safe_confirmation` 只显示分类数量、总项目数和预计字节，一次 Yes 后发执行信号；不得显示 `StorageEntry`。

- [ ] **Step 4：实现手动候选和两次确认**

`ManualCleanupDialog` 显示选择、相对文件名、关联任务、类别、大小、修改时间、状态和保护原因。第一次 Yes 只把选中 ID 发给 service 准备；收到 `ManualCleanupConfirmation` 后第二次显示精确数量/字节，第二次 Yes 才发执行信号。任一次 No 都不删除；确认失败或过期保持对话框打开并要求重新扫描。

- [ ] **Step 5：运行并提交**

Run: `.venv\Scripts\python.exe -m pytest tests/ui/test_storage.py -q`

Expected: all tests pass under pytest-qt.

```powershell
git add src/telegram_downloader/ui/storage.py tests/ui/test_storage.py
git commit -m "feat: present safe storage maintenance controls"
```

## Task 10：维护中心容器与主导航兼容

**Files:**
- Create: `src/telegram_downloader/ui/maintenance.py`
- Create: `tests/ui/test_maintenance.py`
- Modify: `src/telegram_downloader/ui/diagnostics.py`
- Modify: `src/telegram_downloader/ui/main.py:75-180,183-233,1017-1063`
- Modify: `tests/ui/test_diagnostics.py`
- Modify: `tests/ui/test_main_window.py:856-869`

- [ ] **Step 1：写两标签和导航 RED 测试**

```python
def test_maintenance_center_has_only_real_tabs(qtbot) -> None:
    page = MaintenancePage()
    qtbot.addWidget(page)
    assert page.tabs.count() == 2
    assert page.tabs.tabText(0) == "健康检查"
    assert page.tabs.tabText(1) == "存储空间"
    assert page.diagnostics_page is page.tabs.widget(0)
    assert page.storage_page is page.tabs.widget(1)


def test_maintenance_navigation_keeps_diagnostics_compatibility(qtbot) -> None:
    window = MainWindow()
    qtbot.addWidget(window)
    qtbot.mouseClick(window.maintenance_nav_button, Qt.MouseButton.LeftButton)
    assert window.page_stack.currentWidget() is window.maintenance_page
    assert window.diagnostics_page is window.maintenance_page.diagnostics_page
    assert window.storage_page is window.maintenance_page.storage_page
```

- [ ] **Step 2：运行并确认 RED**

Run: `.venv\Scripts\python.exe -m pytest tests/ui/test_maintenance.py tests/ui/test_main_window.py -q`

Expected: maintenance container and aliases are missing.

- [ ] **Step 3：实现维护中心容器**

`MaintenancePage` 使用 `QTabWidget`，只构造 `DiagnosticsPage()` 和 `StoragePage()`。提供 `show_health()`、`show_storage()`；转到 storage 标签时发出 `storage_page.activated`。

- [ ] **Step 4：替换导航并保留兼容属性**

在 `MainWindow` 中：

```python
self.maintenance_page = MaintenancePage()
self.diagnostics_page = self.maintenance_page.diagnostics_page
self.storage_page = self.maintenance_page.storage_page
self.maintenance_nav_button = self._nav_button("维护中心")
self.diagnostics_nav_button = self.maintenance_nav_button
```

`show_page("maintenance")` 显示维护中心并保留当前标签；`show_page("diagnostics")` 先调用 `maintenance_page.show_health()` 再显示维护中心，保持旧调用语义。两条路径都发出原 `diagnostics_activated` 以加载健康历史；只有切到存储标签才发出 `storage_page.activated`。通知路由使用新名字，统计侧栏在维护中心隐藏。

- [ ] **Step 5：运行并提交**

Run: `.venv\Scripts\python.exe -m pytest tests/ui/test_maintenance.py tests/ui/test_main_window.py tests/ui/test_diagnostics.py -q`

Expected: all existing diagnostics interactions remain green.

```powershell
git add src/telegram_downloader/ui/maintenance.py src/telegram_downloader/ui/diagnostics.py src/telegram_downloader/ui/main.py tests/ui/test_maintenance.py tests/ui/test_diagnostics.py tests/ui/test_main_window.py
git commit -m "feat: upgrade diagnostics to maintenance center"
```

## Task 11：应用装配、活动接入与安全关闭

**Files:**
- Modify: `src/telegram_downloader/app.py:340-1380`
- Modify: `src/telegram_downloader/controller.py:430-2100`
- Modify: `src/telegram_downloader/scheduler.py:150-430`
- Modify: `src/telegram_downloader/subscription_scheduler.py:35-160`
- Modify: `src/telegram_downloader/update.py:91-174`
- Modify: `src/telegram_downloader/ui/async_actions.py`
- Modify: `tests/test_app.py`
- Modify: `tests/test_controller.py`
- Modify: `tests/test_scheduler.py`
- Modify: `tests/test_subscription_scheduler.py`
- Modify: `tests/update/test_update_coordinator.py`

- [ ] **Step 1：写共享登记器、互斥和关闭顺序 RED 测试**

扩展 `test_create_application_initializes_project_local_content_services`，断言 controller、下载调度器、订阅调度器、更新器和存储调度器共享同一个 registry。分别让下载、搜索、订阅、诊断、完整性和更新操作阻塞，断言自动维护不启动；释放后连续空闲才启动。

关闭测试要求顺序为：storage scheduler → async actions → controller diagnostics/integrity → subscription scheduler → controller download scheduler → download-window scheduler → activity registry close → application quit。

- [ ] **Step 2：运行并确认 RED**

Run: `.venv\Scripts\python.exe -m pytest tests/test_app.py tests/test_controller.py tests/test_scheduler.py tests/test_subscription_scheduler.py tests/update/test_update_coordinator.py -q`

Expected: composition and activity assertions fail.

- [ ] **Step 3：构造共享维护运行时**

在 `create_application` 创建：

```python
activity = OperationActivityRegistry()
update_protection = UpdateProtectionProvider(paths)
storage_state = StorageStateStore(paths.storage_maintenance_state)
storage_inventory = StorageInventoryService(paths, repository)
storage_cleanup = StorageCleanupExecutor(paths, repository, update_protection)
storage_service = StorageMaintenanceService(
    paths=paths,
    settings=settings.storage_maintenance,
    state_store=storage_state,
    inventory=storage_inventory,
    planner=StorageCleanupPlanner(),
    executor=storage_cleanup,
    activity=activity,
    publish=publish_event,
)
storage_scheduler = StorageMaintenanceScheduler(
    storage_service,
    activity,
    lambda: controller_ref["controller"].settings.storage_maintenance,
)
```

把对象作为 controller 明确属性保存，供测试和关闭使用；不要使用模块全局单例。

- [ ] **Step 4：让业务操作持有活动令牌**

- 下载 scheduler 在每个活动 task operation 生命周期持有 `DOWNLOAD`。
- integrity verify/repair 持有 `INTEGRITY`。
- 链接/批量扫描持有 `SCAN`；内容同步/搜索持有 `SEARCH`。
- `SubscriptionScheduler._execute` 持有 `SUBSCRIPTION`。
- `run_diagnostics` 持有 `DIAGNOSTICS`。
- `UpdateCoordinator.startup` 只在用户接受后下载和启动 helper 的阶段持有 `UPDATE`；普通后台版本检查不阻止维护。

每处统一使用 `with activity.track(kind):` 包裹现有 try/finally 外层，使成功、异常和 `CancelledError` 都释放。各用户入口捕获 `MaintenanceBusyError`，显示固定“存储维护正在收尾，请稍后重试”，并保持原任务/搜索/订阅状态不变；后台订阅遇到该异常走 15 分钟既有唤醒机制，不记为规则失败。

- [ ] **Step 5：连接存储页异步动作**

给 `AsyncActionBridge` 增加完整键：`maintenance.storage.activate`、`maintenance.storage.scan`、`maintenance.storage.prepare-safe`、`maintenance.storage.execute-safe`、`maintenance.storage.scan-downloads`、`maintenance.storage.prepare-manual`、`maintenance.storage.execute-manual`、`maintenance.storage.cancel`，全部使用 `DEDUPLICATE`，取消键允许与当前存储操作并存。

在 app 中连接页面信号到 service；`maintenance.storage.activate` 先把 `load_state()` 的上次脱敏汇总送入页面，再异步调用 `scan_automatic` 刷新前五类和 `disk_free_bytes`（这是用户打开页面触发，不是关闭状态下的后台自动周期）。`scan_downloads` 只在用户点击“管理分片与留档”后运行并合并后两类汇总。扫描/清理期间调用 `set_busy`，进度回调使用 `loop.call_soon_threadsafe` 转到 Qt 主线程。设置自动开关时先 `dataclasses.replace` 当前 AppSettings，保存成功后 `storage_scheduler.reconfigure`；保存失败恢复 checkbox。

- [ ] **Step 6：启动与关闭**

`start_application` 在 controller 启动成功后调用 `storage_scheduler.start()`。把 `_GracefulShutdown` 的单一前置回调拆为 `before_async_shutdown` 和 `after_controller_shutdown`，`_run()` 固定按“前置 → async actions → controller → 后置 → quit”执行：

```python
async def stop_storage_before_actions() -> None:
    await storage_scheduler.shutdown()


async def stop_downloads_after_controller() -> None:
    await download_schedule.shutdown()
    activity.close()
```

分别传给 `_GracefulShutdown(before_async_shutdown=stop_storage_before_actions, after_controller_shutdown=stop_downloads_after_controller)`。更新既有 `_GracefulShutdown` 单元测试的顺序断言；最终 fallback 再调用这些幂等 shutdown，不得重复释放活动令牌。

- [ ] **Step 7：运行回归并提交**

Run: `.venv\Scripts\python.exe -m pytest tests/test_app.py tests/test_controller.py tests/test_scheduler.py tests/test_subscription_scheduler.py tests/update/test_update_coordinator.py -q`

Expected: all tests pass and no activity token leaks.

```powershell
git add src/telegram_downloader/app.py src/telegram_downloader/controller.py src/telegram_downloader/scheduler.py src/telegram_downloader/subscription_scheduler.py src/telegram_downloader/update.py src/telegram_downloader/ui/async_actions.py tests/test_app.py tests/test_controller.py tests/test_scheduler.py tests/test_subscription_scheduler.py tests/update/test_update_coordinator.py
git commit -m "feat: integrate maintenance-safe runtime activity"
```

## Task 12：通知、设置快捷入口与路径诊断

**Files:**
- Modify: `src/telegram_downloader/notifications.py`
- Modify: `src/telegram_downloader/ui/settings.py`
- Modify: `src/telegram_downloader/app.py`
- Modify: `src/telegram_downloader/diagnostic_probes.py`
- Modify: `tests/test_notifications.py`
- Modify: `tests/ui/test_settings_dialog.py`
- Modify: `tests/test_diagnostic_probes.py`
- Modify: `tests/test_self_test.py`

- [ ] **Step 1：写聚合通知、快捷入口和自检 RED 测试**

通知测试构造两次 storage cleaned 事件，断言字节合并、固定文本和维护中心路由；把秘密、任务名和路径放进 `private_context`，断言 payload 不含它们。失败事件只显示固定“自动清理需要处理”。

设置对话框测试点击旧缓存按钮后发出 `storage_maintenance_requested`（并保留 deprecated 旧信号），随后关闭设置，不再直接清理缓存。自检固定受管路径数量增加 maintenance/state 的目录保护，但 CLI 继续只输出允许的聚合字段。

- [ ] **Step 2：运行并确认 RED**

Run: `.venv\Scripts\python.exe -m pytest tests/test_notifications.py tests/ui/test_settings_dialog.py tests/test_diagnostic_probes.py tests/test_self_test.py -q`

Expected: maintenance notification/shortcut/path assertions fail.

- [ ] **Step 3：扩展通知合同**

增加 `EventKind.STORAGE_CLEANED`、`STORAGE_CLEANUP_FAILED` 和 `NotificationRoute.MAINTENANCE`。把 `ApplicationEvent.byte_count: int = 0` 放在现有 `private_context` 之后，保持第五个位置参数兼容，并严格验证它是非负非 bool 整数；`_PendingBatch` 增加 `byte_count` 并在同类事件合并时累加。固定文本：

```python
EventKind.STORAGE_CLEANED: (
    "已释放存储空间",
    "后台安全清理已删除 {count} 项，释放 {size}",
)
EventKind.STORAGE_CLEANUP_FAILED: (
    "自动清理需要处理",
    "自动清理有 {count} 项未处理，请打开维护中心查看",
)
```

`flush_due` 固定调用 `body.format(count=batch.count, size=format_size(batch.byte_count))`。`format_size` 只根据整数生成 B/KiB/MiB/GiB，不读取 `private_context`。storage service 只为 `AUTOMATIC` 触发在释放 `>= 100 * 1024**2` 或失败数大于零时发布事件，手动操作只在页面显示结果。app 的 `QtWindowPort` 路由表增加 `NotificationRoute.MAINTENANCE: lambda: controller.window.show_page("maintenance")`。

- [ ] **Step 4：迁移设置快捷入口并扩展路径自检**

新增 `storage_maintenance_requested`，保留一个版本的 deprecated `thumbnail_cache_clear_requested`；按钮文本改为“前往存储空间”，点击时发出两者，但 app 移除旧信号的直接缓存删除连接，只监听新信号。app 收到后关闭设置对话框并调用 `window.show_page("maintenance")`、`window.maintenance_page.show_storage()`；旧信号在本版本不再触发任何删除。

在 `managed_writable_paths` 增加 `"maintenanceState": paths.storage_maintenance_state`，在 `run_self_test.public_names` 增加 `"maintenance_state": "maintenanceState"`；probe 和 `--self-test` 必须验证该路径位于项目根，但报告不得读取或输出状态文件内容。更新既有可写路径数量断言并保留所有原 key。

- [ ] **Step 5：运行并提交**

Run: `.venv\Scripts\python.exe -m pytest tests/test_notifications.py tests/ui/test_settings_dialog.py tests/test_diagnostic_probes.py tests/test_self_test.py -q`

Expected: all tests pass.

```powershell
git add src/telegram_downloader/notifications.py src/telegram_downloader/ui/settings.py src/telegram_downloader/app.py src/telegram_downloader/diagnostic_probes.py tests/test_notifications.py tests/ui/test_settings_dialog.py tests/test_diagnostic_probes.py tests/test_self_test.py
git commit -m "feat: surface private maintenance results"
```

## Task 13：文档、v0.15.0 候选版与三轮验收

**Files:**
- Modify: `README.md`
- Create: `docs/releases/v0.15.0.md`
- Create: `docs/verification/v0.15.0-storage-maintenance.md`
- Modify: `pyproject.toml`
- Modify: `src/telegram_downloader/__init__.py`
- Modify: `installer/TelegramDownloader.iss`
- Modify: `scripts/build.ps1`
- Modify: `tests/test_packaging_contract.py`

- [ ] **Step 1：先写版本与隐私打包 RED 测试**

扩展 packaging contract，要求三个版本位置均为 `0.15.0`，README 和 release notes 明确：默认关闭、固定 7/30 天、1 GiB/900 MiB、更新备份 1 份、自动不扫描 downloads、手动双确认和来源不明禁止删除。要求构建脚本的敏感文件扫描包含：

```python
forbidden = (
    "storage-state.json",
    ".part",
    ".corrupt",
    "app.log",
    "tasks.sqlite3",
    "catalog.sqlite3",
    "secrets.dat",
)
```

- [ ] **Step 2：运行并确认 RED**

Run: `.venv\Scripts\python.exe -m pytest tests/test_packaging_contract.py -q`

Expected: version and documentation assertions fail.

- [ ] **Step 3：更新版本、用户文档和 ZIP 隐私门禁**

把 `pyproject.toml`、package `__version__` 和 Inno Setup `AppVersion` 同步为 0.15.0。README 新增“维护中心与存储空间”章节，清楚区分自动类别、手动类别、不可删除内容、永久删除和退出取消语义。release notes 使用用户可见结果，不记录私人 QA 路径。

在 `scripts/build.ps1` 的 `Compress-Archive` 后、输出 ZIP 前，用 `System.IO.Compression.ZipFile.OpenRead($zip)` 枚举规范化为小写 `/` 分隔符的 `Entry.FullName`；若任一条目等于/位于 `data`、`downloads`，或文件名匹配 `storage-state.json`、`app.log`、`tasks.sqlite3`、`catalog.sqlite3`、`secrets.dat`、`.part`、`.corrupt`，立即抛出 `Portable ZIP contains private runtime entry`。`finally` 必须 dispose archive；测试断言所有 fragment 和异常文本都存在于脚本。

- [ ] **Step 4：第一轮自检——规格、安全和定向回归**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_settings.py tests/test_paths.py tests/test_repository.py tests/test_thumbnail_cache.py tests/test_maintenance_activity.py tests/test_storage_models.py tests/test_storage_state.py tests/test_update_protection.py tests/test_storage_inventory.py tests/test_storage_cleanup.py tests/test_storage_maintenance.py tests/test_storage_scheduler.py tests/test_storage_maintenance_e2e.py tests/test_notifications.py tests/test_diagnostic_probes.py tests/test_self_test.py tests/test_app.py tests/test_controller.py tests/test_scheduler.py tests/test_subscription_scheduler.py tests/update/test_update_coordinator.py tests/update/test_update_transaction.py tests/ui/test_storage.py tests/ui/test_maintenance.py tests/ui/test_diagnostics.py tests/ui/test_main_window.py tests/ui/test_settings_dialog.py tests/test_packaging_contract.py -q
git diff --check
```

Expected: all selected tests pass; `git diff --check` produces no output. 对照设计文档逐项记录覆盖结果，并确认没有真实项目数据路径出现在测试输出。

- [ ] **Step 5：第二轮自检——完整源码回归**

Run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/test.ps1
.venv\Scripts\python.exe -m compileall -q src tests
.venv\Scripts\python.exe -m pip check
```

Expected: pytest 100% pass、Ruff `All checks passed!`、compileall exit 0、`No broken requirements found.`

- [ ] **Step 6：第三轮自检——真实临时树、冻结产物和安装器**

Run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build-installer.ps1 -SkipAppBuild
$projectRoot = (Resolve-Path ".").Path
$smokeRoot = Join-Path $projectRoot (".build-temp\storage-maintenance-smoke-" + [Guid]::NewGuid().ToString("N"))
$portableZip = Join-Path $projectRoot "dist\TelegramDownloader-0.15.0-win-x64-portable.zip"
New-Item -ItemType Directory -Path $smokeRoot | Out-Null
Expand-Archive -LiteralPath $portableZip -DestinationPath $smokeRoot
$selfTest = (& (Join-Path $smokeRoot "TelegramDownloader.exe") --self-test | ConvertFrom-Json)
if (-not $selfTest.ok -or $selfTest.version -ne "0.15.0") { throw "Frozen self-test failed" }
if (-not ([IO.Path]::GetFullPath($selfTest.runtime_root)).StartsWith($smokeRoot, [StringComparison]::OrdinalIgnoreCase)) { throw "Frozen self-test escaped smoke root" }
if (-not ([IO.Path]::GetFullPath($selfTest.writable_paths.maintenance_state)).StartsWith($smokeRoot, [StringComparison]::OrdinalIgnoreCase)) { throw "Maintenance state escaped smoke root" }
Get-FileHash -Algorithm SHA256 -LiteralPath $portableZip
Get-FileHash -Algorithm SHA256 -LiteralPath "dist\release\TelegramDownloader-0.15.0-win-x64-setup.exe"
```

Expected: `PACKAGED_SMOKE_OK`、`INSTALLER_SMOKE_OK`。从最终 portable ZIP 解压到新的项目内 `.build-temp` 目录，运行 `TelegramDownloader.exe --self-test`，要求 `ok=true`、版本 0.15.0、维护目录在隔离根内。ZIP 条目扫描要求 forbidden 计数为 0。

- [ ] **Step 7：记录候选证据并提交**

`docs/verification/v0.15.0-storage-maintenance.md` 记录三轮命令、精确测试数量、产物字节数/SHA-256、冻结自检结果、ZIP 隐私计数和真实 Telegram 未参与自动化的外部边界。

```powershell
git add README.md docs/releases/v0.15.0.md docs/verification/v0.15.0-storage-maintenance.md pyproject.toml src/telegram_downloader/__init__.py installer/TelegramDownloader.iss scripts/build.ps1 tests/test_packaging_contract.py
git commit -m "release: prepare TelegramDownloader 0.15.0"
git status --short --branch
```

Expected: commit succeeds and worktree is clean. 不推送、不打标签、不发布；进入代码审查和分支完成流程后再向用户提供合并/发布选择。

## 计划完成定义

- 13 个任务均有对应 RED/GREEN 证据和独立提交。
- 自动清理默认关闭，只处理固定白名单和固定策略。
- 正式媒体、当前日志、可续传分片、未完成修复留档、来源不明条目和更新事务在所有路径下受到保护。
- 手动下载残留必须使用新鲜清单、一次性确认 ID 和两次界面确认。
- 活动登记器证明维护不会与下载、搜索、订阅、诊断、完整性或更新并发。
- 状态、日志和通知不含私人路径、文件名、任务名、账号、来源或 Telegram 标识。
- 完整源码、便携包、安装包和隔离重启自检三轮通过。
- 最终只形成 v0.15.0 本地候选版；远端发布保持未授权状态。
