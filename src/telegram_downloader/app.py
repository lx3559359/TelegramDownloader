from __future__ import annotations

import asyncio
import importlib
import json
import os
import sys
from pathlib import Path

from telegram_downloader import __version__
from telegram_downloader.catalog import CatalogRepository
from telegram_downloader.content import ContentSearchQuery
from telegram_downloader.content_browser import ContentBrowserService
from telegram_downloader.controller import AppController
from telegram_downloader.downloader import MediaDownloader
from telegram_downloader.gateway import TelethonGateway
from telegram_downloader.logging import configure_logging
from telegram_downloader.paths import PortablePaths
from telegram_downloader.planner import ScanPreview, TaskPlanner
from telegram_downloader.repository import TaskRepository
from telegram_downloader.scheduler import DownloadScheduler
from telegram_downloader.security import SecretsError, SecretsVault
from telegram_downloader.settings import AppSettings, SettingsError, SettingsStore
from telegram_downloader.thumbnail_cache import ThumbnailCache
from telegram_downloader.update import HttpBytesClient, UpdateCoordinator
from telegram_downloader.update_contract import load_trusted_keys
from telegram_downloader.update_download import ResumableUpdateDownloader


def run_self_test(root: Path) -> dict[str, object]:
    paths = PortablePaths(root)
    paths.ensure_layout()
    repository = TaskRepository(paths.database)
    repository.initialize()
    repository.recover_interrupted()
    catalog = CatalogRepository(paths.catalog_database)
    catalog.initialize()
    ThumbnailCache(paths.thumbnail_cache)

    writable = {
        "settings": paths.settings,
        "secrets": paths.secrets,
        "database": paths.database,
        "catalog_database": paths.catalog_database,
        "log": paths.log,
        "cache": paths.cache,
        "thumbnail_cache": paths.thumbnail_cache,
        "temp": paths.temp,
        "downloads": paths.downloads,
        "update_staging": paths.update_staging,
        "update_backup": paths.update_backup,
        "update_helper": paths.update_helper,
        "update_journal": paths.update_journal,
    }
    resolved = {name: str(paths.guard(path)) for name, path in writable.items()}
    components = {
        "pyside6": _can_import("PySide6"),
        "telethon": _can_import("telethon"),
        "qasync": _can_import("qasync"),
        "qrcode": _can_import("qrcode"),
        "sqlite": _can_import("sqlite3"),
        "dpapi": os.name == "nt",
    }
    report: dict[str, object] = {
        "ok": all(components.values()),
        "version": __version__,
        "runtime_root": str(paths.root),
        "components": components,
        "writable_paths": resolved,
    }
    report_path = paths.guard(paths.log.parent / "self-test.json")
    temporary = paths.guard(report_path.with_suffix(".json.tmp"))
    content = (
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
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


def create_application(root: Path):
    import qasync
    from PySide6.QtWidgets import QApplication, QMessageBox

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
    catalog = CatalogRepository(paths.catalog_database)
    catalog_error: Exception | None = None
    try:
        catalog.initialize()
    except Exception as error:
        catalog_error = error
    thumbnails = ThumbnailCache(paths.thumbnail_cache)
    content_browser = ContentBrowserService(catalog, thumbnails)
    window = MainWindow()
    if catalog_error is not None:
        window.content_page.show_error(
            f"内容目录不可用（{type(catalog_error).__name__}）"
        )
    login_dialog = LoginDialog(window)

    def gateway_factory(
        api_id: int,
        api_hash: str,
        session: str,
        proxy,
        proxy_password: str,
    ) -> TelethonGateway:
        return TelethonGateway(api_id, api_hash, session, proxy, proxy_password)

    def build_services(gateway: TelethonGateway, concurrency: int):
        planner = TaskPlanner(gateway, repository, paths.downloads)
        downloader = MediaDownloader(gateway, repository, paths)
        scheduler = DownloadScheduler(repository, downloader, concurrency=concurrency)
        content_browser.bind_online(gateway, planner)
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
            settings.concurrency,
        )

    def confirm_preview(preview: ScanPreview) -> bool:
        known = AppController._format_bytes(preview.known_bytes)
        unknown = (
            f"，另有 {preview.unknown_size_count} 项大小未知"
            if preview.unknown_size_count
            else ""
        )
        answer = QMessageBox.question(
            window,
            "确认下载任务",
            f"扫描到 {len(preview.items)} 项媒体，已知大小 {known}{unknown}。\n\n加入下载队列？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        return _standard_button_selected(answer, QMessageBox.StandardButton.Yes)

    trusted_keys = load_trusted_keys(Path(__file__).with_name("trusted_update_keys.json"))
    update_coordinator = UpdateCoordinator(
        paths,
        __version__,
        trusted_keys,
        HttpBytesClient(),
        ResumableUpdateDownloader(),
    )

    def confirm_update(manifest) -> bool:
        return UpdateDialog(manifest, window).exec() == UpdateDialog.DialogCode.Accepted

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

    @qasync.asyncSlot()
    async def qr_refresh_requested() -> None:
        await controller.refresh_qr_login()

    @qasync.asyncSlot()
    async def phone_fallback_requested() -> None:
        await controller.use_phone_fallback()

    @qasync.asyncSlot()
    async def credentials_edit_requested() -> None:
        await controller.edit_credentials()

    @qasync.asyncSlot()
    async def login_cancelled() -> None:
        await controller.cancel_login()

    @qasync.asyncSlot(str)
    async def resume_requested(task_id: str) -> None:
        await controller.resume_task(task_id)

    @qasync.asyncSlot(str)
    async def retry_requested(task_id: str) -> None:
        await controller.retry_failed(task_id)

    @qasync.asyncSlot()
    async def content_refresh_requested() -> None:
        await controller.refresh_content_dialogs()

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
        dialog.thumbnail_cache_clear_requested.connect(
            controller.clear_thumbnail_cache
        )
        dialog.accepted.connect(save_settings)
        controller._ui_slots.extend((proxy_test_requested, save_settings))
        dialog.open()

    window.scan_requested.connect(scan_requested)
    window.pause_requested.connect(controller.pause_task)
    window.resume_requested.connect(resume_requested)
    window.retry_failed_requested.connect(retry_requested)
    window.open_directory_requested.connect(controller.open_task_directory)
    window.settings_requested.connect(open_settings)
    window.login_requested.connect(controller.show_login)
    window.content_page.refresh_requested.connect(content_refresh_requested)
    window.content_page.search_requested.connect(content_search_requested)
    window.content_page.cancel_search_requested.connect(
        controller.cancel_content_search
    )
    window.content_page.load_more_requested.connect(content_load_more_requested)
    window.content_page.selection_changed.connect(controller.set_content_selected)
    window.content_page.queue_requested.connect(content_queue_requested)
    window.content_page.thumbnail_requested.connect(controller.request_thumbnail)
    window.content_page.history_open_requested.connect(
        controller._reload_content_search
    )
    window.content_page.history_delete_requested.connect(
        controller.delete_content_history
    )
    window.content_page.history_clear_requested.connect(
        controller.clear_content_history
    )
    login_dialog.credentials_submitted.connect(credentials_submitted)
    login_dialog.phone_submitted.connect(phone_submitted)
    login_dialog.code_submitted.connect(code_submitted)
    login_dialog.password_submitted.connect(password_submitted)
    login_dialog.qr_refresh_requested.connect(qr_refresh_requested)
    login_dialog.phone_fallback_requested.connect(phone_fallback_requested)
    login_dialog.credentials_edit_requested.connect(credentials_edit_requested)
    login_dialog.login_cancelled.connect(login_cancelled)
    controller._ui_slots.extend(
        (
            scan_requested,
            credentials_submitted,
            phone_submitted,
            code_submitted,
            password_submitted,
            qr_refresh_requested,
            phone_fallback_requested,
            credentials_edit_requested,
            login_cancelled,
            resume_requested,
            retry_requested,
            content_refresh_requested,
            content_search_requested,
            content_load_more_requested,
            content_queue_requested,
            open_settings,
        )
    )
    return application, loop, controller


def datetime_now_timezone():
    from datetime import datetime

    return datetime.now().astimezone().tzinfo


def run(root: Path) -> int:
    application, loop, controller = create_application(root)
    application.aboutToQuit.connect(loop.stop)
    with loop:
        loop.run_until_complete(controller.start())
        controller.window.show()
        loop.run_forever()
        loop.run_until_complete(controller.shutdown())
    return 0
