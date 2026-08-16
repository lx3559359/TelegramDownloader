from __future__ import annotations

import asyncio
import importlib
import json
import os
import platform
import sys
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from telegram_downloader import __version__
from telegram_downloader.catalog import CatalogRepository
from telegram_downloader.content import ContentSearchQuery
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
from telegram_downloader.downloader import MediaDownloader
from telegram_downloader.file_integrity import FileIntegrityService
from telegram_downloader.gateway import TelethonGateway
from telegram_downloader.instance_guard import WindowsInstanceGuard
from telegram_downloader.logging import configure_logging
from telegram_downloader.paths import PortablePaths
from telegram_downloader.planner import ScanPreview, TaskPlanner
from telegram_downloader.repository import TaskRepository
from telegram_downloader.resource_control import AsyncBandwidthLimiter
from telegram_downloader.scheduler import DownloadScheduler
from telegram_downloader.security import SecretsError, SecretsVault
from telegram_downloader.settings import AppSettings, SettingsError, SettingsStore
from telegram_downloader.subscription_scheduler import SubscriptionScheduler
from telegram_downloader.subscription_service import SubscriptionService
from telegram_downloader.thumbnail_cache import ThumbnailCache
from telegram_downloader.update import HttpBytesClient, UpdateCoordinator
from telegram_downloader.update_contract import load_trusted_keys
from telegram_downloader.update_download import ResumableUpdateDownloader


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


class _GracefulShutdown:
    def __init__(self, controller: Any, quit_application: Callable[[], None]) -> None:
        self.controller = controller
        self.quit_application = quit_application
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
            await self.controller.shutdown()
        finally:
            self.completed = True
            self.quit_application()


def _install_graceful_shutdown(application: Any, controller: Any):
    from PySide6.QtCore import QEvent, QObject

    shutdown = _GracefulShutdown(controller, application.quit)

    class WindowCloseFilter(QObject):
        def eventFilter(self, watched, event):
            if event.type() == QEvent.Type.Close and not shutdown.completed:
                event.ignore()
                watched.hide()
                shutdown.request()
                return True
            return super().eventFilter(watched, event)

    close_filter = WindowCloseFilter(controller.window)
    controller.window.installEventFilter(close_filter)
    application.setQuitOnLastWindowClosed(False)
    controller.update_shutdown = shutdown.request
    return shutdown, close_filter


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


