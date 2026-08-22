from __future__ import annotations

import hashlib
import os
from collections.abc import Callable, Iterable
from contextlib import suppress
from pathlib import Path
from uuid import uuid4

from telegram_downloader.paths import PortablePaths
from telegram_downloader.settings import DownloadStorageSettings


class DownloadPathError(ValueError):
    """Raised when a media root or target violates the download path policy."""


class DownloadPathPolicy:
    def __init__(
        self,
        paths: PortablePaths,
        settings: DownloadStorageSettings,
        *,
        probe: Callable[[Path], None] | None = None,
    ) -> None:
        self.paths = paths
        self.default_root = paths.downloads.resolve()
        self._probe = probe or probe_writable_directory
        self._current_root = self.default_root
        self._roots: dict[str, Path] = {}
        self.apply(settings)

    @property
    def current_root(self) -> Path:
        return self._current_root

    @property
    def roots(self) -> tuple[Path, ...]:
        return tuple(self._roots.values())

    def prepare(self, requested: DownloadStorageSettings) -> DownloadStorageSettings:
        if not isinstance(requested, DownloadStorageSettings):
            raise DownloadPathError("下载存储设置无效")
        selected = self._resolve_setting_root(requested.root)
        self._validate_root(selected)
        self._probe(selected)
        history = [*requested.trusted_roots]
        if selected != self._current_root:
            history.append(str(self._current_root))
        normalized_history = self._normalized_unique_roots(history, exclude=selected)
        saved_root = "" if selected == self.default_root else str(selected)
        return DownloadStorageSettings(saved_root, tuple(map(str, normalized_history)))

    def apply(self, settings: DownloadStorageSettings) -> None:
        if not isinstance(settings, DownloadStorageSettings):
            raise DownloadPathError("下载存储设置无效")
        current = self._resolve_setting_root(settings.root)
        self._validate_root(current)
        history = self._normalized_unique_roots(settings.trusted_roots, exclude=current)
        ordered = (self.default_root, *history, current)
        self._roots = {}
        for root in ordered:
            self._roots[self.root_id(root)] = root
        self._current_root = current

    def require_current_writable(self) -> Path:
        self._probe(self._current_root)
        return self._current_root

    def guard(self, candidate: Path, *, allow_root: bool = False) -> Path:
        resolved = Path(candidate).resolve()
        for root in self.roots:
            try:
                return self.guard_in(root, resolved, allow_root=allow_root)
            except DownloadPathError:
                continue
        raise DownloadPathError(f"媒体路径超出受信下载目录: {resolved}")

    def guard_in(
        self,
        root: Path,
        candidate: Path,
        *,
        allow_root: bool = False,
    ) -> Path:
        trusted = Path(root).resolve()
        if self.root_id(trusted) not in self._roots:
            raise DownloadPathError("下载根目录不受信")
        resolved = Path(candidate).resolve()
        try:
            relative = resolved.relative_to(trusted)
        except ValueError as exc:
            raise DownloadPathError("媒体路径超出指定下载目录") from exc
        if not relative.parts and not allow_root:
            raise DownloadPathError("媒体文件目标不能是下载根目录本身")
        return resolved

    def root_id(self, root: Path) -> str:
        normalized = os.path.normcase(str(Path(root).resolve())).encode("utf-8")
        return f"download-{hashlib.sha256(normalized).hexdigest()[:16]}"

    def root_for_id(self, root_id: str) -> Path:
        try:
            return self._roots[root_id]
        except KeyError as exc:
            raise DownloadPathError("下载根目录标识不受信") from exc

    def _resolve_setting_root(self, value: str) -> Path:
        if not value:
            return self.default_root
        candidate = Path(value)
        if not candidate.is_absolute():
            raise DownloadPathError("下载根目录必须是绝对路径")
        return candidate.resolve()

    def _validate_root(self, root: Path) -> None:
        anchor = Path(root.anchor).resolve()
        if root == anchor or root == self.paths.root:
            raise DownloadPathError("不能使用磁盘、共享或应用根目录")
        if root == self.paths.data or root.is_relative_to(self.paths.data):
            raise DownloadPathError("下载目录不能位于应用内部数据目录")

    def _normalized_unique_roots(
        self,
        values: Iterable[str | Path],
        *,
        exclude: Path,
    ) -> tuple[Path, ...]:
        result: list[Path] = []
        seen = {os.path.normcase(str(exclude))}
        for value in values:
            root = self._resolve_setting_root(str(value))
            self._validate_root(root)
            key = os.path.normcase(str(root))
            if key in seen:
                continue
            seen.add(key)
            result.append(root)
        return tuple(result)


def probe_writable_directory(root: Path) -> None:
    root = Path(root)
    if not root.is_dir():
        raise DownloadPathError("下载根目录不存在")
    target = root / f".telegram-downloader-write-{uuid4().hex}.tmp"
    try:
        with target.open("xb") as stream:
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        with suppress(OSError):
            target.unlink(missing_ok=True)
        raise DownloadPathError("下载根目录当前不可写") from exc
    try:
        target.unlink()
    except OSError as exc:
        raise DownloadPathError("下载目录写入探测文件无法清理") from exc
