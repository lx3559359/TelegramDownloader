from pathlib import Path


def test_installer_is_current_user_x64_and_rejects_system_volumes() -> None:
    root = Path(__file__).parents[1]
    script = (root / "installer" / "TelegramDownloader.iss").read_text(encoding="utf-8")

    for required in (
        "PrivilegesRequired=lowest",
        "ArchitecturesAllowed=x64compatible",
        "ArchitecturesInstallIn64BitMode=x64compatible",
        "GetDefaultInstallDir",
        "IsForbiddenInstallPath",
        "ExtractFileDrive(ExpandConstant('{win}'))",
        "CandidateDrive = 'C:\\'",
        "if not WizardSilent then",
        "SuppressibleMsgBox",
        "PrepareToInstall",
        "TelegramDownloader.exe",
        "UpdateHelper.exe",
        "runtime-manifest.json",
        "AppName=TG 快取",
        "UninstallDisplayName=TG 快取",
        "SetupIconFile=..\\src\\telegram_downloader\\resources\\tg_quick_fetch.ico",
        'Name: "{userprograms}\\TG 快取"',
        'Name: "{userdesktop}\\TG 快取"',
    ):
        assert required in script

    assert 'Name: "{userprograms}\\Telegram 下载器.lnk"' in script
    assert 'Name: "{userdesktop}\\Telegram 下载器.lnk"' in script


def test_installer_preserves_local_data_unless_separately_confirmed() -> None:
    script = (
        Path(__file__).parents[1] / "installer" / "TelegramDownloader.iss"
    ).read_text(encoding="utf-8")

    assert 'Excludes: "data\\*,downloads\\*"' in script
    assert "SuppressibleMsgBox" in script
    assert "RemoveUserData" in script
    assert "DelTree(ExpandConstant('{app}\\data')" in script
    assert "DelTree(ExpandConstant('{app}\\downloads')" in script
    assert "msiexec" not in script.casefold()


def test_installer_build_and_smoke_paths_are_project_local_and_versioned() -> None:
    root = Path(__file__).parents[1]
    build = (root / "scripts" / "build-installer.ps1").read_text(encoding="utf-8")
    smoke = (root / "scripts" / "smoke-installer.ps1").read_text(encoding="utf-8")

    for required in (".tool-cache", ".build-temp", "dist\\release", "Assert-ProjectChild"):
        assert required in build
    for required in (
        ".build-temp\\installed-smoke",
        "--self-test",
        "data\\database\\catalog.sqlite3",
        "data\\cache\\thumbnails\\preserve.thumb",
        "data\\sentinel.keep",
        "Get-FileHash",
        "upgrade changed preserved user data",
        "uninstall changed preserved user data",
        "C:\\TelegramDownloader-Installer-Rejection-Smoke",
        "Uninstall",
    ):
        assert required in smoke
    assert "$sourceVersion = & $python -c" in smoke
    assert "TelegramDownloader-$sourceVersion-win-x64-setup.exe" in smoke
    assert "TelegramDownloader-0.1.0-win-x64-setup.exe" not in smoke


def test_inno_compiler_arguments_preserve_project_paths_with_spaces() -> None:
    build = (
        Path(__file__).parents[1] / "scripts" / "build-installer.ps1"
    ).read_text(encoding="utf-8")

    assert "[Diagnostics.ProcessStartInfo]::new()" in build
    assert "$compilerStart.Arguments =" in build
    assert ".ArgumentList.Add(" not in build
    assert "$compilerArguments" in build
    assert "$sourceDefinition" not in build
