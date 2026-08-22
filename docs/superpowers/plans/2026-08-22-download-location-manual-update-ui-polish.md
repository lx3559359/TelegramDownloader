# 下载位置、手动更新与界面可读性优化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让用户通过 Windows 文件夹选择器设置安全的外部媒体目录，统一修复复选框和托盘菜单可读性，改为完全手动更新，并让搜索摘要完整且高效地自动换行。

**Architecture:** `PortablePaths` 继续只保护应用内部数据；新增 `DownloadPathPolicy` 管理当前及历史媒体根目录，并被 Planner、下载器、完整性、打开目录和存储维护共同注入。UI 层新增应用级勾号代理样式和按可见行懒计算的摘要委托；更新协调器保留现有签名下载事务，但入口只来自设置页手动按钮。

**Tech Stack:** Python 3.12、pathlib/os、asyncio、PySide6、qasync、pytest/pytest-asyncio/pytest-qt、Ruff、PyInstaller、Inno Setup。

---

## 实施前约束

- 基线提交：`38800ce`（设计规格已确认）。
- 执行阶段先使用 `superpowers:using-git-worktrees` 创建 `codex/download-location-ui-polish` 隔离工作树。
- 每个任务严格执行 RED → GREEN → 相关回归 → 提交；不要把多个任务压成一个提交。
- 文件夹写入探测、外部媒体根目录和清理测试只能使用 pytest 临时目录或项目内 `.build-temp`，不能触碰真实用户下载目录。
- 本计划不改版本号、不推送、不打标签、不发布。构建仅用于本地第三轮自检。
- 设计规格：`docs/superpowers/specs/2026-08-22-download-location-manual-update-ui-polish-design.md`。

## 文件职责映射

### 新建文件

- `src/telegram_downloader/download_paths.py`：媒体根目录规范化、写入探测、当前/历史根目录、稳定根 ID 和媒体目标守卫。
- `src/telegram_downloader/ui/checkmark_style.py`：应用级复选框与表格勾选 indicator 的白色勾号绘制。
- `src/telegram_downloader/ui/wrapped_text.py`：完整摘要绘制、宽度相关高度缓存和测量计数边界。
- `tests/test_download_paths.py`：路径拒绝、探测、历史根目录、重启和越界测试。
- `tests/test_download_location_e2e.py`：设置迁移、Planner、下载器和旧任务跨根目录端到端测试。
- `tests/ui/test_checkmark_style.py`：选中、未选中、焦点和禁用状态离屏渲染测试。
- `tests/ui/test_wrapped_text.py`：完整换行、缓存失效和只测量可见行测试。
- `docs/verification/2026-08-22-download-location-ui-polish.md`：三轮新鲜验证证据。

### 修改文件

- `src/telegram_downloader/settings.py:15-147`、`tests/test_settings.py`：新增下载存储设置，迁移并停止保存旧自动更新字段。
- `src/telegram_downloader/planner.py:104-124,320-340`、`src/telegram_downloader/downloader.py:96-140,300-316`、`src/telegram_downloader/file_integrity.py:90-280`：使用媒体路径策略并支持运行时切换。
- `src/telegram_downloader/controller.py:430-520,687-693,1765-1793,2047-2110,2586-2607`、`tests/test_controller.py`：应用根目录切换、受信打开路径和手动更新结果。
- `src/telegram_downloader/runtime_settings.py`、`tests/test_runtime_settings.py`：持久化规范化后的新设置，不把路径策略并入应用内部路径守卫。
- `src/telegram_downloader/storage_models.py`、`src/telegram_downloader/storage_inventory.py`、`src/telegram_downloader/storage_cleanup.py`、对应测试：让下载残留条目携带媒体根 ID，并扫描所有受信媒体根。
- `src/telegram_downloader/diagnostic_probes.py`、`tests/test_diagnostic_probes.py`：诊断当前媒体目录，不把外部目录传给 `PortablePaths.guard()`。
- `src/telegram_downloader/ui/settings.py:36-405`、`tests/ui/test_settings_dialog.py`：文件夹浏览、恢复默认、当前版本和手动检查状态。
- `src/telegram_downloader/ui/theme.py:1-233`、`src/telegram_downloader/ui/check_delegate.py`：共享勾号颜色、尺寸和表格勾选一致性。
- `src/telegram_downloader/background.py:141-205`、`tests/test_background.py`：托盘菜单独立浅色样式与调色板。
- `src/telegram_downloader/ui/content_browser.py:58-570`、`tests/ui/test_content_browser.py`：摘要完整换行和可见行懒调整。
- `src/telegram_downloader/ui/main.py:75-235`、`tests/ui/test_main_window.py`：修正侧栏存储说明并安装后的视觉回归。
- `src/telegram_downloader/app.py:360-710,944-990,1047-1056,1408-1422,1505-1525`、`tests/test_app.py`：构造并连接媒体路径策略、文件夹设置和手动更新入口。
- `tests/update/test_update_coordinator.py`：启动零检查、手动防重和结果状态。
- `README.md`：说明媒体目录自定义、完全手动更新和旧任务行为。

## Task 1：设置合同与旧自动更新迁移

**Files:**
- Modify: `src/telegram_downloader/settings.py:15-147`
- Test: `tests/test_settings.py`

- [ ] **Step 1：写下载存储默认值、严格解析和旧字段迁移 RED 测试**

在 `tests/test_settings.py` 增加：

```python
from telegram_downloader.settings import DownloadStorageSettings


def test_download_storage_defaults_to_portable_downloads() -> None:
    assert DownloadStorageSettings() == DownloadStorageSettings("", ())
    assert AppSettings().download_storage == DownloadStorageSettings()


def test_old_auto_update_field_is_ignored_and_not_saved(tmp_path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(
        '{"api_id": 7, "concurrency": 2, "check_updates_on_startup": true}',
        encoding="utf-8",
    )
    store = SettingsStore(path)
    loaded = store.load()
    assert loaded.check_updates_on_startup is False
    store.save(loaded)
    assert "check_updates_on_startup" not in path.read_text(encoding="utf-8")


def test_download_storage_round_trips_root_and_history(tmp_path) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    expected = AppSettings(
        download_storage=DownloadStorageSettings(
            root=r"D:\\Telegram Media",
            trusted_roots=(r"E:\\Old Telegram",),
        )
    )
    store.save(expected)
    assert store.load() == expected


@pytest.mark.parametrize(
    "value",
    (
        {"root": 1},
        {"trusted_roots": "D:/media"},
        {"trusted_roots": ["D:/media", "D:/media"]},
    ),
)
def test_download_storage_rejects_malformed_json(tmp_path, value) -> None:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"download_storage": value}), encoding="utf-8")
    with pytest.raises(SettingsError):
        SettingsStore(path).load()
```

- [ ] **Step 2：运行测试并确认 RED**

Run: `.venv\Scripts\python.exe -m pytest tests/test_settings.py -q`

Expected: `DownloadStorageSettings` 不存在，新增测试失败。

- [ ] **Step 3：实现嵌套设置和兼容序列化**

在 `settings.py` 增加并接入：

```python
@dataclass(frozen=True, slots=True)
class DownloadStorageSettings:
    root: str = ""
    trusted_roots: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.root, str):
            raise SettingsError("下载根目录必须是文本")
        roots = self.trusted_roots
        if not isinstance(roots, (tuple, list)) or any(
            not isinstance(value, str) or not value.strip() for value in roots
        ):
            raise SettingsError("历史下载根目录格式无效")
        normalized = tuple(value.strip() for value in roots)
        if len({value.casefold() for value in normalized}) != len(normalized):
            raise SettingsError("历史下载根目录不能重复")
        object.__setattr__(self, "root", self.root.strip())
        object.__setattr__(self, "trusted_roots", normalized)
```

