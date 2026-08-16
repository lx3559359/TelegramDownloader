from __future__ import annotations

from collections import namedtuple
from pathlib import Path

import pytest

from telegram_downloader.diagnostic_probes import (
    GIB,
    MIB,
    component_availability,
    managed_writable_paths,
    probe_components,
    probe_disk,
    probe_environment,
    probe_project_write,
)
from telegram_downloader.diagnostics import DiagnosticStatus
from telegram_downloader.paths import PortablePaths

DiskUsage = namedtuple("DiskUsage", "total used free")


def usage(total: int, free: int) -> DiskUsage:
    return DiskUsage(total, total - free, free)


def test_managed_writable_paths_are_guarded_and_include_diagnostics(tmp_path: Path) -> None:
    paths = PortablePaths(tmp_path)
    values = managed_writable_paths(paths)

    assert len(values) == 15
    assert values["diagnostics"] == paths.diagnostics
    assert values["diagnosticTemp"] == paths.diagnostic_temp
    assert all(paths.guard(path) == path.resolve() for path in values.values())


def test_environment_probe_requires_non_system_volume_for_frozen_runtime(
    tmp_path: Path,
) -> None:
    paths = PortablePaths(tmp_path)
    system_drive = f"{tmp_path.drive}\\"

    failed = probe_environment(
        paths,
        frozen=True,
        windows_x64=True,
        system_drive=system_drive,
    )
    source_warning = probe_environment(
        paths,
        frozen=False,
        windows_x64=True,
        system_drive=system_drive,
    )
    passed = probe_environment(
        paths,
        frozen=True,
        windows_x64=True,
        system_drive="C:\\" if tmp_path.drive.casefold() != "c:" else "Z:\\",
    )

    assert failed.status is DiagnosticStatus.FAILED
    assert failed.code == "runtime-system-volume"
    assert source_warning.status is DiagnosticStatus.WARNING
    assert source_warning.code == "source-system-volume"
    assert passed.status is DiagnosticStatus.PASSED
    assert passed.metrics == {
        "frozen": True,
        "windowsX64": True,
        "nonSystemVolume": True,
        "guardedPathCount": 15,
    }


def test_environment_probe_fails_unsupported_runtime(tmp_path: Path) -> None:
    result = probe_environment(
        PortablePaths(tmp_path),
        frozen=True,
        windows_x64=False,
        system_drive="C:\\",
    )

    assert result.status is DiagnosticStatus.FAILED
    assert result.code == "runtime-unsupported"


def test_disk_probe_uses_fixed_thresholds(tmp_path: Path) -> None:
    paths = PortablePaths(tmp_path)

    passed = probe_disk(paths, lambda _: usage(10 * GIB, 2 * GIB))
    warning = probe_disk(paths, lambda _: usage(10 * GIB, 512 * MIB))
    failed = probe_disk(paths, lambda _: usage(10 * GIB, 128 * MIB))

    assert (passed.status, passed.code) == (
        DiagnosticStatus.PASSED,
        "disk-space-ok",
    )
    assert (warning.status, warning.code) == (
        DiagnosticStatus.WARNING,
        "disk-space-low",
    )
    assert (failed.status, failed.code) == (
        DiagnosticStatus.FAILED,
        "disk-space-critical",
    )
    assert warning.metrics == {
        "totalBytes": 10 * GIB,
        "freeBytes": 512 * MIB,
    }


def test_disk_probe_maps_provider_failure_to_fixed_safe_result(tmp_path: Path) -> None:
    def fail(_path: Path) -> DiskUsage:
        raise OSError(r"D:\\private\\disk")

    result = probe_disk(PortablePaths(tmp_path), fail)

    assert result.status is DiagnosticStatus.FAILED
    assert result.code == "disk-unavailable"
    assert "private" not in result.summary
    assert result.metrics == {}


def test_write_probe_cleans_project_local_marker(tmp_path: Path) -> None:
    paths = PortablePaths(tmp_path)
    paths.ensure_layout()

    result = probe_project_write(paths, marker=b"diagnostic", token_factory=lambda: "fixed")

    assert result.status is DiagnosticStatus.PASSED
    assert result.code == "project-write-ok"
    assert list(paths.diagnostic_temp.iterdir()) == []


def test_write_probe_cleans_partial_marker_after_failure(tmp_path: Path) -> None:
    paths = PortablePaths(tmp_path)
    paths.ensure_layout()

    def failed_writer(path: Path, content: bytes) -> None:
        path.write_bytes(content[:1])
        raise OSError(r"D:\\private\\failed")

    result = probe_project_write(
        paths,
        marker=b"diagnostic",
        token_factory=lambda: "fixed",
        writer=failed_writer,
    )

    assert result.status is DiagnosticStatus.FAILED
    assert result.code == "project-write-failed"
    assert "private" not in result.summary
    assert list(paths.diagnostic_temp.iterdir()) == []


def test_component_availability_uses_six_fixed_keys() -> None:
    imported: list[str] = []

    def importer(name: str) -> object:
        imported.append(name)
        if name == "qrcode":
            raise ImportError(name)
        return object()

    availability = component_availability(importer, dpapi_available=True)

    assert availability == {
        "pyside6": True,
        "telethon": True,
        "qasync": True,
        "qrcode": False,
        "sqlite": True,
        "dpapi": True,
    }
    assert imported == ["PySide6", "telethon", "qasync", "qrcode", "sqlite3"]


@pytest.mark.parametrize("missing", ["pyside6", "telethon", "qasync", "qrcode", "sqlite", "dpapi"])
def test_components_probe_fails_when_any_required_component_is_missing(
    missing: str,
) -> None:
    availability = {
        "pyside6": True,
        "telethon": True,
        "qasync": True,
        "qrcode": True,
        "sqlite": True,
        "dpapi": True,
    }
    availability[missing] = False

    result = probe_components(availability)

    assert result.status is DiagnosticStatus.FAILED
    assert result.code == "component-missing"
    assert result.metrics == availability
