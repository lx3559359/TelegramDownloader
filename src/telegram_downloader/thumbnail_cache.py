from __future__ import annotations

import hashlib
import os
from pathlib import Path
from uuid import uuid4


class ThumbnailCache:
    def __init__(
        self,
        root: Path,
        *,
        max_item_bytes: int = 256 * 1024,
        max_total_bytes: int = 1024**3,
        target_total_bytes: int | None = None,
    ) -> None:
        if max_item_bytes < 1 or max_total_bytes < 1:
            raise ValueError("缩略图缓存上限必须大于零")
        if target_total_bytes is None:
            target_total_bytes = min(max_total_bytes, 900 * 1024**2)
        if target_total_bytes < 1 or target_total_bytes > max_total_bytes:
            raise ValueError("缩略图缓存目标必须在上限以内")
        self.root = root.resolve()
        self.max_item_bytes = max_item_bytes
        self.max_total_bytes = max_total_bytes
        self.target_total_bytes = target_total_bytes
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        path = (self.root / f"{digest}.thumb").resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("缩略图路径越出应用目录")
        return path

    def get(self, key: str) -> Path | None:
        path = self.path_for(key)
        if not path.is_file():
            return None
        try:
            os.utime(path, None)
        except FileNotFoundError:
            return None
        return path

    def put(self, key: str, content: bytes) -> Path | None:
        if not content or len(content) > self.max_item_bytes:
            return None
        path = self.path_for(key)
        temporary = self.root / f"{path.name}.{uuid4().hex}.tmp"
        try:
            temporary.write_bytes(content)
            os.replace(temporary, path)
            self._prune()
            return path if path.exists() else None
        finally:
            temporary.unlink(missing_ok=True)

    def delete(self, key: str) -> bool:
        path = self.path_for(key)
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        return True

    def total_bytes(self) -> int:
        total = 0
        for path in self.root.glob("*.thumb"):
            try:
                if path.is_file():
                    total += path.stat().st_size
            except FileNotFoundError:
                continue
        return total

    def clear(self) -> tuple[int, int]:
        removed_count = 0
        removed_bytes = 0
        for path in self.root.glob("*.thumb"):
            try:
                if not path.is_file():
                    continue
                size = path.stat().st_size
                path.unlink()
            except FileNotFoundError:
                continue
            removed_count += 1
            removed_bytes += size
        return removed_count, removed_bytes

    def _prune(self) -> None:
        files: list[tuple[int, str, Path, int]] = []
        total = 0
        for path in self.root.glob("*.thumb"):
            try:
                if not path.is_file():
                    continue
                stat = path.stat()
            except FileNotFoundError:
                continue
            files.append((stat.st_mtime_ns, path.name, path, stat.st_size))
            total += stat.st_size
        if total <= self.max_total_bytes:
            return
        for _modified, _name, path, size in sorted(files):
            try:
                path.unlink()
            except FileNotFoundError:
                continue
            total -= size
            if total <= self.target_total_bytes:
                return
