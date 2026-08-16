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


def test_v080_version_and_content_runtime_contract_are_consistent() -> None:
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
    readme = (root / "README.md").read_text(encoding="utf-8")
    release_notes = (root / "docs/releases/v0.8.0.md")

    assert project["project"]["version"] == "0.8.0"
    assert '__version__ = "0.8.0"' in package_init
    assert '#define AppVersion "0.8.0"' in installer
    assert "qrcode==8.2" in requirements
    assert '"qrcode"' in spec
    assert "app_version=__version__" in gateway
    assert 'f"v{__version__} · stable"' in main
    assert '"telegram_downloader.subscription_diagnostics"' in spec
    assert '"telegram_downloader.ui.subscription_diagnostics"' in spec
    assert '"telegram_downloader.ui.models"' in spec
    assert '"telegram_downloader.startup"' in spec
    assert "普通链接、搜索和订阅统一去重" in readme
    assert "启动阶段立即显示加载状态" in readme
    assert release_notes.is_file()
    assert "# TelegramDownloader v0.8.0" in release_notes.read_text(encoding="utf-8")
    assert "v0.1.0 · stable" not in main
    for component in (
        "ContentBrowserService",
        "CatalogRepository",
        "ThumbnailCache",
        "SubscriptionService",
        "SubscriptionScheduler",
    ):
        assert f"import {component}" in app
        assert f"{component}(" in app


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