给 `AppSettings` 增加 `download_storage: DownloadStorageSettings = DownloadStorageSettings()`；保留 `check_updates_on_startup` 一个兼容周期，但默认固定为 `False`，运行时不再读取。

`SettingsStore.load()` 必须：

```python
storage_raw = raw.get("download_storage", {})
if not isinstance(storage_raw, dict):
    raise SettingsError("下载存储设置必须是对象")
values = dict(raw)
values["check_updates_on_startup"] = False
values["download_storage"] = DownloadStorageSettings(**storage_raw)
```

`SettingsStore.save()` 必须在 `asdict()` 后删除兼容字段：

```python
payload = asdict(settings)
payload.pop("check_updates_on_startup", None)
content = (
    json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
).encode("utf-8")
_atomic_write(self.path, content)
```

- [ ] **Step 4：运行设置回归并提交**

Run: `.venv\Scripts\python.exe -m pytest tests/test_settings.py tests/test_runtime_settings.py -q`

Expected: 全部通过，保存后的 JSON 不含自动检查字段。

```powershell
git add src/telegram_downloader/settings.py tests/test_settings.py tests/test_runtime_settings.py
git commit -m "feat: define custom download storage settings"
```

## Task 2：独立媒体路径策略

**Files:**
- Create: `src/telegram_downloader/download_paths.py`
- Create: `tests/test_download_paths.py`
- Modify: `tests/test_paths.py`

- [ ] **Step 1：写路径边界、写入探测和历史根目录 RED 测试**

核心测试：

```python
def test_policy_defaults_to_portable_downloads(tmp_path) -> None:
    paths = PortablePaths(tmp_path / "app")
    paths.ensure_layout()
    policy = DownloadPathPolicy(paths, DownloadStorageSettings())
    assert policy.current_root == paths.downloads.resolve()
    assert policy.guard(paths.downloads / "a.bin") == (paths.downloads / "a.bin").resolve()


def test_prepare_switches_root_and_trusts_previous_root(tmp_path) -> None:
    paths = PortablePaths(tmp_path / "app")
    paths.ensure_layout()
    external = tmp_path / "external"
    external.mkdir()
    policy = DownloadPathPolicy(paths, DownloadStorageSettings())
    prepared = policy.prepare(DownloadStorageSettings(str(external)))
    assert prepared.root == str(external.resolve())
    assert str(paths.downloads.resolve()) in prepared.trusted_roots
    policy.apply(prepared)
    assert policy.guard(external / "new.bin") == (external / "new.bin").resolve()
    assert policy.guard(paths.downloads / "old.bin") == (paths.downloads / "old.bin").resolve()


@pytest.mark.parametrize("relative", ("escape.bin", "other/file.bin"))
def test_policy_rejects_targets_outside_trusted_roots(tmp_path, relative) -> None:
    paths = PortablePaths(tmp_path / "app")
    paths.ensure_layout()
    policy = DownloadPathPolicy(paths, DownloadStorageSettings())
    with pytest.raises(DownloadPathError):
        policy.guard(tmp_path / relative)


def test_prepare_rejects_application_data_and_filesystem_root(tmp_path) -> None:
    paths = PortablePaths(tmp_path / "app")
    paths.ensure_layout()
    policy = DownloadPathPolicy(paths, DownloadStorageSettings())
    with pytest.raises(DownloadPathError):
        policy.prepare(DownloadStorageSettings(str(paths.data)))
    with pytest.raises(DownloadPathError):
        policy.prepare(DownloadStorageSettings(Path(tmp_path.anchor).as_posix()))
```

另用可注入 `probe` 写不可写、探测文件创建失败和清理失败测试；用 `policy.root_id()`/`root_for_id()` 测稳定 ID、大小写去重和重启恢复。

- [ ] **Step 2：运行并确认 RED**

Run: `.venv\Scripts\python.exe -m pytest tests/test_download_paths.py tests/test_paths.py -q`

Expected: 新模块缺失。

- [ ] **Step 3：实现 `DownloadPathPolicy`**

模块公开接口固定为：

```python
import hashlib
import os
from collections.abc import Callable, Iterable
from pathlib import Path
from uuid import uuid4

from telegram_downloader.paths import PortablePaths
from telegram_downloader.settings import DownloadStorageSettings


class DownloadPathError(ValueError):
    pass


class DownloadPathPolicy:
    def __init__(
        self,
        paths: PortablePaths,
        settings: DownloadStorageSettings,
        *,
        probe: Callable[[Path], None] | None = None,
    ) -> None:
        self.paths = paths
        self.default_root = paths.downloads.resolve()
        self._probe = probe or probe_writable_directory
        self._current_root = self.default_root
        self._roots: dict[str, Path] = {}
        self.apply(settings)

    @property
    def current_root(self) -> Path:
        return self._current_root

    @property
    def roots(self) -> tuple[Path, ...]:
        return tuple(self._roots.values())

    def prepare(self, requested: DownloadStorageSettings) -> DownloadStorageSettings:
        if not isinstance(requested, DownloadStorageSettings):
            raise DownloadPathError("下载存储设置无效")
        selected = self._resolve_setting_root(requested.root)
        self._validate_root(selected)
        self._probe(selected)
        history = [*requested.trusted_roots]
        if selected != self._current_root:
            history.append(str(self._current_root))
        normalized_history = self._normalized_unique_roots(history, exclude=selected)
        saved_root = "" if selected == self.default_root else str(selected)
        return DownloadStorageSettings(saved_root, tuple(map(str, normalized_history)))

    def apply(self, settings: DownloadStorageSettings) -> None:
        if not isinstance(settings, DownloadStorageSettings):
            raise DownloadPathError("下载存储设置无效")
        current = self._resolve_setting_root(settings.root)
        self._validate_root(current)
        history = self._normalized_unique_roots(settings.trusted_roots, exclude=current)
        ordered = (self.default_root, *history, current)
        self._roots = {}
        for root in ordered:
            self._roots[self.root_id(root)] = root
        self._current_root = current

    def require_current_writable(self) -> Path:
        self._probe(self._current_root)
        return self._current_root

    def guard(self, candidate: Path, *, allow_root: bool = False) -> Path:
        resolved = Path(candidate).resolve()
        for root in self.roots:
            try:
                return self.guard_in(root, resolved, allow_root=allow_root)
            except DownloadPathError:
                continue
        raise DownloadPathError(f"媒体路径超出受信下载目录: {resolved}")

    def guard_in(
        self,
        root: Path,
        candidate: Path,
        *,
        allow_root: bool = False,
    ) -> Path:
        trusted = Path(root).resolve()
        if self.root_id(trusted) not in self._roots:
            raise DownloadPathError("下载根目录不受信")
        resolved = Path(candidate).resolve()
        try:
            relative = resolved.relative_to(trusted)
        except ValueError as exc:
            raise DownloadPathError("媒体路径超出指定下载目录") from exc
        if not relative.parts and not allow_root:
            raise DownloadPathError("媒体文件目标不能是下载根目录本身")
        return resolved

    def root_id(self, root: Path) -> str:
        normalized = os.path.normcase(str(Path(root).resolve())).encode("utf-8")
        return f"download-{hashlib.sha256(normalized).hexdigest()[:16]}"

    def root_for_id(self, root_id: str) -> Path:
        try:
            return self._roots[root_id]
        except KeyError as exc:
            raise DownloadPathError("下载根目录标识不受信") from exc

    def _resolve_setting_root(self, value: str) -> Path:
        if not value:
            return self.default_root
        candidate = Path(value)
        if not candidate.is_absolute():
            raise DownloadPathError("下载根目录必须是绝对路径")
        return candidate.resolve()

    def _validate_root(self, root: Path) -> None:
        anchor = Path(root.anchor).resolve()
        if root == anchor or root == self.paths.root:
            raise DownloadPathError("不能使用磁盘、共享或应用根目录")
        if root == self.paths.data or root.is_relative_to(self.paths.data):
            raise DownloadPathError("下载目录不能位于应用内部数据目录")

    def _normalized_unique_roots(
        self,
        values: Iterable[str | Path],
        *,
        exclude: Path,
    ) -> tuple[Path, ...]:
        result: list[Path] = []
        seen = {os.path.normcase(str(exclude))}
        for value in values:
            root = self._resolve_setting_root(str(value))
            self._validate_root(root)
            key = os.path.normcase(str(root))
            if key in seen:
                continue
            seen.add(key)
            result.append(root)
        return tuple(result)
```

