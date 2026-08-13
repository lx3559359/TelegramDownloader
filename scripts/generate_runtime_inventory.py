from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path

_VERSION = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--version", required=True)
    arguments = parser.parse_args()
    root = arguments.root.resolve()
    if _VERSION.fullmatch(arguments.version) is None:
        raise ValueError("version must be strict X.Y.Z")
    output = root / "runtime-manifest.json"
    files = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix().casefold()):
        if not path.is_file() or path == output:
            continue
        relative = path.relative_to(root).as_posix()
        if relative.split("/", 1)[0].casefold() in {"data", "downloads"}:
            continue
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        files.append({"path": relative, "sha256": digest.hexdigest(), "size": path.stat().st_size})
    required = {item["path"].casefold() for item in files}
    if not {"telegramdownloader.exe", "updatehelper.exe"}.issubset(required):
        raise RuntimeError("packaged runtime is missing an executable")
    content = (
        json.dumps(
            {
                "files": files,
                "schemaVersion": 1,
                "version": arguments.version,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode()
    temporary = output.with_suffix(".json.tmp")
    with temporary.open("wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
