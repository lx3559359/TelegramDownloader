from __future__ import annotations

from telegram_downloader.subscriptions import (
    SubscriptionProbeReport,
    SubscriptionRun,
    SubscriptionRunStatus,
)


def explain_run(run: SubscriptionRun) -> str:
    """Return a safe, actionable summary for one automatic check."""
    if run.status is SubscriptionRunStatus.FAILED:
        return "检查失败，请确认网络、登录状态和群组访问权限后重试"
    if run.status is SubscriptionRunStatus.CANCELLED:
        return "检查已取消"
    if run.inspected == 0:
        return "没有新消息"
    if run.keyword_hits == 0:
        return f"扫描 {run.inspected} 条，新消息未命中规则"
    if run.matched == 0:
        return f"规则命中 {run.keyword_hits} 条消息，但没有所选媒体类型"
    if run.queued == 0 and run.duplicate == run.matched:
        return f"匹配 {run.matched} 项，均已在队列"
    if run.duplicate:
        return f"新增 {run.queued} 项，另有 {run.duplicate} 项已在队列"
    return f"新增 {run.queued} 项"


def explain_probe(report: SubscriptionProbeReport) -> str:
    """Explain a read-only latest-message probe without exposing its rule."""
    if report.inspected == 0:
        return "最近消息范围内没有最近消息"
    if report.keyword_hits == 0:
        return f"最近 {report.inspected} 条中未命中规则"
    if report.matched == 0:
        return f"规则命中 {report.keyword_hits} 条消息，但没有所选媒体类型"
    if report.duplicate == report.matched:
        return f"匹配 {report.matched} 项，均已在队列"
    available = report.matched - report.duplicate
    if report.duplicate:
        return (
            f"匹配 {report.matched} 项，其中 {report.duplicate} 项已在队列，"
            f"{available} 项可加入队列"
        )
    return f"匹配 {report.matched} 项，均可加入队列"