实现规则：空 `root` 映射到 `paths.downloads`；所有根使用 `resolve()` 和 `os.path.normcase()` 去重；`prepare()` 先验证绝对路径、拒绝 `Path(anchor)`、应用根和 `paths.data` 子树，再执行探测；切换时把旧当前根加入历史；`apply()` 只应用已经准备好的不可变设置，不执行 I/O。

默认探测器使用独占临时文件：

```python
def probe_writable_directory(root: Path) -> None:
    if not root.is_dir():
        raise DownloadPathError("下载根目录不存在")
    target = root / f".telegram-downloader-write-{uuid4().hex}.tmp"
    try:
        with target.open("xb") as stream:
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        raise DownloadPathError("下载根目录当前不可写") from exc
    try:
        target.unlink()
    except OSError as exc:
        raise DownloadPathError("下载目录写入探测文件无法清理") from exc
```

根 ID 使用规范化绝对路径的 SHA-256 前 16 位，格式 `download-<16 hex>`；`guard()` 只接受任一受信根的真子路径，`allow_root=True` 才允许根本身。

- [ ] **Step 4：运行并提交**

Run: `.venv\Scripts\python.exe -m pytest tests/test_download_paths.py tests/test_paths.py -q`

Expected: 全部通过，`PortablePaths.guard()` 的原项目内测试保持不变。

```powershell
git add src/telegram_downloader/download_paths.py tests/test_download_paths.py tests/test_paths.py
git commit -m "feat: guard trusted media download roots"
```

## Task 3：Planner、下载、完整性与打开目录接入

**Files:**
- Modify: `src/telegram_downloader/planner.py:104-124,320-340`
- Modify: `src/telegram_downloader/downloader.py:96-140,300-316`
- Modify: `src/telegram_downloader/file_integrity.py:90-280`
- Modify: `src/telegram_downloader/controller.py:430-520,1765-1793,2047-2110`
- Modify: `src/telegram_downloader/app.py:375-710,1505-1525`
- Test: `tests/test_planner.py`
- Test: `tests/test_downloader.py`
- Test: `tests/test_file_integrity.py`
- Test: `tests/test_controller.py`
- Create: `tests/test_download_location_e2e.py`

- [ ] **Step 1：写新任务换根、旧任务续传和越界拒绝 RED 测试**

新增断言：

```python
def test_planner_configure_downloads_changes_only_future_targets(tmp_path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    now = datetime(2026, 8, 22, tzinfo=UTC)
    base = RemoteMedia(
        "peer",
        "来源",
        1,
        None,
        "media-1",
        MediaKind.VIDEO,
        "clip.mp4",
        3,
        now,
    )
    planner = TaskPlanner(FakeGateway([]), FakeRepository(), first)
    query = ContentSearchQuery(
        "测试",
        ScanFilters(now, now, frozenset({MediaKind.VIDEO}), 10),
    )
    old = planner.plan_selected("peer", "来源", query, [base]).items[0].target_path
    planner.configure_downloads(second, DownloadNamingSettings())
    new = planner.plan_selected(
        "peer",
        "来源",
        query,
        [replace(base, message_id=2, media_id="media-2")],
    ).items[0].target_path
    assert Path(old).is_relative_to(first)
    assert Path(new).is_relative_to(second)


@pytest.mark.asyncio
async def test_downloader_accepts_old_and_current_trusted_roots(tmp_path) -> None:
    paths = PortablePaths(tmp_path / "app")
    paths.ensure_layout()
    external = tmp_path / "external"
    external.mkdir()
    policy = DownloadPathPolicy(paths, DownloadStorageSettings())
    prepared = policy.prepare(DownloadStorageSettings(str(external)))
    policy.apply(prepared)
    media = downloader(
        paths,
        FakeGateway([b"abc"]),
        FakeRepository(),
        download_paths=policy,
    )
    await media.download(item(paths.downloads / "old.bin", size=3))
    await media.download(item(external / "new.bin", size=3))
    assert (paths.downloads / "old.bin").exists()
    assert (external / "new.bin").exists()
```

`tests/test_download_location_e2e.py` 保存自定义设置、重建 `DownloadPathPolicy`、保留一个默认目录旧任务并创建一个外部目录新任务，断言两者都能完成且未知第三目录被拒绝。
`tests/test_app.py` 另覆盖：结构不安全的保存路径只恢复下载设置并给出启动提示；合法但已离线的外部路径保持原配置，直到 Planner 预检时返回“下载根目录当前不可写”。

- [ ] **Step 2：运行并确认 RED**

Run: `.venv\Scripts\python.exe -m pytest tests/test_planner.py tests/test_downloader.py tests/test_file_integrity.py tests/test_controller.py tests/test_download_location_e2e.py -q`

Expected: `configure_downloads`、`download_paths` 注入和新端到端模块失败。

- [ ] **Step 3：接入媒体守卫并保留应用守卫**

`TaskPlanner` 增加：

```python
# __init__ 新增关键字参数并保存；现有调用不传时保持原行为
download_root_provider: Callable[[], Path] | None = None
self._download_root_provider = download_root_provider


def configure_downloads(
    self,
    downloads: Path,
    naming: DownloadNamingSettings,
) -> None:
    if not isinstance(naming, DownloadNamingSettings):
        raise ValueError("下载命名设置无效")
    self.downloads = downloads.resolve()
    self.naming = naming


def configure_naming(self, naming: DownloadNamingSettings) -> None:
    self.configure_downloads(self.downloads, naming)


def _available_download_root(self) -> Path:
    root = (
        self._download_root_provider()
        if self._download_root_provider is not None
        else self.downloads
    )
    resolved = root.resolve()
    if resolved != self.downloads:
        raise ValueError("下载根目录设置与运行时策略不一致")
    return resolved
```

构造 Planner 时注入 `download_root_provider=download_paths.require_current_writable`；`_build_preview()` 开始时只调用一次 `_available_download_root()`，并把局部 `downloads` 用于该批所有 `render_download_target()`，不能在媒体循环内重复写入探测。

`MediaDownloader` 和 `FileIntegrityService` 增加关键字参数 `download_paths`，默认回退现有 `PortablePaths` 以保持单元测试构造兼容；所有媒体目标、`.part` 和 `.corrupt` 改走 `self.download_paths.guard()`，诊断、更新和应用数据继续走 `PortablePaths.guard()`。

- [ ] **Step 4：在 app/controller 原子应用规范化设置**

加载设置后构造一次：

```python
download_paths = DownloadPathPolicy(paths, settings.download_storage)
```

