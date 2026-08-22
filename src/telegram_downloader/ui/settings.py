from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from PySide6.QtCore import QTime, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from telegram_downloader.domain import MediaKind
from telegram_downloader.files import DownloadNamingSettings, render_download_target
from telegram_downloader.settings import (
    AppSettings,
    DownloadScheduleSettings,
    ProxySettings,
    SettingsError,
)
from telegram_downloader.ui.effects import ElevationLevel, apply_elevation
from telegram_downloader.ui.theme import APP_STYLESHEET, ensure_cjk_font


class SettingsDialog(QDialog):
    test_proxy_requested = Signal(object, str)
    thumbnail_cache_clear_requested = Signal()
    save_requested = Signal()

    def __init__(
        self,
        settings: AppSettings,
        proxy_password: str = "",
        parent: QWidget | None = None,
        *,
        thumbnail_cache_bytes: int = 0,
        autostart_available: bool = True,
        tray_available: bool = True,
    ) -> None:
        super().__init__(parent)
        ensure_cjk_font()
        self.setStyleSheet(APP_STYLESHEET)
        self.setWindowTitle("设置")
        self.setModal(True)
        self.setMinimumWidth(520)
        self._settings = settings

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 18)
        self.dialog_surface = QFrame(self)
        self.dialog_surface.setObjectName("dialogSurface")
        apply_elevation(self.dialog_surface, ElevationLevel.MAJOR)
        outer.addWidget(self.dialog_surface)
        layout = QVBoxLayout(self.dialog_surface)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(13)
        title = QLabel("应用设置")
        title.setObjectName("pageTitle")
        description = QLabel("配置会保存在应用目录；代理密码单独使用 DPAPI 加密。")
        description.setObjectName("muted")
        layout.addWidget(title)
        layout.addWidget(description)

        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)
        general_tab = QWidget()
        general_layout = QVBoxLayout(general_tab)
        general_layout.setContentsMargins(8, 12, 8, 8)
        form = QFormLayout()
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(10)
        self.api_id = QSpinBox()
        self.api_id.setRange(0, 2_147_483_647)
        self.api_id.setValue(settings.api_id)
        self.concurrency = QSpinBox()
        self.concurrency.setRange(1, 5)
        self.concurrency.setValue(settings.concurrency)
        self.speed_limit = QComboBox()
        speed_presets = (
            ("不限速", 0),
            ("256 KiB/s", 256),
            ("512 KiB/s", 512),
            ("1 MiB/s", 1024),
            ("2 MiB/s", 2048),
            ("5 MiB/s", 5120),
            ("10 MiB/s", 10240),
            ("20 MiB/s", 20480),
            ("50 MiB/s", 51200),
        )
        for label, value in speed_presets:
            self.speed_limit.addItem(label, value)
        selected_speed = self.speed_limit.findData(settings.speed_limit_kib)
        if selected_speed < 0:
            self.speed_limit.addItem(
                f"自定义 {settings.speed_limit_kib} KiB/s",
                settings.speed_limit_kib,
            )
            selected_speed = self.speed_limit.count() - 1
        self.speed_limit.setCurrentIndex(selected_speed)
        self.check_updates = QCheckBox("启动后自动检查正式版更新")
        self.check_updates.setChecked(settings.check_updates_on_startup)
        self.proxy_kind = QComboBox()
        self.proxy_kind.addItem("不使用代理", "none")
        self.proxy_kind.addItem("SOCKS5", "socks5")
        self.proxy_kind.addItem("HTTP", "http")
        self.proxy_kind.setCurrentIndex(self.proxy_kind.findData(settings.proxy.kind))
        self.proxy_host = QLineEdit(settings.proxy.host)
        self.proxy_port = QSpinBox()
        self.proxy_port.setRange(0, 65535)
        self.proxy_port.setValue(settings.proxy.port)
        self.proxy_username = QLineEdit(settings.proxy.username)
        self.proxy_password = QLineEdit(proxy_password)
        self.proxy_password.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("API ID", self.api_id)
        self.concurrency_label = QLabel("全局媒体槽")
        form.addRow(self.concurrency_label, self.concurrency)
        form.addRow("总下载限速", self.speed_limit)
        form.addRow("在线更新", self.check_updates)
        form.addRow("代理类型", self.proxy_kind)
        form.addRow("代理地址", self.proxy_host)
        form.addRow("代理端口", self.proxy_port)
        form.addRow("代理用户名", self.proxy_username)
        form.addRow("代理密码", self.proxy_password)
        general_layout.addLayout(form)

        cache_row = QHBoxLayout()
        self.thumbnail_cache_size = QLabel(
            self._format_bytes(thumbnail_cache_bytes)
        )
        self.thumbnail_cache_size.setObjectName("muted")
        self.thumbnail_cache_clear_button = QPushButton("清理缩略图缓存")
        cache_row.addWidget(self.thumbnail_cache_size)
        cache_row.addStretch()
        cache_row.addWidget(self.thumbnail_cache_clear_button)
        form.addRow("缩略图缓存", cache_row)
        general_layout.addStretch()
        self.tabs.addTab(general_tab, "常规")

        naming_tab = QWidget()
        naming_layout = QVBoxLayout(naming_tab)
        naming_layout.setContentsMargins(8, 12, 8, 8)
        naming_form = QFormLayout()
        naming_form.setHorizontalSpacing(14)
        naming_form.setVerticalSpacing(10)
        self.directory_template = QComboBox()
        self.directory_template.setEditable(True)
        self.directory_template.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        for template in (
            "{source}/{year_month}/{media_type}",
            "{year}/{month}/{source}/{media_type}",
            "{source}/{message_date}",
        ):
            self.directory_template.addItem(template)
        self.directory_template.setEditText(settings.download_naming.directory_template)

        self.filename_template = QComboBox()
        self.filename_template.setEditable(True)
        self.filename_template.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        for template in (
            "{original_name}",
            "{stem}_{message_id}{extension}",
            "{message_date}_{message_id}_{original_name}",
        ):
            self.filename_template.addItem(template)
        self.filename_template.setEditText(settings.download_naming.filename_template)
        naming_form.addRow("目录模板", self.directory_template)
        naming_form.addRow("文件名模板", self.filename_template)

        placeholders = QLabel(
            "目录：{source} {year} {month} {year_month} {media_type} "
            "{message_date} {message_id}\n"
            "文件名另支持：{original_name} {stem} {extension}"
        )
        placeholders.setObjectName("muted")
        placeholders.setWordWrap(True)
        naming_form.addRow("可用占位符", placeholders)
        self.naming_preview = QLabel()
        self.naming_preview.setObjectName("muted")
        self.naming_preview.setWordWrap(True)
        naming_form.addRow("路径预览", self.naming_preview)
        naming_note = QLabel("模板只影响新建任务；已入队任务会继续使用原保存路径。")
        naming_note.setObjectName("muted")
        naming_note.setWordWrap(True)
        naming_form.addRow("", naming_note)
        naming_layout.addLayout(naming_form)
        naming_layout.addStretch()
        self.tabs.addTab(naming_tab, "下载路径")

        background_tab = QWidget()
        background_layout = QVBoxLayout(background_tab)
        background_layout.setContentsMargins(8, 12, 8, 8)
        background_form = QFormLayout()
        background_form.setHorizontalSpacing(14)
        background_form.setVerticalSpacing(10)
        self.close_to_tray = QCheckBox("关闭主窗口时继续在托盘运行")
        self.close_to_tray.setChecked(settings.close_to_tray)
        self.notifications = QCheckBox("显示下载、订阅和登录状态通知")
        self.notifications.setChecked(settings.notifications_enabled)
        self.autostart = QCheckBox("登录 Windows 后自动在后台启动")
        self.autostart.setChecked(
            settings.autostart_enabled if autostart_available else False
        )
        self.schedule_enabled = QCheckBox("仅在指定时段运行下载")
        self.schedule_enabled.setChecked(settings.download_schedule.enabled)

        weekday_row = QWidget()
        weekday_layout = QHBoxLayout(weekday_row)
        weekday_layout.setContentsMargins(0, 0, 0, 0)
        weekday_layout.setSpacing(8)
        self.weekdays = tuple(QCheckBox(label) for label in "一二三四五六日")
        for day, checkbox in enumerate(self.weekdays):
            checkbox.setChecked(day in settings.download_schedule.weekdays)
            weekday_layout.addWidget(checkbox)
        weekday_layout.addStretch()

        time_row = QWidget()
        time_layout = QHBoxLayout(time_row)
        time_layout.setContentsMargins(0, 0, 0, 0)
        time_layout.setSpacing(8)
        self.schedule_start = QTimeEdit()
        self.schedule_start.setDisplayFormat("HH:mm")
        self.schedule_start.setTime(
            self._time_from_minute(settings.download_schedule.start_minute)
        )
        self.schedule_end = QTimeEdit()
        self.schedule_end.setDisplayFormat("HH:mm")
        self.schedule_end.setTime(
            self._time_from_minute(settings.download_schedule.end_minute)
        )
        time_layout.addWidget(self.schedule_start)
        time_layout.addWidget(QLabel("至"))
        time_layout.addWidget(self.schedule_end)
        time_layout.addStretch()

        background_form.addRow("托盘后台", self.close_to_tray)
        background_form.addRow("系统通知", self.notifications)
        background_form.addRow("开机启动", self.autostart)
        background_form.addRow("下载时段", self.schedule_enabled)
        background_form.addRow("运行星期", weekday_row)
        background_form.addRow("运行时间", time_row)
        schedule_note = QLabel("开始与结束相同表示全天；跨越午夜时自动按次日结束。")
        schedule_note.setObjectName("muted")
        schedule_note.setWordWrap(True)
        background_form.addRow("", schedule_note)
        background_layout.addLayout(background_form)
        background_layout.addStretch()
        self.tabs.addTab(background_tab, "后台与通知")
        self.schedule_detail_widgets = (*self.weekdays, self.schedule_start, self.schedule_end)

        if not tray_available:
            self.close_to_tray.setEnabled(False)
            self.close_to_tray.setToolTip("当前系统托盘不可用")
        if not autostart_available:
            self.autostart.setEnabled(False)
            self.autostart.setToolTip("开机启动只支持正式打包程序")

        self.error_label = QLabel()
        self.error_label.setStyleSheet("color: #fb923c;")
        self.error_label.setWordWrap(True)
        self.error_label.setVisible(False)
        layout.addWidget(self.error_label)

        actions = QHBoxLayout()
        self.test_button = QPushButton("测试代理")
        cancel_button = QPushButton("取消")
        self.save_button = QPushButton("保存")
        self.save_button.setObjectName("primaryButton")
        actions.addWidget(self.test_button)
        actions.addStretch()
        actions.addWidget(cancel_button)
        actions.addWidget(self.save_button)
        layout.addLayout(actions)

        self.proxy_kind.currentIndexChanged.connect(self._update_proxy_fields)
        self.schedule_enabled.toggled.connect(self._update_schedule_fields)
        self.directory_template.editTextChanged.connect(self._update_naming_preview)
        self.filename_template.editTextChanged.connect(self._update_naming_preview)
        self.test_button.clicked.connect(self._test_proxy)
        self.thumbnail_cache_clear_button.clicked.connect(
            self.thumbnail_cache_clear_requested.emit
        )
        cancel_button.clicked.connect(self.reject)
        self.save_button.clicked.connect(self._save)
        self._update_proxy_fields()
        self._update_schedule_fields()
        self._update_naming_preview()

    def proxy_values(self) -> ProxySettings:
        kind = str(self.proxy_kind.currentData())
        if kind == "none":
            return ProxySettings()
        return ProxySettings(
            kind,
            self.proxy_host.text().strip(),
            self.proxy_port.value(),
            self.proxy_username.text().strip(),
        )

    def values(self) -> AppSettings:
        weekdays = tuple(
            day for day, checkbox in enumerate(self.weekdays) if checkbox.isChecked()
        )
        return AppSettings(
            api_id=self.api_id.value(),
            concurrency=self.concurrency.value(),
            proxy=self.proxy_values(),
            check_updates_on_startup=self.check_updates.isChecked(),
            speed_limit_kib=int(self.speed_limit.currentData()),
            close_to_tray=self.close_to_tray.isChecked(),
            notifications_enabled=self.notifications.isChecked(),
            autostart_enabled=self.autostart.isChecked(),
            tray_hint_shown=self._tray_hint_shown,
            download_schedule=DownloadScheduleSettings(
                enabled=self.schedule_enabled.isChecked(),
                weekdays=weekdays,
                start_minute=self._minute_from_time(self.schedule_start.time()),
                end_minute=self._minute_from_time(self.schedule_end.time()),
            ),
            download_naming=DownloadNamingSettings(
                self.directory_template.currentText().strip(),
                self.filename_template.currentText().strip(),
            ),
        )

    @property
    def _tray_hint_shown(self) -> bool:
        return self._settings.tray_hint_shown

    def _update_proxy_fields(self) -> None:
        enabled = self.proxy_kind.currentData() != "none"
        for widget in (
            self.proxy_host,
            self.proxy_port,
            self.proxy_username,
            self.proxy_password,
        ):
            widget.setEnabled(enabled)
        self.test_button.setEnabled(enabled)

    def _update_schedule_fields(self) -> None:
        enabled = self.schedule_enabled.isChecked()
        for widget in self.schedule_detail_widgets:
            widget.setEnabled(enabled)

    def _update_naming_preview(self) -> None:
        try:
            naming = DownloadNamingSettings(
                self.directory_template.currentText().strip(),
                self.filename_template.currentText().strip(),
            )
            root = Path("downloads").resolve()
            target = render_download_target(
                root,
                naming,
                "示例频道",
                datetime(2026, 8, 22, tzinfo=UTC),
                MediaKind.VIDEO,
                12345,
                "video.mp4",
            )
            preview = (Path("downloads") / target.relative_to(root)).as_posix()
            self.naming_preview.setText(f"{preview}")
        except ValueError as error:
            self.naming_preview.setText(f"模板错误：{error}")

    def _test_proxy(self) -> None:
        try:
            proxy = self.proxy_values()
        except SettingsError as error:
            self._show_error(str(error))
            return
        self.error_label.setVisible(False)
        self.test_proxy_requested.emit(proxy, self.proxy_password.text())

    def _save(self) -> None:
        try:
            self.values()
        except ValueError as error:
            self._show_error(str(error))
            return
        self.error_label.setVisible(False)
        self.save_requested.emit()

    def _show_error(self, text: str) -> None:
        self.error_label.setText(text)
        self.error_label.setVisible(True)

    def set_thumbnail_cache_bytes(self, value: int) -> None:
        self.thumbnail_cache_size.setText(self._format_bytes(value))

    def set_thumbnail_cache_busy(self, busy: bool) -> None:
        self.thumbnail_cache_clear_button.setEnabled(not busy)
        self.thumbnail_cache_clear_button.setText(
            "正在清理…" if busy else "清理缩略图缓存"
        )

    def set_save_busy(self, busy: bool) -> None:
        self.save_button.setEnabled(not busy)
        self.save_button.setText("正在保存…" if busy else "保存")

    @staticmethod
    def _format_bytes(value: int) -> str:
        amount = float(max(0, value))
        units = ("B", "KB", "MB", "GB", "TB")
        for unit in units:
            if amount < 1024 or unit == units[-1]:
                return (
                    f"{amount:.0f} {unit}"
                    if unit == "B"
                    else f"{amount:.1f} {unit}"
                )
            amount /= 1024
        return f"{value} B"

    @staticmethod
    def _time_from_minute(value: int) -> QTime:
        return QTime(value // 60, value % 60)

    @staticmethod
    def _minute_from_time(value: QTime) -> int:
        return value.hour() * 60 + value.minute()