def create_application(root: Path):
    import qasync
    from PySide6.QtWidgets import QApplication, QMessageBox

    from telegram_downloader.ui.async_actions import ActionHooks, AsyncActionBridge
    from telegram_downloader.ui.login import LoginDialog
    from telegram_downloader.ui.main import MainWindow
    from telegram_downloader.ui.settings import SettingsDialog
    from telegram_downloader.ui.update_dialog import UpdateDialog

    paths = PortablePaths(root)
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
        planner = TaskPlanner(gateway, repository, paths.downloads)
        bandwidth = AsyncBandwidthLimiter(resource_settings.speed_limit_kib)
        downloader = MediaDownloader(gateway, repository, paths, bandwidth=bandwidth)
        scheduler = DownloadScheduler(
            repository,
            downloader,
            concurrency=resource_settings.concurrency,
            bandwidth=bandwidth,
        )
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
        known = AppController._format_bytes(preview.known_bytes)
        unknown = (
            f"，另有 {preview.unknown_size_count} 项大小未知" if preview.unknown_size_count else ""
        )
        dialog = QMessageBox(window)
        dialog.setWindowTitle("确认下载任务")
        dialog.setText(
            f"扫描到 {len(preview.items)} 项媒体，已知大小 {known}{unknown}。\n\n加入下载队列？"
        )
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

    def confirm_update(manifest) -> bool:
        return UpdateDialog(manifest, window).exec() == UpdateDialog.DialogCode.Accepted

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
        controller = controller_ref["controller"]
        gateway_value = controller.gateway
        if gateway_value is None:
            return await probe_telegram(None)

        class RecoveredConnection:
            async def test_connection(self) -> None:
                await controller.connection_recovery.ensure_connected(gateway_value)
                await gateway_value.test_connection()

        return await probe_telegram(RecoveredConnection())

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
    subscription_scheduler = SubscriptionScheduler(
        subscriptions,
        foreground_busy=lambda: (
            controller_ref["controller"].foreground_telegram_busy()
            if "controller" in controller_ref
            else True
        ),
        on_rules_changed=lambda: (
            controller_ref["controller"]._reload_subscriptions()
            if "controller" in controller_ref
            else None
        ),
        on_task_created=lambda task_id: (
            controller_ref["controller"].subscription_task_created(task_id)
            if "controller" in controller_ref
            else None
        ),
        on_progress=window.subscriptions_page.set_progress,
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
        update_prompt=confirm_update,
        update_shutdown=application.quit,
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

    def pause_tasks_requested(value: object) -> None:
        controller.pause_tasks(_task_ids(value))

    def prioritize_task_requested(task_id: str) -> None:
        controller.prioritize_task(task_id)

    async def resume_tasks_requested(value: object) -> None:
        await controller.resume_tasks(_task_ids(value))

    async def retry_tasks_requested(value: object) -> None:
        await controller.retry_failed_tasks(_task_ids(value))

    def archive_tasks_requested(value: object) -> None:
        controller.archive_tasks(_task_ids(value))

    def restore_tasks_requested(value: object) -> None:
        controller.restore_tasks(_task_ids(value))

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

    @qasync.asyncSlot(str, str, object, object, object, int)
    async def content_search_requested(
        peer_ref: str,
        keyword: str,
        date_from: object,
        date_to: object,
        media_kinds: object,
        item_limit: int,
    ) -> None:
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
        )

    @qasync.asyncSlot(str)
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

    def open_settings() -> None:
        dialog = SettingsDialog(
            controller.settings,
            controller.secrets.get("proxy_password", ""),
            window,
            thumbnail_cache_bytes=thumbnails.total_bytes(),
        )
        controller._settings_dialog = dialog

        @qasync.asyncSlot(object, str)
        async def proxy_test_requested(proxy: object, password: str) -> None:
            await controller.test_proxy(proxy, password)

        def save_settings() -> None:
            controller.apply_settings(dialog.values(), dialog.proxy_password.text())

        dialog.test_proxy_requested.connect(proxy_test_requested)
        dialog.thumbnail_cache_clear_requested.connect(controller.clear_thumbnail_cache)
        dialog.accepted.connect(save_settings)
        controller._ui_slots.extend((proxy_test_requested, save_settings))
        dialog.open()

    window.scan_requested.connect(scan_requested)
    window.task_selection_changed.connect(task_selection_changed)
    window.pause_tasks_requested.connect(pause_tasks_requested)
    window.prioritize_task_requested.connect(prioritize_task_requested)
    window.archive_tasks_requested.connect(archive_tasks_requested)
    window.restore_tasks_requested.connect(restore_tasks_requested)
    window.open_media_requested.connect(open_media_requested)
    window.integrity_cancel_requested.connect(integrity_cancel_requested)
    window.open_directory_requested.connect(controller.open_task_directory)
    window.settings_requested.connect(open_settings)
    window.login_requested.connect(controller.show_login)
    window.content_page.dialog_selected.connect(content_dialog_selected)
    window.content_page.link_requested.connect(controller.route_content_link)
    window.content_page.search_requested.connect(content_search_requested)
    window.content_page.cancel_search_requested.connect(controller.cancel_content_search)
    window.content_page.load_more_requested.connect(content_load_more_requested)
    window.content_page.selection_changed.connect(controller.set_content_selected)
    window.content_page.queue_requested.connect(content_queue_requested)
    window.content_page.thumbnail_requested.connect(controller.request_thumbnail)
    window.content_page.preview_requested.connect(content_preview_requested)
    window.content_page.history_open_requested.connect(controller._reload_content_search)
    window.content_page.history_delete_requested.connect(controller.delete_content_history)
    window.content_page.history_clear_requested.connect(controller.clear_content_history)
    window.subscriptions_page.create_requested.connect(subscription_create_requested)
    window.subscriptions_page.update_requested.connect(subscription_update_requested)
    window.subscriptions_page.run_requested.connect(controller.run_subscription_now)
    window.subscriptions_page.enabled_requested.connect(controller.set_subscription_enabled)
    window.subscriptions_page.delete_requested.connect(controller.delete_subscription)
    window.subscriptions_page.rule_selected.connect(controller.show_subscription_details)
    window.subscriptions_page.probe_requested.connect(subscription_probe_requested)
    window.subscriptions_page.probe_cancel_requested.connect(controller.cancel_subscription_probe)
    window.diagnostics_activated.connect(controller.activate_diagnostics)
    window.diagnostics_page.export_requested.connect(controller.export_diagnostics)
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
            credentials_submitted,
            phone_submitted,
            code_submitted,
            password_submitted,
            task_selection_changed,
            pause_tasks_requested,
            resume_tasks_requested,
            retry_tasks_requested,
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
) -> int:
    guard = instance_guard or WindowsInstanceGuard()
    if not guard.acquire():
        guard.notify_already_running()
        _startup_close(startup_indicator)
        return 2

    try:
        _startup_status(startup_indicator, "正在准备本地数据…")
        application, loop, controller = create_application(root)
        graceful_shutdown, close_filter = _install_graceful_shutdown(
            application,
            controller,
        )
        application.aboutToQuit.connect(loop.stop)
        with loop:

            async def start_application() -> None:
                _startup_status(startup_indicator, "正在恢复任务与账号…")
                controller.window.show()
                _startup_finish(startup_indicator, controller.window)
                await controller.start()

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
        return 0
    finally:
        _startup_close(startup_indicator)
        guard.release()
