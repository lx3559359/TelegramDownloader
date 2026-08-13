from __future__ import annotations

import argparse
from pathlib import Path

from telegram_downloader.bootstrap import configure_process


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--root", type=Path, required=True)
    arguments, remaining = parser.parse_known_args()
    root = configure_process(arguments.root)

    from telegram_downloader.update_helper import helper_main

    return helper_main(["--root", str(root), *remaining])


if __name__ == "__main__":
    raise SystemExit(main())