构造时若保存值是相对路径、盘根或应用内部数据目录，捕获 `DownloadPathError`，只把 `download_storage` 替换为默认值并记录启动状态“下载目录设置不安全，已恢复默认”；其他设置不丢失。合法但当前离线/不存在的外部路径不会在构造阶段探测或回退。

把同一实例注入 Planner 根提供器、下载器、完整性服务、Controller、存储服务和诊断。`AppController.for_test()` 在调用方没有注入策略时，使用测试 `PortablePaths` 和当前设置构造默认策略，保持现有测试工厂可用。`AppController.apply_settings()` 在持久化前执行：

```python
prepared_storage = await asyncio.to_thread(
    self.download_paths.prepare,
    settings.download_storage,
)
settings = replace(settings, download_storage=prepared_storage)
await self.runtime_settings_effects.apply(previous_settings, settings)
# vault 成功后以下操作均为内存内、不可失败的已验证应用
self.download_paths.apply(settings.download_storage)
self.planner.configure_downloads(
    self.download_paths.current_root,
    settings.download_naming,
)
```

若 vault 保存失败，沿用现有反向 `runtime_settings_effects.apply(settings, previous_settings)`；此时路径策略尚未 `apply()`，无需额外回滚。

`open_media_file()`、`open_task_directory()` 和托盘 `open_downloads()` 改用媒体策略；打开当前根时传 `allow_root=True`，任务公共父目录必须属于任一受信根。

- [ ] **Step 5：运行并提交**

Run: `.venv\Scripts\python.exe -m pytest tests/test_planner.py tests/test_downloader.py tests/test_file_integrity.py tests/test_controller.py tests/test_download_location_e2e.py -q`

Expected: 新旧根任务通过，第三目录和符号链接越界仍失败。

```powershell
git add src/telegram_downloader/planner.py src/telegram_downloader/downloader.py src/telegram_downloader/file_integrity.py src/telegram_downloader/controller.py src/telegram_downloader/app.py tests/test_planner.py tests/test_downloader.py tests/test_file_integrity.py tests/test_controller.py tests/test_download_location_e2e.py
git commit -m "feat: apply custom roots across media operations"
```

## Task 4：存储维护与诊断支持多媒体根

**Files:**
- Modify: `src/telegram_downloader/storage_models.py`
- Modify: `src/telegram_downloader/storage_inventory.py`
- Modify: `src/telegram_downloader/storage_cleanup.py`
- Modify: `src/telegram_downloader/diagnostic_probes.py`
- Modify: `src/telegram_downloader/app.py`
- Test: `tests/test_storage_models.py`
- Test: `tests/test_storage_inventory.py`
- Test: `tests/test_storage_cleanup.py`
- Test: `tests/test_storage_maintenance_e2e.py`
- Test: `tests/test_diagnostic_probes.py`

- [ ] **Step 1：写外部 `.part` 扫描、根 ID 重验和诊断 RED 测试**

```python
def custom_policy(tmp_path) -> tuple[PortablePaths, DownloadPathPolicy]:
    paths = PortablePaths(tmp_path / "app")
    paths.ensure_layout()
    old_root = tmp_path / "old-media"
    current_root = tmp_path / "current-media"
    old_root.mkdir()
    current_root.mkdir()
    policy = DownloadPathPolicy(paths, DownloadStorageSettings())
    old_settings = policy.prepare(DownloadStorageSettings(str(old_root)))
    policy.apply(old_settings)
    current_settings = policy.prepare(
        DownloadStorageSettings(str(current_root), old_settings.trusted_roots)
    )
    policy.apply(current_settings)
    return paths, policy


def test_inventory_scans_download_leftovers_in_every_trusted_root(tmp_path) -> None:
    paths, policy = custom_policy(tmp_path)
    repository = TaskRepository(paths.database)
    repository.initialize()
    old_root, current_root = policy.roots[-2:]
    (old_root / "old.bin.part").write_bytes(b"old")
    (current_root / "new.bin.part").write_bytes(b"new")
    inventory = StorageInventoryService(
        paths,
        repository,
        download_paths=policy,
    ).scan_download_candidates(
        datetime(2026, 8, 22, tzinfo=UTC),
        active_paths=(),
    )
    entries = [item for item in inventory.entries if item.category is StorageCategory.DOWNLOAD_PART]
    assert {item.root_id for item in entries} == {
        policy.root_id(old_root),
        policy.root_id(current_root),
    }


def test_cleanup_rejects_entry_when_root_id_and_relative_path_disagree(tmp_path) -> None:
    paths, policy = custom_policy(tmp_path)
    target = policy.current_root / "file.bin.part"
    target.write_bytes(b"part")
    target_stat = target.stat(follow_symlinks=False)
    entry = StorageEntry(
        id="forged-root",
        relative_path=PurePosixPath("file.bin.part"),
        category=StorageCategory.DOWNLOAD_PART,
        size=target_stat.st_size,
        mtime_ns=target_stat.st_mtime_ns,
        selectable=True,
        root_id="download-0000000000000000",
    )
    plan = StorageCleanupPlan(
        "manual-forged-root",
        NOW,
        StorageTrigger.MANUAL_DOWNLOAD,
        (entry,),
    )
    cleanup = StorageCleanupExecutor(
        paths,
        repository=None,
        update_protection=SnapshotProvider(),
        download_paths=policy,
        utc_clock=lambda: NOW,
    )
    result = cleanup.execute(plan)
    assert result.items[0].code is StorageResultCode.UNSAFE_PATH
```

把同一个 `custom_policy()` fixture helper 分别放入 `tests/test_storage_inventory.py` 和 `tests/test_storage_cleanup.py`；清理文件沿用该文件现有的 `SnapshotProvider` 与 `NOW`，扫描文件使用真实临时 `TaskRepository`。

诊断测试要求 `managed_writable_paths(paths, download_paths=policy)["downloads"]` 等于当前外部根，且内部路径仍由 `PortablePaths.guard()` 验证。

- [ ] **Step 2：运行并确认 RED**

Run: `.venv\Scripts\python.exe -m pytest tests/test_storage_models.py tests/test_storage_inventory.py tests/test_storage_cleanup.py tests/test_storage_maintenance_e2e.py tests/test_diagnostic_probes.py -q`

Expected: `root_id` 和 `download_paths` 参数缺失。

- [ ] **Step 3：给下载残留增加根 ID**

给 `StorageEntry` 最后增加兼容默认字段：

```python
root_id: str = "app"
```

验证只接受 `app` 或正则 `download-[0-9a-f]{16}`。`storage_entry_id()` 改成：

```python
def storage_entry_id(
    category: StorageCategory,
    relative: PurePosixPath,
    root_id: str = "app",
) -> str:
    payload = f"{root_id}\0{category.value}\0{relative.as_posix()}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
```

`_FileRecord` 最后增加 `root_id: str = "app"`；`_selectable_entry()`、`_download_entry()` 和 `_unsafe_entry()` 都把 record/root 的 ID 复制到 `StorageEntry`。应用内部五类条目继续使用 `app` 和相对 `paths.root` 的路径；下载分片与损坏留档使用 `policy.root_id(root)` 和相对该媒体根的路径。

`StorageInventoryService._walk_download_candidates()` 遍历 `download_paths.roots`，每个根分别使用 `guard_in(root, candidate, allow_root=True)`，不跟随 reparse point。下载残留清单的 `disk_free_bytes` 使用当前媒体根的磁盘值；应用内部自动清理清单仍使用应用根磁盘值。

- [ ] **Step 4：执行前按类别选择守卫**

`StorageCleanupExecutor` 对下载类别执行：

```python
root = self.download_paths.root_for_id(entry.root_id)
target = self.download_paths.guard_in(root, root / entry.relative_path)
category_root = root
```

