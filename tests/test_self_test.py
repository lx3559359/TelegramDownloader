import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from telegram_downloader import __version__, app
from telegram_downloader.app import run_self_test
from telegram_downloader.catalog import CatalogRepository
from telegram_downloader.content import AccountProfile, ContentDialog, DialogKind
from telegram_downloader.domain import (
    ItemStatus,
    MediaItem,
    MediaKind,
    ScanFilters,
    SourceKind,
    TaskRecord,
    TaskStatus,
)
from telegram_downloader.paths import PortablePaths
from telegram_downloader.repository import TaskRepository
from telegram_downloader.subscription_matching import SubscriptionCriteria
from telegram_downloader.subscriptions import SubscriptionRule, SubscriptionState


def _business_state_projection(paths: PortablePaths) -> tuple[object, ...]:
    with sqlite3.connect(paths.database) as connection:
        task_state = connection.execute(
            "SELECT id, status, updated_at, pause_reason FROM tasks ORDER BY id"
        ).fetchall()
        media_state = connection.execute(
            "SELECT id, status, downloaded_bytes, retry_count, last_error, "
            "integrity_status, verified_at FROM media_items ORDER BY id"
        ).fetchall()
    with sqlite3.connect(paths.catalog_database) as connection:
        subscription_state = connection.execute(
            "SELECT id, state, next_run_at, last_run_at, last_error, failure_count, "
            "updated_at FROM subscription_rules ORDER BY id"
        ).fetchall()
    return task_state, media_state, subscription_state


def _seed_live_business_state(paths: PortablePaths) -> None:
    now = datetime(2026, 8, 25, 12, tzinfo=UTC)
    repository = TaskRepository(paths.database)
    repository.initialize()
    task = TaskRecord(
        "task-live",
        SourceKind.CHANNEL_OR_GROUP,
        "peer-live",
        "测试频道",
        "https://t.me/peer-live",
        ScanFilters(now, now, frozenset({MediaKind.VIDEO}), 1),
        TaskStatus.DOWNLOADING,
        now,
        now,
    )
    item = MediaItem(
        "item-live",
        task.id,
        "peer-live",
        7,
        None,
        "media-live",
        MediaKind.VIDEO,
        "live.mp4",
        paths.downloads / "live.mp4",
        8,
        now,
        downloaded_bytes=4,
        status=ItemStatus.DOWNLOADING,
    )
    repository.create_task(task, [item])

    catalog = CatalogRepository(paths.catalog_database)
    catalog.initialize()
    catalog.upsert_account(AccountProfile("account-live", "测试账号"), now)
    catalog.replace_dialogs(
        "account-live",
        [
            ContentDialog(
                "account-live",
                "peer-live",
                "测试频道",
                "peer-live",
                DialogKind.CHANNEL,
                False,
                True,
                now,
            )
        ],
        now,
    )
    catalog.save_subscription(
        SubscriptionRule(
            id="rule-live",
            account_id="account-live",
            peer_ref="peer-live",
            dialog_title="测试频道",
            criteria=SubscriptionCriteria(("测试",)),
            media_kinds=frozenset({MediaKind.VIDEO}),
            interval_minutes=30,
            history_days=0,
            enabled=True,
            state=SubscriptionState.RUNNING,
            last_message_id=7,
            backfill_from_utc=None,
            backfill_through_id=None,
            next_run_at=None,
            last_run_at=now,
            last_error=None,
            failure_count=0,
            created_at=now,
            updated_at=now,
        )
    )


def test_self_test_does_not_recover_live_business_state(tmp_path: Path) -> None:
    paths = PortablePaths(tmp_path)
    paths.ensure_layout()
    _seed_live_business_state(paths)
    before = _business_state_projection(paths)

    report = run_self_test(tmp_path)

    assert report["ok"] is True
    assert _business_state_projection(paths) == before


def test_self_test_reports_only_paths_under_root(tmp_path: Path) -> None:
    report = run_self_test(tmp_path)

    assert report["ok"] is True
    assert report["version"] == __version__
    root = tmp_path.resolve()
    assert all(Path(value).is_relative_to(root) for value in report["writable_paths"].values())
    report_path = tmp_path / "data" / "logs" / "self-test.json"
    disk_report = json.loads(report_path.read_text(encoding="utf-8"))
    assert disk_report == report


def test_self_test_includes_update_storage_and_database(tmp_path: Path) -> None:
    report = run_self_test(tmp_path)

    assert report["catalog_schema_version"] == 5
    assert "update_staging" in report["writable_paths"]
    assert "catalog_database" in report["writable_paths"]
    assert "thumbnail_cache" in report["writable_paths"]
    assert report["writable_paths"]["maintenance_state"] == str(
        (tmp_path / "data" / "maintenance" / "storage-state.json").resolve()
    )
    assert (tmp_path / "data" / "database" / "tasks.sqlite3").is_file()
    assert (tmp_path / "data" / "database" / "catalog.sqlite3").is_file()


def test_self_test_verifies_frozen_runtime_components_without_secrets(tmp_path: Path) -> None:
    state_path = tmp_path / "data" / "maintenance" / "storage-state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        '{"privateMarker":"never-export-this-maintenance-content"}',
        encoding="utf-8",
    )
    report = run_self_test(tmp_path)

    assert report["components"] == {
        "pyside6": True,
        "telethon": True,
        "qasync": True,
        "qrcode": True,
        "sqlite": True,
        "dpapi": True,
    }
    serialized = json.dumps(report, ensure_ascii=False)
    assert "tg://login?token=" not in serialized
    assert "api_hash" not in serialized
    assert "session" not in serialized
    assert "account_id" not in serialized
    assert "keyword" not in serialized
    assert "message_text" not in serialized
    assert "never-export-this-maintenance-content" not in serialized


def test_self_test_reuses_diagnostic_component_and_path_helpers(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: list[str] = []
    original_paths = app.managed_writable_paths
    original_components = app.component_availability

    def paths_probe(paths):
        calls.append("paths")
        return original_paths(paths)

    def component_probe():
        calls.append("components")
        return original_components()

    monkeypatch.setattr(app, "managed_writable_paths", paths_probe)
    monkeypatch.setattr(app, "component_availability", component_probe)

    report = run_self_test(tmp_path)

    assert calls == ["paths", "components"]
    assert len(report["writable_paths"]) == 14
