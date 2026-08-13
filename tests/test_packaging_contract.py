from pathlib import Path


def test_build_contract_uses_onedir_and_project_local_workpaths() -> None:
    root = Path(__file__).parents[1]
    spec = (root / "TelegramDownloader.spec").read_text(encoding="utf-8")
    build = (root / "scripts" / "build.ps1").read_text(encoding="utf-8")
    test = (root / "scripts" / "test.ps1").read_text(encoding="utf-8")

    assert "COLLECT(" in spec
    assert "console=False" in spec
    assert "QtWebEngine" in spec
    assert "--onefile" not in build
    assert ".build-temp" in build
    assert ".tool-cache" in build
    assert "smoke.ps1" in build
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
