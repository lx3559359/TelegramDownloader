from dataclasses import replace
from datetime import UTC, date, datetime

from PySide6.QtCore import Qt

from telegram_downloader.content import (
    ContentDialog,
    ContentSearchQuery,
    DialogKind,
    SearchResult,
    SearchSession,
    SearchStatus,
)
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


def test_logged_out_page_keeps_history_visible_but_disables_online_actions(
    qtbot,
) -> None:
    page = ContentBrowserPage()
    qtbot.addWidget(page)
    page.set_logged_in(False)

    assert page.refresh_button.isEnabled() is False
    assert page.search_button.isEnabled() is False
    assert page.queue_button.isEnabled() is False
    assert page.history_table.isEnabled() is True
    assert "登录" in page.empty_hint.text()


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
