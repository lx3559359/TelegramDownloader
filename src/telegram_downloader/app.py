from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import logging
import os
import platform
import sys
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, datetime
from math import ceil
from pathlib import Path
from time import monotonic
from typing import Any

from telegram_downloader import __version__
from telegram_downloader.activation import (
    ACTIVATION_CHANNEL,
    LocalActivationServer,
    request_activation,
)
from telegram_downloader.autostart import CurrentUserAutostart, WindowsCurrentUserRegistry
from telegram_downloader.background import (
    BackgroundModeController,
    QtTrayAdapter,
    QtWindowPort,
)
from telegram_downloader.catalog import CatalogRepository
from telegram_downloader.content import ContentSearchQuery, SearchScope
from telegram_downloader.content_browser import ContentBrowserService
from telegram_downloader.controller import AppController
from telegram_downloader.diagnostic_probes import (
    component_availability,
    managed_writable_paths,
    probe_components,
    probe_content_database,
    probe_credentials,
    probe_disk,
    probe_environment,
    probe_project_write,
    probe_task_database,
    probe_telegram,
    probe_update_sources,
)
from telegram_downloader.diagnostic_store import DiagnosticReportStore
from telegram_downloader.diagnostics import DiagnosticResult, DiagnosticsService
from telegram_downloader.domain import TaskStatus
from telegram_downloader.download_schedule import (
    DownloadScheduleController,
    evaluate_download_schedule,
)
from telegram_downloader.downloader import MediaDownloader
from telegram_downloader.file_integrity import FileIntegrityService
from telegram_downloader.gateway import SessionExpiredError, TelethonGateway
from telegram_downloader.instance_guard import WindowsInstanceGuard
from telegram_downloader.logging import configure_logging
from telegram_downloader.notifications import (
    ApplicationEvent,
    NotificationBatcher,
    NotificationRoute,
    update_available_event,
)
from telegram_downloader.paths import PortablePaths
from telegram_downloader.planner import ScanPreview, TaskPlanner
from telegram_downloader.repository import TaskRepository
from telegram_downloader.resource_control import AsyncBandwidthLimiter
from telegram_downloader.runtime_settings import RuntimeSettingsCoordinator
from telegram_downloader.scheduler import DownloadScheduler
from telegram_downloader.security import SecretsError, SecretsVault
from telegram_downloader.settings import AppSettings, SettingsError, SettingsStore
from telegram_downloader.subscription_scheduler import SubscriptionScheduler
from telegram_downloader.subscription_service import SubscriptionService
from telegram_downloader.thumbnail_cache import ThumbnailCache
from telegram_downloader.update import HttpBytesClient, UpdateCoordinator
from telegram_downloader.update_contract import load_trusted_keys
from telegram_downloader.update_download import ResumableUpdateDownloader

_LOGGER = logging.getLogger("telegram_downloader.app")


class BackgroundUpdatePrompt:
    def __init__(
        self,
        *,
        window_visible: Callable[[], bool],
        show_dialog: Callable[[object], bool | Awaitable[bool]],
        publish: Callable[[ApplicationEvent], None],
    ) -> None:
        self.window_visible = window_visible
        self.show_dialog = show_dialog
        self.publish = publish

    async def __call__(self, manifest: object) -> bool:
        if not self.window_visible():
            version = str(getattr(manifest, "version", "unknown"))
            self.publish(update_available_event(version))
            return False
        decision = self.show_dialog(manifest)
        return bool(await decision if inspect.isawaitable(decision) else decision)


class _FunctionDiagnosticProbe:
    def __init__(
        self,
        probe_id: str,
        title: str,
        action: Callable[[], Any],
        *,
        threaded: bool,
    ) -> None:
        self.id = probe_id
        self.title = title
        self.action = action
        self.threaded = threaded
        self.cancel_active = not threaded

    async def run(self, cancel_event: asyncio.Event) -> DiagnosticResult:
        if cancel_event.is_set():
            raise asyncio.CancelledError
        if self.threaded:
            return await asyncio.to_thread(self.action)
        return await self.action()


async def _telegram_health(controller: AppController) -> DiagnosticResult:
    reason = controller.last_authorization_failure_reason
    if reason is not None:
        return await probe_telegram(None, authorization_reason=reason)
    gateway_value = controller.gateway
    if gateway_value is None:
        return await probe_telegram(None)

    class RecoveredConnection:
        async def test_connection(self) -> None:
            await controller.connection_recovery.ensure_connected(gateway_value)
            await gateway_value.test_connection()

    return await probe_telegram(RecoveredConnection())


