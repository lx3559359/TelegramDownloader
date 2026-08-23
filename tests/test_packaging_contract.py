import tomllib
from pathlib import Path


def test_build_contract_uses_onedir_and_project_local_workpaths() -> None:
    root = Path(__file__).parents[1]
    spec = (root / "TelegramDownloader.spec").read_text(encoding="utf-8")
    build = (root / "scripts" / "build.ps1").read_text(encoding="utf-8")
    test = (root / "scripts" / "test.ps1").read_text(encoding="utf-8")

    assert "COLLECT(" in spec
    assert 'name="UpdateHelper"' in spec
    assert "trusted_update_keys.json" in spec
    assert "tg_quick_fetch.ico" in spec
    assert "tg_quick_fetch-256.png" in spec
    icon_contract = (
        'icon=str(root / "src" / "telegram_downloader" / "resources" '
        '/ "tg_quick_fetch.ico")'
    )
    assert icon_contract in spec
    assert "console=False" in spec
    assert "QtWebEngine" in spec
    assert "--onefile" not in build
    assert "-m PyInstaller" in build
    assert ".venv\\Scripts\\pyinstaller.exe" not in build
    assert ".build-temp" in build
    assert ".tool-cache" in build
    assert "smoke.ps1" in build
    assert "generate_runtime_inventory.py" in build
    assert "UpdateHelper.exe" in build
    assert "$quotedConfirmation" in (root / "scripts" / "smoke.ps1").read_text(
        encoding="utf-8"
    )
    assert "APPDATA" in test
    assert "LOCALAPPDATA" in test


