from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class PathOutsideRootError(ValueError):
    """Raised when an application-managed path is not below the runtime root."""


@dataclass(frozen=True, slots=True)
class PortablePaths:
    root: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", self.root.resolve())

    @property
    def data(self) -> Path:
        return self.root / "data"

    @property
    def settings(self) -> Path:
        return self.data / "config" / "settings.json"

    @property
    def secrets(self) -> Path:
        return self.data / "config" / "secrets.dat"

    @property
    def database(self) -> Path:
        return self.data / "database" / "tasks.sqlite3"

    @property
    def catalog_database(self) -> Path:
        return self.data / "database" / "catalog.sqlite3"

    @property
    def log(self) -> Path:
        return self.data / "logs" / "app.log"

    @property
    def cache(self) -> Path:
        return self.data / "cache"

    @property
    def thumbnail_cache(self) -> Path:
        return self.cache / "thumbnails"

    @property
    def temp(self) -> Path:
        return self.data / "temp"

    @property
    def diagnostics(self) -> Path:
        return self.data / "diagnostics"

    @property
    def diagnostic_temp(self) -> Path:
        return self.temp / "diagnostics"

    @property
    def maintenance(self) -> Path:
        return self.data / "maintenance"

    @property
    def storage_maintenance_state(self) -> Path:
        return self.maintenance / "storage-state.json"

    @property
    def update(self) -> Path:
        return self.data / "update"

    @property
    def update_staging(self) -> Path:
        return self.update / "staging"

    @property
    def update_backup(self) -> Path:
        return self.update / "backup"

    @property
    def update_helper(self) -> Path:
        return self.update / "helper"

    @property
    def update_journal(self) -> Path:
        return self.update / "journal.json"

    @property
    def downloads(self) -> Path:
        return self.root / "downloads"

    def guard(self, candidate: Path) -> Path:
        if not candidate.is_absolute():
            raise PathOutsideRootError(f"路径必须是绝对路径: {candidate}")

        resolved = candidate.resolve()
        try:
            relative = resolved.relative_to(self.root)
        except ValueError as exc:
            raise PathOutsideRootError(f"路径超出应用目录: {resolved}") from exc
        if not relative.parts:
            raise PathOutsideRootError("不允许把应用根目录本身作为写入目标")
        return resolved

    def ensure_layout(self) -> None:
        directories = {
            self.settings.parent,
            self.database.parent,
            self.log.parent,
            self.cache,
            self.thumbnail_cache,
            self.temp,
            self.diagnostics,
            self.diagnostic_temp,
            self.maintenance,
            self.update_staging,
            self.update_backup,
            self.update_helper,
            self.downloads,
        }
        for directory in directories:
            self.guard(directory).mkdir(parents=True, exist_ok=True)
