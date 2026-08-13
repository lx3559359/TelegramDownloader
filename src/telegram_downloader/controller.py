from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import Any

from telegram_downloader.domain import (
    ItemStatus,
    MediaKind,
    ScanFilters,
    TaskStatus,
)
from telegram_downloader.gateway import AuthState, GatewayError, TelegramGateway
from telegram_downloader.links import InvalidTelegramLink, parse_telegram_link
from telegram_downloader.paths import PortablePaths
from telegram_downloader.settings import AppSettings, ProxySettings
from telegram_downloader.ui.models import TaskSummary


class _MemorySettingsStore:
    def __init__(self) -> None:
        self.value = AppSettings()

    def load(self) -> AppSettings:
        return self.value

    def save(self, value: AppSettings) -> None:
        self.value = value


class _MemoryVault:
    def __init__(self) -> None:
        self.value: dict[str, str] = {}

    def load(self) -> dict[str, str]:
        return dict(self.value)

    def save(self, value: dict[str, str]) -> None:
        self.value = dict(value)


class _NullStatusBar:
    def showMessage(self, _message: str, _timeout: int = 0) -> None:
        pass


class _NullWindow:
    def __init__(self) -> None:
        self.account = None
        self.tasks = []
        self.message = _NullStatusBar()

    def set_account(self, value: str | None) -> None:
        self.account = value

    def set_task_summaries(self, value: list[TaskSummary]) -> None:
        self.tasks = value

    def statusBar(self) -> _NullStatusBar:
        return self.message


class _NullLoginDialog:
    def show_page(self, _page: object) -> None:
        pass

    def show_error(self, _message: str) -> None:
        pass

    def show_ready(self, _name: str) -> None:
        pass

    def accept(self) -> None:
        pass

    def show(self) -> None:
        pass

    def raise_(self) -> None:
        pass

    def activateWindow(self) -> None:
        pass


class _NullRepository:
    def list_tasks(self) -> list[object]:
        return []

    def list_items(self, _task_id: str, _statuses=None) -> list[object]:
        return []

    def get_task(self, task_id: str):
        raise KeyError(task_id)


class _NullScheduler:
    async def run_task(self, _task_id: str) -> None:
        pass

    async def resume_task(self, _task_id: str) -> None:
        pass

    def pause_task(self, _task_id: str) -> None:
        pass

    async def shutdown(self) -> None:
        pass


