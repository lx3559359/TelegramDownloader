from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zipfile import ZipFile

import pytest

from telegram_downloader.diagnostic_store import (
    DiagnosticPrivacyError,
    DiagnosticReportStore,
)
from telegram_downloader.diagnostics import (
    DiagnosticReport,
    DiagnosticResult,
    DiagnosticStatus,
)
from telegram_downloader.paths import PortablePaths

NOW = datetime(2026, 8, 16, 8, 9, 10, tzinfo=UTC)


def report(
    *,
    summary: str = "运行环境和项目内路径正常",
    duration_ms: int = 17,
) -> DiagnosticReport:
    result = DiagnosticResult(
        "environment",
        "运行环境与路径",
        DiagnosticStatus.PASSED,
        "runtime-paths-ok",
        summary,
        duration_ms,
        {"guardedPathCount": 15, "nonSystemVolume": True},
    )
    return DiagnosticReport.build(
        "0.10.0",
        NOW,
        NOW + timedelta(seconds=1),
        (result,),
    )


def test_report_serialization_is_canonical_schema_one_and_round_trips(
    tmp_path: Path,
) -> None:
    paths = PortablePaths(tmp_path)
    paths.ensure_layout()
    store = DiagnosticReportStore(paths, secrets=set(), environment_username="tester")

    payload = store.serialize(report())
    saved = store.save(report())
    loaded = store.load_latest()

    assert payload.endswith(b"\n")
    assert json.loads(payload) == {
        "schemaVersion": 1,
        "appVersion": "0.10.0",
        "startedAt": "2026-08-16T08:09:10Z",
        "finishedAt": "2026-08-16T08:09:11Z",
        "status": "passed",
        "results": [
            {
                "id": "environment",
                "title": "运行环境与路径",
                "status": "passed",
                "code": "runtime-paths-ok",
                "summary": "运行环境和项目内路径正常",
                "durationMs": 17,
                "metrics": {"guardedPathCount": 15, "nonSystemVolume": True},
            }
        ],
    }
    assert saved == paths.diagnostics / "latest.json"
    assert saved.read_bytes() == payload
    assert loaded == report()
    assert not (paths.diagnostics / "latest.json.tmp").exists()


