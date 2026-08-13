from __future__ import annotations

import argparse
import hashlib
from collections.abc import Sequence
from pathlib import Path


def compare_release_directories(
    expected: Path,
    actual: Path,
    names: Sequence[str],
) -> None:
    if sorted(path.name for path in expected.iterdir()) != sorted(names):
        raise ValueError("expected release directory has an invalid file set")
    if sorted(path.name for path in actual.iterdir()) != sorted(names):
        raise ValueError("remote release directory has an invalid file set")
    for name in names:
        left = expected / name
        right = actual / name
        if left.stat().st_size != right.stat().st_size or _sha256(left) != _sha256(right):
            raise ValueError(f"release asset differs: {name}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected", type=Path, required=True)
    parser.add_argument("--actual", type=Path, required=True)
    parser.add_argument("--names", nargs="+", required=True)
    arguments = parser.parse_args()
    try:
        compare_release_directories(arguments.expected, arguments.actual, arguments.names)
    except (OSError, ValueError):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
