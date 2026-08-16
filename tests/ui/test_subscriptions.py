from __future__ import annotations

from datetime import UTC, datetime, timedelta

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialogButtonBox,
    QGraphicsDropShadowEffect,
    QMessageBox,
)

from telegram_downloader.content import ContentDialog, DialogKind
from telegram_downloader.domain import MediaKind
from telegram_downloader.subscriptions import (
    SubscriptionProbeProgress,
    SubscriptionProbeReport,
    SubscriptionProbeSample,
    SubscriptionProgress,
    SubscriptionRule,
    SubscriptionRun,
    SubscriptionRunStatus,
    SubscriptionState,
)
from telegram_downloader.ui.effects import ElevationLevel
from telegram_downloader.ui.subscription_models import SubscriptionTableModel
from telegram_downloader.ui.subscriptions import (
    SubscriptionEditorDialog,
    SubscriptionPage,
)


def test_subscription_page_uses_major_and_nested_silver_cards(qtbot) -> None:
    page = SubscriptionPage()
    qtbot.addWidget(page)

    assert isinstance(page.subscription_card.graphicsEffect(), QGraphicsDropShadowEffect)
    assert page.subscription_card.objectName() == "elevatedCard"
    assert page.subscription_card.property("elevation") == ElevationLevel.MAJOR.value
    for card in (page.diagnostic_card, page.history_card, page.probe_card):
        assert isinstance(card.graphicsEffect(), QGraphicsDropShadowEffect)
        assert card.objectName() == "elevatedSubCard"
        assert card.property("elevation") == ElevationLevel.SECONDARY.value
    assert page.subscription_card.isAncestorOf(page.rule_table)
    assert page.diagnostic_card.isAncestorOf(page.run_history_table)
    assert page.diagnostic_card.isAncestorOf(page.probe_sample_table)

NOW = datetime(2026, 8, 15, 9, 0, tzinfo=UTC)


def dialog() -> ContentDialog:
    return ContentDialog(
        "a1",
        "-1001",
        "资料群",
        "docs",
        DialogKind.GROUP,
        False,
        True,
        NOW,
    )


def rule(*, enabled: bool = True) -> SubscriptionRule:
    return SubscriptionRule(
        "rule-1",
        "a1",
        "-1001",
        "资料群",
        "美女",
        frozenset({MediaKind.PHOTO, MediaKind.VIDEO}),
        30,
        enabled,
        SubscriptionState.WAITING if enabled else SubscriptionState.PAUSED,
        42,
        NOW + timedelta(minutes=30) if enabled else None,
        NOW,
        None,
        0,
        NOW,
        NOW,
    )


def run() -> SubscriptionRun:
    return SubscriptionRun(
        "run-1",
        "rule-1",
        "a1",
        NOW,
        NOW,
        SubscriptionRunStatus.COMPLETED,
        20,
        5,
        5,
        3,
        2,
    )


def probe_report() -> SubscriptionProbeReport:
    sample = SubscriptionProbeSample(
        42,
        NOW,
        MediaKind.PHOTO,
        "photo.jpg",
        1024,
        False,
        "美女写真",
    )
    return SubscriptionProbeReport("rule-1", 20, 2, 1, 0, (sample,), NOW)


def ready_page(qtbot) -> SubscriptionPage:
    page = SubscriptionPage()
    qtbot.addWidget(page)
    page.set_logged_in(True)
    page.set_dialogs([dialog()])
    page.set_rules([rule()])
    page.rule_table.selectRow(0)
    page.set_selected_rule_details(rule(), [run()])
    return page


def test_subscription_model_exposes_status_schedule_and_rule_id(qtbot) -> None:
    model = SubscriptionTableModel()
    latest = run()
    model.set_rules([rule()], {"rule-1": latest})

    assert model.columnCount() == 5
    assert model.data(model.index(0, 0), Qt.ItemDataRole.UserRole) == "rule-1"
    assert model.data(model.index(0, 2), Qt.ItemDataRole.DisplayRole) == "等待检查"
    assert model.data(model.index(0, 3), Qt.ItemDataRole.DisplayRole) == (
        "新增 3 项，另有 2 项已在队列"
    )
    assert "2026-08-15" in model.data(
        model.index(0, 4),
        Qt.ItemDataRole.DisplayRole,
    )


