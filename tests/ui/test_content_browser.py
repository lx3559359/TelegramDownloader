from dataclasses import replace
from datetime import UTC, date, datetime

from PySide6.QtCore import QSize, Qt

from telegram_downloader.content import (
    ContentDialog,
    ContentSearchQuery,
    DialogKind,
    SearchResult,
    SearchSession,
    SearchStatus,
)
from telegram_downloader.content_progress import SearchProgress
from telegram_downloader.domain import MediaKind, ScanFilters
from telegram_downloader.ui.content_browser import ContentBrowserPage


def dialog(now: datetime, *, available: bool = True) -> ContentDialog:
    return ContentDialog(
        "a1",
        "-1001",
        "资料群",
        "docs",
        DialogKind.GROUP,
        False,
        available,
        now,
    )


def session(now: datetime) -> SearchSession:
    query = ContentSearchQuery(
        "安装",
        ScanFilters(now, now, frozenset(MediaKind), 500),
    )
    return SearchSession(
        "search-1",
        "a1",
        "-1001",
        "资料群",
        query,
        SearchStatus.RUNNING,
        1,
        None,
        False,
        0,
        now,
        now,
    )


def result(now: datetime, result_id: str, message_id: int) -> SearchResult:
    return SearchResult(
        result_id,
        "search-1",
        "a1",
        "-1001",
        message_id,
        None,
        f"m{message_id}",
        MediaKind.VIDEO,
        f"{message_id}.mp4",
        3 * 1024 * 1024,
        now,
        f"摘要 {message_id}",
        f"a1:-1001:{message_id}:m{message_id}",
    )


def test_page_contains_content_browser_controls(qtbot) -> None:
    page = ContentBrowserPage()
    qtbot.addWidget(page)

    assert page.dialog_filter.placeholderText()
    assert page.sync_state_label.text()
    assert page.refresh_button.text() == "刷新"
    assert page.keyword_input.placeholderText()
    assert page.limit_input.minimum() == 1
    assert page.limit_input.maximum() == 10_000
    assert page.limit_input.value() == 500
    assert set(page.media_checks) == set(MediaKind)
    assert page.tabs.tabText(0) == "搜索结果"
    assert page.tabs.tabText(1) == "搜索记录"
    assert page.select_all_button.text() == "全选"
    assert page.invert_button.text() == "反选"
    assert page.queue_button.text() == "加入下载队列"
    assert page.result_table.iconSize() == QSize(112, 84)
    assert page.result_table.verticalHeader().defaultSectionSize() == 96


def test_progress_and_retry_widgets_show_honest_operation_state(qtbot) -> None:
    page = ContentBrowserPage()
    qtbot.addWidget(page)
    page.show()

    page.set_search_busy(True)
    page.set_search_progress(SearchProgress(20, 3, "正在扫描"))

    assert page.search_progress.isVisible()
    assert "已扫描 20 条" in page.search_state_label.text()
    assert "找到 3 项" in page.search_state_label.text()
    assert page.cancel_button.isVisible()

    page.set_sync_state(
        "正在刷新，已发现 3 个群组/频道",
        busy=True,
        count=3,
    )

    assert page.refresh_button.text() == "刷新中…"
    assert page.sync_progress.isVisible()
    assert page.refresh_button.isEnabled() is False

    page.set_connection_state("离线，点击重试", retryable=True)

    assert page.connection_retry_button.isVisible()


def test_connection_retry_button_emits_retry_signal(qtbot) -> None:
    page = ContentBrowserPage()
    qtbot.addWidget(page)
    page.show()
    page.set_connection_state("离线，点击重试", retryable=True)

    with qtbot.waitSignal(page.connection_retry_requested, timeout=500):
        qtbot.mouseClick(
            page.connection_retry_button,
            Qt.MouseButton.LeftButton,
        )


