import sys
from pathlib import Path

from telegram_downloader import __main__ as main_module
from telegram_downloader.bootstrap import configure_process, resolve_runtime_root


def test_source_runtime_root_is_repository_root(tmp_path: Path) -> None:
    module_file = tmp_path / "src" / "telegram_downloader" / "bootstrap.py"
    assert resolve_runtime_root(False, tmp_path / "ignored.exe", module_file) == tmp_path


def test_frozen_runtime_root_is_executable_parent(tmp_path: Path) -> None:
    exe = tmp_path / "portable" / "TelegramDownloader.exe"
    assert resolve_runtime_root(True, exe, tmp_path / "ignored.py") == exe.parent


def test_configure_process_redirects_temp_and_user_data(
    tmp_path: Path, monkeypatch
) -> None:
    for variable in ("TEMP", "TMP", "APPDATA", "LOCALAPPDATA"):
        monkeypatch.delenv(variable, raising=False)

    configure_process(tmp_path)

    environ = __import__("os").environ
    assert Path(environ["TEMP"]) == tmp_path / "data" / "temp"
    assert Path(environ["TMP"]) == tmp_path / "data" / "temp"
    assert Path(environ["APPDATA"]) == tmp_path / "data" / "user-profile" / "Roaming"
    assert Path(environ["LOCALAPPDATA"]) == tmp_path / "data" / "user-profile" / "Local"
    assert (tmp_path / "data" / "temp").is_dir()
    assert (tmp_path / "downloads").is_dir()


def test_background_argument_is_propagated_to_gui_runner(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: list[tuple[Path, bool]] = []
    monkeypatch.setattr(main_module, "runtime_root", lambda: tmp_path)
    monkeypatch.setattr(main_module, "configure_process", lambda root: root)
    monkeypatch.setattr(
        main_module,
        "_run_gui",
        lambda root, *, background: calls.append((root, background)) or 0,
    )
    monkeypatch.setattr(sys, "argv", ["TelegramDownloader", "--background"])

    assert main_module.main() == 0
    assert calls == [(tmp_path, True)]
