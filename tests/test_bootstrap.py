import json
import sys
from dataclasses import dataclass
from pathlib import Path

from telegram_downloader import __main__ as main_module
from telegram_downloader.bootstrap import configure_process, resolve_runtime_root


@dataclass
class FakeGuard:
    acquired: bool
    released: bool = False

    def acquire(self) -> bool:
        return self.acquired

    def release(self) -> None:
        self.released = True


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


def test_self_test_json_forces_utf8_stdout(monkeypatch) -> None:
    class Output:
        def __init__(self) -> None:
            self.encoding = "cp936"
            self.reconfigured: list[tuple[str, str]] = []
            self.parts: list[str] = []

        def reconfigure(self, *, encoding: str, errors: str) -> None:
            self.encoding = encoding
            self.reconfigured.append((encoding, errors))

        def write(self, value: str) -> int:
            self.parts.append(value)
            return len(value)

        def flush(self) -> None:
            pass

    output = Output()
    monkeypatch.setattr(sys, "stdout", output)

    main_module._print_self_test_report({"runtime_root": "D:/Telegram下载器"})

    assert output.reconfigured == [("utf-8", "strict")]
    assert json.loads("".join(output.parts))["runtime_root"].endswith("下载器")


def test_health_command_refuses_database_access_when_instance_runs(
    tmp_path: Path,
    capsys,
) -> None:
    called = False

    def forbidden(_root: Path) -> dict[str, object]:
        nonlocal called
        called = True
        return {"ok": True}

    guard = FakeGuard(False)

    code = main_module._run_health_command(
        tmp_path,
        confirmation=None,
        self_test=forbidden,
        guard=guard,
    )

    assert code == 2
    assert called is False
    assert guard.released is False
    assert json.loads(capsys.readouterr().out) == {
        "ok": False,
        "code": "instance-running",
    }


def test_health_command_writes_confirmation_only_for_success_and_releases_guard(
    tmp_path: Path,
    capsys,
) -> None:
    confirmation = tmp_path / "data" / "updates" / "health-confirmed"
    guard = FakeGuard(True)

    code = main_module._run_health_command(
        tmp_path,
        confirmation=confirmation,
        self_test=lambda _root: {"ok": True, "version": "test"},
        guard=guard,
    )

    assert code == 0
    assert guard.released is True
    assert confirmation.read_bytes() == b"ok\n"
    assert json.loads(capsys.readouterr().out) == {"ok": True, "version": "test"}


def test_health_command_skips_confirmation_for_failure_and_releases_guard(
    tmp_path: Path,
    capsys,
) -> None:
    confirmation = tmp_path / "data" / "updates" / "health-confirmed"
    guard = FakeGuard(True)

    code = main_module._run_health_command(
        tmp_path,
        confirmation=confirmation,
        self_test=lambda _root: {"ok": False, "code": "failed"},
        guard=guard,
    )

    assert code == 1
    assert guard.released is True
    assert confirmation.exists() is False
    assert json.loads(capsys.readouterr().out) == {"ok": False, "code": "failed"}
