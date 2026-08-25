from __future__ import annotations

import math
import re
from dataclasses import dataclass

from telegram_downloader.diagnostics import DiagnosticResult

_FALLBACK_REMEDIATION = "重新运行检查；持续失败时使用诊断包反馈。"

_REMEDIATIONS = {
    "runtime-path-invalid": "检查应用目录与下载目录设置，修复路径安全边界后重新检查。",
    "runtime-unsupported": "请在受支持的 Windows x64 环境中运行正式程序。",
    "runtime-system-volume": "将正式程序完整迁移到非系统盘后重新检查。",
    "source-system-volume": "建议将源码工作区迁移到非系统盘后继续验证。",
    "project-write-failed": "确认应用目录可写且未被安全软件拦截，然后重新检查。",
    "download-write-failed": "在设置中重新选择可用的下载目录，然后重新检查。",
    "disk-unavailable": "确认应用所在磁盘在线且可访问，然后重新检查。",
    "disk-space-critical": "立即清理应用所在磁盘或迁移程序数据，然后重新检查。",
    "disk-space-low": "清理应用所在磁盘并预留更多空间，然后重新检查。",
    "download-disk-unavailable": "确认下载所在磁盘在线，或在设置中更换下载目录。",
    "download-disk-space-critical": "立即清理下载所在磁盘或更换下载目录，然后重新检查。",
    "download-disk-space-low": "清理下载所在磁盘并预留更多空间，然后重新检查。",
    "component-missing": "使用正式安装包修复或重新安装缺失的运行组件。",
    "database-missing": "停止程序，备份 data 目录后再检查数据库文件是否完整。",
    "database-corrupt": "停止程序，备份 data 目录后再处理数据库完整性异常。",
    "database-unreadable": "停止程序，备份 data 目录并确认数据库文件可读取。",
    "database-schema-incompatible": "停止程序并备份 data 目录，再使用匹配版本处理数据库。",
    "database-semantics-invalid": "停止程序，备份 data 目录后再处理无效关系或状态。",
    "settings-unreadable": "修复应用设置或重新保存 Telegram API 凭据。",
    "credentials-not-configured": "重新录入完整的 Telegram API ID 和 API Hash。",
    "credentials-unreadable": "重新录入 Telegram API 凭据，并确认当前 Windows 用户可解密。",
    "telegram-not-configured": "先配置 Telegram API 凭据并完成登录。",
    "telegram-session-expired": "重新登录 Telegram 账号后再检查。",
    "telegram-network-unavailable": "检查网络与代理设置，恢复连接后重新检查。",
    "telegram-network-timeout": "检查网络与代理设置，稍后重新检查 Telegram 连接。",
    "telegram-check-failed": "重新登录并检查网络；持续失败时导出诊断包反馈。",
    "update-check-unavailable": "确认更新组件完整，再重新检查签名更新源。",
    "update-sources-unavailable": "检查网络；持续失败时从正式发布页覆盖安装。",
    "update-sources-timeout": "检查网络与代理；持续失败时从正式发布页覆盖安装。",
    "update-source-degraded": "检查网络；持续失败时从正式发布页覆盖安装。",
    "update-source-invalid": "停止自动更新，并从正式发布页覆盖安装。",
    "probe-failed": _FALLBACK_REMEDIATION,
    "check-cancelled": "需要时重新运行健康检查。",
}

for _code in (
    "runtime-paths-ok",
    "project-write-ok",
    "disk-space-ok",
    "components-ok",
    "task-database-ok",
    "content-database-ok",
    "credentials-ok",
    "telegram-connected",
    "update-sources-ok",
):
    _REMEDIATIONS[_code] = "无需处理，可继续使用。"