def test_chinese_guide_documents_portable_data_and_security() -> None:
    readme = (Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8")

    for required in (
        "Windows 10/11 x64",
        "API ID",
        "API Hash",
        "API development tools",
        "确认码会发送到 Telegram 消息，而不是短信",
        "TG Quick Fetch Personal",
        "Short name",
        "系统默认浏览器",
        "每个手机号只能关联一个 API ID",
        "API Hash 与密码类似",
        "SOCKS5",
        "HTTP",
        ".part",
        "DPAPI",
        "在线更新",
        "GitHub",
        "魔搭",
        "C 盘",
        "自动订阅",
        "仅订阅启用后的新消息",
        "5、15、30、60 或 180 分钟",
        "最多检查 500 条新消息",
        "最近 20 次运行",
        "只读测试最近 100 条",
        "最多展示 20 个样本",
        "不会推进订阅游标",
        "归档所选",
        "下载文件和去重记录都会保留",
        "一次 SQLite 聚合查询",
        "SHA-256 完整性",
        "校验所选",
        "重新下载所选",
        ".corrupt",
        "优先下载",
        "总下载限速",
        "等待中 · 第",
        "全局媒体槽",
        "批量导入",
        "包含词关系",
        "历史补抓",
        "下载路径",
        "{message_id}",
        "浏览…选择下载根目录",
        "已有任务保持原路径",
        "启动时不会自动检查更新",
        "搜索摘要单行省略",
    ):
        assert required in readme


def test_subscription_modules_do_not_escape_project_local_storage() -> None:
    root = Path(__file__).parents[1]
    modules = (
        root / "src/telegram_downloader/subscriptions.py",
        root / "src/telegram_downloader/subscription_service.py",
        root / "src/telegram_downloader/subscription_scheduler.py",
        root / "src/telegram_downloader/subscription_diagnostics.py",
        root / "src/telegram_downloader/ui/subscription_diagnostics.py",
        root / "src/telegram_downloader/ui/subscriptions.py",
    )
    forbidden = (
        "appdata",
        "localappdata",
        "qsettings",
        "tempfile",
        "schtasks",
        "taskscheduler",
        "win32service",
    )

    for module in modules:
        source = module.read_text(encoding="utf-8").casefold()
        assert all(value not in source for value in forbidden), module.name

    diagnostic_modules = modules[3:5]
    diagnostic_forbidden = (
        "catalogrepository",
        "taskplanner",
        "telethongateway",
        "subscriptionservice",
        "subscriptionscheduler",
    )
    for module in diagnostic_modules:
        source = module.read_text(encoding="utf-8").casefold()
        assert all(value not in source for value in diagnostic_forbidden), module.name


def test_v0170_version_and_account_update_brand_contract_are_consistent() -> None:
    root = Path(__file__).parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    package_init = (root / "src/telegram_downloader/__init__.py").read_text(
        encoding="utf-8"
    )
    gateway = (root / "src/telegram_downloader/gateway.py").read_text(encoding="utf-8")
    main = (root / "src/telegram_downloader/ui/main.py").read_text(encoding="utf-8")
    installer = (root / "installer/TelegramDownloader.iss").read_text(encoding="utf-8")
    requirements = (root / "requirements.txt").read_text(encoding="utf-8")
    spec = (root / "TelegramDownloader.spec").read_text(encoding="utf-8")
    app = (root / "src/telegram_downloader/app.py").read_text(encoding="utf-8")
    entry = (root / "src/telegram_downloader/__main__.py").read_text(encoding="utf-8")
    repository = (root / "src/telegram_downloader/repository.py").read_text(
        encoding="utf-8"
    )
    readme = (root / "README.md").read_text(encoding="utf-8")
    release_notes = root / "docs/releases/v0.17.0.md"

    assert project["project"]["version"] == "0.17.0"
    assert '__version__ = "0.17.0"' in package_init
    assert '#define AppVersion "0.17.0"' in installer
    assert "qrcode==8.2" in requirements
    assert '"qrcode"' in spec
    assert "app_version=__version__" in gateway
    assert 'f"v{__version__} · {APP_CHANNEL}"' in main
    assert '"telegram_downloader.subscription_diagnostics"' in spec
    assert '"telegram_downloader.ui.subscription_diagnostics"' in spec
    assert '"telegram_downloader.ui.models"' in spec
    assert '"telegram_downloader.startup"' in spec
    assert "import AsyncBandwidthLimiter" in app
    assert "bandwidth=bandwidth" in app
    assert "resource_settings.speed_limit_kib" in app
    assert all(
        term in readme
        for term in ("健康诊断", "开始自检", "导出诊断包", "data/diagnostics")
    )
    assert "默认选择“全部会话”" in readme
    assert "每条结果的真实来源" in readme
    assert "继续搜索" in readme
    assert "120 秒" in readme
    assert "关闭到托盘" in readme
    assert "系统通知" in readme
    assert "下载时段" in readme
    assert "pause_reason" in repository
    assert '"--background"' in entry
    assert release_notes.is_file()
    notes = release_notes.read_text(encoding="utf-8")
    assert "# TG 快取 v0.17.0" in notes
    assert all(
        term in notes
        for term in (
            "账号状态",
            "不会自动生成二维码",
            "独立候选会话",
            "活动下载",
            "检查更新",
            "启动时不会自动检查",
            "单行省略",
            "TG 快取",
            "旧快捷方式",
        )
    )
    assert "v0.1.0 · stable" not in main
    for component in (
        "ContentBrowserService",
        "CatalogRepository",
        "ThumbnailCache",
        "SubscriptionService",
        "SubscriptionScheduler",
        "FileIntegrityService",
        "DiagnosticsService",
        "DiagnosticReportStore",
        "StorageMaintenanceService",
        "StorageMaintenanceScheduler",
    ):
        assert component in app
        assert f"{component}(" in app

    for term in (
        "维护中心与存储空间",
        "默认关闭",
        "7 天",
        "30 天",
        "1 GiB",
        "900 MiB",
        "最新 1 份",
        "不会自动扫描 `downloads`",
        "两次确认",
        "来源不明",
        "永久删除",
        "退出",
    ):
        assert term in readme


def test_portable_zip_has_private_runtime_entry_gate() -> None:
    root = Path(__file__).parents[1]
    build = (root / "scripts/build.ps1").read_text(encoding="utf-8")
    forbidden = (
        "storage-state.json",
        ".part",
        ".corrupt",
        "app.log",
        "tasks.sqlite3",
        "catalog.sqlite3",
        "secrets.dat",
    )

    assert all(value in build for value in forbidden)
    for fragment in (
        "System.IO.Compression.ZipFile",
        "OpenRead($zip)",
        "Entry.FullName",
        "ToLowerInvariant",
        "Dispose()",
        "Portable ZIP contains private runtime entry",
    ):
        assert fragment in build


def test_build_preserves_existing_project_local_runtime_data() -> None:
    root = Path(__file__).parents[1]
    script = (root / "scripts/build.ps1").read_text(encoding="utf-8")

    for required in (
        "build-runtime-preservation",
        "$preservedRuntime",
        "foreach ($runtimeData in ('data', 'downloads'))",
        "Copy-Item -LiteralPath $source",
        "finally {",
        "Copy-Item -LiteralPath $preserved",
        "data\\database\\catalog.sqlite3",
        "data\\cache\\thumbnails\\preserve.thumb",
        "data\\sentinel.keep",
        "Get-FileHash",
        "Expand-Archive",
        "Portable ZIP unexpectedly contains user data",
    ):
        assert required in script


def test_packaging_cleanup_preserves_versioned_direct_run_releases() -> None:
    root = Path(__file__).parents[1]
    build = (root / "scripts/build.ps1").read_text(encoding="utf-8")
    installer = (root / "scripts/build-installer.ps1").read_text(encoding="utf-8")

    assert "$ownedBuildOutputs" in build
    assert "foreach ($directory in ($work, $dist))" not in build
    owned_outputs = build.split("$ownedBuildOutputs = @(", 1)[1].split(")", 1)[0]
    assert "$dist" not in owned_outputs
    assert "$existingAppDir" in owned_outputs
    assert "$helperOutput" in owned_outputs
    assert "Remove-Item -LiteralPath $releaseDir -Recurse -Force" not in installer
    assert "Remove-Item -LiteralPath $setup -Force" in installer


def test_file_integrity_runtime_is_reachable_and_project_local() -> None:
    root = Path(__file__).parents[1]
    app = (root / "src/telegram_downloader/app.py").read_text(encoding="utf-8")
    service = (root / "src/telegram_downloader/file_integrity.py").read_text(
        encoding="utf-8"
    )

    assert "import FileIntegrityService" in app
    assert "download_paths=download_paths" in app
    assert "self.download_paths.guard" in service
    assert "asyncio.to_thread" in service
    for forbidden in ("appdata", "localappdata", "qsettings", "tempfile"):
        assert forbidden not in service.casefold()
