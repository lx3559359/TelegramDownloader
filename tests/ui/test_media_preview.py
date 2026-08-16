from datetime import UTC, datetime

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QGraphicsDropShadowEffect

from telegram_downloader.content import SearchResult
from telegram_downloader.domain import MediaKind
from telegram_downloader.ui.effects import ElevationLevel
from telegram_downloader.ui.media_preview import MediaPreviewDialog
from telegram_downloader.ui.theme import APP_STYLESHEET


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
        1024,
        now,
        f"摘要 {message_id}",
        f"a1:-1001:{message_id}:m{message_id}",
    )


def test_image_preview_scales_without_losing_aspect_ratio(qtbot, tmp_path) -> None:
    path = tmp_path / "preview.png"
    image = QImage(400, 200, QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.cyan)
    assert image.save(str(path))
    dialog = MediaPreviewDialog(
        result(datetime(2026, 8, 15, tzinfo=UTC), "r1", 1),
        path,
    )
    qtbot.addWidget(dialog)

    assert dialog.styleSheet() == APP_STYLESHEET
    assert dialog.dialog_surface.objectName() == "dialogSurface"
    assert dialog.dialog_surface.property("elevation") == ElevationLevel.MAJOR.value
    assert isinstance(dialog.dialog_surface.graphicsEffect(), QGraphicsDropShadowEffect)
    pixmap = dialog.preview_label.pixmap()
    assert pixmap is not None
    assert pixmap.size().width() >= 400
    assert pixmap.width() / pixmap.height() == 2
    assert "1.mp4" in dialog.metadata_label.text()


def test_non_image_preview_shows_metadata_without_crashing(qtbot) -> None:
    dialog = MediaPreviewDialog(
        result(datetime(2026, 8, 15, tzinfo=UTC), "r1", 1),
        None,
    )
    qtbot.addWidget(dialog)

    assert "视频" in dialog.metadata_label.text()
    assert "暂无可用预览" in dialog.preview_label.text()


def test_placeholder_preview_can_be_upgraded_when_thumbnail_arrives(
    qtbot,
    tmp_path,
) -> None:
    path = tmp_path / "preview.png"
    image = QImage(400, 200, QImage.Format.Format_RGB32)
    image.fill(Qt.GlobalColor.cyan)
    assert image.save(str(path))
    dialog = MediaPreviewDialog(
        result(datetime(2026, 8, 15, tzinfo=UTC), "r1", 1),
        None,
    )
    qtbot.addWidget(dialog)

    dialog.set_preview(path)

    assert dialog.preview_label.pixmap() is not None
    assert dialog.size_button.isEnabled() is True
