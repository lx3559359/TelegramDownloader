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
    ):
        assert required in readme


def test_v042_version_and_content_runtime_contract_are_consistent() -> None:
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

    assert project["project"]["version"] == "0.4.2"
    assert '__version__ = "0.4.2"' in package_init
    assert '#define AppVersion "0.4.2"' in installer
    assert "qrcode==8.2" in requirements
    assert '"qrcode"' in spec
    assert "app_version=__version__" in gateway
    assert 'f"v{__version__} · stable"' in main
    assert "v0.1.0 · stable" not in main
    for component in (
        "ContentBrowserService",
        "CatalogRepository",
        "ThumbnailCache",
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
