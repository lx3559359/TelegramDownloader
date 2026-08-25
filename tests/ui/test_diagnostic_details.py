from telegram_downloader.diagnostics import (
    DiagnosticResult,
    DiagnosticStatus,
)
from telegram_downloader.ui.diagnostic_details import present_diagnostic_details

MIB = 1024 * 1024


def test_details_present_fixed_remediation_and_safe_byte_metric() -> None:
    result = DiagnosticResult(
        "disk",
        "磁盘空间",
        DiagnosticStatus.WARNING,
        "download-disk-space-low",
        "下载所在磁盘可用空间低于 1 GiB",
        1,
        {
            "downloadFreeBytes": 512 * MIB,
            "privateUnknown": "D:/private",
        },
    )

    details = present_diagnostic_details(result)

    assert "清理下载所在磁盘" in details.remediation
    assert "512 MiB" in details.metrics_text
    assert "D:/private" not in details.metrics_text


def test_details_use_fixed_fallbacks_for_unknown_code_and_metrics() -> None:
    result = DiagnosticResult(
        "environment",
        "运行环境与路径",
        DiagnosticStatus.FAILED,
        "future-result",
        "未来结果",
        1,
        {"privateUnknown": "D:/private"},
    )

    details = present_diagnostic_details(result)

    assert details.remediation == "重新运行检查；持续失败时使用诊断包反馈。"
    assert details.metrics_text == "无可显示的安全指标"


def test_details_format_only_fixed_boolean_status_version_and_reason_values() -> None:
    result = DiagnosticResult(
        "updates",
        "签名更新源",
        DiagnosticStatus.WARNING,
        "update-source-degraded",
        "一个签名更新源暂时不可用",
        1,
        {
            "downloadSameVolume": False,
            "githubStatus": "valid",
            "githubLatencyMs": 12,
            "githubVersion": "0.18.4",
            "authorizationReason": "session-revoked",
            "modelscopeVersion": "private-version",
        },
    )

    details = present_diagnostic_details(result)

    assert "下载目录与应用同磁盘：否" in details.metrics_text
    assert "GitHub 状态：正常" in details.metrics_text
    assert "GitHub 延迟：12 ms" in details.metrics_text
    assert "GitHub 版本：0.18.4" in details.metrics_text
    assert "授权状态：会话已撤销" in details.metrics_text
    assert "private-version" not in details.metrics_text