其他类别要求 `entry.root_id == "app"`，继续使用 `paths.root / relative_path` 和 `paths.guard()`。从 `category_root` 开始逐段 `stat(follow_symlinks=False)`，不能再假定遍历从应用根开始。

诊断 API 增加可选 `download_paths`；`downloads` 键用媒体策略探测，其他键沿用应用守卫。app 的健康诊断注入当前共享策略。

- [ ] **Step 5：运行并提交**

Run: `.venv\Scripts\python.exe -m pytest tests/test_storage_models.py tests/test_storage_inventory.py tests/test_storage_cleanup.py tests/test_storage_maintenance.py tests/test_storage_maintenance_e2e.py tests/test_diagnostic_probes.py -q`

Expected: 所有受信根可扫描/清理，伪造根 ID、变化文件和 reparse point 继续被拒绝。

```powershell
git add src/telegram_downloader/storage_models.py src/telegram_downloader/storage_inventory.py src/telegram_downloader/storage_cleanup.py src/telegram_downloader/diagnostic_probes.py src/telegram_downloader/app.py tests/test_storage_models.py tests/test_storage_inventory.py tests/test_storage_cleanup.py tests/test_storage_maintenance_e2e.py tests/test_diagnostic_probes.py
git commit -m "feat: maintain media across trusted roots"
```

## Task 5：设置页系统文件夹选择与侧栏说明

**Files:**
- Modify: `src/telegram_downloader/ui/settings.py:36-405`
- Modify: `src/telegram_downloader/ui/main.py:229-235`
- Modify: `src/telegram_downloader/app.py:944-990`
- Test: `tests/ui/test_settings_dialog.py`
- Test: `tests/ui/test_main_window.py`

- [ ] **Step 1：写只读路径、浏览、取消、恢复默认和预览 RED 测试**

```python
def test_download_root_uses_folder_browser_and_round_trips(qtbot, monkeypatch, tmp_path) -> None:
    default = tmp_path / "app" / "downloads"
    selected = tmp_path / "media"
    default.mkdir(parents=True)
    selected.mkdir()
    dialog = SettingsDialog(AppSettings(), default_download_root=default)
    qtbot.addWidget(dialog)
    assert dialog.download_root.isReadOnly() is True
    monkeypatch.setattr(
        QFileDialog,
        "getExistingDirectory",
        lambda *_args, **_kwargs: str(selected),
    )
    qtbot.mouseClick(dialog.browse_download_root_button, Qt.MouseButton.LeftButton)
    assert dialog.download_root.text() == str(selected.resolve())
    assert dialog.values().download_storage.root == str(selected.resolve())
    assert str(selected.resolve()) in dialog.naming_preview.text()


def test_cancel_folder_browser_keeps_value_and_reset_restores_default(
    qtbot, monkeypatch, tmp_path
) -> None:
    default = tmp_path / "downloads"
    default.mkdir()
    dialog = SettingsDialog(AppSettings(), default_download_root=default)
    qtbot.addWidget(dialog)
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *_a, **_k: "")
    qtbot.mouseClick(dialog.browse_download_root_button, Qt.MouseButton.LeftButton)
    assert dialog.download_root.text() == str(default.resolve())
    qtbot.mouseClick(dialog.reset_download_root_button, Qt.MouseButton.LeftButton)
    assert dialog.values().download_storage.root == ""
```

再用 `AppSettings(storage_maintenance=StorageMaintenanceSettings(automatic_enabled=True))` 构造对话框，断言 `dialog.values().storage_maintenance` 原样保留。主窗口测试断言侧栏包含“媒体目录可自定义”，不再包含“数据不离开应用目录”。

- [ ] **Step 2：运行并确认 RED**

Run: `.venv\Scripts\python.exe -m pytest tests/ui/test_settings_dialog.py tests/ui/test_main_window.py -q`

Expected: 新控件和构造参数缺失。

- [ ] **Step 3：实现原位增强布局**

`SettingsDialog` 增加 `default_download_root: Path | None = None` 参数；测试或兼容调用未传值时使用 `Path("downloads").resolve()`，app 始终传 `paths.downloads`。在“下载路径”页模板之前创建：

```python
self.download_root = QLineEdit(str(selected_root))
self.download_root.setReadOnly(True)
self.browse_download_root_button = QPushButton("浏览…")
self.reset_download_root_button = QPushButton("恢复默认")
root_row = QHBoxLayout()
root_row.addWidget(self.download_root, 1)
root_row.addWidget(self.browse_download_root_button)
root_row.addWidget(self.reset_download_root_button)
naming_form.addRow("下载根目录", root_row)
```

浏览方法只接受 `QFileDialog.getExistingDirectory()` 的非空结果并 `Path(value).resolve()`；恢复默认设置文本并记录空配置值。`values()` 构造 `DownloadStorageSettings(root, self._settings.download_storage.trusted_roots)`，并显式复制 `storage_maintenance=self._settings.storage_maintenance`，避免保存普通设置时重置维护策略；模板标签改为“目录组织模板（高级）”。预览直接显示当前绝对根目录拼接的安全结果。

app 打开设置时传 `paths.downloads`；实际写入探测仍由 Controller 保存路径完成。侧栏改为：

```python
privacy = QLabel("本地存储\n应用数据保存在本机\n媒体目录可自定义")
```

- [ ] **Step 4：运行并提交**

Run: `.venv\Scripts\python.exe -m pytest tests/ui/test_settings_dialog.py tests/ui/test_main_window.py tests/test_controller.py -q`

Expected: 浏览、取消、恢复默认和保存失败不关闭设置窗口全部通过。

```powershell
git add src/telegram_downloader/ui/settings.py src/telegram_downloader/ui/main.py src/telegram_downloader/app.py tests/ui/test_settings_dialog.py tests/ui/test_main_window.py tests/test_controller.py
git commit -m "feat: browse for media download folders"
```

## Task 6：完全手动更新入口

**Files:**
- Modify: `src/telegram_downloader/ui/settings.py:90-140,270-405`
- Modify: `src/telegram_downloader/controller.py:687-693,2586-2607`
- Modify: `src/telegram_downloader/app.py:944-990,1408-1422`
- Test: `tests/ui/test_settings_dialog.py`
- Test: `tests/update/test_update_coordinator.py`
- Test: `tests/test_app.py`

- [ ] **Step 1：写启动零检查、按钮状态和通知路由 RED 测试**

把旧启动检查测试改为：

```python
@pytest.mark.asyncio
async def test_controller_start_never_checks_for_updates() -> None:
    calls = 0

    class Coordinator:
        async def startup(self, _prompt, _shutdown):
            nonlocal calls
            calls += 1

    controller = AppController.for_test(update_coordinator=Coordinator())
    await controller.start(background=False)
    await asyncio.sleep(0)
    assert calls == 0
    await controller.shutdown()
```

设置 UI 测试：

```python
def test_manual_update_button_emits_and_reports_result(qtbot) -> None:
    dialog = SettingsDialog(AppSettings(), application_version="0.15.0")
    qtbot.addWidget(dialog)
    assert not hasattr(dialog, "check_updates")
    assert "0.15.0" in dialog.update_version_label.text()
    with qtbot.waitSignal(dialog.update_check_requested, timeout=500):
        qtbot.mouseClick(dialog.update_check_button, Qt.MouseButton.LeftButton)
    dialog.set_update_busy(True)
    assert dialog.update_check_button.isEnabled() is False
    assert "正在检查" in dialog.update_check_button.text()
    dialog.set_update_result("当前已是最新正式版")
    assert dialog.update_status_label.text() == "当前已是最新正式版"
```

