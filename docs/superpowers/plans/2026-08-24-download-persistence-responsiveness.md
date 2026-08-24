# Download Persistence Responsiveness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将下载进度、任务状态、重试状态和下载终态的 SQLite 操作移出 asyncio 事件循环，在 500 ms 固定窗口内合并进度，并保证暂停、失败、重试和完成在返回前耐久落盘。

**Architecture:** 保留同步 `TaskRepository` 和现有数据库结构，新增一个由下载器与调度器共享的 `DownloadPersistenceCoordinator`。协调器在事件循环内维护 latest-wins 进度快照与有序屏障，所有真实仓库调用通过 `asyncio.to_thread` 或可注入后台执行器运行；生产装配共享一个实例，兼容装配使用无合并的线程桥接器。

**Tech Stack:** Python 3.12, asyncio, SQLite, PySide6, pytest, Ruff, PyInstaller, Inno Setup

---

## 实施约束

- 以已批准设计 `docs/superpowers/specs/2026-08-24-download-persistence-responsiveness-design.md` 为唯一需求来源。
- 不新增数据库表、字段或迁移，不引入 `aiosqlite`，不改变文件写入批次、带宽控制、媒体槽或 Telegram 协议。
- 进度合并窗口固定为 0.5 秒；新进度不能重置已经开始的窗口。
- 暂停、失败、重试和完成必须使用媒体屏障；成功返回即表示最新进度与终态已提交。
- 协调器首次仓库故障为粘性故障；后续调用得到同一异常，不继续积累工作。
- 同一生产协调器必须同时注入 `MediaDownloader` 与 `DownloadScheduler`，并由调度器关闭。
- 所有新增错误信息保持脱敏，不记录 Telegram 链接、消息内容、账号、路径或数据库记录。
- 本计划只形成本地 v0.18.3 候选。未获得新的“合并并发布更新”授权前，不合并 `main`、不打标签、不上传资产、不修改在线 stable 指针。

## 文件结构

### 新增

- `src/telegram_downloader/download_persistence.py`：异步持久化协议、兼容线程桥接器、500 ms 合并协调器、屏障、故障和关闭生命周期。
- `tests/test_download_persistence.py`：协调器的合并、排序、取消、故障、关闭和心跳合同。
- `tests/ui/test_download_persistence_responsiveness.py`：Qt 定时器与 asyncio 共用事件循环时的合成慢仓库响应门禁。
- `scripts/benchmark_download_persistence.py`：仅使用临时目录、固定合成媒体和人工 50 ms 仓库延迟的性能门禁。
- `docs/releases/v0.18.3.md`：用户可见变更与兼容性说明。
- `docs/verification/v0.18.3-download-persistence-responsiveness.md`：三轮真实验证记录。

### 修改

- `src/telegram_downloader/repository.py`：增加不可变进度值对象和单事务批量进度更新。
- `src/telegram_downloader/downloader.py`：进度、暂停和完成全部经过异步持久化边界。
- `src/telegram_downloader/scheduler.py`：任务/重试状态与读取移出事件循环，公开暂停和优先级入口改为可等待。
- `src/telegram_downloader/controller.py`：等待调度器的暂停/优先级操作，异步处理完整性取消。
- `src/telegram_downloader/app.py`：创建并共享一个生产协调器，更新信号适配。
- `tests/test_repository.py`：批量事务、输入验证和回滚测试。
- `tests/test_downloader.py`：终态耐久性、进度合并和慢仓库心跳测试。
- `tests/test_scheduler.py`：异步仓库边界、故障暂停、关闭排空和入口签名测试。
- `tests/test_controller.py`：等待顺序和完整性取消测试。
- `tests/test_app.py`：共享实例和生命周期装配测试。
- `tests/test_download_queue_e2e.py`：真实仓库的共享协调器、暂停/恢复与重启读取。
- `tests/test_download_queue_stress.py`：异步暂停/优先级调用和高任务数回归。
- `tests/test_packaging_contract.py`：v0.18.3 元数据和合成基准隐私合同。
- `pyproject.toml`、`src/telegram_downloader/__init__.py`、`installer/TelegramDownloader.iss`：本地候选版本 0.18.3。
- `README.md`：下载期间界面响应、500 ms 进度合并、终态耐久与恢复语义。

## Task 1: 为仓库增加原子批量进度事务

**Files:**

- Modify: `src/telegram_downloader/repository.py`
- Modify: `tests/test_repository.py`

- [ ] **Step 1: 写入批量成功、重复 ID 和整体回滚的失败测试**

在 `tests/test_repository.py` 中从仓库模块导入 `ItemProgressUpdate`，新增以下合同：