def test_connection_and_queue_busy_states_disable_duplicate_actions(qtbot) -> None:
    now = datetime(2026, 8, 15, tzinfo=UTC)
    page = ContentBrowserPage()
    qtbot.addWidget(page)
    page.set_logged_in(True)
    page.set_dialogs([dialog(now)])
    page.dialog_list.setCurrentIndex(page.dialog_model.index(0, 0))
    page.set_active_search(session(now))
    page.set_results([replace(result(now, "r1", 1), selected=True)])
    page.set_connection_state("离线，点击重试", retryable=True)

    page.set_connection_action_busy(True)
    assert page.connection_retry_button.text() == "重连中…"
    assert page.connection_retry_button.isEnabled() is False
    page.set_connection_state("正在重连（1/3）…", retryable=False)
    assert page.connection_retry_button.isHidden() is False
    page.set_connection_action_busy(False)
    assert page.connection_retry_button.text() == "重新连接"
    assert page.connection_retry_button.isEnabled() is True

    page.set_queue_busy(True)
    assert page.queue_button.text() == "正在准备已选 1 项…"
    assert page.queue_button.isEnabled() is False
    page.set_queue_busy(False)
    assert page.queue_button.text() == "加入下载队列"
    assert page.queue_button.isEnabled() is True


def test_logged_out_page_keeps_query_editable_and_history_visible(
    qtbot,
) -> None:
    page = ContentBrowserPage()
    qtbot.addWidget(page)
    page.set_logged_in(False)

    assert page.refresh_button.isEnabled() is True
    assert page.search_button.isEnabled() is True
    assert page.keyword_input.isEnabled() is True
    assert page.queue_button.isEnabled() is False
    assert page.history_table.isEnabled() is True
    assert "登录" in page.empty_hint.text()


def test_offline_page_routes_tme_link_without_selected_dialog(qtbot) -> None:
    page = ContentBrowserPage()
    qtbot.addWidget(page)
    page.set_logged_in(False)
    page.keyword_input.setText(
        "https://t.me/Zhangzhoulao66/56156?single"
    )

    with qtbot.waitSignal(page.link_requested, timeout=500) as caught:
        qtbot.mouseClick(page.search_button, Qt.MouseButton.LeftButton)

    assert caught.args == ["https://t.me/Zhangzhoulao66/56156?single"]


def test_dialog_selection_emits_peer_and_restores_search_form(qtbot) -> None:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    page = ContentBrowserPage()
    qtbot.addWidget(page)
    page.set_dialogs([dialog(now)])

    with qtbot.waitSignal(page.dialog_selected, timeout=500) as caught:
        page.dialog_list.setCurrentIndex(page.dialog_model.index(0, 0))

    assert caught.args == ["-1001"]

    restored = session(now)
    page.set_active_search(restored)

    assert page.keyword_input.text() == "安装"
    assert page.limit_input.value() == 500
    assert all(page.media_checks[kind].isChecked() for kind in MediaKind)

    page.keyword_input.setText("不会串群")
    page.set_active_search(None)
    assert page.keyword_input.text() == ""
    assert page.limit_input.value() == 500


def test_connection_state_replaces_account_hint(qtbot) -> None:
    page = ContentBrowserPage()
    qtbot.addWidget(page)

    page.set_connection_state("正在重连（2/3）…")

    assert page.empty_hint.text() == "正在重连（2/3）…"


def test_valid_search_emits_trimmed_parameters_and_invalid_input_stays_local(
    qtbot,
) -> None:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    page = ContentBrowserPage()
    qtbot.addWidget(page)
    page.set_logged_in(True)
    page.set_dialogs([dialog(now)])
    page.dialog_list.setCurrentIndex(page.dialog_model.index(0, 0))
    page.keyword_input.setText("  安装教程  ")
    page.date_from.setDate(page.date_from.date().addDays(-1))

    with qtbot.waitSignal(page.search_requested, timeout=500) as caught:
        qtbot.mouseClick(page.search_button, Qt.MouseButton.LeftButton)

    assert caught.args[0] == "-1001"
    assert caught.args[1] == "安装教程"
    assert isinstance(caught.args[2], date)
    assert isinstance(caught.args[3], date)
    assert caught.args[4] == frozenset(MediaKind)
    assert caught.args[5] == 500

    emissions = []
    page.search_requested.connect(lambda *args: emissions.append(args))
    page.keyword_input.clear()
    qtbot.mouseClick(page.search_button, Qt.MouseButton.LeftButton)
    assert emissions == []
    assert "关键词" in page.error_label.text()

    page.keyword_input.setText("安装")
    for check in page.media_checks.values():
        check.setChecked(False)
    qtbot.mouseClick(page.search_button, Qt.MouseButton.LeftButton)
    assert emissions == []
    assert "媒体类型" in page.error_label.text()


