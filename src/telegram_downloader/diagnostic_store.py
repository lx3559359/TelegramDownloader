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
from telegram_downloader.gateway import AuthorizationFailureReason
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
_ALLOWED_METRICS = {
    "environment": frozenset(
        {"frozen", "windowsX64", "nonSystemVolume", "guardedPathCount"}
    ),
    "project-write": frozenset({"downloadWritable"}),
    "disk": frozenset(
        {
            "totalBytes",
            "freeBytes",
            "downloadSameVolume",
            "downloadTotalBytes",
            "downloadFreeBytes",
        }
    ),
    "components": frozenset(
        {"pyside6", "telethon", "qasync", "qrcode", "sqlite", "dpapi"}
    ),
    "task-database": frozenset(
        {
            "taskCount",
            "mediaCount",
            "schemaCompatible",
            "foreignKeysValid",
            "stateValuesValid",
            "taskStatusDraft",
            "taskStatusQueued",
            "taskStatusScanning",
            "taskStatusDownloading",
            "taskStatusWaitingRetry",
            "taskStatusPaused",
            "taskStatusCompleted",
            "taskStatusPartialFailure",
            "taskStatusOther",
            "itemStatusQueued",
            "itemStatusDownloading",
            "itemStatusWaitingRetry",
            "itemStatusPaused",
            "itemStatusCompleted",
            "itemStatusFailed",
            "itemStatusOther",
            "integrityStatusUnverified",
            "integrityStatusVerified",
            "integrityStatusMissing",
            "integrityStatusSizeMismatch",
            "integrityStatusHashMismatch",
            "integrityStatusReadError",
            "integrityStatusOther",
        }
    ),
    "content-database": frozenset(
        {
            "schemaVersion",
            "schemaCompatible",
            "foreignKeysValid",
            "stateValuesValid",
            "accountCount",
            "dialogCount",
            "searchCount",
            "searchResultCount",
            "subscriptionCount",
            "subscriptionRunCount",
        }
    ),
    "credentials": frozenset(
        {
            "settingsReadable",
            "secretsPresent",
            "secretsDecryptable",
            "credentialsConfigured",
        }
    ),
    "telegram": frozenset({"authorizationReason"}),
    "updates": frozenset(
        {
            "githubStatus",
            "githubLatencyMs",
            "githubVersion",
            "modelscopeStatus",
            "modelscopeLatencyMs",
            "modelscopeVersion",
        }
    ),
}
_STATUS_TEXT = {
    DiagnosticStatus.PASSED: "正常",
    DiagnosticStatus.WARNING: "需关注",
    DiagnosticStatus.FAILED: "异常",
    DiagnosticStatus.SKIPPED: "已跳过",
    DiagnosticStatus.CANCELLED: "已取消",
}
_BOOLEAN_METRICS = frozenset(
    {
        "frozen",
        "windowsX64",
        "nonSystemVolume",
        "downloadWritable",
        "downloadSameVolume",
        "schemaCompatible",
        "foreignKeysValid",
        "stateValuesValid",
        "pyside6",
        "telethon",
        "qasync",
        "qrcode",
        "sqlite",
        "dpapi",
        "settingsReadable",
        "secretsPresent",
        "secretsDecryptable",
        "credentialsConfigured",
    }
)
_SOURCE_STATUS_METRICS = frozenset({"githubStatus", "modelscopeStatus"})
_VERSION_METRICS = frozenset({"githubVersion", "modelscopeVersion"})
_AUTHORIZATION_REASON_METRICS = frozenset({"authorizationReason"})
_SAFE_AUTHORIZATION_REASONS = frozenset(
    reason.value for reason in AuthorizationFailureReason
)
_SAFE_VERSION = re.compile(r"\d+\.\d+\.\d+\Z")
_RESULT_TITLES = {
    "environment": "运行环境与路径",
    "project-write": "项目内写入",
    "disk": "磁盘空间",
    "components": "运行组件",
    "task-database": "下载任务数据库",
    "content-database": "账号内容数据库",
    "credentials": "登录凭据",
    "telegram": "Telegram 连接",
    "updates": "签名更新源",
}
_GENERIC_VARIANTS = frozenset(
    {
        (DiagnosticStatus.FAILED, "probe-failed", "检查执行失败"),
        (DiagnosticStatus.CANCELLED, "check-cancelled", "检查已取消"),
    }
)
_RESULT_VARIANTS = {
    "environment": frozenset(
        {
            (
                DiagnosticStatus.FAILED,
                "runtime-path-invalid",
                "应用写入路径未通过安全边界检查",
            ),
            (
                DiagnosticStatus.FAILED,
                "runtime-unsupported",
                "当前运行环境不是受支持的 Windows x64",
            ),
            (
                DiagnosticStatus.FAILED,
                "runtime-system-volume",
                "正式程序位于系统盘，必须迁移到非系统盘",
            ),
            (
                DiagnosticStatus.WARNING,
                "source-system-volume",
                "源码开发环境位于系统盘",
            ),
            (
                DiagnosticStatus.PASSED,
                "runtime-paths-ok",
                "运行环境和项目内路径正常",
            ),
        }
    ),
    "project-write": frozenset(
        {
            (
                DiagnosticStatus.PASSED,
                "project-write-ok",
                "项目内临时写入和读取正常",
            ),
            (
                DiagnosticStatus.PASSED,
                "project-write-ok",
                "项目内临时写入和当前下载目录写入正常",
            ),
            (
                DiagnosticStatus.FAILED,
                "project-write-failed",
                "项目内临时写入检查失败",
            ),
            (
                DiagnosticStatus.FAILED,
                "download-write-failed",
                "当前下载目录写入检查失败",
            ),
        }
    ),
    "disk": frozenset(
        {
            (
                DiagnosticStatus.FAILED,
                "disk-unavailable",
                "无法读取应用所在磁盘的空间信息",
            ),
            (
                DiagnosticStatus.FAILED,
                "disk-space-critical",
                "磁盘可用空间低于 256 MiB",
            ),
            (
                DiagnosticStatus.FAILED,
                "disk-space-critical",
                "应用所在磁盘可用空间低于 256 MiB",
            ),
            (DiagnosticStatus.WARNING, "disk-space-low", "磁盘可用空间低于 1 GiB"),
            (
                DiagnosticStatus.WARNING,
                "disk-space-low",
                "应用所在磁盘可用空间低于 1 GiB",
            ),
            (
                DiagnosticStatus.FAILED,
                "download-disk-unavailable",
                "无法读取下载所在磁盘的空间信息",
            ),
            (
                DiagnosticStatus.FAILED,
                "download-disk-space-critical",
                "下载所在磁盘可用空间低于 256 MiB",
            ),
            (
                DiagnosticStatus.WARNING,
                "download-disk-space-low",
                "下载所在磁盘可用空间低于 1 GiB",
            ),
            (DiagnosticStatus.PASSED, "disk-space-ok", "磁盘可用空间正常"),
            (
                DiagnosticStatus.PASSED,
                "disk-space-ok",
                "应用和下载所在磁盘可用空间正常",
            ),
        }
    ),
    "components": frozenset(
        {
            (
                DiagnosticStatus.FAILED,
                "component-missing",
                "一个或多个必要运行组件不可用",
            ),
            (DiagnosticStatus.PASSED, "components-ok", "必要运行组件全部可用"),
        }
    ),
    "task-database": frozenset(
        {
            (DiagnosticStatus.FAILED, "database-missing", "数据库文件不存在"),
            (DiagnosticStatus.FAILED, "database-corrupt", "数据库完整性检查失败"),
            (DiagnosticStatus.FAILED, "database-unreadable", "数据库无法读取"),
            (
                DiagnosticStatus.FAILED,
                "database-schema-incompatible",
                "下载任务数据库结构不兼容",
            ),
            (
                DiagnosticStatus.FAILED,
                "database-semantics-invalid",
                "下载任务数据库包含无效关系或状态",
            ),
            (
                DiagnosticStatus.PASSED,
                "task-database-ok",
                "下载任务数据库结构和聚合状态正常",
            ),
        }
    ),
    "content-database": frozenset(
        {
            (DiagnosticStatus.FAILED, "database-missing", "数据库文件不存在"),
            (DiagnosticStatus.FAILED, "database-corrupt", "数据库完整性检查失败"),
            (DiagnosticStatus.FAILED, "database-unreadable", "数据库无法读取"),
            (
                DiagnosticStatus.FAILED,
                "database-schema-incompatible",
                "账号内容数据库结构不兼容",
            ),
            (
                DiagnosticStatus.FAILED,
                "database-semantics-invalid",
                "账号内容数据库包含无效关系或状态",
            ),
            (
                DiagnosticStatus.PASSED,
                "content-database-ok",
                "账号内容数据库结构和聚合状态正常",
            ),
        }
    ),
    "credentials": frozenset(
        {
            (DiagnosticStatus.FAILED, "settings-unreadable", "应用设置无法读取"),
            (
                DiagnosticStatus.WARNING,
                "credentials-not-configured",
                "尚未配置 Telegram 登录凭据",
            ),
            (
                DiagnosticStatus.FAILED,
                "credentials-unreadable",
                "Telegram 登录凭据无法解密",
            ),
            (DiagnosticStatus.PASSED, "credentials-ok", "Telegram 登录凭据可用"),
        }
    ),
    "telegram": frozenset(
        {
            (
                DiagnosticStatus.SKIPPED,
                "telegram-not-configured",
                "尚未建立可检查的 Telegram 会话",
            ),
            (
                DiagnosticStatus.FAILED,
                "telegram-session-expired",
                "Telegram 登录会话已失效",
            ),
            (
                DiagnosticStatus.WARNING,
                "telegram-network-unavailable",
                "暂时无法连接 Telegram 服务",
            ),
            (
                DiagnosticStatus.WARNING,
                "telegram-network-timeout",
                "Telegram 连接检查超时",
            ),
            (
                DiagnosticStatus.FAILED,
                "telegram-check-failed",
                "Telegram 连接检查失败",
            ),
            (
                DiagnosticStatus.PASSED,
                "telegram-connected",
                "Telegram 登录会话和连接正常",
            ),
        }
    ),
    "updates": frozenset(
        {
            (
                DiagnosticStatus.SKIPPED,
                "update-check-unavailable",
                "当前未配置签名更新检查",
            ),
            (
                DiagnosticStatus.WARNING,
                "update-sources-unavailable",
                "暂时无法检查签名更新源",
            ),
            (
                DiagnosticStatus.WARNING,
                "update-sources-timeout",
                "签名更新源检查超时",
            ),
            (
                DiagnosticStatus.FAILED,
                "update-source-invalid",
                "签名更新源返回结构无效",
            ),
            (
                DiagnosticStatus.FAILED,
                "update-source-invalid",
                "签名更新源验证失败或内容不一致",
            ),
            (
                DiagnosticStatus.WARNING,
                "update-sources-unavailable",
                "两个签名更新源暂时均不可用",
            ),
            (
                DiagnosticStatus.WARNING,
                "update-source-degraded",
                "一个签名更新源暂时不可用",
            ),
            (
                DiagnosticStatus.PASSED,
                "update-sources-ok",
                "GitHub 与魔搭签名更新源正常",
            ),
        }
    ),
}


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
        _validate_report_contract(report)
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
        report = _parse_report(value)
        _validate_report_contract(report)
        return report

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
            _sync_file(temporary)
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
            f"报告状态：{_STATUS_TEXT[report.status]}",
            f"开始时间：{_utc_text(report.started_at)}",
            f"结束时间：{_utc_text(report.finished_at)}",
            "",
        ]
        lines.extend(
            f"- [{_STATUS_TEXT[item.status]}] {item.title}：{item.summary}（{item.duration_ms} ms）"
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
                if name == "diagnostic-report.json":
                    self.deserialize(content)
                else:
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


def _validate_report_contract(report: DiagnosticReport) -> None:
    for result in report.results:
        allowed = _ALLOWED_METRICS.get(result.id)
        if allowed is None:
            raise DiagnosticPrivacyError("诊断检查项不在白名单")
        if result.title != _RESULT_TITLES[result.id]:
            raise DiagnosticPrivacyError("诊断检查项标题不在白名单")
        variant = (result.status, result.code, result.summary)
        if variant not in _RESULT_VARIANTS[result.id] | _GENERIC_VARIANTS:
            raise DiagnosticPrivacyError("诊断结果说明不在白名单")
        if not set(result.metrics) <= allowed:
            raise DiagnosticPrivacyError("诊断指标不在白名单")
        for key, value in result.metrics.items():
            if key in _BOOLEAN_METRICS:
                valid = isinstance(value, bool)
            elif key in _SOURCE_STATUS_METRICS:
                valid = isinstance(value, str) and value in {
                    "valid",
                    "unavailable",
                    "invalid",
                }
            elif key in _VERSION_METRICS:
                valid = isinstance(value, str) and _SAFE_VERSION.fullmatch(value) is not None
            elif key in _AUTHORIZATION_REASON_METRICS:
                valid = isinstance(value, str) and value in _SAFE_AUTHORIZATION_REASONS
            else:
                valid = (
                    isinstance(value, int)
                    and not isinstance(value, bool)
                    and value >= 0
                )
            if not valid:
                raise DiagnosticPrivacyError("诊断指标类型不符合白名单")


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
    return value.astimezone(UTC).isoformat(timespec="auto").replace("+00:00", "Z")


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


def _sync_file(path: Path) -> None:
    with path.open("r+b") as stream:
        stream.flush()
        os.fsync(stream.fileno())
