from __future__ import annotations

import os
import sys
from pathlib import Path


def resolve_runtime_root(frozen: bool, executable: Path, module_file: Path) -> Path:
    candidate = executable.parent if frozen else module_file.parents[2]
    return candidate.resolve()


def runtime_root() -> Path:
    return resolve_runtime_root(
        bool(getattr(sys, "frozen", False)),
        Path(sys.executable),
        Path(__file__),
    )


def configure_process(root: Path) -> Path:
    root = root.resolve()
    temp = root / "data" / "temp"
    roaming = root / "data" / "user-profile" / "Roaming"
    local = root / "data" / "user-profile" / "Local"

    for directory in (temp, roaming, local, root / "downloads"):
        directory.mkdir(parents=True, exist_ok=True)

    os.environ["TEMP"] = str(temp)
    os.environ["TMP"] = str(temp)
    os.environ["APPDATA"] = str(roaming)
    os.environ["LOCALAPPDATA"] = str(local)
    return root
