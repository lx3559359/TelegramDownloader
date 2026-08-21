from dataclasses import replace
from datetime import UTC, date, datetime

from PySide6.QtCore import QPoint, QSize, Qt
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QGraphicsDropShadowEffect, QHeaderView

from telegram_downloader.content import (
    ALL_DIALOGS_SCOPE_REF,
    ALL_DIALOGS_TITLE,
    ContentDialog,
    ContentSearchQuery,
    DialogKind,
    SearchResult,
    SearchScope,
    SearchSession,
    SearchStatus,
)
from telegram_downloader.content_progress import SearchProgress, SearchResultBatch
from telegram_downloader.domain import MediaKind, ScanFilters
from telegram_downloader.ui.content_browser import ContentBrowserPage
from telegram_downloader.ui.effects import ElevationLevel
from telegram_downloader.ui.theme import APP_STYLESHEET


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
    assert page.result_table.iconSize() == QSize(88, 60)
    assert page.result_table.verticalHeader().defaultSectionSize() == 78
    assert page.result_table.wordWrap() is False
    assert (
        page.result_table.textElideMode()
        == Qt.TextElideMode.ElideRight
    )
    header = page.result_table.horizontalHeader()
    fixed_widths = {
        0: 52,
        1: 96,
        2: 132,
        3: 92,
        5: 58,
        6: 82,
        7: 64,
    }
    for column, width in fixed_widths.items():
        assert header.sectionResizeMode(column) == QHeaderView.ResizeMode.Fixed
        assert page.result_table.columnWidth(column) == width
    assert header.sectionResizeMode(4) == QHeaderView.ResizeMode.Stretch


def test_account_content_card_structure_keeps_filter_minimums(qtbot) -> None:
    page = ContentBrowserPage()
    qtbot.addWidget(page)

    assert page.objectName() == "accountContentPage"
    assert page.dialog_card.objectName() == "elevatedCard"
    assert page.filter_card.objectName() == "elevatedCard"
    assert page.results_card.objectName() == "elevatedCard"
    assert page.dialog_card.minimumWidth() == 210
    assert page.dialog_card.maximumWidth() == 270
    assert page.search_column.minimumWidth() >= 680
    assert page.date_from.minimumWidth() >= 132
    assert page.date_to.minimumWidth() >= 132
    assert page.limit_input.minimumWidth() >= 90
    assert page.error_label.parentWidget() is page.filter_card
    assert (
        page.dialog_list.horizontalScrollBarPolicy()
        == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    )
    assert page.dialog_list.textElideMode() == Qt.TextElideMode.ElideRight


def test_result_columns_do_not_squeeze_fixed_text_at_minimum_size(qtbot) -> None:
    now = datetime(2026, 8, 15, tzinfo=UTC)
    page = ContentBrowserPage()
    page.resize(996, 650)
    qtbot.addWidget(page)
    page.show()
    page.set_results(
        [
            replace(
                result(now, "long-summary", 1),
                excerpt=(
                    "这是一段很长的摘要，只允许在摘要列内省略，"
                    "不能挤压日期、类型、大小或状态列。"
                )
                * 4,
            )
        ]
    )
    qtbot.wait(20)

    assert page.result_table.horizontalScrollBar().maximum() == 0
    assert page.result_table.columnWidth(4) > 0


def test_account_content_cards_use_shared_major_elevation(qtbot) -> None:
    page = ContentBrowserPage()
    qtbot.addWidget(page)

    effects = []
    for card in (page.dialog_card, page.filter_card, page.results_card):
        assert card.objectName() == "elevatedCard"
        assert card.property("elevation") == ElevationLevel.MAJOR.value
        effect = card.graphicsEffect()
        assert isinstance(effect, QGraphicsDropShadowEffect)
        effects.append(effect)
    assert len({id(effect) for effect in effects}) == 3
    assert "QFrame#elevatedCard" in APP_STYLESHEET


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


