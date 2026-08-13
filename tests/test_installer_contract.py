from pathlib import Path

from telegram_downloader import __version__


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
    ):
        assert required in script


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
        "sentinel",
        "C:\\TelegramDownloader-Installer-Rejection-Smoke",
        "Uninstall",
    ):
        assert required in smoke
    assert f"TelegramDownloader-{__version__}-win-x64-setup.exe" in smoke