```python
def test_batch_item_progress_updates_commit_in_one_call(tmp_path: Path) -> None:
    repo = TaskRepository(tmp_path / "tasks.sqlite3")
    repo.initialize()
    task, first = records(tmp_path)
    second = replace(
        first,
        id="item-2",
        message_id=8,
        media_id="media-8",
        target_path=tmp_path / "y.mp4",
    )
    repo.create_task(task, [first, second])

    repo.update_item_progresses(
        [
            ItemProgressUpdate(first.id, 3, ItemStatus.DOWNLOADING),
            ItemProgressUpdate(
                second.id,
                5,
                ItemStatus.WAITING_RETRY,
                "network",
                2,
            ),
        ]
    )

    stored = {item.id: item for item in repo.list_items(task.id)}
    assert stored[first.id].downloaded_bytes == 3
    assert stored[second.id].retry_count == 2
    assert stored[second.id].last_error == "network"


def test_batch_item_progress_rejects_duplicates_before_writing(tmp_path: Path) -> None:
    repo = TaskRepository(tmp_path / "tasks.sqlite3")
    repo.initialize()
    task, media = records(tmp_path)
    repo.create_task(task, [media])

    with pytest.raises(ValueError, match="重复"):
        repo.update_item_progresses(
            [
                ItemProgressUpdate(media.id, 1, ItemStatus.DOWNLOADING),
                ItemProgressUpdate(media.id, 2, ItemStatus.DOWNLOADING),
            ]
        )

    assert repo.get_item(media.id).downloaded_bytes == 0


def test_batch_item_progress_rolls_back_if_any_item_is_missing(tmp_path: Path) -> None:
    repo = TaskRepository(tmp_path / "tasks.sqlite3")
    repo.initialize()
    task, media = records(tmp_path)
    repo.create_task(task, [media])

    with pytest.raises(KeyError, match="missing"):
        repo.update_item_progresses(
            [
                ItemProgressUpdate(media.id, 4, ItemStatus.DOWNLOADING),
                ItemProgressUpdate("missing", 7, ItemStatus.DOWNLOADING),
            ]
        )

    assert repo.get_item(media.id).downloaded_bytes == 0
```

- [ ] **Step 2: 运行测试并确认因 API 不存在失败**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_repository.py -k "batch_item_progress" -q
```

Expected: FAIL，导入或属性错误明确指出 `ItemProgressUpdate` / `update_item_progresses` 尚不存在。

- [ ] **Step 3: 实现值对象、共享验证和单事务更新**

在 `repository.py` 中加入：

```python
@dataclass(frozen=True, slots=True)
class ItemProgressUpdate:
    item_id: str
    downloaded_bytes: int
    status: ItemStatus
    error: str | None = None
    retry_count: int | None = None
```

提取 `_validate_item_progress` 和 `_update_item_progress_on_connection` 私有辅助方法。现有 `update_item_progress` 继续打开自己的连接并调用辅助方法；新增方法必须先验证全部输入和重复 ID，再在一个 `_connection()` 上逐项执行：

```python
def update_item_progresses(
    self,
    updates: Sequence[ItemProgressUpdate],
) -> None:
    ordered = tuple(updates)
    ids = [update.item_id for update in ordered]
    if len(ids) != len(set(ids)):
        raise ValueError("批量进度包含重复媒体 ID")
    for update in ordered:
        self._validate_item_progress(update.downloaded_bytes, update.retry_count)
    with self._connection() as connection:
        for update in ordered:
            self._update_item_progress_on_connection(connection, update)
```

任何 `rowcount != 1` 都抛出 `KeyError(item_id)`，让连接上下文整体回滚。空序列为无操作。

- [ ] **Step 4: 运行仓库回归**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_repository.py -q
.\.venv\Scripts\python.exe -m ruff check src/telegram_downloader/repository.py tests/test_repository.py
```

Expected: PASS；单项更新行为不变，批量中缺失项不会留下部分更新。

- [ ] **Step 5: 提交仓库事务**

```powershell
git add src/telegram_downloader/repository.py tests/test_repository.py
git commit -m "feat: batch download progress persistence"
```

## Task 2: 建立异步持久化协议和兼容线程桥接器

**Files:**

- Create: `src/telegram_downloader/download_persistence.py`
- Create: `tests/test_download_persistence.py`

- [ ] **Step 1: 写入线程桥接、操作排序和取消保护的失败测试**

测试使用 `threading.get_ident()` 记录仓库执行线程，要求它不同于事件循环线程；连续两个 `execute` 操作按调用顺序完成。再创建一个阻塞后台操作，取消调用方后释放阻塞，断言仓库操作仍完成且调用方最终收到 `CancelledError`。

核心断言：

```python
bridge = ThreadedDownloadPersistence(repository)
loop_thread = threading.get_ident()
worker_thread = await bridge.execute(repository.record_thread)
assert worker_thread != loop_thread

operation = asyncio.create_task(bridge.execute(repository.blocking_write))
await repository.started.wait()
operation.cancel()
repository.release.set()
with pytest.raises(asyncio.CancelledError):
    await operation
assert repository.write_finished is True
```

- [ ] **Step 2: 运行测试并确认模块不存在**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_download_persistence.py -k "threaded" -q
```

Expected: FAIL，`telegram_downloader.download_persistence` 尚不存在。

- [ ] **Step 3: 定义协议、类型和线程桥接器**

模块公开以下最小接口：

```python
T = TypeVar("T")
BlockingRunner = Callable[[Callable[[], T]], Awaitable[T]]


