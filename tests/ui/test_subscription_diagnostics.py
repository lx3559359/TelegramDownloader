from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from PySide6.QtCore import Qt

from telegram_downloader.domain import MediaKind
from telegram_downloader.subscriptions import (
    SubscriptionProbeSample,
    SubscriptionRun,
    SubscriptionRunStatus,
)
from telegram_downloader.ui.subscription_diagnostics import (
    SubscriptionProbeSampleModel,
    SubscriptionRunHistoryModel,
)

NOW = datetime(2026, 8, 15, 9, 0, tzinfo=UTC)


def run(**changes: object) -> SubscriptionRun:
    value = SubscriptionRun(
        id="run-1",
        rule_id="rule-1",
        account_id="account-1",
        started_at=NOW,
        finished_at=NOW,
        status=SubscriptionRunStatus.COMPLETED,
        inspected=5,
        keyword_hits=2,
        matched=1,
        queued=1,
        duplicate=0,
    )
    return replace(value, **changes)


def sample(**changes: object) -> SubscriptionProbeSample:
    value = SubscriptionProbeSample(
        message_id=42,
        message_date_utc=NOW,
        media_kind=MediaKind.VIDEO,
        original_name="tutorial.mp4",
        expected_size=1536,
        already_queued=True,
        excerpt="消息摘要只在表格中显示",
    )
    return replace(value, **changes)


def test_run_history_model_formats_explanations_and_counts(qtbot) -> None:
    model = SubscriptionRunHistoryModel()
    model.set_runs([run()])

    assert model.headerData(1, Qt.Orientation.Horizontal) == "结果"
    assert "新增 1 项" in model.data(model.index(0, 1))
    assert model.data(model.index(0, 2)) == "5"
    assert model.data(model.index(0, 3)) == "2"
    assert model.data(model.index(0, 4)) == "1"
    assert model.data(model.index(0, 5)) == "1"
    assert model.data(model.index(0, 6)) == "0"
    assert model.data(model.index(0, 0)) == NOW.astimezone().strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def test_run_history_is_newest_first_limited_and_hides_raw_errors(qtbot) -> None:
    model = SubscriptionRunHistoryModel()
    values = [
        run(
            id=f"run-{index:02d}",
            finished_at=NOW + timedelta(minutes=index),
        )
        for index in range(25)
    ]
    failed = run(
        id="run-failed",
        finished_at=NOW + timedelta(hours=2),
        status=SubscriptionRunStatus.FAILED,
        error="secret-token private-channel",
    )

    model.set_runs(values + [failed])

    assert model.rowCount() == 20
    assert "检查失败" in model.data(model.index(0, 1))
    tooltip = model.data(model.index(0, 1), Qt.ItemDataRole.ToolTipRole)
    assert "secret-token" not in (tooltip or "")
    assert "private-channel" not in (tooltip or "")
    assert model.run_at(0) == failed


def test_probe_sample_model_formats_media_size_and_existing_state(qtbot) -> None:
    model = SubscriptionProbeSampleModel()
    existing = sample()
    unknown = sample(
        message_id=43,
        media_kind=MediaKind.DOCUMENT,
        original_name="manual.pdf",
        expected_size=None,
        already_queued=False,
    )

    model.set_samples([existing, unknown])

    assert model.headerData(5, Qt.Orientation.Horizontal) == "状态"
    assert model.data(model.index(0, 1)) == "视频"
    assert model.data(model.index(0, 3)) == "1.5 KB"
    assert model.data(model.index(0, 5)) == "已在队列"
    assert model.data(model.index(1, 1)) == "文档"
    assert model.data(model.index(1, 3)) == "未知"
    assert model.data(model.index(1, 5)) == "可加入队列"
    assert model.sample_at(0) == existing


def test_probe_samples_are_limited_and_excerpt_is_display_only(qtbot) -> None:
    model = SubscriptionProbeSampleModel()
    values = [sample(message_id=index + 1, excerpt=f"摘要-{index}") for index in range(25)]

    model.set_samples(values)

    assert model.rowCount() == 20
    assert model.data(model.index(0, 4)) == "摘要-0"
    assert model.data(model.index(0, 4), Qt.ItemDataRole.ToolTipRole) is None
