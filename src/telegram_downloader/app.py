from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from telegram_downloader import __version__
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


def run_self_test(root: Path) -> dict[str, object]:
    paths = PortablePaths(root)
    paths.ensure_layout()
    repository = TaskRepository(paths.database)
    repository.initialize()
    repository.recover_interrupted()

    writable = {
        "settings": paths.settings,
        "secrets": paths.secrets,
        "database": paths.database,
        "log": paths.log,
        "cache": paths.cache,
        "temp": paths.temp,
        "downloads": paths.downloads,
        "update_staging": paths.update_staging,
        "update_backup": paths.update_backup,
        "update_helper": paths.update_helper,
        "update_journal": paths.update_journal,
    }
    resolved = {name: str(paths.guard(path)) for name, path in writable.items()}
    report: dict[str, object] = {
        "ok": True,
        "version": __version__,
        "runtime_root": str(paths.root),
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


def create_application(root: Path):
    import qasync
    from PySide6.QtWidgets import QApplication, QMessageBox

    from telegram_downloader.ui.login import LoginDialog
    from telegram_downloader.ui.main import MainWindow
    from telegram_downloader.ui.settings import SettingsDialog

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
    window = MainWindow()
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
        return planner, scheduler

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
        planner, scheduler = build_services(gateway, settings.concurrency)

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
        return answer is QMessageBox.StandardButton.Yes

    controller = AppController(
        gateway=gateway,
        planner=planner,
        scheduler=scheduler,
        repository=repository,
        settings_store=settings_store,
        vault=vault,
        window=window,
        login_dialog=login_dialog,
        paths=paths,
        gateway_factory=gateway_factory,
        service_builder=build_services,
        confirm_preview=confirm_preview,
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

    @qasync.asyncSlot(str)
    async def resume_requested(task_id: str) -> None:
        await controller.resume_task(task_id)

    @qasync.asyncSlot(str)
    async def retry_requested(task_id: str) -> None:
        await controller.retry_failed(task_id)

    def open_settings() -> None:
        dialog = SettingsDialog(
            controller.settings,
            controller.secrets.get("proxy_password", ""),
            window,
        )
        controller._settings_dialog = dialog

        @qasync.asyncSlot(object, str)
        async def proxy_test_requested(proxy: object, password: str) -> None:
            await controller.test_proxy(proxy, password)

        def save_settings() -> None:
            controller.apply_settings(dialog.values(), dialog.proxy_password.text())

        dialog.test_proxy_requested.connect(proxy_test_requested)
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
    login_dialog.credentials_submitted.connect(credentials_submitted)
    login_dialog.phone_submitted.connect(phone_submitted)
    login_dialog.code_submitted.connect(code_submitted)
    login_dialog.password_submitted.connect(password_submitted)
    controller._ui_slots.extend(
        (
            scan_requested,
            credentials_submitted,
            phone_submitted,
            code_submitted,
            password_submitted,
            resume_requested,
            retry_requested,
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