class DownloadPersistence(Protocol):
    async def record_progress(self, update: ItemProgressUpdate) -> None: ...

    async def execute(
        self,
        operation: Callable[[], T],
        *,
        flush_item_ids: Collection[str] = (),
        flush_all: bool = False,
    ) -> T: ...

    async def drain(self) -> None: ...
    async def close(self) -> None: ...
```

`ThreadedDownloadPersistence` 用于旧式测试装配和非共享调用：

- `record_progress` 通过后台执行单项 `repository.update_item_progress` 并等待完成，不合并。
- `execute` 忽略空的屏障参数，通过同一 `asyncio.Lock` 串行调用后台执行器。
- 默认执行器为包装 `asyncio.to_thread` 的协程函数。
- 实际后台 await 使用独立 task 和 `asyncio.shield`；调用方取消时先等待不可取消的仓库动作落定，再重新抛出取消。
- `drain` 等待锁前的全部操作，`close` 标记关闭并排空；关闭后新操作抛出 `RuntimeError("下载持久化已关闭")`。

- [ ] **Step 4: 运行新模块测试和静态检查**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_download_persistence.py -k "threaded" -q
.\.venv\Scripts\python.exe -m ruff check src/telegram_downloader/download_persistence.py tests/test_download_persistence.py
```

Expected: PASS，无未取回异常、无测试结束后的 pending task 警告。

- [ ] **Step 5: 提交异步边界**

```powershell
git add src/telegram_downloader/download_persistence.py tests/test_download_persistence.py
git commit -m "feat: add async download persistence boundary"
```

## Task 3: 实现 500 ms latest-wins 协调器、屏障和粘性故障

**Files:**

- Modify: `src/telegram_downloader/download_persistence.py`
- Modify: `tests/test_download_persistence.py`

- [ ] **Step 1: 写入固定窗口合并和跨媒体批量测试**

使用可控单调时钟或 20 ms 测试窗口，验证：

- 同一媒体 20 次 `record_progress` 只在待处理映射保留最后值。
- 更新发生在窗口内不会把截止时间向后移动。
- 五个媒体在同一窗口只调用一次 `update_item_progresses`。
- `drain()` 立即提交最后快照，不等待完整窗口。

关键断言：

```python
for downloaded in range(1, 21):
    await persistence.record_progress(
        ItemProgressUpdate("media", downloaded, ItemStatus.DOWNLOADING)
    )
await persistence.drain()

assert repository.batch_calls == 1
assert repository.batches == [
    (ItemProgressUpdate("media", 20, ItemStatus.DOWNLOADING),)
]
```

- [ ] **Step 2: 写入媒体屏障和全局屏障顺序测试**

事件日志必须分别得到：

```text
batch:media=20
terminal:media
```

以及多媒体场景：`flush_item_ids=("a",)` 只提交 a，b 留到窗口或 `drain`；`flush_all=True` 在读取/汇总前提交全部快照。

终态仓库动作由门闩阻塞时，`execute` 返回的 future 不得提前完成。

- [ ] **Step 3: 写入故障、关闭和取消测试**

覆盖以下合同：

- 批量事务第一次失败后调用一次故障回调。
- 后续 `record_progress`、`execute`、`drain` 得到同一个异常对象。
- 故障后不再调用仓库。
- `close()` 排空最后进度、停止 worker，二次关闭幂等。
- 关闭开始后拒绝新工作。
- 取消屏障等待不能取消已经进入线程的批量或终态操作。

- [ ] **Step 4: 运行测试并确认协调器 API 尚未实现**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_download_persistence.py -k "coordinator or barrier or sticky or close" -q
```

Expected: FAIL，缺少 `DownloadPersistenceCoordinator` 或不满足合并/屏障断言。

- [ ] **Step 5: 实现协调器状态机**

`DownloadPersistenceCoordinator` 构造参数：

```python
def __init__(
    self,
    repository: ProgressRepository,
    *,
    flush_interval: float = 0.5,
    runner: BlockingRunner | None = None,
) -> None:
```

实现细节：

- 构造阶段不创建 asyncio task；第一次 `record_progress` 才在当前运行循环懒启动 worker。
- `_pending: dict[str, ItemProgressUpdate]` 按 ID 覆盖。
- `_deadline` 只在从“无待处理”变成“有待处理”时设置为 `loop.time() + flush_interval`；后续覆盖不延期。
- `_wake` 仅唤醒 worker 重新计算截止时间。
- `_operation_lock` 串行所有批量写和有序命令，形成单一命令通道。
- 屏障在锁内提取指定 ID 或全部待处理快照，先调用 `repository.update_item_progresses`，再调用目标 operation。
- 快照从映射取出后发生的新更新形成下一窗口；同一媒体终态依赖现有“单活动操作”不变量，不并发产生新进度。
- 后台执行器调用统一通过不可取消 `_run_blocking`；成功或失败都取回 task 结果。
- `_set_fault` 只保存首个异常，调用内存故障回调一次，唤醒 worker，并让所有后续 API 快速失败。
- 提供 `set_fault_handler(handler: Callable[[BaseException], None]) -> None`；只允许替换内存回调，不立即访问仓库。兼容线程桥接器不要求粘性故障，调度器通过 `getattr` 仅在协调器支持时注册。
- worker 自己捕获异常并进入故障状态，不能留下 “Task exception was never retrieved”。
- `drain` 在无新工作条件下循环屏障至 `_pending` 为空，并等待 `_operation_lock` 前面的动作。
- `close` 先阻止新公开操作，再使用内部排空路径刷新，唤醒并等待 worker 结束；二次调用不重复关闭。

不要让 `record_progress` 获取会跨 `await` 持有的锁；它必须只做常量时间内存更新。

- [ ] **Step 6: 运行完整协调器测试**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_download_persistence.py -q
.\.venv\Scripts\python.exe -m ruff check src/telegram_downloader/download_persistence.py tests/test_download_persistence.py
```