def test_selection_summary_and_queue_signal_skip_unavailable_and_queued(
    qtbot,
) -> None:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    page = ContentBrowserPage()
    qtbot.addWidget(page)
    page.set_logged_in(True)
    page.set_dialogs([dialog(now)])
    page.dialog_list.setCurrentIndex(page.dialog_model.index(0, 0))
    active = session(now)
    page.set_active_search(active)
    first = result(now, "r1", 9)
    second = replace(result(now, "r2", 8), expected_size=None)
    unavailable = replace(result(now, "r3", 7), available=False)
    queued = replace(result(now, "r4", 6), queued=True)
    page.set_results([first, second, unavailable, queued])

    qtbot.mouseClick(page.select_all_button, Qt.MouseButton.LeftButton)

    assert page.result_model.result_at(0).selected is True
    assert page.result_model.result_at(1).selected is True
    assert page.result_model.result_at(2).selected is False
    assert page.result_model.result_at(3).selected is False
    assert page.selection_summary.text() == "已选 2 项 · 已知 3.0 MB · 1 项大小未知"

    with qtbot.waitSignal(page.queue_requested, timeout=500) as caught:
        qtbot.mouseClick(page.queue_button, Qt.MouseButton.LeftButton)
    assert caught.args == ["search-1"]
    assert page.queue_button.text() == "正在准备已选 2 项…"
    assert page.queue_button.isEnabled() is False


def test_history_open_delete_and_clear_emit_independent_signals(qtbot) -> None:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    page = ContentBrowserPage()
    qtbot.addWidget(page)
    page.resize(900, 620)
    page.show()
    page.set_sessions([session(now)])
    page.tabs.setCurrentWidget(page.history_tab)
    page.history_table.selectRow(0)
    qtbot.wait(20)

    with qtbot.waitSignal(page.history_open_requested, timeout=500) as opened:
        page.history_table.doubleClicked.emit(page.history_model.index(0, 0))
    assert opened.args == ["search-1"]

    with qtbot.waitSignal(page.history_delete_requested, timeout=500) as deleted:
        qtbot.mouseClick(page.history_delete_button, Qt.MouseButton.LeftButton)
    assert deleted.args == ["search-1"]

    with qtbot.waitSignal(page.history_clear_requested, timeout=500):
        qtbot.mouseClick(page.history_clear_button, Qt.MouseButton.LeftButton)


def test_only_visible_thumbnails_are_requested_once(qtbot) -> None:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    page = ContentBrowserPage()
    page.resize(900, 620)
    qtbot.addWidget(page)
    page.show()
    page.result_table.setFixedHeight(180)
    requested = []
    page.thumbnail_requested.connect(requested.append)
    results = [result(now, f"r{index}", 100 - index) for index in range(30)]

    page.set_results(results)
    qtbot.wait(20)

    assert requested
    assert len(requested) < len(results)
    assert len(requested) == len(set(requested))
    visible_ids = {
        page.result_model.data(
            page.result_model.index(row, 0),
            Qt.ItemDataRole.UserRole,
        )
        for row in range(page.result_model.rowCount())
        if page.result_table.visualRect(
            page.result_model.index(row, 0)
        ).intersects(page.result_table.viewport().rect())
    }
    assert set(requested) <= visible_ids

    before = list(requested)
    page.request_visible_thumbnails()
    assert requested == before


def test_result_preview_double_click_emits_result_id(qtbot) -> None:
    now = datetime(2026, 8, 15, tzinfo=UTC)
    page = ContentBrowserPage()
    qtbot.addWidget(page)
    page.set_results([result(now, "r1", 1)])

    with qtbot.waitSignal(page.preview_requested, timeout=500) as caught:
        page.result_table.doubleClicked.emit(page.result_model.index(0, 1))

    assert caught.args == ["r1"]


def test_nonblocking_preview_is_retained_until_closed(qtbot) -> None:
    now = datetime(2026, 8, 15, tzinfo=UTC)
    page = ContentBrowserPage()
    qtbot.addWidget(page)

    page.show_preview(result(now, "r1", 1), None)

    assert len(page._preview_dialogs) == 1
    preview = next(iter(page._preview_dialogs))
    preview.reject()
    qtbot.waitUntil(lambda: not page._preview_dialogs, timeout=500)
