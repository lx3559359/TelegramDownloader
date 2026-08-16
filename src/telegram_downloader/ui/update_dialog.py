from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from telegram_downloader.ui.effects import ElevationLevel, apply_elevation
from telegram_downloader.ui.theme import APP_STYLESHEET, ensure_cjk_font
from telegram_downloader.update_contract import ReleaseManifest


class UpdateDialog(QDialog):
    def __init__(self, manifest: ReleaseManifest, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        ensure_cjk_font()
        self.setStyleSheet(APP_STYLESHEET)
        self.setWindowTitle("发现签名更新")
        self.setModal(True)
        self.setMinimumWidth(480)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 18)
        self.dialog_surface = QFrame(self)
        self.dialog_surface.setObjectName("dialogSurface")
        apply_elevation(self.dialog_surface, ElevationLevel.MAJOR)
        outer.addWidget(self.dialog_surface)
        layout = QVBoxLayout(self.dialog_surface)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(12)
        title = QLabel("Telegram 下载器有新版本")
        title.setObjectName("pageTitle")
        layout.addWidget(title)

        summary = QHBoxLayout()
        self.version_label = QLabel(f"版本 {manifest.version}")
        self.version_label.setObjectName("accountBadge")
        self.size_label = QLabel(_format_bytes(manifest.runtime.size))
        self.size_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.size_label.setObjectName("muted")
        summary.addWidget(self.version_label)
        summary.addStretch()
        summary.addWidget(self.size_label)
        layout.addLayout(summary)

        security = QLabel("清单已通过 Ed25519 签名验证；下载完成后还会校验大小和 SHA-256。")
        security.setWordWrap(True)
        security.setObjectName("muted")
        layout.addWidget(security)

        self.notes = QTextBrowser()
        self.notes.setPlainText(manifest.release_notes)
        self.notes.setMinimumHeight(120)
        layout.addWidget(self.notes)

        buttons = QDialogButtonBox()
        self.cancel_button = buttons.addButton("暂不更新", QDialogButtonBox.ButtonRole.RejectRole)
        self.update_button = buttons.addButton("下载并更新", QDialogButtonBox.ButtonRole.AcceptRole)
        self.update_button.setObjectName("primaryButton")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


def _format_bytes(value: int) -> str:
    amount = float(value)
    for unit in ("B", "KB", "MB", "GB"):
        if amount < 1024 or unit == "GB":
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{value} B"