Expected: PASS；固定窗口、latest-wins、屏障、故障和关闭合同全部通过。

- [ ] **Step 7: 提交协调器**

```powershell
git add src/telegram_downloader/download_persistence.py tests/test_download_persistence.py
git commit -m "feat: coalesce download persistence off loop"
```

## Task 4: 将 MediaDownloader 迁移到协调器

**Files:**

- Modify: `src/telegram_downloader/downloader.py`
- Modify: `tests/test_downloader.py`

- [ ] **Step 1: 写入终态耐久和旧进度不可越过终态的失败测试**

给 `MediaDownloader` 注入 `DownloadPersistenceCoordinator`。完成操作阻塞时，下载 future 不得完成；释放后数据库事件顺序必须是开始、最后合并进度、完成。暂停、空间不足和取消分别断言最新 `.part` 长度先于 `PAUSED` 状态持久化。

```python
operation = asyncio.create_task(media.download(media_item))
await repository.complete_started.wait()
assert operation.done() is False
repository.complete_release.set()
await operation
assert repository.events[-2:] == ["batch:i:20", "complete:i:20"]
```

- [ ] **Step 2: 写入 50 ms 慢仓库心跳和 20 次更新上限测试**

创建仅含固定字节块的网关和每次仓库调用 `time.sleep(0.05)` 的假仓库。以 5 ms 心跳记录相邻 tick 最大差值，使用生产默认 0.5 秒合并窗口并快速发送 20 个块。

```python
assert max_gap_ms <= 20.0
assert repository.media_write_calls <= 4
assert repository.terminal_committed is True
```

计数口径：单项开始写、每个批量进度事务和 `complete_item` 各算一次媒体持久化调用；读取和任务状态不计入。

- [ ] **Step 3: 运行测试并确认当前同步调用违反心跳合同**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_downloader.py -k "persistence or slow_repository or terminal" -q
```

Expected: FAIL；当前实现直接同步调用仓库，50 ms 人工延迟使最大心跳超过 20 ms，且产生过多进度写。

- [ ] **Step 4: 注入持久化协议并迁移所有仓库调用**

构造函数新增关键字参数：

```python
persistence: DownloadPersistence | None = None
```

`self.persistence = persistence or ThreadedDownloadPersistence(repository)`，保留 `repository` 供绑定 operation。逐项替换：

- 已存在目标文件验证完成：`execute(complete_item, flush_item_ids=(item.id,))`。
- 磁盘空间不足、下载前暂停、运行中暂停、取消：耐久刷新文件后，`execute(update_item_progress(PAUSED), flush_item_ids=(item.id,))`。
- 下载开始：`execute(update_item_progress(DOWNLOADING), flush_item_ids=(item.id,))` 并等待落盘。
- 普通进度：`await record_progress(ItemProgressUpdate(...DOWNLOADING))`；调用只更新协调器内存。
- 正常完成：先 `os.replace`，再 `execute(complete_item, flush_item_ids=(item.id,))` 并等待提交。

用小型私有 async 辅助方法集中构造 lambda，避免在每个异常分支复制参数和屏障规则。不要改变文件哈希、`BufferedPartWriter`、带宽和空间检查顺序。

- [ ] **Step 5: 运行下载器完整回归**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_downloader.py tests/test_download_io.py tests/test_download_paths.py -q
.\.venv\Scripts\python.exe -m ruff check src/telegram_downloader/downloader.py tests/test_downloader.py
```

Expected: PASS；现有精确 `.part` 字节、哈希、取消和路径安全断言不变，新心跳与写入次数合同通过。

- [ ] **Step 6: 提交下载器迁移**

```powershell
git add src/telegram_downloader/downloader.py tests/test_downloader.py
git commit -m "perf: keep download persistence off event loop"
```

## Task 5: 将 DownloadScheduler 的状态与读取迁移到有序命令

**Files:**

- Modify: `src/telegram_downloader/scheduler.py`
- Modify: `tests/test_scheduler.py`
- Modify: `tests/test_download_queue_stress.py`

- [ ] **Step 1: 将测试期望改为异步入口并增加慢仓库心跳合同**

把测试中的调用迁移为：

