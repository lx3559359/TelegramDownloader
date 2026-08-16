from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile

from telegram_downloader.diagnostics import (
    DiagnosticReport,
    DiagnosticResult,
    DiagnosticStatus,
)
from telegram_downloader.paths import PortablePaths

_REPORT_KEYS = frozenset(
    {"schemaVersion", "appVersion", "startedAt", "finishedAt", "status", "results"}
)
_RESULT_KEYS = frozenset(
    {"id", "title", "status", "code", "summary", "durationMs", "metrics"}
)
_PHONE = re.compile(r"(?<!\d)(?:\+\d{7,15}|1[3-9]\d{9})(?!\d)")
_TELEGRAM_URL = re.compile(
    r"(?i)(?:tg://|https?://(?:www\.)?(?:t\.me|telegram\.me)/)"
)
_DRIVE_PATH = re.compile(r"(?i)[a-z]:[\\/]")


class DiagnosticPrivacyError(ValueError):
    pass


class DiagnosticReportStore:
    def __init__(
        self,
        paths: PortablePaths,
        *,
        secrets: Iterable[str],
        environment_username: str | None = None,
    ) -> None:
        self.paths = paths
        self.secrets: tuple[str, ...] = ()
        self.register_secrets(secrets)
        username = environment_username
        if username is None:
            username = os.environ.get("USERNAME") or os.environ.get("USER") or ""
        self.environment_username = username.casefold().strip()
        root = str(paths.root.resolve())
        self.root_markers = tuple(
            marker.casefold()
            for marker in {root, root.replace("\\", "/"), root.replace("/", "\\")}
            if marker
        )

    def register_secrets(self, values: Iterable[str]) -> None:
        registered = set(self.secrets)
        registered.update(
            value.casefold()
            for value in values
            if isinstance(value, str) and value
        )
        self.secrets = tuple(
            sorted(
                registered,
                key=len,
                reverse=True,
            )
        )

    def serialize(self, report: DiagnosticReport) -> bytes:
        document = _report_document(report)
        self.validate_value(document)
        return (
            json.dumps(
                document,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )

    def deserialize(self, payload: bytes) -> DiagnosticReport:
        value = json.loads(payload.decode("utf-8"))
        self.validate_value(value)
        return _parse_report(value)

    def save(self, report: DiagnosticReport) -> Path:
        payload = self.serialize(report)
        directory = self.paths.guard(self.paths.diagnostics)
        directory.mkdir(parents=True, exist_ok=True)
        target = self.paths.guard(directory / "latest.json")
        temporary = self.paths.guard(directory / "latest.json.tmp")
        with suppress(OSError):
            temporary.unlink(missing_ok=True)
        try:
            _durable_write(temporary, payload)
            os.replace(temporary, target)
        except Exception:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
            raise
        return target

    def load_latest(self) -> DiagnosticReport | None:
        target = self.paths.guard(self.paths.diagnostics / "latest.json")
        try:
            return self.deserialize(target.read_bytes())
        except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
            return None

    def export(self, report: DiagnosticReport) -> Path:
        report_payload = self.serialize(report)
        summary_payload = self._summary(report).encode("utf-8")
        self.validate_value(summary_payload.decode("utf-8"))
        self.paths.guard(self.paths.diagnostics).mkdir(parents=True, exist_ok=True)
        temporary_directory = self.paths.guard(self.paths.diagnostic_temp)
        temporary_directory.mkdir(parents=True, exist_ok=True)
        temporary = self.paths.guard(
            temporary_directory / f"diagnostic-export-{uuid4().hex}.tmp"
        )
        destination = self._next_export_path(report.finished_at)
        try:
            with ZipFile(temporary, "x", compression=ZIP_DEFLATED) as archive:
                archive.writestr("diagnostic-report.json", report_payload)
                archive.writestr("diagnostic-summary.txt", summary_payload)
            self._verify_export(temporary, report_payload, summary_payload)
            os.replace(temporary, destination)
        except Exception:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
            raise
        return destination

    def validate_value(self, value: object) -> None:
        if isinstance(value, str):
            self._validate_string(value)
            return
        if value is None or isinstance(value, (bool, int, float)):
            return
        if isinstance(value, Mapping):
            for key, item in value.items():
                if not isinstance(key, str):
                    raise DiagnosticPrivacyError("诊断数据键类型不安全")
                self._validate_string(key)
                self.validate_value(item)
            return
        if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
            for item in value:
                self.validate_value(item)
            return
        raise DiagnosticPrivacyError("诊断数据包含不允许的类型")

    def _validate_string(self, value: str) -> None:
        folded = value.casefold()
        if any(secret in folded for secret in self.secrets):
            raise DiagnosticPrivacyError("诊断数据包含已登记私密值")
        if self.environment_username and self.environment_username in folded:
            raise DiagnosticPrivacyError("诊断数据包含环境用户名")
        if any(marker in folded for marker in self.root_markers):
            raise DiagnosticPrivacyError("诊断数据包含应用根路径")
        if _PHONE.search(value) is not None:
            raise DiagnosticPrivacyError("诊断数据包含电话号码")
        if _TELEGRAM_URL.search(value) is not None:
            raise DiagnosticPrivacyError("诊断数据包含 Telegram 链接")
        if _DRIVE_PATH.search(value) is not None or value.startswith("\\\\"):
            raise DiagnosticPrivacyError("诊断数据包含绝对路径")

    def _summary(self, report: DiagnosticReport) -> str:
        lines = [
            "Telegram Downloader 健康诊断摘要",
            f"应用版本：{report.app_version}",
            f"报告状态：{report.status.value}",
            f"开始时间：{_utc_text(report.started_at)}",
            f"结束时间：{_utc_text(report.finished_at)}",
            "",
        ]
        lines.extend(
            f"- [{item.status.value}] {item.title}：{item.summary}（{item.duration_ms} ms）"
            for item in report.results
        )
        return "\n".join(lines) + "\n"

    def _next_export_path(self, finished_at: datetime) -> Path:
        timestamp = finished_at.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
        stem = f"TelegramDownloader-diagnostics-{timestamp}"
        directory = self.paths.guard(self.paths.diagnostics)
        candidate = self.paths.guard(directory / f"{stem}.zip")
        suffix = 2
        while candidate.exists():
            candidate = self.paths.guard(directory / f"{stem}.{suffix}.zip")
            suffix += 1
        return candidate

    def _verify_export(
        self,
        package: Path,
        report_payload: bytes,
        summary_payload: bytes,
    ) -> None:
        expected = {
            "diagnostic-report.json": report_payload,
            "diagnostic-summary.txt": summary_payload,
        }
        with ZipFile(package) as archive:
            if set(archive.namelist()) != set(expected) or len(archive.infolist()) != 2:
                raise DiagnosticPrivacyError("诊断包条目不符合白名单")
            for name, payload in expected.items():
                content = archive.read(name)
                if content != payload:
                    raise DiagnosticPrivacyError("诊断包内容校验失败")
                self.validate_value(content.decode("utf-8"))


def _report_document(report: DiagnosticReport) -> dict[str, object]:
    return {
        "schemaVersion": report.schema_version,
        "appVersion": report.app_version,
        "startedAt": _utc_text(report.started_at),
        "finishedAt": _utc_text(report.finished_at),
        "status": report.status.value,
        "results": [
            {
                "id": item.id,
                "title": item.title,
                "status": item.status.value,
                "code": item.code,
                "summary": item.summary,
                "durationMs": item.duration_ms,
                "metrics": dict(item.metrics),
            }
            for item in report.results
        ],
    }


def _parse_report(value: object) -> DiagnosticReport:
    if not isinstance(value, dict) or set(value) != _REPORT_KEYS:
        raise ValueError("诊断报告字段无效")
    results_value = value["results"]
    if not isinstance(results_value, list):
        raise ValueError("诊断结果列表无效")
    results = tuple(_parse_result(item) for item in results_value)
    report = DiagnosticReport(
        _required_int(value["schemaVersion"]),
        _required_string(value["appVersion"]),
        _parse_utc(value["startedAt"]),
        _parse_utc(value["finishedAt"]),
        DiagnosticStatus(_required_string(value["status"])),
        results,
    )
    expected = DiagnosticReport.build(
        report.app_version,
        report.started_at,
        report.finished_at,
        report.results,
        cancelled=report.status is DiagnosticStatus.CANCELLED,
    ).status
    if report.status is not expected:
        raise ValueError("诊断报告总状态不一致")
    return report


def _parse_result(value: object) -> DiagnosticResult:
    if not isinstance(value, dict) or set(value) != _RESULT_KEYS:
        raise ValueError("诊断结果字段无效")
    metrics = value["metrics"]
    if not isinstance(metrics, dict):
        raise ValueError("诊断指标无效")
    return DiagnosticResult(
        _required_string(value["id"]),
        _required_string(value["title"]),
        DiagnosticStatus(_required_string(value["status"])),
        _required_string(value["code"]),
        _required_string(value["summary"]),
        _required_int(value["durationMs"]),
        metrics,
    )


def _parse_utc(value: object) -> datetime:
    text = _required_string(value)
    if not text.endswith("Z"):
        raise ValueError("诊断时间不是 UTC")
    return datetime.fromisoformat(f"{text[:-1]}+00:00")


def _utc_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _required_string(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("诊断字符串字段无效")
    return value


def _required_int(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("诊断整数字段无效")
    return value


def _durable_write(path: Path, payload: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