def test_rule_editor_validates_then_returns_trimmed_draft(qtbot) -> None:
    editor = SubscriptionEditorDialog([dialog()])
    qtbot.addWidget(editor)
    editor.show()
    save = editor.buttons.button(QDialogButtonBox.StandardButton.Save)

    qtbot.mouseClick(save, Qt.MouseButton.LeftButton)
    assert editor.isVisible()
    assert "关键词" in editor.error_label.text()

    editor.keyword_input.setText("  美女  ")
    with qtbot.waitSignal(editor.accepted, timeout=500):
        qtbot.mouseClick(save, Qt.MouseButton.LeftButton)

    draft = editor.draft()
    assert draft.peer_ref == "-1001"
    assert draft.keyword == "美女"
    assert draft.media_kinds == frozenset(MediaKind)
    assert draft.interval_minutes == 30


def test_page_emits_run_pause_resume_and_tracks_busy_progress(qtbot) -> None:
    page = SubscriptionPage()
    qtbot.addWidget(page)
    page.set_logged_in(True)
    page.set_dialogs([dialog()])
    page.set_rules([rule()])
    page.rule_table.selectRow(0)

    with qtbot.waitSignal(page.run_requested, timeout=500) as run_signal:
        qtbot.mouseClick(page.run_button, Qt.MouseButton.LeftButton)
    assert run_signal.args == ["rule-1"]
    assert page.run_button.isEnabled() is False
    page.set_rule_busy(None, False)

    with qtbot.waitSignal(page.enabled_requested, timeout=500) as pause_signal:
        qtbot.mouseClick(page.toggle_button, Qt.MouseButton.LeftButton)
    assert pause_signal.args == ["rule-1", False]

    page.set_rule_busy("rule-1", True, "正在立即检查…")
    assert page.run_button.isEnabled() is False
    assert page.busy_label.text() == "正在立即检查…"

    page.set_progress(SubscriptionProgress("rule-1", 20, 5, 3, 2, 1, "正在筛选"))
    assert "已扫描 20 条" in page.progress_label.text()
    assert "新增 2 项" in page.progress_label.text()

    page.set_rule_busy(None, False)
    assert page.run_button.isEnabled() is True


def test_offline_page_keeps_rules_visible_but_disables_online_actions(qtbot) -> None:
    page = SubscriptionPage()
    qtbot.addWidget(page)
    page.set_rules([rule(enabled=False)])
    page.rule_table.selectRow(0)
    page.set_logged_in(False)

    assert page.rule_model.rowCount() == 1
    assert page.new_button.isEnabled() is False
    assert page.run_button.isEnabled() is False
    assert "登录" in page.connection_label.text()


def test_automatic_progress_locks_actions_until_run_finishes(qtbot) -> None:
    page = SubscriptionPage()
    qtbot.addWidget(page)
    page.set_logged_in(True)
    page.set_dialogs([dialog()])
    page.set_rules([rule()])
    page.rule_table.selectRow(0)

    page.set_progress(SubscriptionProgress("rule-1", 1, 0, 0, 0, 0, "正在读取"))

    assert page.new_button.isEnabled() is False
    assert page.edit_button.isEnabled() is False
    assert page.run_button.isEnabled() is False
    assert page.toggle_button.isEnabled() is False
    assert page.delete_button.isEnabled() is False

    page.set_progress(None)

    assert page.new_button.isEnabled() is True
    assert page.run_button.isEnabled() is True