```python
accepted = await scheduler.pause_tasks([...])
await scheduler.pause_task(task_id)
assert await scheduler.prioritize_task(task_id) is True
await scheduler.recover()
```

新增人工 50 ms 的 `list_items` / `update_task_status` / `get_item` 仓库，运行一个任务并记录 5 ms 心跳，要求最大间隔不超过 20 ms。

- [ ] **Step 2: 增加顺序、重复完成去除和故障响应测试**

测试必须证明：

- `_set_item_state` 读取媒体前使用该媒体屏障。
- 真正的 `MediaDownloader.complete_item` 已把媒体置为 `COMPLETED` 时，调度器不再追加重复 `COMPLETED` 写。
- 重试顺序为“最新进度批量写 → 媒体 WAITING_RETRY → 任务 WAITING_RETRY → sleep → 任务 DOWNLOADING”。
- 最终 `list_items` / `recompute_task_status` 前使用全局屏障。
- 协调器故障回调同步关闭 `_admission_open`，并设置所有活动任务的 pause event。
- `shutdown()` 即使没有 runner 也调用一次 `drain` 和 `close`；超时取消 runner 后仍执行关闭。

- [ ] **Step 3: 运行聚焦测试并确认同步入口或心跳失败**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_scheduler.py tests/test_download_queue_stress.py -k "pause or prioritize or persistence or retry or shutdown or recover" -q
```

Expected: FAIL；现有同步签名和直接仓库访问不满足新合同。

- [ ] **Step 4: 注入同一持久化协议并迁移全部异步路径**

构造函数新增 `persistence: DownloadPersistence | None = None`，默认线程桥接器只为兼容装配。生产实例由应用显式注入共享协调器。

改造规则：

- `set_schedule_open` 的 `list_paused_by_reason` 经 `execute`。
- `recover` 改为 async，经 `execute(repository.recover_interrupted)`。
- `pause_task`、`pause_tasks` 改为 async；先立即设置 pause flag、移除等待队列并完成等待 future，再等待批量任务状态持久化。
- `resume_tasks` 的批量状态更新经 `execute`。
- `run_items` 的 `get_item`、`_dispatch_key`、`clear_task_priority` 经 `execute`；`_dispatch_key` 改为 async。
- `_execute_task` / `_execute_items` 的任务状态、列表读取和汇总全部经 `execute`；读取终态前使用 `flush_all=True`。
- `_set_item_state` 改为 async，先 `execute(get_item, flush_item_ids=(item_id,))`；若现有状态、字节、错误和重试次数已等于目标值则直接返回，否则用同一媒体屏障写终态。
- `_run_item` 每一处分支都 `await _set_item_state`，任务重试状态也通过 `execute`。
- `shutdown` 使用 `try/finally`，无论活动 runner 是否存在都 `await persistence.drain()` 后 `await persistence.close()`。

设置协调器故障处理器为 `_handle_persistence_fault`。该同步回调只能改内存：关闭接纳、为活动任务设置暂停标记、发布一次无隐私详情的现有失败事件；不能再访问仓库。

- [ ] **Step 5: 运行调度器和压力回归**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_scheduler.py tests/test_download_queue_stress.py tests/test_resource_control.py -q
.\.venv\Scripts\python.exe -m ruff check src/telegram_downloader/scheduler.py tests/test_scheduler.py tests/test_download_queue_stress.py
```

Expected: PASS；队列排序、重复调用去重、并发槽、退避、关闭和通知语义保持不变。

- [ ] **Step 6: 提交调度器迁移**

```powershell
git add src/telegram_downloader/scheduler.py tests/test_scheduler.py tests/test_download_queue_stress.py
git commit -m "perf: serialize scheduler persistence off loop"
```

## Task 6: 更新控制器、应用装配和真实队列生命周期

**Files:**

- Modify: `src/telegram_downloader/controller.py`
- Modify: `src/telegram_downloader/app.py`
- Modify: `tests/test_controller.py`
- Modify: `tests/test_app.py`
- Modify: `tests/test_download_queue_e2e.py`

- [ ] **Step 1: 写入共享协调器和关闭所有权失败测试**

扩展 `test_service_builder_shares_runtime_download_resource_settings`：

```python
assert scheduler.persistence is scheduler.downloader.persistence
assert isinstance(scheduler.persistence, DownloadPersistenceCoordinator)
```

增加服务替换和应用退出测试，记录事件顺序，要求旧 scheduler 关闭时协调器先 `drain` 后 `close`，再断开旧 gateway；新候选服务失败时也关闭其协调器。

- [ ] **Step 2: 写入控制器等待顺序和完整性取消测试**

将 scheduler double 的 `pause_tasks` / `prioritize_task` 改为 `AsyncMock`。断言事件顺序：查询 → 等待持久化 → 刷新。把 `cancel_integrity` 改为 async 后，测试用 `await controller.cancel_integrity()`，并验证每个活动修复任务都已等待 `pause_task`。

- [ ] **Step 3: 写入真实仓库暂停、重启和完成集成测试**

`tests/test_download_queue_e2e.py` 显式创建一个协调器并同时传给下载器、调度器。测试流程保持原有三来源任务与全局槽场景，并将调用更新为：

