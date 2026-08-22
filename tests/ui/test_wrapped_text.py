from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import QStyleOptionViewItem, QTableView

from telegram_downloader.ui.wrapped_text import WrappedSummaryDelegate


def _model_with_text(text: str) -> QStandardItemModel:
    model = QStandardItemModel(1, 1)
    item = QStandardItem(text)
    item.setData("result-1", Qt.ItemDataRole.UserRole)
    model.setItem(0, 0, item)
    return model


def _option(width: int) -> QStyleOptionViewItem:
    option = QStyleOptionViewItem()
    option.rect = QRect(0, 0, width, 78)
    return option


def test_delegate_wraps_complete_text_without_eliding(qtbot) -> None:
    model = _model_with_text("这是一段需要完整显示的长摘要。" * 10)
    view = QTableView()
    qtbot.addWidget(view)
    view.setModel(model)
    delegate = WrappedSummaryDelegate(view)
    option = _option(150)

    delegate.initStyleOption(option, model.index(0, 0))
    size = delegate.sizeHint(option, model.index(0, 0))

    assert option.features & QStyleOptionViewItem.ViewItemFeature.WrapText
    assert option.textElideMode == Qt.TextElideMode.ElideNone
    assert option.text == model.data(model.index(0, 0))
    assert size.height() > 78


def test_delegate_caches_by_identity_width_font_and_text(qtbot) -> None:
    model = _model_with_text("缓存测量摘要 " * 20)
    view = QTableView()
    qtbot.addWidget(view)
    view.setModel(model)
    delegate = WrappedSummaryDelegate(view)
    index = model.index(0, 0)
    first = _option(180)

    delegate.sizeHint(first, index)
    delegate.sizeHint(first, index)
    assert delegate.measurement_count == 1

    delegate.sizeHint(_option(130), index)
    assert delegate.measurement_count == 2

    larger_font = first.font
    larger_font.setPointSize(larger_font.pointSize() + 2)
    font_option = _option(130)
    font_option.font = larger_font
    delegate.sizeHint(font_option, index)
    assert delegate.measurement_count == 3

    model.setData(index, "已经变化的摘要 " * 20)
    delegate.sizeHint(font_option, index)
    assert delegate.measurement_count == 4

    delegate.clear_cache()
    delegate.sizeHint(font_option, index)
    assert delegate.measurement_count == 5


def test_delegate_keeps_short_rows_at_minimum_height(qtbot) -> None:
    model = _model_with_text("短摘要")
    view = QTableView()
    qtbot.addWidget(view)
    view.setModel(model)
    delegate = WrappedSummaryDelegate(view)

    assert delegate.sizeHint(_option(240), model.index(0, 0)).height() == 78