_BOOLEAN_LABELS = {
    "frozen": "正式程序运行",
    "windowsX64": "Windows x64",
    "nonSystemVolume": "应用位于非系统盘",
    "downloadWritable": "下载目录可写",
    "downloadSameVolume": "下载目录与应用同磁盘",
    "schemaCompatible": "数据库结构兼容",
    "foreignKeysValid": "数据库关系有效",
    "stateValuesValid": "数据库状态有效",
    "pyside6": "PySide6",
    "telethon": "Telethon",
    "qasync": "qasync",
    "qrcode": "QR Code",
    "sqlite": "SQLite",
    "dpapi": "Windows DPAPI",
    "settingsReadable": "设置可读取",
    "secretsPresent": "凭据文件存在",
    "secretsDecryptable": "凭据可解密",
    "credentialsConfigured": "API 凭据完整",
}
_BYTE_LABELS = {
    "totalBytes": "应用磁盘总空间",
    "freeBytes": "应用磁盘可用空间",
    "downloadTotalBytes": "下载磁盘总空间",
    "downloadFreeBytes": "下载磁盘可用空间",
}
_COUNT_LABELS = {
    "guardedPathCount": "受保护路径数",
    "taskCount": "任务数",
    "mediaCount": "媒体项数",
    "accountCount": "账号数",
    "dialogCount": "会话数",
    "searchCount": "搜索记录数",
    "searchResultCount": "搜索结果数",
    "subscriptionCount": "订阅规则数",
    "subscriptionRunCount": "订阅运行数",
    "schemaVersion": "数据库结构版本",
}
_COUNT_LABELS.update(
    {
        f"taskStatus{suffix}": f"任务状态·{label}"
        for suffix, label in (
            ("Draft", "草稿"),
            ("Queued", "排队"),
            ("Scanning", "扫描"),
            ("Downloading", "下载"),
            ("WaitingRetry", "等待重试"),
            ("Paused", "暂停"),
            ("Completed", "完成"),
            ("PartialFailure", "部分失败"),
            ("Other", "其他"),
        )
    }
)
_COUNT_LABELS.update(
    {
        f"itemStatus{suffix}": f"媒体状态·{label}"
        for suffix, label in (
            ("Queued", "排队"),
            ("Downloading", "下载"),
            ("WaitingRetry", "等待重试"),
            ("Paused", "暂停"),
            ("Completed", "完成"),
            ("Failed", "失败"),
            ("Other", "其他"),
        )
    }
)
_COUNT_LABELS.update(
    {
        f"integrityStatus{suffix}": f"完整性·{label}"
        for suffix, label in (
            ("Unverified", "未校验"),
            ("Verified", "已校验"),
            ("Missing", "缺失"),
            ("SizeMismatch", "大小不符"),
            ("HashMismatch", "哈希不符"),
            ("ReadError", "读取失败"),
            ("Other", "其他"),
        )
    }
)
_MILLISECOND_LABELS = {
    "githubLatencyMs": "GitHub 延迟",
    "modelscopeLatencyMs": "魔搭延迟",
}
_VERSION_LABELS = {
    "githubVersion": "GitHub 版本",
    "modelscopeVersion": "魔搭版本",
}
_SOURCE_STATUS_LABELS = {
    "githubStatus": "GitHub 状态",
    "modelscopeStatus": "魔搭状态",
}
_SOURCE_STATUS_VALUES = {
    "valid": "正常",
    "unavailable": "暂不可用",
    "invalid": "验证失败",
}
_AUTHORIZATION_REASONS = {
    "auth-key-duplicated": "授权密钥重复",
    "auth-key-invalid": "授权密钥无效",
    "auth-key-unregistered": "授权密钥未注册",
    "session-revoked": "会话已撤销",
    "not-authorized": "未授权",
    "unknown": "授权状态未知",
}
_SAFE_VERSION = re.compile(r"\d+\.\d+\.\d+\Z")


@dataclass(frozen=True, slots=True)
class DiagnosticDetails:
    remediation: str
    metrics_text: str


def present_diagnostic_details(result: DiagnosticResult) -> DiagnosticDetails:
    lines: list[str] = []
    for key, value in result.metrics.items():
        formatted = _format_metric(key, value)
        if formatted is not None:
            lines.append(formatted)
    return DiagnosticDetails(
        _REMEDIATIONS.get(result.code, _FALLBACK_REMEDIATION),
        "\n".join(lines) if lines else "无可显示的安全指标",
    )


def _format_metric(key: str, value: object) -> str | None:
    if key in _BOOLEAN_LABELS:
        if not isinstance(value, bool):
            return None
        return f"{_BOOLEAN_LABELS[key]}：{'是' if value else '否'}"
    if key in _BYTE_LABELS:
        text = _format_bytes(value)
        return f"{_BYTE_LABELS[key]}：{text}" if text is not None else None
    if key in _COUNT_LABELS:
        if not _is_nonnegative_int(value):
            return None
        return f"{_COUNT_LABELS[key]}：{value}"
    if key in _MILLISECOND_LABELS:
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(float(value))
            or value < 0
        ):
            return None
        return f"{_MILLISECOND_LABELS[key]}：{int(round(value))} ms"
    if key in _VERSION_LABELS:
        if not isinstance(value, str) or _SAFE_VERSION.fullmatch(value) is None:
            return None
        return f"{_VERSION_LABELS[key]}：{value}"
    if key in _SOURCE_STATUS_LABELS:
        if not isinstance(value, str) or value not in _SOURCE_STATUS_VALUES:
            return None
        return f"{_SOURCE_STATUS_LABELS[key]}：{_SOURCE_STATUS_VALUES[value]}"
    if key == "authorizationReason":
        if not isinstance(value, str) or value not in _AUTHORIZATION_REASONS:
            return None
        return f"授权状态：{_AUTHORIZATION_REASONS[value]}"
    return None


def _format_bytes(value: object) -> str | None:
    if not _is_nonnegative_int(value):
        return None
    for unit, factor in (("GiB", 1024**3), ("MiB", 1024**2), ("KiB", 1024)):
        if value >= factor:
            amount = value / factor
            text = f"{amount:.1f}".rstrip("0").rstrip(".")
            return f"{text} {unit}"
    return f"{value} B"


def _is_nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0