```python
assert await scheduler.prioritize_task(task_id) is True
await scheduler.pause_task(task_id)
```

关闭后重新打开 `TaskRepository`，断言暂停任务字节数等于 `.part` 大小，完成任务为 `COMPLETED`，没有旧 `DOWNLOADING` 进度覆盖终态。

- [ ] **Step 4: 运行聚焦测试并确认装配尚未共享**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_app.py tests/test_controller.py tests/test_download_queue_e2e.py -k "service_builder or persistence or pause or prioritize or integrity or shutdown or restart" -q
```

Expected: FAIL；应用尚未创建共享协调器，控制器仍按同步签名调用。

- [ ] **Step 5: 更新控制器和 Qt 信号适配**

- `pause_tasks` 中改为 `accepted = await self.scheduler.pause_tasks(eligible)`。
- `prioritize_task` 保留仓库优先级写入的 `asyncio.to_thread`，随后 `await self.scheduler.prioritize_task(task_id)` 读取新的 dispatch key。
- `cancel_integrity` 改为 async，并 `await asyncio.gather(...)` 暂停修复任务。
- `app.py` 中 `integrity_cancel_requested` 改为 `@qasync.asyncSlot()` 的 async 适配器并等待控制器。
- 更新 `_NullScheduler` 的 async 签名及所有测试 doubles。

- [ ] **Step 6: 在生产服务构造中共享协调器**

`build_online_services` 创建：

```python
persistence = DownloadPersistenceCoordinator(repository)
downloader = MediaDownloader(..., persistence=persistence)
scheduler = DownloadScheduler(..., persistence=persistence)
```

不把协调器放入 `OnlineServices`，避免扩展账号服务公共结构；调度器拥有关闭职责。现有账号切换成功、失败回滚与应用退出都已经调用 scheduler shutdown，因此能覆盖每个协调器实例。

- [ ] **Step 7: 运行应用、控制器和真实队列回归**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_app.py tests/test_controller.py tests/test_download_queue_e2e.py tests/test_file_integrity_e2e.py -q
.\.venv\Scripts\python.exe -m ruff check src/telegram_downloader/app.py src/telegram_downloader/controller.py tests/test_app.py tests/test_controller.py tests/test_download_queue_e2e.py
```

Expected: PASS；没有 pending task 警告，账号服务替换和退出均关闭各自协调器。

- [ ] **Step 8: 提交装配和生命周期**

```powershell
git add src/telegram_downloader/app.py src/telegram_downloader/controller.py tests/test_app.py tests/test_controller.py tests/test_download_queue_e2e.py
git commit -m "feat: share download persistence lifecycle"
```

## Task 7: 增加合成性能基准和隐私合同

**Files:**

- Create: `scripts/benchmark_download_persistence.py`
- Create: `tests/ui/test_download_persistence_responsiveness.py`
- Modify: `tests/test_packaging_contract.py`

- [ ] **Step 1: 写入基准脚本隐私失败测试**

在 `tests/test_packaging_contract.py` 增加：

```python
def test_download_persistence_benchmark_is_synthetic_and_private() -> None:
    root = Path(__file__).parents[1]
    script = root / "scripts/benchmark_download_persistence.py"
    source = script.read_text(encoding="utf-8").casefold()
    assert "temporarydirectory" in source
    assert "syntheticrepository" in source
    assert all(
        forbidden not in source
        for forbidden in (
            "telethongateway",
            "taskrepository(",
            "secrets.dat",
            "catalog.sqlite3",
            "tasks.sqlite3",
            "t.me/",
            "api_hash",
        )
    )
```