def test_incomplete_search_exposes_continue_action(qtbot) -> None:
    now = datetime(2026, 8, 20, tzinfo=UTC)
    page = ContentBrowserPage()
    qtbot.addWidget(page)
    page.show()
    page.set_logged_in(True)
    incomplete = replace(
        session(now),
        status=SearchStatus.INCOMPLETE,
        last_error="Telegram 请求需等待 121 秒",
    )

    page.set_active_search(incomplete)

    assert page.load_more_button.isVisible()
    assert page.load_more_button.text() == "继续搜索"

    page.set_active_search(replace(incomplete, status=SearchStatus.RUNNING))
    assert page.load_more_button.text() == "加载更多"


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
        page.dialog_list.setCurrentIndex(page.dialog_model.index(1, 0))

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
    page.dialog_list.setCurrentIndex(page.dialog_model.index(1, 0))
    page.keyword_input.setText("  安装教程  ")
    page.date_from.setDate(page.date_from.date().addDays(-1))

    with qtbot.waitSignal(page.search_requested, timeout=500) as caught:
        qtbot.mouseClick(page.search_button, Qt.MouseButton.LeftButton)

    assert caught.args[0] == SearchScope.SINGLE_DIALOG.value
    assert caught.args[1] == "-1001"
    assert caught.args[2] == "安装教程"
    assert isinstance(caught.args[3], date)
    assert isinstance(caught.args[4], date)
    assert caught.args[5] == frozenset(MediaKind)
    assert caught.args[6] == 500

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


def test_all_dialogs_is_selected_by_default_and_emits_global_scope(qtbot) -> None:
    now = datetime(2026, 8, 17, tzinfo=UTC)
    page = ContentBrowserPage()
    qtbot.addWidget(page)
    page.set_logged_in(True)
    page.set_dialogs([dialog(now)])
    page.keyword_input.setText("安装教程")

    choice = page.dialog_model.choice_at(page.dialog_list.currentIndex().row())
    assert choice.scope is SearchScope.ALL_DIALOGS
    assert page.current_dialog_label.text() == ALL_DIALOGS_TITLE

    with qtbot.waitSignal(page.search_requested, timeout=500) as caught:
        qtbot.mouseClick(page.search_button, Qt.MouseButton.LeftButton)

    assert caught.args[0] == SearchScope.ALL_DIALOGS.value
    assert caught.args[1] == ALL_DIALOGS_SCOPE_REF
    assert caught.args[2] == "安装教程"


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

    assert page.select_all_button.isEnabled() is True
    assert page.invert_button.isEnabled() is True
    qtbot.mouseClick(page.select_all_button, Qt.MouseButton.LeftButton)

    assert page.result_model.result_at(0).selected is True
    assert page.result_model.result_at(1).selected is True
    assert page.result_model.result_at(2).selected is False
    assert page.result_model.result_at(3).selected is False
    assert page.selection_summary.text() == (
        "已选 2 项 · 已知 3.0 MB · 1 项大小未知 · 1 项已入队 · 1 项不可用"
    )

    with qtbot.waitSignal(page.queue_requested, timeout=500) as caught:
        qtbot.mouseClick(page.queue_button, Qt.MouseButton.LeftButton)
    assert caught.args == ["search-1"]
    assert page.queue_button.text() == "正在准备已选 2 项…"
    assert page.queue_button.isEnabled() is False


def test_bulk_selection_explains_when_every_result_is_excluded(qtbot) -> None:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    page = ContentBrowserPage()
    qtbot.addWidget(page)
    page.set_logged_in(True)
    page.set_dialogs([dialog(now)])
    page.dialog_list.setCurrentIndex(page.dialog_model.index(0, 0))
    page.set_active_search(session(now))
    page.set_results(
        [
            replace(result(now, "queued-1", 9), queued=True),
            replace(result(now, "queued-2", 8), queued=True),
            replace(result(now, "unavailable", 7), available=False),
        ]
    )

    assert page.select_all_button.isEnabled() is False
    assert page.invert_button.isEnabled() is False
    assert page.selection_summary.text() == (
        "已选 0 项 · 2 项已入队 · 1 项不可用"
    )


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