app 测试断言 `NotificationRoute.UPDATE` 触发 `window.settings_requested`，不直接调用 coordinator。

- [ ] **Step 2：运行并确认 RED**

Run: `.venv\Scripts\python.exe -m pytest tests/update/test_update_coordinator.py tests/ui/test_settings_dialog.py tests/test_app.py -q`

Expected: Controller 仍在启动检查，设置页仍是复选框。

- [ ] **Step 3：实现可等待且防重的手动结果**

删除 `AppController.start()` 中启动检查分支。让 `_run_update_check()` 返回 `UpdateStartupResult`：

```python
async def _run_update_check(self) -> UpdateStartupResult:
    try:
        result = await self.update_coordinator.startup(
            self.update_prompt,
            self.update_shutdown,
        )
        if result is UpdateStartupResult.NO_UPDATE:
            self._show_status("当前已是最新正式版")
        elif result is UpdateStartupResult.BLOCKED:
            self._show_status("更新检查暂不可用，已继续使用当前版本")
        return result
    except MaintenanceBusyError as error:
        self._show_status(str(error))
        return UpdateStartupResult.BLOCKED
    except Exception as error:
        self._show_status(f"更新检查失败（{type(error).__name__}）")
        raise
```

`check_for_updates()` 继续返回同一个后台 Task，保留现有防重测试。

- [ ] **Step 4：连接设置页异步动作**

用版本标签、状态标签和 `检查更新` 按钮替换自动检查复选框；构造参数增加 `application_version: str = __version__`，并增加 `update_check_requested = Signal()`、`set_update_busy()` 和 `set_update_result()`。

`open_settings()` 内连接：

```python
async def check_updates() -> None:
    task = controller.check_for_updates()
    if task is None:
        dialog.set_update_result("更新检查当前不可用")
        return
    result = await task
    messages = {
        UpdateStartupResult.NO_UPDATE: "当前已是最新正式版",
        UpdateStartupResult.BLOCKED: "更新检查暂不可用，请稍后重试",
        UpdateStartupResult.DECLINED: "已取消本次更新",
        UpdateStartupResult.LAUNCHED: "正在安装更新并重新启动",
    }
    dialog.set_update_result(messages[result])
```

通过 `AsyncActionBridge` 设置 started/failed/finished 钩子，保证按钮恢复。更新确认框的 parent 使用 `QApplication.activeModalWidget() or window`，避免被设置模态框遮挡。通知 UPDATE 路由改成 `window.settings_requested.emit`。

- [ ] **Step 5：运行并提交**

Run: `.venv\Scripts\python.exe -m pytest tests/update/test_update_coordinator.py tests/ui/test_settings_dialog.py tests/test_app.py tests/test_controller.py -q`

Expected: 所有启动路径更新调用数为 0；只有按钮触发检查，取消不下载，接受继续通过现有签名事务。

```powershell
git add src/telegram_downloader/ui/settings.py src/telegram_downloader/controller.py src/telegram_downloader/app.py tests/ui/test_settings_dialog.py tests/update/test_update_coordinator.py tests/test_app.py tests/test_controller.py
git commit -m "feat: make application updates fully manual"
```

## Task 7：应用级白色勾号样式

**Files:**
- Create: `src/telegram_downloader/ui/checkmark_style.py`
- Create: `tests/ui/test_checkmark_style.py`
- Modify: `src/telegram_downloader/ui/theme.py:150-170`
- Modify: `src/telegram_downloader/ui/check_delegate.py`
- Modify: `src/telegram_downloader/app.py:375-385`

- [ ] **Step 1：写选中/未选中/禁用离屏渲染 RED 测试**

```python
def render_indicator(style, state: QStyle.StateFlag) -> QImage:
    image = QImage(24, 24, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    option = QStyleOptionButton()
    option.rect = QRect(3, 3, 18, 18)
    option.state = state
    style.drawPrimitive(QStyle.PrimitiveElement.PE_IndicatorCheckBox, option, painter)
    painter.end()
    return image


def test_checked_indicator_contains_brand_fill_and_white_check(qapp) -> None:
    style = CheckmarkProxyStyle(qapp.style())
    image = render_indicator(
        style,
        QStyle.StateFlag.State_Enabled | QStyle.StateFlag.State_On,
    )
    colors = [image.pixelColor(x, y) for x in range(24) for y in range(24)]
    assert sum(color.red() < 40 and color.green() > 130 for color in colors) > 20
    assert sum(min(color.red(), color.green(), color.blue()) > 235 for color in colors) >= 4
```

另断言未选中没有品牌填充、禁用选中不是纯青色、两次 `install_checkmark_style(qapp)` 不重复包装。

- [ ] **Step 2：运行并确认 RED**

Run: `.venv\Scripts\python.exe -m pytest tests/ui/test_checkmark_style.py -q`

Expected: 新样式模块缺失。

- [ ] **Step 3：实现代理绘制并安装**

`CheckmarkProxyStyle.drawPrimitive()` 对 `PE_IndicatorCheckBox` 和 `PE_IndicatorItemViewItemCheck` 完整绘制 18×18 圆角 indicator；其他 primitive 才调用基类。启用选中状态先填充 `#17A8C2`，未选中为白底灰蓝边框，禁用状态改用浅灰，`State_HasFocus` 在外侧绘制柔和青色环；选中状态最后绘制两段抗锯齿折线：

```python
painter.save()
painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
color = QColor("#FFFFFF") if option.state & QStyle.StateFlag.State_Enabled else QColor("#B7C4CB")
pen = QPen(color, 2.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
painter.setPen(pen)
rect = option.rect.adjusted(4, 4, -4, -4)
path = QPainterPath(QPointF(rect.left(), rect.center().y()))
path.lineTo(QPointF(rect.center().x() - 1, rect.bottom()))
path.lineTo(QPointF(rect.right(), rect.top()))
painter.drawPath(path)
painter.restore()
```

`pixelMetric()` 对 `PM_IndicatorWidth`/`PM_IndicatorHeight` 返回 18。`install_checkmark_style()` 用动态属性防止重复安装。app 创建 `QApplication` 后立即安装；从 QSS 删除会吞掉代理绘制的 `QCheckBox::indicator` 背景规则，只保留 spacing/文字颜色。表格委托继续走应用 style 的 item-view primitive。

- [ ] **Step 4：运行全 UI 勾选回归并提交**

Run: `.venv\Scripts\python.exe -m pytest tests/ui/test_checkmark_style.py tests/ui/test_settings_dialog.py tests/ui/test_content_browser.py tests/ui/test_subscriptions.py tests/ui/test_storage.py -q`

Expected: 选中状态有白色勾号，点击和空格切换语义不变。

```powershell
git add src/telegram_downloader/ui/checkmark_style.py src/telegram_downloader/ui/theme.py src/telegram_downloader/ui/check_delegate.py src/telegram_downloader/app.py tests/ui/test_checkmark_style.py
git commit -m "fix: draw clear checkmarks across application"
```

## Task 8：托盘菜单独立浅色主题

**Files:**
- Modify: `src/telegram_downloader/background.py:141-205`
- Test: `tests/test_background.py`

- [ ] **Step 1：写深色系统调色板下可读性 RED 测试**

```python
def test_tray_menu_uses_explicit_readable_palette(qtbot, monkeypatch) -> None:
    window = QWidget()
    qtbot.addWidget(window)
    monkeypatch.setattr(QSystemTrayIcon, "isSystemTrayAvailable", lambda: True)
    adapter = QtTrayAdapter(window)
    palette = adapter.menu.palette()
    assert palette.color(QPalette.ColorRole.Window).name().lower() == "#ffffff"
    assert palette.color(QPalette.ColorRole.WindowText).name().lower() == "#22394a"
    assert "QMenu::item:selected" in adapter.menu.styleSheet()
    assert "#E8F9FC" in adapter.menu.styleSheet()
    assert "QMenu::item:disabled" in adapter.menu.styleSheet()
```

