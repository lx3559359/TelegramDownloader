from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QIcon, QPixmap

from telegram_downloader.content import (
    ContentDialog,
    ContentSearchQuery,
    DialogKind,
    SearchResult,
    SearchSession,
    SearchStatus,
)
from telegram_downloader.domain import MediaKind, ScanFilters
from telegram_downloader.ui.content_models import (
    DialogListModel,
    SearchHistoryTableModel,
    SearchResultTableModel,
)


def dialogs(now: datetime) -> list[ContentDialog]:
    return [
        ContentDialog(
            "a1",
            "-1001",
            "普通群",
            "general",
            DialogKind.GROUP,
            False,
            True,
            now,
        ),
        ContentDialog(
            "a1",
            "-1002",
            "资料频道",
            "docs",
            DialogKind.CHANNEL,
            True,
            True,
            now,
        ),
        ContentDialog(
            "a1",
            "-1003",
            "Ä文档群",
            "Archive",
            DialogKind.GROUP,
            False,
            False,
            now,
        ),
    ]


def search_session(now: datetime, *, status=SearchStatus.INCOMPLETE) -> SearchSession:
    query = ContentSearchQuery(
        "安装",
        ScanFilters(
            now - timedelta(days=1),
            now,
            frozenset({MediaKind.VIDEO, MediaKind.DOCUMENT}),
            500,
        ),
    )
    return SearchSession(
        "search-1",
        "a1",
        "-1001",
        "资料群",
        query,
        status,
        1,
        None,
        status is SearchStatus.COMPLETED,
        3,
        now,
        now,
        "安全错误摘要",
    )


def search_results(now: datetime) -> list[SearchResult]:
    base = SearchResult(
        "result-1",
        "search-1",
        "a1",
        "-1001",
        9,
        None,
        "m9",
        MediaKind.VIDEO,
        "video.mp4",
        3 * 1024 * 1024,
        now,
        "安装教程",
        "a1:-1001:9:m9",
    )
    return [
        base,
        replace(
            base,
            id="result-2",
            message_id=8,
            media_id="m8",
            expected_size=None,
            media_kind=MediaKind.DOCUMENT,
        ),
        replace(
            base,
            id="result-3",
            message_id=7,
            media_id="m7",
            available=False,
        ),
        replace(
            base,
            id="result-4",
            message_id=6,
            media_id="m6",
            queued=True,
        ),
    ]


def test_dialog_model_filters_by_title_or_username() -> None:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    model = DialogListModel()
    model.set_dialogs(dialogs(now))
    model.set_filter("DOCS")

    assert model.rowCount() == 1
    index = model.index(0, 0)
    assert model.data(index, Qt.ItemDataRole.DisplayRole).startswith("资料频道")
    assert "已归档" in model.data(index, Qt.ItemDataRole.DisplayRole)
    assert model.data(index, Qt.ItemDataRole.UserRole) == "-1002"
    assert model.dialog_at(0).username == "docs"

    model.set_filter("ä")
    assert model.rowCount() == 1
    assert "不可用" in model.data(model.index(0, 0), Qt.ItemDataRole.DisplayRole)


def test_dialog_model_sorts_available_before_unavailable_then_title() -> None:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    model = DialogListModel()
    values = dialogs(now)
    model.set_dialogs([values[2], values[1], values[0]])

    assert [model.dialog_at(row).peer_ref for row in range(model.rowCount())] == [
        "-1001",
        "-1002",
        "-1003",
    ]


def test_history_model_exposes_columns_status_id_and_safe_error() -> None:
    now = datetime(2026, 8, 14, 12, 34, tzinfo=UTC)
    model = SearchHistoryTableModel()
    session = search_session(now)
    model.set_sessions([session])

    assert model.HEADERS == (
        "群组/频道",
        "关键词",
        "筛选",
        "状态",
        "结果数",
        "更新时间",
    )
    assert model.data(model.index(0, 0)) == "资料群"
    assert model.data(model.index(0, 1)) == "安装"
    assert model.data(model.index(0, 3)) == "不完整"
    assert model.data(model.index(0, 4)) == 3
    assert model.data(model.index(0, 0), Qt.ItemDataRole.UserRole) == "search-1"
    assert (
        model.data(model.index(0, 0), Qt.ItemDataRole.ToolTipRole)
        == "安全错误摘要"
    )


def test_result_model_selection_roles_and_disabled_rows(qtbot) -> None:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    model = SearchResultTableModel()
    changed = []
    model.selection_changed.connect(
        lambda result_id, selected: changed.append((result_id, selected))
    )
    results = search_results(now)
    model.set_results(results)
    select_index = model.index(0, 0)

    assert model.HEADERS == ("选择", "预览", "日期", "摘要", "类型", "大小", "状态")
    assert model.flags(select_index) & Qt.ItemFlag.ItemIsUserCheckable
    assert model.setData(
        select_index,
        Qt.CheckState.Checked,
        Qt.ItemDataRole.CheckStateRole,
    )
    assert changed == [(results[0].id, True)]
    assert model.data(select_index, Qt.ItemDataRole.UserRole) == results[0].id
    assert model.data(model.index(1, 5)) == "未知"
    assert not (
        model.flags(model.index(2, 0)) & Qt.ItemFlag.ItemIsUserCheckable
    )
    assert not (
        model.flags(model.index(3, 0)) & Qt.ItemFlag.ItemIsUserCheckable
    )
    assert model.data(model.index(3, 6)) == "已入队"


def test_result_model_accepts_integer_check_state_once(qtbot) -> None:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    model = SearchResultTableModel()
    values = search_results(now)
    model.set_results(values)
    changed: list[tuple[str, bool]] = []
    model.selection_changed.connect(
        lambda result_id, selected: changed.append((result_id, selected))
    )
    index = model.index(0, 0)

    assert model.setData(index, 2, Qt.ItemDataRole.CheckStateRole)
    assert (
        model.data(index, Qt.ItemDataRole.CheckStateRole)
        == Qt.CheckState.Checked
    )
    assert changed == [(values[0].id, True)]

    assert model.setData(index, 2, Qt.ItemDataRole.CheckStateRole)
    assert changed == [(values[0].id, True)]


def test_result_model_uses_thumbnail_then_media_fallback(
    qtbot,
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    model = SearchResultTableModel()
    results = search_results(now)
    model.set_results(results)
    fallback = model.data(model.index(0, 1), Qt.ItemDataRole.DecorationRole)
    assert isinstance(fallback, QIcon)
    assert fallback.isNull() is False

    image_path = tmp_path / "thumbnail.png"
    pixmap = QPixmap(8, 8)
    pixmap.fill(QColor("#22d3ee"))
    assert pixmap.save(str(image_path))
    changed = []
    model.dataChanged.connect(lambda top, bottom, roles: changed.append((top, roles)))

    model.set_thumbnail(results[0].id, image_path)

    thumbnail = model.data(model.index(0, 1), Qt.ItemDataRole.DecorationRole)
    assert isinstance(thumbnail, QIcon)
    assert thumbnail.isNull() is False
    assert changed[-1][0].row() == 0
    assert Qt.ItemDataRole.DecorationRole in changed[-1][1]


def test_thumbnail_path_returns_the_cached_project_file(tmp_path: Path) -> None:
    model = SearchResultTableModel()
    item = search_results(datetime(2026, 8, 15, tzinfo=UTC))[0]
    path = tmp_path / "data" / "cache" / "thumbnails" / "r1.jpg"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"image")
    model.set_results([replace(item, id="r1")])

    model.set_thumbnail("r1", path)

    assert model.thumbnail_path("r1") == path
