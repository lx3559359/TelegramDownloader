import subprocess
from pathlib import Path

import pytest

from telegram_downloader.autostart import (
    RUN_KEY,
    RUN_VALUE_NAME,
    AutostartUnavailableError,
    CurrentUserAutostart,
)


class FakeRegistry:
    def __init__(self, values: dict[str, str] | None = None) -> None:
        self.values = dict(values or {})
        self.calls: list[tuple[str, str, str | None]] = []

    def set_value(self, key: str, name: str, value: str) -> None:
        self.calls.append(("set", key, name))
        self.values[name] = value

    def delete_value(self, key: str, name: str) -> None:
        self.calls.append(("delete", key, name))
        self.values.pop(name, None)

    def get_value(self, key: str, name: str) -> str | None:
        self.calls.append(("get", key, name))
        return self.values.get(name)


def packaged_service(
    tmp_path: Path,
    registry: FakeRegistry,
) -> tuple[CurrentUserAutostart, Path]:
    executable = tmp_path / "Telegram Downloader.exe"
    executable.write_bytes(b"exe")
    return CurrentUserAutostart(registry, executable, frozen=True), executable


def test_enabling_autostart_writes_fixed_background_command(tmp_path: Path) -> None:
    registry = FakeRegistry()
    service, executable = packaged_service(tmp_path, registry)

    service.reconcile(True)

    assert registry.values[RUN_VALUE_NAME] == subprocess.list2cmdline(
        [str(executable.resolve()), "--background"]
    )
    assert registry.calls == [("set", RUN_KEY, RUN_VALUE_NAME)]


def test_disabling_autostart_removes_only_owned_value(tmp_path: Path) -> None:
    registry = FakeRegistry({RUN_VALUE_NAME: "old", "Unrelated": "keep"})
    service, _executable = packaged_service(tmp_path, registry)

    service.reconcile(False)

    assert RUN_VALUE_NAME not in registry.values
    assert registry.values["Unrelated"] == "keep"
    assert registry.calls == [("delete", RUN_KEY, RUN_VALUE_NAME)]


def test_source_mode_rejects_enabling_autostart(tmp_path: Path) -> None:
    with pytest.raises(AutostartUnavailableError, match="正式打包"):
        CurrentUserAutostart(
            FakeRegistry(),
            tmp_path / "python.exe",
            frozen=False,
        ).reconcile(True)


def test_source_mode_does_not_touch_registry_when_disabled(tmp_path: Path) -> None:
    registry = FakeRegistry({RUN_VALUE_NAME: "packaged-value"})
    service = CurrentUserAutostart(
        registry,
        tmp_path / "python.exe",
        frozen=False,
    )

    service.reconcile(False)

    assert registry.values[RUN_VALUE_NAME] == "packaged-value"
    assert registry.calls == []


def test_enabled_reports_only_the_exact_owned_command(tmp_path: Path) -> None:
    registry = FakeRegistry()
    service, _executable = packaged_service(tmp_path, registry)
    registry.values[RUN_VALUE_NAME] = "different.exe --background"

    assert service.enabled() is False

    registry.values[RUN_VALUE_NAME] = service.command()
    assert service.enabled() is True