def test_selection_cell_click_and_space_toggle_once(qtbot) -> None:
    now = datetime(2026, 8, 15, tzinfo=UTC)
    page = ContentBrowserPage()
    page.resize(996, 650)
    qtbot.addWidget(page)
    page.show()
    page.set_results([result(now, "r1", 1)])
    index = page.result_model.index(0, 0)
    qtbot.waitUntil(lambda: page.result_table.visualRect(index).isValid())
    changed: list[tuple[str, bool]] = []
    page.result_model.selection_changed.connect(
        lambda result_id, selected: changed.append((result_id, selected))
    )
    rect = page.result_table.visualRect(index)
    full_cell_target = QPoint(rect.right() - 6, rect.center().y())

    qtbot.mouseClick(
        page.result_table.viewport(),
        Qt.MouseButton.LeftButton,
        pos=full_cell_target,
    )

    assert (
        page.result_model.data(index, Qt.ItemDataRole.CheckStateRole)
        == Qt.CheckState.Checked
    )
    assert changed == [("r1", True)]

    page.result_table.setCurrentIndex(index)
    page.result_table.setFocus()
    qtbot.keyClick(page.result_table, Qt.Key.Key_Space)

    assert (
        page.result_model.data(index, Qt.ItemDataRole.CheckStateRole)
        == Qt.CheckState.Unchecked
    )
    assert changed == [("r1", True), ("r1", False)]


def test_progressive_batch_disables_queue_until_results_are_stable(qtbot) -> None:
    now = datetime(2026, 8, 21, tzinfo=UTC)
    page = ContentBrowserPage()
    qtbot.addWidget(page)
    page.set_logged_in(True)
    page.set_dialogs([dialog(now)])
    page.set_active_search(session(now))
    selected = replace(result(now, "r1", 1), selected=True)

    page.apply_search_batch(
        SearchResultBatch("search-1", 1, (selected,), stable=False)
    )

    assert page.result_model.rowCount() == 1
    assert page.queue_button.isEnabled() is False
    assert page._batch_search_id == "search-1"
    assert page._batch_generation == 1

    page.apply_search_batch(
        SearchResultBatch("search-1", 1, (selected,), stable=True)
    )
    assert page.queue_button.isEnabled() is True


def test_disabled_selection_cells_ignore_mouse_and_keyboard(qtbot) -> None:
    now = datetime(2026, 8, 15, tzinfo=UTC)
    page = ContentBrowserPage()
    page.resize(996, 650)
    qtbot.addWidget(page)
    page.show()
    page.set_results(
        [
            replace(result(now, "unavailable", 1), available=False),
            replace(result(now, "queued", 2), queued=True),
        ]
    )
    changed: list[tuple[str, bool]] = []
    page.result_model.selection_changed.connect(
        lambda result_id, selected: changed.append((result_id, selected))
    )

    for row in range(2):
        index = page.result_model.index(row, 0)
        qtbot.waitUntil(
            lambda index=index: page.result_table.visualRect(index).isValid()
        )
        rect = page.result_table.visualRect(index)
        qtbot.mouseClick(
            page.result_table.viewport(),
            Qt.MouseButton.LeftButton,
            pos=QPoint(rect.right() - 6, rect.center().y()),
        )
        page.result_table.setCurrentIndex(index)
        page.result_table.setFocus()
        qtbot.keyClick(page.result_table, Qt.Key.Key_Space)
        assert (
            page.result_model.data(index, Qt.ItemDataRole.CheckStateRole)
            == Qt.CheckState.Unchecked
        )

    assert changed == []


def test_real_mouse_double_click_on_preview_emits_result_id(qtbot) -> None:
    now = datetime(2026, 8, 15, tzinfo=UTC)
    page = ContentBrowserPage()
    page.resize(900, 620)
    qtbot.addWidget(page)
    page.show()
    page.set_results([result(now, "r1", 1)])
    qtbot.wait(20)
    index = page.result_model.index(0, 1)
    point = page.result_table.visualRect(index).center()

    with qtbot.waitSignal(page.preview_requested, timeout=500) as caught:
        qtbot.mouseClick(
            page.result_table.viewport(),
            Qt.MouseButton.LeftButton,
            pos=point,
        )
        qtbot.mouseDClick(
            page.result_table.viewport(),
            Qt.MouseButton.LeftButton,
            pos=point,
        )

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


def test_open_preview_is_updated_when_thumbnail_arrives(qtbot, tmp_path) -> None:
    now = datetime(2026, 8, 15, tzinfo=UTC)
    preview_path = tmp_path / "preview.png"
    image = QImage(400, 200, QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.cyan)
    assert image.save(str(preview_path))
    page = ContentBrowserPage()
    qtbot.addWidget(page)
    page.show_preview(result(now, "r1", 1), None)

    page.update_preview("r1", preview_path)

    preview = next(iter(page._preview_dialogs))
    assert preview.preview_label.pixmap() is not None