def test_page_create_edit_and_confirmed_delete_emit_complete_payloads(
    qtbot,
    monkeypatch,
) -> None:
    page = SubscriptionPage()
    qtbot.addWidget(page)
    page.set_logged_in(True)
    page.set_dialogs([dialog()])
    page.set_rules([rule()])
    page.rule_table.selectRow(0)

    qtbot.mouseClick(page.new_button, Qt.MouseButton.LeftButton)
    create_editor = next(iter(page._editors))
    create_editor.keyword_input.setText("写真")
    with qtbot.waitSignal(page.create_requested, timeout=500) as created:
        qtbot.mouseClick(
            create_editor.buttons.button(QDialogButtonBox.StandardButton.Save),
            Qt.MouseButton.LeftButton,
        )
    assert created.args[0].keyword == "写真"

    page.set_rule_busy(None, False)
    qtbot.mouseClick(page.edit_button, Qt.MouseButton.LeftButton)
    edit_editor = next(iter(page._editors))
    edit_editor.keyword_input.setText("视频")
    with qtbot.waitSignal(page.update_requested, timeout=500) as updated:
        qtbot.mouseClick(
            edit_editor.buttons.button(QDialogButtonBox.StandardButton.Save),
            Qt.MouseButton.LeftButton,
        )
    assert updated.args[0] == "rule-1"
    assert updated.args[1].keyword == "视频"

    page.set_rule_busy(None, False)
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Yes,
    )
    with qtbot.waitSignal(page.delete_requested, timeout=500) as deleted:
        qtbot.mouseClick(page.delete_button, Qt.MouseButton.LeftButton)
    assert deleted.args == ["rule-1"]


def test_selecting_rule_emits_history_request(qtbot) -> None:
    page = SubscriptionPage()
    qtbot.addWidget(page)
    page.set_rules([rule()])

    with qtbot.waitSignal(page.rule_selected, timeout=500) as emitted:
        page.rule_table.selectRow(0)

    assert emitted.args == ["rule-1"]


def test_probe_button_emits_selected_rule_and_locks_conflicting_actions(
    qtbot,
) -> None:
    page = ready_page(qtbot)

    with qtbot.waitSignal(page.probe_requested, timeout=500) as emitted:
        qtbot.mouseClick(page.probe_button, Qt.MouseButton.LeftButton)

    assert emitted.args == ["rule-1"]
    assert page.probe_button.isEnabled() is False
    assert page.edit_button.isEnabled() is False
    assert page.run_button.isEnabled() is False


def test_probe_progress_shows_counts_and_cancel(qtbot) -> None:
    page = ready_page(qtbot)
    page.show()
    qtbot.waitExposed(page)
    page.set_probe_busy("rule-1", True)
    page.set_probe_progress(
        SubscriptionProbeProgress("rule-1", 12, 3, 2, "正在筛选")
    )

    assert "已扫描 12" in page.probe_progress_label.text()
    assert "关键词 3" in page.probe_progress_label.text()
    assert page.probe_cancel_button.isVisible()
    with qtbot.waitSignal(page.probe_cancel_requested, timeout=500):
        qtbot.mouseClick(page.probe_cancel_button, Qt.MouseButton.LeftButton)


def test_probe_report_explains_result_and_populates_samples(qtbot) -> None:
    page = ready_page(qtbot)
    page.set_probe_busy("rule-1", True)

    page.set_probe_result(probe_report())

    assert page.probe_sample_model.rowCount() == 1
    assert "匹配 1 项" in page.probe_result_label.text()
    assert page.probe_button.isEnabled()
    assert page.edit_button.isEnabled()


def test_offline_history_remains_visible_but_probe_is_disabled(qtbot) -> None:
    page = ready_page(qtbot)
    page.set_logged_in(False)

    assert page.run_history_model.rowCount() == 1
    assert page.probe_button.isEnabled() is False
    assert page.detail_summary.text()


def test_probe_repeated_click_emits_once_and_cancelled_state_recovers(qtbot) -> None:
    page = ready_page(qtbot)
    emissions: list[str] = []
    page.probe_requested.connect(emissions.append)

    qtbot.mouseClick(page.probe_button, Qt.MouseButton.LeftButton)
    qtbot.mouseClick(page.probe_button, Qt.MouseButton.LeftButton)

    assert emissions == ["rule-1"]
    page.show_probe_cancelled()
    assert "规则、游标和下载队列均未改变" in page.probe_result_label.text()
    assert page.probe_button.isEnabled()


def test_rule_table_and_diagnostics_remain_readable_at_1024x720(qtbot) -> None:
    page = ready_page(qtbot)
    page.resize(1024, 720)
    page.show()
    qtbot.waitExposed(page)

    assert page.detail_splitter.sizes()[1] >= 180
    assert page.probe_button.isVisible()
    assert page.run_history_table.viewport().width() > 0
    assert page.probe_sample_table.viewport().width() > 0
