from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from telegram_downloader.storage_models import (
    StorageCategory,
    StorageCategoryCount,
    StorageCategorySummary,
    StorageMaintenanceState,
    StorageResultCode,
    StorageRunHistory,
    StorageTrigger,
)


class StorageStateError(ValueError):
    """Raised when private maintenance state cannot be trusted."""


_STATE_FIELDS = {
    "schemaVersion",
    "lastScanAt",
    "lastAutomaticCheckAt",
    "lastCleanupAt",
    "nextDueAt",
    "summaries",
    "history",
}
_SUMMARY_FIELDS = {
    "category",
    "scannedAt",
    "totalCount",
    "totalBytes",
    "reclaimableCount",
    "reclaimableBytes",
}
_HISTORY_FIELDS = {
    "occurredAt",
    "trigger",
    "categories",
    "deletedCount",
    "skippedCount",
    "failedCount",
    "cancelledCount",
    "releasedBytes",
    "resultCode",
}
_CATEGORY_COUNT_FIELDS = {
    "category",
    "deletedCount",
    "skippedCount",
    "failedCount",
    "cancelledCount",
    "releasedBytes",
}


def _pairs_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("JSON 字段重复")
        value[key] = item
    return value


def _object(value: Any, fields: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("状态字段集合无效")
    return value


def _items(value: Any) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError("状态数组无效")
    return value


def _integer(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("状态整数无效")
    return value


def _timestamp(value: Any, *, optional: bool = False) -> datetime | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("状态时间必须是 UTC")
    parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    if parsed.tzinfo is not UTC:
        parsed = parsed.astimezone(UTC)
    return parsed


def _timestamp_text(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat().replace("+00:00", "Z")


def _summary_from_json(value: Any) -> StorageCategorySummary:
    item = _object(value, _SUMMARY_FIELDS)
    scanned_at = _timestamp(item["scannedAt"])
    assert scanned_at is not None
    return StorageCategorySummary(
        category=StorageCategory(item["category"]),
        scanned_at=scanned_at,
        total_count=_integer(item["totalCount"]),
        total_bytes=_integer(item["totalBytes"]),
        reclaimable_count=_integer(item["reclaimableCount"]),
        reclaimable_bytes=_integer(item["reclaimableBytes"]),
    )


def _summary_to_json(value: StorageCategorySummary) -> dict[str, Any]:
    return {
        "category": value.category.value,
        "scannedAt": _timestamp_text(value.scanned_at),
        "totalCount": value.total_count,
        "totalBytes": value.total_bytes,
        "reclaimableCount": value.reclaimable_count,
        "reclaimableBytes": value.reclaimable_bytes,
    }


def _category_count_from_json(value: Any) -> StorageCategoryCount:
    item = _object(value, _CATEGORY_COUNT_FIELDS)
    return StorageCategoryCount(
        category=StorageCategory(item["category"]),
        deleted_count=_integer(item["deletedCount"]),
        skipped_count=_integer(item["skippedCount"]),
        failed_count=_integer(item["failedCount"]),
        cancelled_count=_integer(item["cancelledCount"]),
        released_bytes=_integer(item["releasedBytes"]),
    )


def _category_count_to_json(value: StorageCategoryCount) -> dict[str, Any]:
    return {
        "category": value.category.value,
        "deletedCount": value.deleted_count,
        "skippedCount": value.skipped_count,
        "failedCount": value.failed_count,
        "cancelledCount": value.cancelled_count,
        "releasedBytes": value.released_bytes,
    }


def _history_from_json(value: Any) -> StorageRunHistory:
    item = _object(value, _HISTORY_FIELDS)
    occurred_at = _timestamp(item["occurredAt"])
    assert occurred_at is not None
    return StorageRunHistory(
        occurred_at=occurred_at,
        trigger=StorageTrigger(item["trigger"]),
        categories=tuple(
            _category_count_from_json(category)
            for category in _items(item["categories"])
        ),
        deleted_count=_integer(item["deletedCount"]),
        skipped_count=_integer(item["skippedCount"]),
        failed_count=_integer(item["failedCount"]),
        cancelled_count=_integer(item["cancelledCount"]),
        released_bytes=_integer(item["releasedBytes"]),
        result_code=StorageResultCode(item["resultCode"]),
    )


def _history_to_json(value: StorageRunHistory) -> dict[str, Any]:
    return {
        "occurredAt": _timestamp_text(value.occurred_at),
        "trigger": value.trigger.value,
        "categories": [_category_count_to_json(item) for item in value.categories],
        "deletedCount": value.deleted_count,
        "skippedCount": value.skipped_count,
        "failedCount": value.failed_count,
        "cancelledCount": value.cancelled_count,
        "releasedBytes": value.released_bytes,
        "resultCode": value.result_code.value,
    }


class StorageStateStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> StorageMaintenanceState:
        if not self.path.exists():
            return StorageMaintenanceState()
        try:
            text = self.path.read_text(encoding="utf-8")
            raw = json.loads(text, object_pairs_hook=_pairs_without_duplicates)
            value = _object(raw, _STATE_FIELDS)
            schema_version = _integer(value["schemaVersion"])
            if schema_version != 1:
                raise ValueError("未知状态 schema")
            history_items = _items(value["history"])
            if len(history_items) > 20:
                raise ValueError("状态历史超过上限")
            return StorageMaintenanceState(
                schema_version=schema_version,
                last_scan_at=_timestamp(value["lastScanAt"], optional=True),
                last_automatic_check_at=_timestamp(
                    value["lastAutomaticCheckAt"], optional=True
                ),
                last_cleanup_at=_timestamp(value["lastCleanupAt"], optional=True),
                next_due_at=_timestamp(value["nextDueAt"], optional=True),
                summaries=tuple(
                    _summary_from_json(item) for item in _items(value["summaries"])
                ),
                history=tuple(
                    _history_from_json(item) for item in history_items
                ),
            )
        except StorageStateError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise StorageStateError("存储维护记录不可用") from exc

    def save(self, state: StorageMaintenanceState) -> None:
        if not isinstance(state, StorageMaintenanceState):
            raise StorageStateError("存储维护记录不可用")
        bounded = replace(state, history=state.history[-20:])
        value = {
            "schemaVersion": bounded.schema_version,
            "lastScanAt": _timestamp_text(bounded.last_scan_at),
            "lastAutomaticCheckAt": _timestamp_text(
                bounded.last_automatic_check_at
            ),
            "lastCleanupAt": _timestamp_text(bounded.last_cleanup_at),
            "nextDueAt": _timestamp_text(bounded.next_due_at),
            "summaries": [_summary_to_json(item) for item in bounded.summaries],
            "history": [_history_to_json(item) for item in bounded.history],
        }
        content = (
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        try:
            _atomic_write(self.path, content)
        except OSError as exc:
            raise StorageStateError("存储维护记录不可用") from exc


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        with temporary.open("wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