- [ ] **Step 2: 运行合同并确认脚本不存在**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_packaging_contract.py -k "download_persistence_benchmark" -q
```

Expected: FAIL，基准脚本不存在。

- [ ] **Step 3: 实现可独立执行的合成基准**

脚本只能使用：

- `TemporaryDirectory` 下的 `PortablePaths`。
- 固定 ID、固定 20 个字节块和无网络 `SyntheticGateway`。
- `SyntheticRepository`，每个单项、批量和完成调用固定 `time.sleep(0.05)`。
- 生产 `DownloadPersistenceCoordinator` 与 `MediaDownloader`。
- 5 ms 事件循环心跳和持久化调用计数。

输出稳定机器可读字段：

```text
MAX_EVENT_LOOP_GAP_MS=12.34
MEDIA_PERSISTENCE_WRITES=3
LATEST_DOWNLOADED_BYTES=20
TERMINAL_DURABLE=true
DOWNLOAD_PERSISTENCE_BENCHMARK_OK
```

若最大间隔大于 20 ms、写入大于 4、最终字节不是 20 或终态未提交，脚本返回非零。命令行支持 `--repository-delay-ms`、`--heartbeat-ms`、`--max-gap-ms`，默认分别为 50、5、20；不允许传入数据库、下载根或账号参数。

- [ ] **Step 4: 增加 Qt 与 asyncio 共用循环的合成响应测试**

在 `tests/ui/test_download_persistence_responsiveness.py` 中使用 `create_application(tmp_path)` 返回的 qasync 循环、5 ms `QTimer` 和只记录计数的合成仓库。协调器执行每次 50 ms 的后台仓库动作期间持续处理 Qt timer，断言最大 timer 间隔不超过 20 ms；再触发媒体屏障并断言终态提交后 future 才完成。测试不创建 Telegram gateway、不读取真实配置、不连接网络。

Run:

```powershell
$env:QT_QPA_PLATFORM='offscreen'
.\.venv\Scripts\python.exe -m pytest tests/ui/test_download_persistence_responsiveness.py -q
```

Expected: PASS；Qt 定时器在慢仓库调用期间继续跳动，最大间隔不超过 20 ms。

- [ ] **Step 5: 连续运行七次性能门禁**

Run:

```powershell
1..7 | ForEach-Object { .\.venv\Scripts\python.exe scripts\benchmark_download_persistence.py }
```

Expected: 七次均打印 `DOWNLOAD_PERSISTENCE_BENCHMARK_OK`；每次 `MAX_EVENT_LOOP_GAP_MS <= 20.00`、`MEDIA_PERSISTENCE_WRITES <= 4`、`TERMINAL_DURABLE=true`。

- [ ] **Step 6: 运行隐私合同与静态检查**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_packaging_contract.py -k "download_persistence_benchmark" -q
.\.venv\Scripts\python.exe -m ruff check scripts/benchmark_download_persistence.py tests/ui/test_download_persistence_responsiveness.py tests/test_packaging_contract.py
```

Expected: PASS，脚本没有真实应用数据入口。

- [ ] **Step 7: 提交基准**

```powershell
git add scripts/benchmark_download_persistence.py tests/ui/test_download_persistence_responsiveness.py tests/test_packaging_contract.py
git commit -m "test: gate download persistence responsiveness"
```

## Task 8: 完整回归、代码审查和缺陷修复

**Files:**

- Modify only when a failing regression has a focused reproduction test.

- [ ] **Step 1: 运行所有下载相关测试**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_repository.py tests/test_download_persistence.py tests/test_downloader.py tests/test_scheduler.py tests/test_download_queue_e2e.py tests/test_download_queue_stress.py tests/test_controller.py tests/test_app.py tests/test_file_integrity_e2e.py -q
```

Expected: PASS，无 RuntimeWarning、未取回 task 异常或销毁 pending task。

- [ ] **Step 2: 运行完整自动化与 Ruff**

Run:

```powershell
.\scripts\test.ps1
```

Expected: 完整 Pytest 和 Ruff 均通过。

- [ ] **Step 3: 使用代码审查技能检查实现**

REQUIRED SUB-SKILL: `superpowers:requesting-code-review`。

审查必须覆盖：

- worker 启动/停止和 task 异常回收。
- 固定窗口是否会被更新延期。
- pending 快照、媒体屏障和终态之间的竞态。
- `asyncio.to_thread` 取消保护和关闭等待。
- 同一真实协调器是否被下载器与调度器共享。
- 任务状态、重试状态和读取是否仍有事件循环内同步仓库调用。
- 故障回调是否仅改内存且不泄露隐私。
- 账号服务切换、应用退出和无活动 runner 时是否关闭协调器。

- [ ] **Step 4: 对每个有效问题先写失败测试再修复**

每项修复独立运行对应聚焦测试；拒绝只为满足实现细节而修改测试。审查无问题时不制造改动。

- [ ] **Step 5: 再次运行完整自动化并提交修复**

Run:

```powershell
.\scripts\test.ps1
git diff --check
```

Expected: PASS，`git diff --check` 无输出。若产生修复：

```powershell
git add src tests scripts
git commit -m "fix: harden download persistence coordination"
```

## Task 9: 形成 v0.18.3 本地候选元数据和文档

**Files:**

- Modify: `pyproject.toml`
- Modify: `src/telegram_downloader/__init__.py`
- Modify: `installer/TelegramDownloader.iss`
- Modify: `tests/test_packaging_contract.py`
- Modify: `README.md`
- Create: `docs/releases/v0.18.3.md`
- Create: `docs/verification/v0.18.3-download-persistence-responsiveness.md`

- [ ] **Step 1: 先把版本合同测试改为 v0.18.3**

将 `test_v0182_version_and_runtime_contracts_are_consistent` 重命名为 `test_v0183_version_and_runtime_contracts_are_consistent`，要求三个权威版本源均为 `0.18.3`，release notes 文件存在，并包含以下行为词：

```python
for term in (
    "500 ms",
    "事件循环",
    "批量进度",
    "暂停",
    "完成",
    ".part",
    "兼容性",
    "隐私边界",
):
    assert term in notes
```

- [ ] **Step 2: 运行版本合同并确认旧版本导致失败**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_packaging_contract.py -k "v0183" -q
```

Expected: FAIL，当前权威版本仍为 0.18.2，v0.18.3 notes 不存在。

- [ ] **Step 3: 更新版本、README 和发布说明**