class AppController:
    def __init__(
        self,
        *,
        gateway: TelegramGateway | Any | None,
        planner: Any | None,
        scheduler: Any | None,
        repository: Any,
        settings_store: Any,
        vault: Any,
        window: Any,
        login_dialog: Any,
        paths: PortablePaths | None = None,
        gateway_factory: Callable[..., TelegramGateway] | None = None,
        service_builder: Callable[[TelegramGateway, int], tuple[Any, Any]] | None = None,
        confirm_preview: Callable[[Any], bool] | None = None,
        update_coordinator: Any | None = None,
        update_prompt: Callable[[Any], bool] | None = None,
        update_shutdown: Callable[[], None] | None = None,
        settings: AppSettings | None = None,
        secrets: dict[str, str] | None = None,
    ) -> None:
        self.gateway = gateway
        self.planner = planner
        self.scheduler = scheduler or _NullScheduler()
        self.repository = repository
        self.settings_store = settings_store
        self.vault = vault
        self.window = window
        self.login_dialog = login_dialog
        self.paths = paths
        self.gateway_factory = gateway_factory
        self.service_builder = service_builder
        self.confirm_preview = confirm_preview or (lambda _preview: True)
        self.update_coordinator = update_coordinator
        self.update_prompt = update_prompt or (lambda _manifest: False)
        self.update_shutdown = update_shutdown or (lambda: None)
        self.settings = settings or settings_store.load()
        self.secrets = dict(secrets if secrets is not None else vault.load())
        self.phone = ""
        self.phone_code_hash = ""
        self._background: set[asyncio.Task[Any]] = set()
        self._ui_slots: list[object] = []
        self._shutting_down = False
        self._settings_dialog: Any | None = None

    @classmethod
    def for_test(cls, **dependencies) -> AppController:
        settings_store = dependencies.pop("settings_store", _MemorySettingsStore())
        vault = dependencies.pop("vault", _MemoryVault())
        return cls(
            gateway=dependencies.pop("gateway", None),
            planner=dependencies.pop("planner", None),
            scheduler=dependencies.pop("scheduler", _NullScheduler()),
            repository=dependencies.pop("repository", _NullRepository()),
            settings_store=settings_store,
            vault=vault,
            window=dependencies.pop("window", _NullWindow()),
            login_dialog=dependencies.pop("login_dialog", _NullLoginDialog()),
            paths=dependencies.pop("paths", None),
            gateway_factory=dependencies.pop("gateway_factory", None),
            service_builder=dependencies.pop("service_builder", None),
            confirm_preview=dependencies.pop("confirm_preview", None),
            update_coordinator=dependencies.pop("update_coordinator", None),
            update_prompt=dependencies.pop("update_prompt", None),
            update_shutdown=dependencies.pop("update_shutdown", None),
            settings=dependencies.pop("settings", None),
            secrets=dependencies.pop("secrets", None),
            **dependencies,
        )

    async def start(self) -> None:
        self.refresh_tasks()
        if self.update_coordinator is not None and self.settings.check_updates_on_startup:
            self._spawn_background(self._run_update_check())
        if self.gateway is None:
            self.show_login()
            return
        try:
            await self.gateway.connect()
            name = await self._account_name()
            if name is None:
                self.show_login()
                return
            self.window.set_account(name)
            for task in self.repository.list_tasks():
                if task.status is TaskStatus.QUEUED:
                    self._start_task(task.id)
        except Exception as error:
            self._show_status(f"Telegram 连接失败：{self._safe_error(error)}")

    async def submit_credentials(
        self,
        api_id: int,
        api_hash: str,
        proxy: ProxySettings,
        proxy_password: str,
    ) -> None:
        try:
            updated_settings = replace(self.settings, api_id=api_id, proxy=proxy)
            updated_secrets = dict(self.secrets)
            updated_secrets["api_hash"] = api_hash
            if proxy_password:
                updated_secrets["proxy_password"] = proxy_password
            else:
                updated_secrets.pop("proxy_password", None)

            if self.gateway_factory is None:
                raise GatewayError("无法创建 Telegram 连接")
            gateway = self.gateway_factory(
                api_id,
                api_hash,
                updated_secrets.get("session", ""),
                proxy,
                proxy_password,
            )
            await gateway.connect()
            self.settings_store.save(updated_settings)
            self.vault.save(updated_secrets)
            self.settings = updated_settings
            self.secrets = updated_secrets
            self.gateway = gateway
            if self.service_builder is not None:
                self.planner, self.scheduler = self.service_builder(
                    gateway,
                    updated_settings.concurrency,
                )
            from telegram_downloader.ui.login import LoginPage

            self.login_dialog.show_page(LoginPage.PHONE)
        except Exception as error:
            self.login_dialog.show_error(self._safe_error(error))

    async def submit_phone(self, phone: str) -> None:
        if self.gateway is None:
            self.login_dialog.show_error("请先填写 API 凭据")
            return
        try:
            self.phone_code_hash = await self.gateway.request_code(phone)
            self.phone = phone
            from telegram_downloader.ui.login import LoginPage

            self.login_dialog.show_page(LoginPage.CODE)
        except Exception as error:
            self.login_dialog.show_error(self._safe_error(error))

    async def submit_code(self, code: str) -> None:
        if self.gateway is None or not self.phone or not self.phone_code_hash:
            self.login_dialog.show_error("验证码会话已失效，请重新发送验证码")
            return
        try:
            state = await self.gateway.sign_in(self.phone, code, self.phone_code_hash)
            if state is AuthState.PASSWORD_REQUIRED:
                from telegram_downloader.ui.login import LoginPage

                self.login_dialog.show_page(LoginPage.PASSWORD)
                return
            await self._finish_login()
        except Exception as error:
            self.login_dialog.show_error(self._safe_error(error))

    async def submit_password(self, password: str) -> None:
        if self.gateway is None:
            self.login_dialog.show_error("Telegram 连接尚未创建")
            return
        try:
            state = await self.gateway.check_password(password)
            if state is AuthState.READY:
                await self._finish_login()
        except Exception as error:
            self.login_dialog.show_error(self._safe_error(error))

    async def scan_link(self, link: str, filters: ScanFilters) -> None:
        if self.planner is None:
            self._show_status("请先登录 Telegram 账号")
            return
        try:
            source = parse_telegram_link(link)
            preview = await self.planner.scan(source, filters)
            if not self.confirm_preview(preview):
                self._show_status("已取消创建任务")
                return
            task = self.planner.commit(preview)
            self.refresh_tasks()
            if task is not None:
                self._start_task(task.id)
            self._show_status("任务已加入下载队列")
        except (InvalidTelegramLink, ValueError, GatewayError) as error:
            self._show_status(self._safe_error(error))

    async def test_proxy(self, proxy: ProxySettings, password: str) -> None:
        api_hash = self.secrets.get("api_hash", "")
        if self.gateway_factory is None or self.settings.api_id <= 0 or not api_hash:
            self._show_status("请先保存 API ID 和 API Hash")
            return
        probe = self.gateway_factory(self.settings.api_id, api_hash, "", proxy, password)
        try:
            await probe.connect()
            self._show_status("代理连接成功")
        except Exception as error:
            self._show_status(f"代理连接失败：{self._safe_error(error)}")
        finally:
            with suppress(Exception):
                await probe.disconnect()

    def apply_settings(self, settings: AppSettings, proxy_password: str) -> None:
        updated_secrets = dict(self.secrets)
        if proxy_password:
            updated_secrets["proxy_password"] = proxy_password
        else:
            updated_secrets.pop("proxy_password", None)
        self.settings_store.save(settings)
        self.vault.save(updated_secrets)
        self.settings = settings
        self.secrets = updated_secrets
        self._show_status("设置已保存；代理变更将在下次连接时生效")

    def pause_task(self, task_id: str) -> None:
        self.scheduler.pause_task(task_id)
        self.refresh_tasks()

    async def resume_task(self, task_id: str) -> None:
        await self.scheduler.resume_task(task_id)
        self.refresh_tasks()

    async def retry_failed(self, task_id: str) -> None:
        await self.resume_task(task_id)

    def open_task_directory(self, task_id: str) -> None:
        if self.paths is None:
            return
        task = self.repository.get_task(task_id)
        directory = self.paths.guard(self.paths.downloads / task.source_title)
        directory.mkdir(parents=True, exist_ok=True)
        startfile = getattr(os, "startfile", None)
        if startfile is not None:
            startfile(directory)

    async def shutdown(self) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        await self.scheduler.shutdown()
        pending = tuple(task for task in self._background if not task.done())
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if self.gateway is not None:
            await self.gateway.disconnect()

    def refresh_tasks(self) -> None:
        summaries: list[TaskSummary] = []
        for task in self.repository.list_tasks():
            items = self.repository.list_items(task.id)
            completed = sum(item.status is ItemStatus.COMPLETED for item in items)
            known_size = sum(item.expected_size or 0 for item in items)
            unknown = any(item.expected_size is None for item in items)
            summaries.append(
                TaskSummary(
                    task.id,
                    task.source_title,
                    task.status,
                    f"{completed} / {len(items)}",
                    self._format_bytes(known_size) + (" + 未知" if unknown else ""),
                    "—",
                    "—",
                )
            )
        self.window.set_task_summaries(summaries)

    def show_login(self) -> None:
        self.login_dialog.show()
        self.login_dialog.raise_()
        self.login_dialog.activateWindow()

    async def _finish_login(self) -> None:
        if self.gateway is None:
            return
        session = self.gateway.export_session()
        self.secrets["session"] = session
        self.vault.save(self.secrets)
        name = await self._account_name() or "已登录"
        self.window.set_account(name)
        self.phone = ""
        self.phone_code_hash = ""
        self.login_dialog.show_ready(name)
        self.login_dialog.accept()

    async def _account_name(self) -> str | None:
        method = getattr(self.gateway, "account_name", None)
        if method is None:
            return "已登录"
        return await method()

    def _start_task(self, task_id: str) -> None:
        self._spawn_background(self._run_and_refresh(task_id))

    def _spawn_background(self, operation) -> None:
        task = asyncio.create_task(operation)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _run_update_check(self) -> None:
        try:
            result = await self.update_coordinator.startup(
                self.update_prompt,
                self.update_shutdown,
            )
            if str(result) == "blocked":
                self._show_status("更新检查暂不可用，已继续使用当前版本")
        except Exception as error:
            self._show_status(f"更新检查失败（{type(error).__name__}）")

    async def _run_and_refresh(self, task_id: str) -> None:
        try:
            await self.scheduler.run_task(task_id)
        finally:
            self.refresh_tasks()

    def _show_status(self, message: str) -> None:
        self.window.statusBar().showMessage(message, 8000)

    @staticmethod
    def _safe_error(error: Exception) -> str:
        if isinstance(error, (GatewayError, InvalidTelegramLink, ValueError)):
            return str(error)
        return f"操作失败（{type(error).__name__}）"

    @staticmethod
    def _format_bytes(value: int) -> str:
        amount = float(value)
        units = ("B", "KB", "MB", "GB", "TB")
        for unit in units:
            if amount < 1024 or unit == units[-1]:
                return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
            amount /= 1024
        return f"{value} B"

    @staticmethod
    def filters_from_dates(
        date_from: date,
        date_to: date,
        media_kinds: frozenset[MediaKind],
        item_limit: int,
        local_timezone: tzinfo,
    ) -> ScanFilters:
        if date_from > date_to:
            raise ValueError("开始日期不能晚于结束日期")
        if not media_kinds:
            raise ValueError("请至少选择一种媒体类型")
        if not 1 <= item_limit <= 100000:
            raise ValueError("数量上限必须在 1 到 100000 之间")
        start = datetime.combine(date_from, time.min, local_timezone).astimezone(UTC)
        end = datetime.combine(date_to + timedelta(days=1), time.min, local_timezone)
        end = end.astimezone(UTC) - timedelta(microseconds=1)
        return ScanFilters(start, end, media_kinds, item_limit)

    @staticmethod
    def default_filters(now: datetime) -> ScanFilters:
        if now.tzinfo is None:
            raise ValueError("当前时间必须包含时区")
        utc = now.astimezone(UTC)
        start = datetime.combine(utc.date(), time.min, UTC)
        end = datetime.combine(utc.date(), time.max, UTC)
        return ScanFilters(start, end, frozenset(MediaKind), 500)
