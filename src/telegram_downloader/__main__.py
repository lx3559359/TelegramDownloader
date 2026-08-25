from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from telegram_downloader.bootstrap import configure_process, runtime_root


def _print_self_test_report(report: dict[str, object]) -> None:
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8", errors="strict")
    print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))


class _InstanceGuard(Protocol):
    def acquire(self) -> bool: ...

    def release(self) -> None: ...


def _write_health_confirmation(root: Path, confirmation_path: Path) -> None:
    from telegram_downloader.paths import PortablePaths

    confirmation = PortablePaths(root).guard(confirmation_path)
    confirmation.parent.mkdir(parents=True, exist_ok=True)
    temporary = confirmation.with_suffix(confirmation.suffix + ".tmp")
    with temporary.open("wb") as stream:
        stream.write(b"ok\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, confirmation)


def _run_health_command(
    root: Path,
    *,
    confirmation: Path | None,
    self_test: Callable[[Path], dict[str, object]] | None = None,
    guard: _InstanceGuard | None = None,
) -> int:
    if self_test is None:
        from telegram_downloader.app import run_self_test as self_test
    if guard is None:
        from telegram_downloader.instance_guard import WindowsInstanceGuard

        guard = WindowsInstanceGuard()

    if not guard.acquire():
        _print_self_test_report({"ok": False, "code": "instance-running"})
        return 2

    try:
        report = self_test(root)
        if report.get("ok") is True and confirmation is not None:
            _write_health_confirmation(root, confirmation)
        _print_self_test_report(report)
        return 0 if report.get("ok") is True else 1
    finally:
        guard.release()


def _default_startup_factory():
    from telegram_downloader.startup import create_startup_indicator

    return create_startup_indicator()


def _run_gui(
    root: Path,
    *,
    background: bool = False,
    startup_factory=None,
    runner=None,
) -> int:
    indicator = None
    if not background:
        indicator = (startup_factory or _default_startup_factory)()
    try:
        if indicator is not None:
            indicator.set_status("正在加载运行组件…")
        if runner is None:
            from telegram_downloader.app import run as runner
        return runner(
            root,
            startup_indicator=indicator,
            background=background,
        )
    finally:
        if indicator is not None:
            indicator.close()


def main() -> int:
    root = configure_process(runtime_root())
    parser = argparse.ArgumentParser(prog="TelegramDownloader")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--update-health-check", type=Path)
    parser.add_argument("--background", action="store_true")
    arguments = parser.parse_args()

    if arguments.self_test or arguments.update_health_check is not None:
        return _run_health_command(
            root,
            confirmation=arguments.update_health_check,
        )

    return _run_gui(root, background=arguments.background)


if __name__ == "__main__":
    raise SystemExit(main())
