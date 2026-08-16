import json
from pathlib import Path

from telegram_downloader import app
from telegram_downloader.app import run_self_test


def test_self_test_reports_only_paths_under_root(tmp_path: Path) -> None:
    report = run_self_test(tmp_path)

    assert report["ok"] is True
    assert report["version"] == "0.9.0"
    root = tmp_path.resolve()
    assert all(
        Path(value).is_relative_to(root)
        for value in report["writable_paths"].values()
    )
    report_path = tmp_path / "data" / "logs" / "self-test.json"
    disk_report = json.loads(report_path.read_text(encoding="utf-8"))
    assert disk_report == report


def test_self_test_includes_update_storage_and_database(tmp_path: Path) -> None:
    report = run_self_test(tmp_path)

    assert report["catalog_schema_version"] == 3
    assert "update_staging" in report["writable_paths"]
    assert "catalog_database" in report["writable_paths"]
    assert "thumbnail_cache" in report["writable_paths"]
    assert (tmp_path / "data" / "database" / "tasks.sqlite3").is_file()
    assert (tmp_path / "data" / "database" / "catalog.sqlite3").is_file()


def test_self_test_verifies_frozen_runtime_components_without_secrets(tmp_path: Path) -> None:
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
    assert len(report["writable_paths"]) == 13