def test_latest_loader_tolerates_absent_invalid_and_unknown_fields(tmp_path: Path) -> None:
    paths = PortablePaths(tmp_path)
    paths.ensure_layout()
    store = DiagnosticReportStore(paths, secrets=set())

    assert store.load_latest() is None
    paths.diagnostics.joinpath("latest.json").write_text("not-json", encoding="utf-8")
    assert store.load_latest() is None
    payload = json.loads(store.serialize(report()))
    payload["unknown"] = "value"
    paths.diagnostics.joinpath("latest.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )
    assert store.load_latest() is None
    payload.pop("unknown")
    payload["schemaVersion"] = True
    paths.diagnostics.joinpath("latest.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )
    assert store.load_latest() is None


def test_atomic_save_failure_preserves_previous_report_and_cleans_temp(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = PortablePaths(tmp_path)
    paths.ensure_layout()
    store = DiagnosticReportStore(paths, secrets=set())
    previous = store.save(report()).read_bytes()

    def fail_replace(source: Path, target: Path) -> None:
        raise OSError("private-path")

    monkeypatch.setattr("telegram_downloader.diagnostic_store.os.replace", fail_replace)
    with pytest.raises(OSError):
        store.save(report(duration_ms=18))

    assert paths.diagnostics.joinpath("latest.json").read_bytes() == previous
    assert not paths.diagnostics.joinpath("latest.json.tmp").exists()


@pytest.mark.parametrize(
    "unsafe",
    [
        "+8613812345678",
        "tg://login?token=secret",
        "https://t.me/example/42",
        r"D:\private\file.mp4",
        r"\\server\private\file.mp4",
    ],
)
def test_privacy_validator_rejects_unsafe_strings(tmp_path: Path, unsafe: str) -> None:
    store = DiagnosticReportStore(PortablePaths(tmp_path), secrets=set())

    with pytest.raises(DiagnosticPrivacyError):
        store.validate_value(unsafe)


def test_privacy_validator_rejects_registered_values_root_and_username(
    tmp_path: Path,
) -> None:
    store = DiagnosticReportStore(
        PortablePaths(tmp_path),
        secrets={"api-secret", "private-video.mp4"},
        environment_username="private-user",
    )

    for unsafe in (
        "prefix api-secret suffix",
        "private-video.mp4",
        str(tmp_path),
        "account private-user",
    ):
        with pytest.raises(DiagnosticPrivacyError):
            store.validate_value(unsafe)


def test_runtime_credentials_can_be_added_before_export(tmp_path: Path) -> None:
    store = DiagnosticReportStore(PortablePaths(tmp_path), secrets=set())

    store.register_secrets({"new-session-secret"})

    with pytest.raises(DiagnosticPrivacyError):
        store.validate_value("new-session-secret")


def test_store_rejects_metric_keys_outside_each_probe_allowlist(tmp_path: Path) -> None:
    unsafe = DiagnosticReport.build(
        "0.10.0",
        NOW,
        NOW,
        (
            DiagnosticResult(
                "environment",
                "运行环境与路径",
                DiagnosticStatus.PASSED,
                "runtime-paths-ok",
                "运行环境和项目内路径正常",
                1,
                {"accountName": "private-account"},
            ),
        ),
    )
    store = DiagnosticReportStore(PortablePaths(tmp_path), secrets=set())

    with pytest.raises(DiagnosticPrivacyError, match="指标"):
        store.serialize(unsafe)


def test_store_rejects_private_text_in_typed_allowlisted_metric(tmp_path: Path) -> None:
    unsafe = DiagnosticReport.build(
        "0.10.0",
        NOW,
        NOW,
        (
            DiagnosticResult(
                "environment",
                "运行环境与路径",
                DiagnosticStatus.PASSED,
                "runtime-paths-ok",
                "运行环境和项目内路径正常",
                1,
                {"guardedPathCount": "private-account"},
            ),
        ),
    )
    store = DiagnosticReportStore(PortablePaths(tmp_path), secrets=set())

    with pytest.raises(DiagnosticPrivacyError, match="类型"):
        store.serialize(unsafe)


def test_store_rejects_non_fixed_summary_even_without_registered_secret(
    tmp_path: Path,
) -> None:
    store = DiagnosticReportStore(PortablePaths(tmp_path), secrets=set())

    with pytest.raises(DiagnosticPrivacyError, match="说明"):
        store.serialize(report(summary="private-group-name"))


def test_export_contains_exact_allowlisted_entries_and_uses_collision_suffix(
    tmp_path: Path,
) -> None:
    paths = PortablePaths(tmp_path)
    paths.ensure_layout()
    store = DiagnosticReportStore(
        paths,
        secrets={"api-secret", "private-video.mp4"},
        environment_username="private-user",
    )

    first = store.export(report())
    second = store.export(report())

    assert first.name == "TelegramDownloader-diagnostics-20260816T080911Z.zip"
    assert second.name == "TelegramDownloader-diagnostics-20260816T080911Z.2.zip"
    for package in (first, second):
        with ZipFile(package) as archive:
            assert sorted(archive.namelist()) == [
                "diagnostic-report.json",
                "diagnostic-summary.txt",
            ]
            payload = b"\n".join(archive.read(name) for name in archive.namelist())
        assert b"api-secret" not in payload
        assert b"private-video.mp4" not in payload
        assert str(tmp_path).encode() not in payload
    assert list(paths.diagnostic_temp.iterdir()) == []


def test_export_flushes_completed_zip_before_atomic_move(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = PortablePaths(tmp_path)
    paths.ensure_layout()
    store = DiagnosticReportStore(paths, secrets=set())
    flushed: list[Path] = []
    monkeypatch.setattr(
        "telegram_downloader.diagnostic_store._sync_file",
        flushed.append,
    )

    package = store.export(report())

    assert package.is_file()
    assert len(flushed) == 1
    assert flushed[0].parent == paths.diagnostic_temp


def test_export_rejects_sensitive_report_before_writing(tmp_path: Path) -> None:
    paths = PortablePaths(tmp_path)
    paths.ensure_layout()
    store = DiagnosticReportStore(paths, secrets={"api-secret"})

    with pytest.raises(DiagnosticPrivacyError):
        store.export(report(summary="泄露 api-secret"))

    assert list(paths.diagnostics.iterdir()) == []
    assert list(paths.diagnostic_temp.iterdir()) == []


def test_export_failure_removes_only_guarded_temporary_file(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = PortablePaths(tmp_path)
    paths.ensure_layout()
    store = DiagnosticReportStore(paths, secrets=set())
    preserved = paths.diagnostics / "preserved.zip"
    preserved.write_bytes(b"keep")

    def fail_replace(source: Path, target: Path) -> None:
        raise OSError("failed")

    monkeypatch.setattr("telegram_downloader.diagnostic_store.os.replace", fail_replace)
    with pytest.raises(OSError):
        store.export(report())

    assert preserved.read_bytes() == b"keep"
    assert list(paths.diagnostic_temp.iterdir()) == []