保留所有动作 signal 测试，增加 separator 和 disabled item 的样式断言。

- [ ] **Step 2：运行并确认 RED**

Run: `.venv\Scripts\python.exe -m pytest tests/test_background.py -q`

Expected: 菜单未设置显式调色板/样式。

- [ ] **Step 3：实现局部菜单主题**

在 `background.py` 定义 `TRAY_MENU_STYLESHEET`，覆盖 `QMenu`、`QMenu::item`、`:selected`、`:disabled` 和 `::separator`。创建菜单后：

```python
self.menu.setObjectName("trayMenu")
palette = self.menu.palette()
palette.setColor(QPalette.ColorRole.Window, QColor("#FFFFFF"))
palette.setColor(QPalette.ColorRole.WindowText, QColor("#22394A"))
palette.setColor(QPalette.ColorRole.Text, QColor("#22394A"))
palette.setColor(QPalette.ColorRole.Disabled, QPalette.ColorRole.Text, QColor("#A8B5BD"))
self.menu.setPalette(palette)
self.menu.setStyleSheet(TRAY_MENU_STYLESHEET)
```

样式只绑定 `self.menu`，不调用 `QApplication.setPalette()`。

- [ ] **Step 4：运行并提交**

Run: `.venv\Scripts\python.exe -m pytest tests/test_background.py tests/test_notifications.py -q`

Expected: 菜单动作和通知测试全部通过。

```powershell
git add src/telegram_downloader/background.py tests/test_background.py
git commit -m "fix: keep tray menu text readable"
```

## Task 9：搜索摘要完整且懒加载换行

**Files:**
- Create: `src/telegram_downloader/ui/wrapped_text.py`
- Create: `tests/ui/test_wrapped_text.py`
- Modify: `src/telegram_downloader/ui/content_browser.py:58-570`
- Modify: `tests/ui/test_content_browser.py:80-165,470-505`

- [ ] **Step 1：写完整高度、缓存和 10,000 行可见测量 RED 测试**

```python
def test_wrapped_delegate_expands_for_complete_text(qtbot) -> None:
    page = ContentBrowserPage()
    page.resize(996, 650)
    qtbot.addWidget(page)
    page.show()
    long = "完整换行摘要" * 60
    page.set_results([replace(result(now(), "r1", 1), excerpt=long)])
    qtbot.waitUntil(lambda: page.result_table.rowHeight(0) > 78)
    assert page.result_table.wordWrap() is True
    assert page.result_table.textElideMode() is Qt.TextElideMode.ElideNone
    assert page.summary_delegate.measurement_count >= 1


def test_initial_layout_does_not_measure_all_ten_thousand_rows(qtbot) -> None:
    page = ContentBrowserPage()
    page.resize(996, 650)
    qtbot.addWidget(page)
    page.show()
    page.set_results([result(now(), f"r{index}", index) for index in range(10_000)])
    qtbot.wait(20)
    assert page.summary_delegate.measurement_count < 100
```

另写列宽改变后缓存 generation 增加、连续 resize 只触发一个定时重算、滚动后新可见行获得完整高度的测试。

- [ ] **Step 2：运行并确认 RED**

Run: `.venv\Scripts\python.exe -m pytest tests/ui/test_wrapped_text.py tests/ui/test_content_browser.py -q`

Expected: 当前 `wordWrap=False`、`ElideRight`，新增委托缺失。

- [ ] **Step 3：实现宽度相关摘要委托**

`WrappedSummaryDelegate` 公开：

```python
class WrappedSummaryDelegate(QStyledItemDelegate):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._cache: dict[tuple[str, int, str, str], QSize] = {}
        self.measurement_count = 0

    def clear_cache(self) -> None:
        self._cache.clear()

    def sizeHint(self, option, index) -> QSize:
        text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        identity = str(index.data(Qt.ItemDataRole.UserRole) or index.row())
        width = max(40, option.rect.width() - 16)
        key = (identity, width, option.font.toString(), text)
        if key not in self._cache:
            try:
                metrics = QFontMetrics(option.font)
                bounds = metrics.boundingRect(
                    QRect(0, 0, width, 100_000),
                    Qt.TextFlag.TextWordWrap | Qt.TextFlag.TextExpandTabs,
                    text,
                )
                height = max(78, bounds.height() + 16)
            except (RuntimeError, TypeError, ValueError):
                height = 78
            self._cache[key] = QSize(option.rect.width(), height)
            self.measurement_count += 1
        return self._cache[key]
```

重写 `initStyleOption()`，设置 `WrapText` 与 `ElideNone`，由基类绘制完整文本。

- [ ] **Step 4：只调整当前可见行**

ContentBrowserPage 安装 summary 列委托，设置 `wordWrap=True`、`ElideNone` 和默认 78。增加单次 40ms `QTimer`：

```python
def _schedule_visible_row_resize(self) -> None:
    self._row_resize_timer.start(40)


def _resize_visible_result_rows(self) -> None:
    viewport = self.result_table.viewport()
    first = max(0, self.result_table.rowAt(0))
    last = self.result_table.rowAt(max(0, viewport.height() - 1))
    if last < 0:
        last = min(self.result_model.rowCount() - 1, first + 20)
    for row in range(first, last + 1):
        index = self.result_model.index(row, 4)
        option = QStyleOptionViewItem()
        self.result_table.initViewItemOption(option)
        option.rect.setWidth(self.result_table.columnWidth(4))
        self.result_table.setRowHeight(
            row,
            max(78, self.summary_delegate.sizeHint(option, index).height()),
        )
```

模型更新时清缓存并调度；摘要列 `sectionResized` 时清缓存并调度；垂直滚动只调度新可见范围。页面收到 `FontChange`、`ApplicationFontChange` 或 `ScreenChangeInternal` 时清缓存并重新调度，以覆盖字体/DPI 变化；缩略图到达不清摘要缓存。

- [ ] **Step 5：运行并提交**

Run: `.venv\Scripts\python.exe -m pytest tests/ui/test_wrapped_text.py tests/ui/test_content_browser.py -q`

Expected: 长摘要完整显示，无横向滚动条，10,000 行初次布局测量少于 100 行。

```powershell
git add src/telegram_downloader/ui/wrapped_text.py src/telegram_downloader/ui/content_browser.py tests/ui/test_wrapped_text.py tests/ui/test_content_browser.py
git commit -m "fix: wrap complete search result summaries"
```

## Task 10：跨功能回归与用户文档

**Files:**
- Modify: `README.md`
- Modify: `tests/test_download_location_e2e.py`
- Modify: `tests/test_naming_templates_e2e.py`
- Modify: `tests/test_app.py`
- Modify: `tests/test_packaging_contract.py`

- [ ] **Step 1：写最终组合 RED 测试**

端到端测试必须依次执行：加载含旧自动更新字段的 v0.15 设置 → 选择外部根 → 保存 → 用现有模板创建新任务 → 重启策略与 Planner → 继续默认根旧任务 → 打开两个任务目录。断言：

```python
assert reloaded.check_updates_on_startup is False
assert reloaded.download_storage.root == str(external.resolve())
assert Path(old_item.target_path).is_relative_to(paths.downloads)
assert Path(new_item.target_path).is_relative_to(external)
assert policy.guard(Path(old_item.target_path)) == Path(old_item.target_path).resolve()
assert policy.guard(Path(new_item.target_path)) == Path(new_item.target_path).resolve()
```