class _GracefulShutdown:
    def __init__(
        self,
        controller: Any,
        quit_application: Callable[[], None],
        *,
        before_controller_shutdown: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.controller = controller
        self.quit_application = quit_application
        self.before_controller_shutdown = before_controller_shutdown
        self.task: asyncio.Task[None] | None = None
        self.completed = False

    def request(self) -> asyncio.Task[None]:
        if self.task is None:
            self.task = asyncio.create_task(self._run())
        return self.task

    async def wait(self) -> None:
        if self.task is not None:
            await asyncio.shield(self.task)

    async def _run(self) -> None:
        try:
            async_actions = getattr(self.controller, "_async_actions", None)
            if async_actions is not None:
                await async_actions.shutdown()
            if self.before_controller_shutdown is not None:
                await self.before_controller_shutdown()
            await self.controller.shutdown()
        finally:
            self.completed = True
            self.quit_application()


def _install_graceful_shutdown(
    application: Any,
    controller: Any,
    background: BackgroundModeController,
    *,
    shutdown: _GracefulShutdown | None = None,
):
    from PySide6.QtCore import QEvent, QObject

    shutdown = shutdown or _GracefulShutdown(controller, application.quit)

    class WindowCloseFilter(QObject):
        def eventFilter(self, watched, event):
            if event.type() == QEvent.Type.Close and not shutdown.completed:
                event.ignore()
                background.handle_window_close()
                return True
            return super().eventFilter(watched, event)

    close_filter = WindowCloseFilter(controller.window)
    controller.window.installEventFilter(close_filter)
    application.setQuitOnLastWindowClosed(False)
    controller.update_shutdown = background.request_exit
    return shutdown, close_filter


def _install_session_shutdown(
    application: Any,
    background: BackgroundModeController,
) -> Callable[[object], None]:
    def commit_data_requested(_manager: object) -> None:
        background.request_exit()

    application.commitDataRequest.connect(commit_data_requested)
    return commit_data_requested


def _show_initial_window(
    controller: Any,
    *,
    background: bool,
    tray_available: bool,
) -> None:
    if background and tray_available:
        return
    controller.window.show()
    if background:
        controller._show_status("系统托盘不可用，已显示主窗口")


def run_self_test(root: Path) -> dict[str, object]:
    paths = PortablePaths(root)
    paths.ensure_layout()
    repository = TaskRepository(paths.database)
    repository.initialize()
    repository.recover_interrupted()
    catalog = CatalogRepository(paths.catalog_database)
    catalog.initialize()
    catalog.recover_interrupted_subscriptions(datetime.now(UTC))
    ThumbnailCache(paths.thumbnail_cache)

    managed = managed_writable_paths(paths)
    public_names = {
        "settings": "settings",
        "secrets": "secrets",
        "database": "database",
        "catalog_database": "catalogDatabase",
        "log": "log",
        "cache": "cache",
        "thumbnail_cache": "thumbnailCache",
        "temp": "temp",
        "downloads": "downloads",
        "update_staging": "updateStaging",
        "update_backup": "updateBackup",
        "update_helper": "updateHelper",
        "update_journal": "updateJournal",
    }
    resolved = {
        public: str(paths.guard(managed[internal]))
        for public, internal in public_names.items()
    }
    components = component_availability()
    report: dict[str, object] = {
        "ok": all(components.values()),
        "version": __version__,
        "catalog_schema_version": catalog.schema_version(),
        "runtime_root": str(paths.root),
        "components": components,
        "writable_paths": resolved,
    }
    report_path = paths.guard(paths.log.parent / "self-test.json")
    temporary = paths.guard(report_path.with_suffix(".json.tmp"))
    content = (json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    with temporary.open("wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, report_path)
    return report


def _can_import(module: str) -> bool:
    try:
        importlib.import_module(module)
    except (ImportError, OSError):
        return False
    return True


def _standard_button_selected(answer: object, expected: object) -> bool:
    return answer == expected


def _download_confirmation_text(preview: object) -> str:
    known = AppController._format_bytes(int(getattr(preview, "known_bytes", 0)))
    unknown_count = int(getattr(preview, "unknown_size_count", 0))
    unknown = f"，另有 {unknown_count} 项大小未知" if unknown_count else ""
    if hasattr(preview, "unique_link_count"):
        return (
            f"输入 {preview.input_count} 条 · 有效唯一 {preview.unique_link_count} 条 · "
            f"无效 {preview.invalid_link_count} 条 · 输入重复 {preview.duplicate_link_count} 条\n"
            f"扫描媒体 {preview.scanned_media_count} 项 · "
            f"跨链接重复 {preview.internal_duplicate_count} 项 · "
            f"队列既有 {preview.existing_media_count} 项\n"
            f"最终新增 {len(preview.items)} 项，已知大小 {known}{unknown}。"
            "\n\n创建一个批量下载任务？"
        )
    return (
        f"扫描到 {len(preview.items)} 项媒体，已知大小 {known}{unknown}。"
        "\n\n加入下载队列？"
    )


def _startup_status(indicator: object | None, text: str) -> None:
    method = getattr(indicator, "set_status", None)
    if not callable(method):
        return
    with suppress(Exception):
        method(text)


def _startup_finish(indicator: object | None, window: object) -> None:
    method = getattr(indicator, "finish", None)
    if not callable(method):
        return
    with suppress(Exception):
        method(window)


def _startup_close(indicator: object | None) -> None:
    method = getattr(indicator, "close", None)
    if not callable(method):
        return
    with suppress(Exception):
        method()


def create_application(
    root: Path,
    *,
    publish_event: Callable[[ApplicationEvent], None] | None = None,
):
    import qasync
    from PySide6.QtWidgets import QApplication, QMessageBox

    from telegram_downloader.ui.async_actions import (
        ActionHooks,
        AsyncActionBridge,
    )
    from telegram_downloader.ui.login import LoginDialog
    from telegram_downloader.ui.main import MainWindow
    from telegram_downloader.ui.settings import SettingsDialog
    from telegram_downloader.ui.update_dialog import UpdateDialog

    paths = PortablePaths(root)
    publish_event = publish_event or (lambda _event: None)
    paths.ensure_layout()
    application = QApplication.instance() or QApplication(sys.argv[:1])
    application.setApplicationName("TelegramDownloader")
    application.setApplicationVersion(__version__)
    application.setQuitOnLastWindowClosed(True)

    loop = qasync.QEventLoop(application)
    asyncio.set_event_loop(loop)

    settings_store = SettingsStore(paths.settings)
    try:
        settings = settings_store.load()
    except SettingsError:
        settings = AppSettings()
    vault = SecretsVault(paths.secrets)
    try:
        secrets = vault.load()
    except SecretsError:
        secrets = {}
    configure_logging(paths.log, set(secrets.values()))

    repository = TaskRepository(paths.database)
    repository.initialize()
    repository.recover_interrupted()
    integrity_service = FileIntegrityService(repository, paths)
    catalog = CatalogRepository(paths.catalog_database)
    catalog_error: Exception | None = None
    try:
        catalog.initialize()
        catalog.recover_interrupted_subscriptions(datetime.now(UTC))
    except Exception as error:
        catalog_error = error
    thumbnails = ThumbnailCache(paths.thumbnail_cache)
    content_browser = ContentBrowserService(catalog, thumbnails)
    subscriptions = SubscriptionService(catalog)
    window = MainWindow()
    if catalog_error is not None:
        window.content_page.show_error(f"内容目录不可用（{type(catalog_error).__name__}）")
    login_dialog = LoginDialog(window)

    def gateway_factory(
        api_id: int,
        api_hash: str,
        session: str,
        proxy,
        proxy_password: str,
    ) -> TelethonGateway:
        return TelethonGateway(api_id, api_hash, session, proxy, proxy_password)

    def build_services(gateway: TelethonGateway, resource_settings: AppSettings):
        planner = TaskPlanner(
            gateway,
            repository,
            paths.downloads,
            naming=resource_settings.download_naming,
        )
        bandwidth = AsyncBandwidthLimiter(resource_settings.speed_limit_kib)
        downloader = MediaDownloader(gateway, repository, paths, bandwidth=bandwidth)
        scheduler = DownloadScheduler(
            repository,
            downloader,
            concurrency=resource_settings.concurrency,
            bandwidth=bandwidth,
            publish=publish_event,
        )
        schedule_state = evaluate_download_schedule(
            resource_settings.download_schedule,
            datetime.now().astimezone(),
        )
        scheduler.set_admission_open(schedule_state.allowed)
        content_browser.bind_online(gateway, planner)
        subscriptions.bind_online(gateway, planner)
        return planner, scheduler, content_browser

    gateway = None
    planner = None
    scheduler = None
    api_hash = secrets.get("api_hash", "")
    if settings.api_id > 0 and api_hash:
        gateway = gateway_factory(
            settings.api_id,
            api_hash,
            secrets.get("session", ""),
            settings.proxy,
            secrets.get("proxy_password", ""),
        )
        planner, scheduler, content_browser = build_services(
            gateway,
            settings,
        )

    async def confirm_preview(preview: ScanPreview) -> bool:
        dialog = QMessageBox(window)
        dialog.setWindowTitle("确认下载任务")
        dialog.setText(_download_confirmation_text(preview))
        dialog.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        dialog.setDefaultButton(QMessageBox.StandardButton.Yes)
        loop = asyncio.get_running_loop()
        finished: asyncio.Future[bool] = loop.create_future()

        def resolve(answer: int) -> None:
            if not finished.done():
                finished.set_result(
                    _standard_button_selected(
                        answer,
                        QMessageBox.StandardButton.Yes,
                    )
                )

        dialog.finished.connect(resolve)
        dialog.open()
        try:
            return await finished
        except asyncio.CancelledError:
            dialog.reject()
            raise
        finally:
            dialog.deleteLater()

    trusted_keys = load_trusted_keys(Path(__file__).with_name("trusted_update_keys.json"))
    update_coordinator = UpdateCoordinator(
        paths,
        __version__,
        trusted_keys,
        HttpBytesClient(),
        ResumableUpdateDownloader(),
    )
    diagnostic_store = DiagnosticReportStore(paths, secrets=set(secrets.values()))

    async def confirm_update(manifest) -> bool:
        dialog = UpdateDialog(manifest, window)
        loop = asyncio.get_running_loop()
        finished: asyncio.Future[bool] = loop.create_future()

        def resolve(answer: int) -> None:
            if not finished.done():
                finished.set_result(
                    answer == UpdateDialog.DialogCode.Accepted.value
                )

        dialog.finished.connect(resolve)
        dialog.open()
        try:
            return await finished
        except asyncio.CancelledError:
            dialog.reject()
            raise
        finally:
            dialog.deleteLater()

    update_prompt = BackgroundUpdatePrompt(
        window_visible=window.isVisible,
        show_dialog=confirm_update,
        publish=publish_event,
    )

    controller_ref: dict[str, AppController] = {}

    def credential_health() -> DiagnosticResult:
        try:
            settings_store.load()
            current_settings_readable = True
        except SettingsError:
            current_settings_readable = False
        current_secrets_present = paths.secrets.is_file()
        try:
            vault.load()
            current_secrets_decryptable = True
        except SecretsError:
            current_secrets_decryptable = False
        return probe_credentials(
            settings_readable=current_settings_readable,
            secrets_present=current_secrets_present,
            secrets_decrypted=current_secrets_decryptable,
        )

    async def telegram_health() -> DiagnosticResult:
        return await _telegram_health(controller_ref["controller"])

    diagnostics = DiagnosticsService(
        (
            _FunctionDiagnosticProbe(
                "environment",
                "运行环境与路径",
                lambda: probe_environment(
                    paths,
                    frozen=bool(getattr(sys, "frozen", False)),
                    windows_x64=(
                        sys.platform == "win32"
                        and platform.machine().casefold() in {"amd64", "x86_64"}
                    ),
                    system_drive=os.environ.get("SYSTEMDRIVE", "C:"),
                ),
                threaded=True,
            ),
            _FunctionDiagnosticProbe(
                "project-write",
                "项目内写入",
                lambda: probe_project_write(paths),
                threaded=True,
            ),
            _FunctionDiagnosticProbe(
                "disk",
                "磁盘空间",
                lambda: probe_disk(paths),
                threaded=True,
            ),
            _FunctionDiagnosticProbe(
                "components",
                "运行组件",
                lambda: probe_components(component_availability()),
                threaded=True,
            ),
            _FunctionDiagnosticProbe(
                "task-database",
                "下载任务数据库",
                lambda: probe_task_database(paths.database),
                threaded=True,
            ),
            _FunctionDiagnosticProbe(
                "content-database",
                "账号内容数据库",
                lambda: probe_content_database(paths.catalog_database),
                threaded=True,
            ),
            _FunctionDiagnosticProbe(
                "credentials",
                "登录凭据",
                credential_health,
                threaded=True,
            ),
            _FunctionDiagnosticProbe(
                "telegram",
                "Telegram 连接",
                telegram_health,
                threaded=False,
            ),
            _FunctionDiagnosticProbe(
                "updates",
                "签名更新源",
                lambda: probe_update_sources(update_coordinator),
                threaded=False,
            ),
        ),
        app_version=__version__,
    )
    async def subscription_session_expired(error: SessionExpiredError) -> None:
        controller = controller_ref.get("controller")
        if controller is not None:
            await controller._handle_session_expired(error)

    def subscription_rules_changed() -> None:
        controller = controller_ref.get("controller")
        if controller is not None:
            controller._spawn_background(controller._reload_subscriptions())

    subscription_scheduler = SubscriptionScheduler(
        subscriptions,
        foreground_busy=lambda: (
            controller_ref["controller"].foreground_telegram_busy()
            if "controller" in controller_ref
            else True
        ),
        on_rules_changed=subscription_rules_changed,
        on_task_created=lambda task_id: (
            controller_ref["controller"].subscription_task_created(task_id)
            if "controller" in controller_ref
            else None
        ),
        on_progress=window.subscriptions_page.set_progress,
        on_session_expired=subscription_session_expired,
        publish=publish_event,
    )

    controller = AppController(
        gateway=gateway,
        planner=planner,
        scheduler=scheduler,
        repository=repository,
        settings_store=settings_store,
        vault=vault,
        window=window,
        login_dialog=login_dialog,
        content_browser=content_browser,
        subscriptions=subscriptions,
        subscription_scheduler=subscription_scheduler,
        integrity_service=integrity_service,
        diagnostics=diagnostics,
        diagnostic_store=diagnostic_store,
        paths=paths,
        gateway_factory=gateway_factory,
        service_builder=build_services,
        confirm_preview=confirm_preview,
        update_coordinator=update_coordinator,
        update_prompt=update_prompt,
        update_shutdown=application.quit,
        publish=publish_event,
        settings=settings,
        secrets=secrets,
    )
    controller_ref["controller"] = controller
    async_actions = AsyncActionBridge()
    controller._async_actions = async_actions

    @qasync.asyncSlot(str)
    async def scan_requested(link: str) -> None:
        local_timezone = datetime_now_timezone()
        filters = AppController.filters_from_dates(
            window.date_from.date().toPython(),
            window.date_to.date().toPython(),
            window.selected_media_kinds(),
            window.limit_input.value(),
            local_timezone,
        )
        await controller.scan_link(link, filters)

    @qasync.asyncSlot(object)
    async def batch_scan_requested(value: object) -> None:
        links = tuple(str(item) for item in value) if isinstance(value, (list, tuple)) else ()
        local_timezone = datetime_now_timezone()
        filters = AppController.filters_from_dates(
            window.date_from.date().toPython(),
            window.date_to.date().toPython(),
            window.selected_media_kinds(),
            window.limit_input.value(),
            local_timezone,
        )
        await controller.scan_links(links, filters)

    @qasync.asyncSlot(int, str, object, str)
    async def credentials_submitted(
        api_id: int,
        api_hash_value: str,
        proxy: object,
        proxy_password: str,
    ) -> None:
        await controller.submit_credentials(
            api_id,
            api_hash_value,
            proxy,
            proxy_password,
        )

    @qasync.asyncSlot(str)
    async def phone_submitted(phone: str) -> None:
        await controller.submit_phone(phone)

    @qasync.asyncSlot(str)
    async def code_submitted(code: str) -> None:
        await controller.submit_code(code)

    @qasync.asyncSlot(str)
    async def password_submitted(password: str) -> None:
        await controller.submit_password(password)

    def _task_ids(value: object) -> list[str]:
        if not isinstance(value, (list, tuple, set)):
            return []
        return list(dict.fromkeys(str(item) for item in value if item))

    def task_selection_changed(value: object) -> None:
        controller.select_task_details(_task_ids(value))

    async def pause_tasks_requested(value: object) -> None:
        await controller.pause_tasks(_task_ids(value))

    async def prioritize_task_requested(task_id: str) -> None:
        await controller.prioritize_task(task_id)

    async def resume_tasks_requested(value: object) -> None:
        await controller.resume_tasks(_task_ids(value))

    async def retry_tasks_requested(value: object) -> None:
        await controller.retry_failed_tasks(_task_ids(value))

    async def delete_content_history_requested(search_id: str) -> None:
        await controller.delete_content_history(search_id)

    async def clear_content_history_requested() -> None:
        await controller.clear_content_history()

    async def archive_tasks_requested(value: object) -> None:
        await controller.archive_tasks(_task_ids(value))

    async def restore_tasks_requested(value: object) -> None:
        await controller.restore_tasks(_task_ids(value))

    async def subscription_run_requested(rule_id: str) -> None:
        await controller.run_subscription_now(rule_id)

    async def subscription_enabled_requested(rule_id: str, enabled: bool) -> None:
        await controller.set_subscription_enabled(rule_id, enabled)

    async def subscription_delete_requested(rule_id: str) -> None:
        await controller.delete_subscription(rule_id)

    def open_media_requested(item_id: str) -> None:
        controller.open_media_file(item_id)

    async def verify_media_requested(value: object) -> None:
        await controller.verify_media(_task_ids(value))

    async def verify_tasks_requested(value: object) -> None:
        await controller.verify_tasks(_task_ids(value))

    async def repair_media_requested(value: object) -> None:
        await controller.repair_media(_task_ids(value))

    def integrity_cancel_requested() -> None:
        controller.cancel_integrity()

    @qasync.asyncSlot(str)
    async def content_dialog_selected(peer_ref: str) -> None:
        await controller.select_content_dialog(peer_ref)

    async def content_search_requested(
        scope_value: str,
        peer_ref: str,
        keyword: str,
        date_from: object,
        date_to: object,
        media_kinds: object,
        item_limit: int,
    ) -> None:
        scope = SearchScope(scope_value)
        filters = AppController.filters_from_dates(
            date_from,
            date_to,
            frozenset(media_kinds),
            item_limit,
            datetime_now_timezone(),
        )
        await controller.search_content(
            peer_ref,
            ContentSearchQuery(keyword, filters),
            scope=scope,
        )

    async def content_load_more_requested(search_id: str) -> None:
        await controller.load_more_content(search_id)

    @qasync.asyncSlot(str)
    async def content_queue_requested(search_id: str) -> None:
        await controller.queue_content_selection(search_id)

    @qasync.asyncSlot(str)
    async def content_preview_requested(result_id: str) -> None:
        await controller.open_content_preview(result_id)

    @qasync.asyncSlot(object)
    async def subscription_create_requested(draft: object) -> None:
        await controller.create_subscription(draft)

    @qasync.asyncSlot(str, object)
    async def subscription_update_requested(rule_id: str, draft: object) -> None:
        await controller.update_subscription(rule_id, draft)

    def subscription_probe_requested(rule_id: str) -> None:
        async_actions.start(
            "subscriptions.probe",
            lambda: controller.probe_subscription(rule_id),
            hooks=ActionHooks(
                failed=lambda error: window.subscriptions_page.show_error(
                    controller._safe_error(error)
                )
            ),
        )

    async def open_settings() -> None:
        thumbnail_cache_bytes = await asyncio.to_thread(thumbnails.total_bytes)
        dialog = SettingsDialog(
            controller.settings,
            controller.secrets.get("proxy_password", ""),
            window,
            thumbnail_cache_bytes=thumbnail_cache_bytes,
            autostart_available=bool(
                getattr(controller.runtime_settings_effects, "autostart_available", False)
            ),
            tray_available=bool(getattr(controller, "tray_available", False)),
        )
        controller._settings_dialog = dialog

        @qasync.asyncSlot(object, str)
        async def proxy_test_requested(proxy: object, password: str) -> None:
            await controller.test_proxy(proxy, password)

        async def clear_thumbnail_cache() -> None:
            await controller.clear_thumbnail_cache()

        async def save_settings() -> None:
            await controller.apply_settings(
                dialog.values(),
                dialog.proxy_password.text(),
            )

        dialog.test_proxy_requested.connect(proxy_test_requested)
        async_actions.connect(
            dialog.thumbnail_cache_clear_requested,
            "settings.thumbnail_cache.clear",
            clear_thumbnail_cache,
            hooks=ActionHooks(
                started=lambda: dialog.set_thumbnail_cache_busy(True),
                failed=lambda error: dialog._show_error(controller._safe_error(error)),
                finished=lambda: dialog.set_thumbnail_cache_busy(False),
            ),
        )
        async_actions.connect(
            dialog.save_requested,
            "settings.save",
            save_settings,
            hooks=ActionHooks(
                started=lambda: dialog.set_save_busy(True),
                succeeded=dialog.accept,
                failed=lambda error: dialog._show_error(controller._safe_error(error)),
                finished=lambda: dialog.set_save_busy(False),
            ),
        )
        controller._ui_slots.extend(
            (proxy_test_requested, clear_thumbnail_cache, save_settings)
        )
        dialog.open()

    window.scan_requested.connect(scan_requested)
    window.batch_scan_requested.connect(batch_scan_requested)
    window.task_selection_changed.connect(task_selection_changed)
    window.open_media_requested.connect(open_media_requested)
    window.integrity_cancel_requested.connect(integrity_cancel_requested)
    window.open_directory_requested.connect(controller.open_task_directory)
    window.login_requested.connect(controller.show_login)
    window.content_page.dialog_selected.connect(content_dialog_selected)
    window.content_page.link_requested.connect(controller.route_content_link)
    window.content_page.cancel_search_requested.connect(controller.cancel_content_search)
    window.content_page.selection_changed.connect(controller.set_content_selected)
    window.content_page.queue_requested.connect(content_queue_requested)
    window.content_page.thumbnail_requested.connect(controller.request_thumbnail)
    window.content_page.preview_requested.connect(content_preview_requested)
    window.content_page.history_open_requested.connect(controller._reload_content_search)
    window.subscriptions_page.create_requested.connect(subscription_create_requested)
    window.subscriptions_page.update_requested.connect(subscription_update_requested)
    window.subscriptions_page.rule_selected.connect(controller.show_subscription_details)
    window.subscriptions_page.probe_requested.connect(subscription_probe_requested)
    window.subscriptions_page.probe_cancel_requested.connect(controller.cancel_subscription_probe)
    window.diagnostics_page.open_directory_requested.connect(
        controller.open_diagnostics_directory
    )
    login_dialog.credentials_submitted.connect(credentials_submitted)
    login_dialog.phone_submitted.connect(phone_submitted)
    login_dialog.code_submitted.connect(code_submitted)
    login_dialog.password_submitted.connect(password_submitted)

    def content_failure(error: Exception) -> None:
        window.content_page.show_error(controller._safe_error(error))

    def login_hooks(action: str) -> ActionHooks:
        return ActionHooks(
            started=lambda: login_dialog.set_action_busy(action, True),
            failed=lambda error: login_dialog.show_error(controller._safe_error(error)),
            finished=lambda: login_dialog.set_action_busy(action, False),
        )

    async_actions.connect(
        window.settings_requested,
        "settings.open",
        open_settings,
        hooks=ActionHooks(
            failed=lambda error: window.statusBar().showMessage(
                controller._safe_error(error),
                0,
            )
        ),
    )
    async_actions.connect_args(
        window.content_page.search_requested,
        "content.search",
        content_search_requested,
        hooks=ActionHooks(failed=content_failure),
    )
    async_actions.connect_payload(
        window.content_page.load_more_requested,
        "content.load_more",
        content_load_more_requested,
        hooks=ActionHooks(failed=content_failure),
    )
    async_actions.connect_payload(
        window.content_page.history_delete_requested,
        "content.history.delete",
        delete_content_history_requested,
        hooks=ActionHooks(failed=content_failure),
    )
    async_actions.connect(
        window.content_page.history_clear_requested,
        "content.history.clear",
        clear_content_history_requested,
        hooks=ActionHooks(failed=content_failure),
    )
    async_actions.connect(
        window.content_activated,
        "content.activate",
        lambda: controller.activate_content_page(),
        hooks=ActionHooks(failed=content_failure),
    )
    async_actions.connect(
        window.subscriptions_activated,
        "subscriptions.activate",
        lambda: controller.activate_subscriptions_page(),
        hooks=ActionHooks(
            failed=lambda error: window.subscriptions_page.show_error(controller._safe_error(error))
        ),
    )
    async_actions.connect(
        window.diagnostics_activated,
        "diagnostics.activate",
        lambda: controller.activate_diagnostics(),
        hooks=ActionHooks(
            failed=lambda error: window.diagnostics_page.show_error(
                controller._safe_error(error)
            )
        ),
    )
    async_actions.connect(
        window.diagnostics_page.export_requested,
        "diagnostics.export",
        lambda: controller.export_diagnostics(),
        hooks=ActionHooks(
            started=lambda: window.diagnostics_page.set_export_busy(True),
            failed=lambda error: window.diagnostics_page.show_error(
                controller._safe_error(error)
            ),
            finished=lambda: window.diagnostics_page.set_export_busy(False),
        ),
    )
    async_actions.connect(
        window.diagnostics_page.run_requested,
        "diagnostics.run",
        lambda: controller.run_diagnostics(),
        hooks=ActionHooks(
            started=lambda: window.diagnostics_page.set_running(True),
            failed=lambda error: window.diagnostics_page.show_error(
                controller._safe_error(error)
            ),
            finished=lambda: window.diagnostics_page.set_running(False),
        ),
    )
    async_actions.connect(
        window.diagnostics_page.cancel_requested,
        "diagnostics.cancel",
        lambda: controller.cancel_diagnostics(),
        hooks=ActionHooks(
            failed=lambda error: window.diagnostics_page.show_error(
                controller._safe_error(error)
            )
        ),
    )

    def task_failure(error: Exception) -> None:
        window.statusBar().showMessage(controller._safe_error(error), 0)

    async_actions.connect_payload(
        window.subscriptions_page.run_requested,
        "subscriptions.run",
        subscription_run_requested,
        hooks=ActionHooks(failed=task_failure),
    )
    async_actions.connect_args(
        window.subscriptions_page.enabled_requested,
        "subscriptions.enabled",
        subscription_enabled_requested,
        hooks=ActionHooks(failed=task_failure),
    )
    async_actions.connect_payload(
        window.subscriptions_page.delete_requested,
        "subscriptions.delete",
        subscription_delete_requested,
        hooks=ActionHooks(failed=task_failure),
    )
    async_actions.connect_payload(
        window.pause_tasks_requested,
        "tasks.pause",
        pause_tasks_requested,
        hooks=ActionHooks(failed=task_failure),
    )
    async_actions.connect_payload(
        window.prioritize_task_requested,
        "tasks.prioritize",
        prioritize_task_requested,
        hooks=ActionHooks(failed=task_failure),
    )
    async_actions.connect_payload(
        window.archive_tasks_requested,
        "tasks.archive",
        archive_tasks_requested,
        hooks=ActionHooks(failed=task_failure),
    )
    async_actions.connect_payload(
        window.restore_tasks_requested,
        "tasks.restore",
        restore_tasks_requested,
        hooks=ActionHooks(failed=task_failure),
    )
    async_actions.connect_payload(
        window.resume_tasks_requested,
        "tasks.resume",
        resume_tasks_requested,
        hooks=ActionHooks(failed=task_failure),
    )
    async_actions.connect_payload(
        window.retry_tasks_requested,
        "tasks.retry",
        retry_tasks_requested,
        hooks=ActionHooks(failed=task_failure),
    )
    async_actions.connect_payload(
        window.verify_media_requested,
        "integrity.operation",
        verify_media_requested,
        hooks=ActionHooks(failed=task_failure),
    )
    async_actions.connect_payload(
        window.verify_tasks_requested,
        "integrity.operation",
        verify_tasks_requested,
        hooks=ActionHooks(failed=task_failure),
    )
    async_actions.connect_payload(
        window.repair_media_requested,
        "integrity.operation",
        repair_media_requested,
        hooks=ActionHooks(failed=task_failure),
    )
    async_actions.connect(
        window.content_page.refresh_requested,
        "dialogs.refresh",
        lambda: controller.refresh_content_dialogs(),
        hooks=ActionHooks(failed=content_failure),
    )
    async_actions.connect(
        window.content_page.connection_retry_requested,
        "telegram.retry",
        lambda: controller.retry_telegram_connection(),
        hooks=ActionHooks(
            started=lambda: window.content_page.set_connection_action_busy(True),
            failed=content_failure,
            finished=lambda: window.content_page.set_connection_action_busy(False),
        ),
    )
    async_actions.connect(
        login_dialog.qr_refresh_requested,
        "login.qr.refresh",
        lambda: controller.refresh_qr_login(),
        hooks=login_hooks("qr.refresh"),
    )
    async_actions.connect(
        login_dialog.phone_fallback_requested,
        "login.phone",
        lambda: controller.use_phone_fallback(),
        hooks=login_hooks("login.phone"),
    )
    async_actions.connect(
        login_dialog.credentials_edit_requested,
        "login.credentials",
        lambda: controller.edit_credentials(),
        hooks=login_hooks("login.credentials"),
    )
    async_actions.connect(
        login_dialog.login_cancelled,
        "login.cancel",
        lambda: controller.cancel_login(),
        hooks=ActionHooks(
            failed=lambda error: login_dialog.show_error(controller._safe_error(error))
        ),
    )
    controller._ui_slots.extend(
        (
            scan_requested,
            batch_scan_requested,
            credentials_submitted,
            phone_submitted,
            code_submitted,
            password_submitted,
            task_selection_changed,
            pause_tasks_requested,
            prioritize_task_requested,
            resume_tasks_requested,
            retry_tasks_requested,
            delete_content_history_requested,
            clear_content_history_requested,
            archive_tasks_requested,
            restore_tasks_requested,
            open_media_requested,
            verify_media_requested,
            verify_tasks_requested,
            repair_media_requested,
            integrity_cancel_requested,
            content_dialog_selected,
            content_search_requested,
            content_load_more_requested,
            content_queue_requested,
            content_preview_requested,
            subscription_create_requested,
            subscription_update_requested,
            subscription_run_requested,
            subscription_enabled_requested,
            subscription_delete_requested,
            subscription_probe_requested,
            open_settings,
        )
    )
    return application, loop, controller


def datetime_now_timezone():
    from datetime import datetime

    return datetime.now().astimezone().tzinfo


def run(
    root: Path,
    instance_guard: WindowsInstanceGuard | None = None,
    *,
    startup_indicator: object | None = None,
    background: bool = False,
) -> int:
    launch_in_background = bool(background)
    guard = instance_guard or WindowsInstanceGuard()
    if not guard.acquire():
        if not request_activation(ACTIVATION_CHANNEL, timeout_ms=1000):
            guard.notify_already_running()
        _startup_close(startup_indicator)
        return 2

    activation_server: LocalActivationServer | None = None
    tray_adapter: QtTrayAdapter | None = None
    notification_batcher = NotificationBatcher(window_seconds=5.0)
    notification_arm: Callable[[], None] | None = None

    def publish_event(event: ApplicationEvent) -> None:
        if notification_batcher.record(event, now=monotonic()) and notification_arm:
            notification_arm()

    try:
        _startup_status(startup_indicator, "正在准备本地数据…")
        application, loop, controller = create_application(
            root,
            publish_event=publish_event,
        )

        download_schedule = DownloadScheduleController(
            lambda: controller.scheduler,
            controller.settings.download_schedule,
            publish=publish_event,
        )
        controller.download_schedule = download_schedule
        graceful_shutdown = _GracefulShutdown(
            controller,
            application.quit,
            before_controller_shutdown=download_schedule.shutdown,
        )
        window_port = QtWindowPort(
            controller.window,
            {
                NotificationRoute.TASKS: lambda: controller.window.show_page("tasks"),
                NotificationRoute.SUBSCRIPTIONS: lambda: controller.window.show_page(
                    "subscriptions"
                ),
                NotificationRoute.LOGIN: controller.show_login,
                NotificationRoute.UPDATE: controller.check_for_updates,
            },
        )
        tray_adapter = QtTrayAdapter(controller.window)

        def persist_tray_hint() -> None:
            updated = replace(controller.settings, tray_hint_shown=True)
            controller.settings_store.save(updated)
            controller.settings = updated

        background = BackgroundModeController(
            window_port,
            tray_adapter,
            graceful_shutdown.request,
            tray_hint_shown=controller.settings.tray_hint_shown,
            persist_tray_hint=persist_tray_hint,
        )
        background.configure(
            close_to_tray=controller.settings.close_to_tray,
            notifications_enabled=controller.settings.notifications_enabled,
        )
        autostart = CurrentUserAutostart(
            WindowsCurrentUserRegistry(),
            Path(sys.executable),
            frozen=bool(getattr(sys, "frozen", False)),
        )
        runtime_settings = RuntimeSettingsCoordinator(
            controller.settings_store,
            autostart,
            background,
            download_schedule,
        )
        controller.runtime_settings_effects = runtime_settings
        controller.tray_available = tray_adapter.available
        if controller.settings.autostart_enabled and autostart.available:
            try:
                autostart.reconcile(True)
            except Exception:
                _LOGGER.warning("无法校正开机启动配置")

        from PySide6.QtCore import QTimer

        notification_timer = QTimer(controller.window)
        notification_timer.setSingleShot(True)

        def arm_notification_timer() -> None:
            deadline = notification_batcher.next_deadline
            if deadline is None or notification_timer.isActive():
                return
            delay_ms = max(1, ceil((deadline - monotonic()) * 1000))
            notification_timer.start(delay_ms)

        def flush_notifications() -> None:
            for payload in notification_batcher.flush_due(now=monotonic()):
                background.show_notification(payload)
            arm_notification_timer()

        notification_timer.timeout.connect(flush_notifications)
        notification_arm = arm_notification_timer
        arm_notification_timer()
        tray_adapter.show_requested.connect(background.show_window)
        tray_adapter.hide_requested.connect(controller.window.hide)
        tray_adapter.exit_requested.connect(background.request_exit)
        tray_adapter.notification_activated.connect(background.show_window)

        async def pause_all_downloads() -> None:
            tasks = await asyncio.to_thread(controller.repository.list_tasks)
            await controller.pause_tasks(
                [
                    task.id
                    for task in tasks
                    if task.status
                    in {
                        TaskStatus.QUEUED,
                        TaskStatus.DOWNLOADING,
                        TaskStatus.WAITING_RETRY,
                    }
                ]
            )

        async def resume_all_downloads() -> None:
            tasks = await asyncio.to_thread(controller.repository.list_tasks)
            await controller.resume_tasks(
                [task.id for task in tasks if task.status is TaskStatus.PAUSED]
            )

        def open_downloads() -> None:
            try:
                downloads = controller.paths.guard(controller.paths.downloads)
                downloads.mkdir(parents=True, exist_ok=True)
                startfile = getattr(os, "startfile", None)
                if startfile is not None:
                    startfile(downloads)
            except (OSError, ValueError):
                controller._show_status("Windows 无法打开下载目录")

        tray_adapter.pause_all_requested.connect(
            lambda: controller._spawn_background(pause_all_downloads())
        )
        tray_adapter.resume_all_requested.connect(
            lambda: controller._spawn_background(resume_all_downloads())
        )
        tray_adapter.subscriptions_requested.connect(
            controller.subscription_scheduler.wake
        )
        tray_adapter.downloads_requested.connect(open_downloads)
        tray_adapter.show()

        def activate_main_window() -> None:
            background.show_window()

        activation_server = LocalActivationServer(
            ACTIVATION_CHANNEL,
            activate_main_window,
        )
        activation_server.start()
        graceful_shutdown, close_filter = _install_graceful_shutdown(
            application,
            controller,
            background,
            shutdown=graceful_shutdown,
        )
        session_shutdown = _install_session_shutdown(application, background)
        application.aboutToQuit.connect(loop.stop)
        with loop:

            async def start_application() -> None:
                _startup_status(startup_indicator, "正在恢复任务与账号…")
                _show_initial_window(
                    controller,
                    background=launch_in_background,
                    tray_available=tray_adapter.available,
                )
                _startup_finish(startup_indicator, controller.window)
                await download_schedule.start()
                await controller.start(
                    background=launch_in_background and tray_adapter.available
                )

            startup_task = loop.create_task(start_application())

            def startup_finished(task: asyncio.Task[None]) -> None:
                if not task.cancelled() and task.exception() is not None:
                    graceful_shutdown.request()

            startup_task.add_done_callback(startup_finished)
            loop.run_forever()
            loop.run_until_complete(graceful_shutdown.wait())
            loop.run_until_complete(controller._async_actions.shutdown())
            loop.run_until_complete(controller.shutdown())
            if not startup_task.done():
                startup_task.cancel()
                loop.run_until_complete(asyncio.gather(startup_task, return_exceptions=True))
            if not startup_task.cancelled():
                startup_task.result()
        del close_filter
        del session_shutdown
        return 0
    finally:
        if activation_server is not None:
            activation_server.close()
        if tray_adapter is not None:
            tray_adapter.hide()
        _startup_close(startup_indicator)
        guard.release()
