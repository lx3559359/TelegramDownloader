from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from telegram_downloader.bootstrap import configure_process, runtime_root


def _print_self_test_report(report: dict[str, object]) -> None:
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8", errors="strict")
    print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))


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
        from telegram_downloader.app import run_self_test

        report = run_self_test(root)
        if report.get("ok") is True and arguments.update_health_check is not None:
            from telegram_downloader.paths import PortablePaths

            confirmation = PortablePaths(root).guard(arguments.update_health_check)
            confirmation.parent.mkdir(parents=True, exist_ok=True)
            temporary = confirmation.with_suffix(confirmation.suffix + ".tmp")
            with temporary.open("wb") as stream:
                stream.write(b"ok\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, confirmation)
        _print_self_test_report(report)
        return 0 if report.get("ok") is True else 1

    return _run_gui(root, background=arguments.background)


if __name__ == "__main__":
    raise SystemExit(main())