把 `pyproject.toml`、`src/telegram_downloader/__init__.py`、`installer/TelegramDownloader.iss` 同步改为 0.18.3。README 说明：

- 下载进度数据库写入在后台运行。
- 500 ms 内同一媒体只保留最新进度。
- 暂停、失败、重试和完成返回前已耐久提交。
- 强制终止最多损失不足 500 ms 的数据库显示进度，实际恢复继续以 `.part` 文件长度为准。
- 不改变现有任务数据库、下载目录和用户配置。

`docs/releases/v0.18.3.md` 使用产品名“TG 快取”，明确这是响应优化、无数据库迁移、无新权限、无真实数据基准。

- [ ] **Step 4: 建立真实验证记录框架**

`docs/verification/v0.18.3-download-persistence-responsiveness.md` 只记录实际执行结果。包含：分支/提交、聚焦测试、完整测试、Ruff、七次基准逐次结果、三轮构建、包隐私、便携自检、安装/升级/卸载保留、GUI 合成响应、制品大小与 SHA-256、发布边界。未执行项目明确写“未执行”，不写推测数字。

- [ ] **Step 5: 运行版本和文档合同**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_packaging_contract.py tests/test_installer_contract.py tests/test_self_test.py -q
.\.venv\Scripts\python.exe -m ruff check tests/test_packaging_contract.py
git diff --check
```

Expected: PASS；所有版本源一致为 0.18.3。

- [ ] **Step 6: 提交本地候选元数据**

```powershell
git add pyproject.toml src/telegram_downloader/__init__.py installer/TelegramDownloader.iss tests/test_packaging_contract.py README.md docs/releases/v0.18.3.md docs/verification/v0.18.3-download-persistence-responsiveness.md
git commit -m "docs: prepare v0.18.3 responsiveness candidate"
```

## Task 10: 按三轮执行最终验收

**Files:**

- Modify: `docs/verification/v0.18.3-download-persistence-responsiveness.md`

- [ ] **Step 1: 第一轮——源码、完整测试、静态检查和七次基准**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_download_persistence.py tests/test_downloader.py tests/test_scheduler.py tests/test_download_queue_e2e.py -q
.\scripts\test.ps1
1..7 | ForEach-Object { .\.venv\Scripts\python.exe scripts\benchmark_download_persistence.py }
git diff --check
```

Expected: 全部 PASS；七次均满足最大事件循环间隔不超过 20 ms、媒体持久化调用不超过 4、终态耐久为 true。

- [ ] **Step 2: 第二轮——便携包构建、隐私和冻结自检**

先确认项目内 `dist/TelegramDownloader/TelegramDownloader.exe` 没有运行，再执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\smoke.ps1
```

Expected: 构建内完整测试和 Ruff 通过，输出 `PACKAGED_SMOKE_OK`；生成 `dist/TelegramDownloader-0.18.3-win-x64-portable.zip`。检查 ZIP 中不存在 `data/`、`downloads/`、`tasks.sqlite3`、`catalog.sqlite3`、`secrets.dat`、日志、`.part` 或 `.corrupt*`。

在隔离的非系统盘临时目录解压 ZIP，执行冻结 `TelegramDownloader.exe --self-test`，要求版本 0.18.3、组件健康、所有可写路径位于该临时根内。记录 EXE 与 ZIP 的字节数和 SHA-256。

- [ ] **Step 3: 第三轮——安装器、升级/卸载保留和 GUI 合成响应**

Run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build-installer.ps1 -SkipAppBuild
```

Expected: 输出 `INSTALLER_SMOKE_OK`，生成 `dist/release/TelegramDownloader-0.18.3-win-x64-setup.exe`；安装位置为非系统盘，安装、升级和卸载流程保留合成 `data` / `downloads` 哨兵与哈希。

再次运行 `tests/ui/test_download_persistence_responsiveness.py`，确认 50 ms 合成慢仓库下 Qt 定时器仍满足 20 ms 门禁。随后使用安装器创建的隔离非系统盘配置离线启动冻结应用，手动检查窗口移动、页面切换、托盘菜单和退出；不注入测试代码，不接入真实 Telegram 账号或真实下载目录。暂停/完成后的重启读取正确性由第一轮真实临时仓库端到端测试提供证据。

- [ ] **Step 4: 更新验证记录并执行完成前验证技能**

REQUIRED SUB-SKILL: `superpowers:verification-before-completion`。

把实际命令、通过数量、耗时、七次基准数值、smoke 标记、制品大小/哈希和隐私扫描结果写入验证文档。然后重新运行：

```powershell
git status --short
git diff --check
.\scripts\test.ps1
```

Expected: 只有预期验证文档改动，完整测试再次通过。

- [ ] **Step 5: 提交验证证据并停止在本地候选边界**

```powershell
git add docs/verification/v0.18.3-download-persistence-responsiveness.md
git commit -m "docs: record v0.18.3 candidate verification"
git status --short
```

Expected: 工作树干净。向用户报告本地候选分支、提交、三轮结果、制品路径和 SHA-256；明确尚未合并 `main`、打标签或发布在线更新，并等待单独授权。
