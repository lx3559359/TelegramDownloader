from __future__ import annotations

import argparse
import json

from telegram_downloader.bootstrap import configure_process, runtime_root


def main() -> int:
    root = configure_process(runtime_root())
    parser = argparse.ArgumentParser(prog="TelegramDownloader")
    parser.add_argument("--self-test", action="store_true")
    arguments = parser.parse_args()

    if arguments.self_test:
        from telegram_downloader.app import run_self_test

        report = run_self_test(root)
        print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
        return 0 if report.get("ok") is True else 1

    from telegram_downloader.app import run

    return run(root)


if __name__ == "__main__":
    raise SystemExit(main())
