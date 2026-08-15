from pathlib import Path


root = Path(SPECPATH)
a = Analysis(
    [str(root / "src" / "telegram_downloader" / "__main__.py")],
    pathex=[str(root / "src")],
    binaries=[],
    datas=[
        (
            str(root / "src" / "telegram_downloader" / "trusted_update_keys.json"),
            "telegram_downloader",
        )
    ],
    hiddenimports=[
        "qasync",
        "python_socks",
        "qrcode",
        "telethon.sessions.string",
        "telegram_downloader.subscription_diagnostics",
        "telegram_downloader.ui.subscription_diagnostics",
        "telegram_downloader.ui.models",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        "PySide6.QtWebEngineCore",
        "PySide6.QtWebEngineWidgets",
        "PySide6.QtWebEngineQuick",
    ],
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

helper_a = Analysis(
    [str(root / "src" / "telegram_downloader" / "update_helper_entry.py")],
    pathex=[str(root / "src")],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=["PySide6", "telethon", "qasync"],
    noarchive=False,
)
helper_pyz = PYZ(helper_a.pure)
helper_exe = EXE(
    helper_pyz,
    helper_a.scripts,
    helper_a.binaries,
    helper_a.datas,
    [],
    name="UpdateHelper",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
)