packaging contract 要求 README 明确包含“浏览…选择下载根目录”“已有任务保持原路径”“启动时不会自动检查更新”“搜索摘要完整换行”。

- [ ] **Step 2：运行并确认 RED**

Run: `.venv\Scripts\python.exe -m pytest tests/test_download_location_e2e.py tests/test_naming_templates_e2e.py tests/test_app.py tests/test_packaging_contract.py -q`

Expected: README 契约和组合流程尚未全部满足。

- [ ] **Step 3：更新 README 和最终装配**

README 设置章节写清：

- 媒体根目录通过系统文件夹选择器设置，配置等应用数据仍在应用目录；
- 目录/文件名模板继续作用于新任务，旧任务不移动；
- 外部磁盘或共享离线时不静默回退；
- 更新只在设置页点击后检查，下载与安装仍需明确接受；
- 搜索摘要会完整换行，行高按内容调整。

清理 app 中残留的 `check_updates_on_startup` 分支、旧复选框连接和对外部媒体调用 `paths.guard()` 的代码。用：

```powershell
rg -n "check_updates_on_startup|paths\.guard\(.*downloads|setWordWrap\(False\)|ElideRight" src tests
```

期望只剩设置兼容迁移测试、非摘要控件合法的 `ElideRight`，以及应用内部路径守卫。

- [ ] **Step 4：运行定向组合回归并提交**

Run: `.venv\Scripts\python.exe -m pytest tests/test_download_paths.py tests/test_download_location_e2e.py tests/test_naming_templates_e2e.py tests/test_app.py tests/test_controller.py tests/test_packaging_contract.py tests/ui/test_settings_dialog.py tests/ui/test_main_window.py tests/ui/test_content_browser.py -q`

Expected: 全部通过。

```powershell
git add README.md src/telegram_downloader/app.py src/telegram_downloader/controller.py src/telegram_downloader/ui/settings.py src/telegram_downloader/ui/content_browser.py tests/test_download_location_e2e.py tests/test_naming_templates_e2e.py tests/test_app.py tests/test_packaging_contract.py
git commit -m "docs: explain custom media storage and manual updates"
```

## Task 11：三轮独立自检与本地成品冒烟

**Files:**
- Create: `docs/verification/2026-08-22-download-location-ui-polish.md`

- [ ] **Step 1：第一轮——五项功能定向自检**

Run:

```powershell
.venv\Scripts\python.exe -m pytest tests/test_settings.py tests/test_download_paths.py tests/test_download_location_e2e.py tests/test_naming_templates_e2e.py tests/test_planner.py tests/test_downloader.py tests/test_file_integrity.py tests/test_storage_models.py tests/test_storage_inventory.py tests/test_storage_cleanup.py tests/test_storage_maintenance_e2e.py tests/test_diagnostic_probes.py tests/test_background.py tests/update/test_update_coordinator.py tests/test_controller.py tests/test_app.py tests/ui/test_checkmark_style.py tests/ui/test_wrapped_text.py tests/ui/test_settings_dialog.py tests/ui/test_main_window.py tests/ui/test_content_browser.py -q
git diff --check
```

Expected: 所有定向测试通过；`git diff --check` 无输出。逐条核对五项用户请求和设计验收标准，并把测试数量与命令写入验证文档。

- [ ] **Step 2：第二轮——完整工程自检**

Run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/test.ps1
.venv\Scripts\python.exe -m compileall -q src tests
.venv\Scripts\python.exe -m pip check
```

Expected: 完整 pytest 通过，Ruff 输出 `All checks passed!`，compileall 退出码 0，pip 输出 `No broken requirements found.`。记录新鲜输出，不复用第一轮结果。

- [ ] **Step 3：第三轮——冻结程序、安装器与 Windows 视觉自检**

Run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build-installer.ps1 -SkipAppBuild
$projectRoot = (Resolve-Path '.').Path
$smokeRoot = Join-Path $projectRoot ('.build-temp\download-ui-smoke-' + [Guid]::NewGuid().ToString('N'))
$portableZip = Join-Path $projectRoot 'dist\TelegramDownloader-0.15.0-win-x64-portable.zip'
New-Item -ItemType Directory -Path $smokeRoot | Out-Null
Expand-Archive -LiteralPath $portableZip -DestinationPath $smokeRoot
$selfTest = (& (Join-Path $smokeRoot 'TelegramDownloader.exe') --self-test | ConvertFrom-Json)
if (-not $selfTest.ok) { throw 'Frozen self-test failed' }
if (-not ([IO.Path]::GetFullPath($selfTest.runtime_root)).StartsWith($smokeRoot, [StringComparison]::OrdinalIgnoreCase)) { throw 'Frozen runtime escaped smoke root' }
Get-FileHash -Algorithm SHA256 -LiteralPath $portableZip
Get-FileHash -Algorithm SHA256 -LiteralPath 'dist\release\TelegramDownloader-0.15.0-win-x64-setup.exe'
```

随后在隔离的冻结程序中检查并保存截图证据：

1. 100% 缩放、Windows 浅色主题：文件夹浏览器可选 `.build-temp` 下独立媒体目录，设置页显示只读路径；
2. 125% 缩放、Windows 深色主题：设置/搜索复选框有白色勾号，托盘菜单为白底可读文字；
3. 手动检查按钮显示忙碌与结果，启动程序前后网络更新协调器日志没有自动调用；
4. 500 字摘要完整换行，滚动和缩放无明显冻结；
5. 退出后先验证冒烟目录解析结果位于项目 `.build-temp`，再执行：

```powershell
$buildTempPrefix = ([IO.Path]::GetFullPath((Join-Path $projectRoot '.build-temp'))).TrimEnd('\') + '\'
$resolvedSmokeRoot = [IO.Path]::GetFullPath($smokeRoot)
if (-not $resolvedSmokeRoot.StartsWith($buildTempPrefix, [StringComparison]::OrdinalIgnoreCase)) { throw 'Smoke cleanup escaped .build-temp' }
Remove-Item -LiteralPath $resolvedSmokeRoot -Recurse -Force
```

不得修改真实用户媒体目录。

Expected: `PACKAGED_SMOKE_OK`、`INSTALLER_SMOKE_OK`，冻结自检 `ok=true`，五项人工冒烟通过。若任何一项失败，修复后从第三轮开头重新执行。

- [ ] **Step 4：记录证据并提交**

`docs/verification/2026-08-22-download-location-ui-polish.md` 记录三轮命令、精确通过数量、构建产物字节数/SHA-256、冻结自检结果、浅/深主题与 100%/125% 检查结论，以及没有使用真实 Telegram 账号或真实用户目录的边界。

```powershell
git add docs/verification/2026-08-22-download-location-ui-polish.md
git commit -m "test: record three-pass download UI verification"
git status --short --branch
```

Expected: 提交成功，工作树干净。不推送、不打标签、不发布。

## 计划完成定义

- 11 个任务均有对应 RED/GREEN 证据和独立提交。
- 媒体可以保存到系统选择的受信目录，应用内部数据安全边界没有放宽。
- 当前与历史根目录支持新任务和旧任务续传；未知根目录、根盘、应用内部数据目录和 reparse 越界被拒绝。
- 所有普通复选框显示清晰白色勾号，托盘菜单在 Windows 浅色/深色主题下可读。
- 前台、后台和托盘启动均不访问更新源；检查、下载和安装均由用户明确触发。
- 搜索摘要完整换行，10,000 条结果初次布局不测量全表。
- 三轮自检均使用新鲜输出并通过；最终只形成未发布的本地验证分支。
