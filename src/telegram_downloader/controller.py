from __future__ import annotations

import asyncio
import inspect
import logging
import os
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from pathlib import Path
from time import monotonic as monotonic_clock
from typing import Any

from telegram_downloader.connectivity import ConnectionRecovery
from telegram_downloader.content import ContentSearchQuery, SearchResult
from telegram_downloader.content_browser import NothingToQueueError
from telegram_downloader.content_progress import DialogSyncProgress, SearchProgress
from telegram_downloader.domain import (
    ItemStatus,
    MediaKind,
    ScanFilters,
    TaskStatus,
)
from telegram_downloader.gateway import (
    AuthState,
    GatewayError,
    SessionExpiredError,
    TelegramGateway,
    TransientNetworkError,
)
from telegram_downloader.links import InvalidTelegramLink, parse_telegram_link
from telegram_downloader.paths import PortablePaths
from telegram_downloader.settings import AppSettings, ProxySettings
from telegram_downloader.subscriptions import SubscriptionDraft
from telegram_downloader.ui.models import TaskSummary

_LOGGER = logging.getLogger("telegram_downloader.controller")


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
        self.content_page = _NullContentPage()
        self.subscriptions_page = _NullSubscriptionPage()

    def set_account(self, value: str | None) -> None:
        self.account = value

    def set_task_summaries(self, value: list[TaskSummary]) -> None:
        self.tasks = value

    def set_scan_busy(self, _busy: bool) -> None:
        pass

    def statusBar(self) -> _NullStatusBar:
        return self.message


class _NullContentPage:
    def set_logged_in(self, _value: bool) -> None:
        pass

    def set_dialogs(self, _value: list[object]) -> None:
        pass

    def set_sessions(self, _value: list[object]) -> None:
        pass

    def set_active_search(self, _value: object | None) -> None:
        pass

    def set_results(self, _value: list[object]) -> None:
        pass

    def set_search_busy(self, _busy: bool) -> None:
        pass

    def set_search_progress(self, _progress: SearchProgress | None) -> None:
        pass

    def set_sync_state(
        self,
        _text: str,
        *,
        busy: bool = False,
        count: int = 0,
    ) -> None:
        pass

    def set_connection_state(
        self,
        _text: str,
        *,
        retryable: bool = False,
    ) -> None:
        pass

    def set_queue_busy(self, _busy: bool) -> None:
        pass

    def set_thumbnail(self, _result_id: str, _path: object) -> None:
        pass

    def show_preview(self, _result: SearchResult, _path: Path | None) -> None:
        pass

    def show_error(self, _message: str) -> None:
        pass


class _NullLoginDialog:
    def set_saved_credentials(
        self,
        _api_id: int,
        _api_hash: str,
        _proxy: ProxySettings,
        _proxy_password: str,
    ) -> None:
        pass

    def show_page(self, _page: object) -> None:
        pass

    def show_error(self, _message: str) -> None:
        pass

    def show_qr(self, _url: str, _expires_at: datetime) -> None:
        pass

    def show_qr_status(self, _text: str) -> None:
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


class _NullSubscriptionPage:
    def set_logged_in(self, _value: bool) -> None:
        pass

    def set_dialogs(self, _value: list[object]) -> None:
        pass

    def set_rules(self, _value: list[object]) -> None:
        pass

    def set_rule_busy(
        self,
        _rule_id: str | None,
        _busy: bool,
        _text: str = "",
    ) -> None:
        pass

    def show_error(self, _message: str) -> None:
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


class _NullSubscriptionService:
    account = None

    def set_account(self, account: object | None) -> None:
        self.account = account

    def list_rules(self) -> list[object]:
        return []

    def go_offline(self) -> None:
        pass


class _NullSubscriptionScheduler:
    def start(self) -> None:
        pass

    def set_account(self, _account_id: str | None) -> None:
        pass

    def wake(self, _rule_id: str | None = None) -> None:
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
        content_browser: Any | None = None,
        subscriptions: Any | None = None,
        subscription_scheduler: Any | None = None,
        paths: PortablePaths | None = None,
        gateway_factory: Callable[..., TelegramGateway] | None = None,
        service_builder: Callable[
            [TelegramGateway, int],
            tuple[Any, Any, Any],
        ]
        | None = None,
        confirm_preview: Callable[[Any], bool | Awaitable[bool]] | None = None,
        update_coordinator: Any | None = None,
        update_prompt: Callable[[Any], bool] | None = None,
        update_shutdown: Callable[[], None] | None = None,
        settings: AppSettings | None = None,
        secrets: dict[str, str] | None = None,
        connection_recovery: ConnectionRecovery | None = None,
        connection_monitor_interval: float = 30.0,
        connection_sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        progress_refresh_interval: float = 0.5,
    ) -> None:
        if progress_refresh_interval <= 0:
            raise ValueError("进度刷新间隔必须大于零")
        if connection_monitor_interval <= 0:
            raise ValueError("连接监测间隔必须大于零")
        self.gateway = gateway
        self.planner = planner
        self.scheduler = scheduler or _NullScheduler()
        self.repository = repository
        self.settings_store = settings_store
        self.vault = vault
        self.window = window
        self.login_dialog = login_dialog
        self.content_browser = content_browser
        self.subscriptions = subscriptions or _NullSubscriptionService()
        self.subscription_scheduler = (
            subscription_scheduler or _NullSubscriptionScheduler()
        )
        self.paths = paths
        self.gateway_factory = gateway_factory
        self.service_builder = service_builder
        self.confirm_preview = confirm_preview or (lambda _preview: True)
        self.update_coordinator = update_coordinator
        self.update_prompt = update_prompt or (lambda _manifest: False)
        self.update_shutdown = update_shutdown or (lambda: None)
        self.settings = settings or settings_store.load()
        self.secrets = dict(secrets if secrets is not None else vault.load())
        self.connection_recovery = connection_recovery or ConnectionRecovery()
        self._connection_monitor_interval = connection_monitor_interval
        self._connection_sleeper = connection_sleeper
        self.phone = ""
        self.phone_code_hash = ""
        self._background: set[asyncio.Task[Any]] = set()
        self._connection_monitor_task: asyncio.Task[None] | None = None
        self._session_restore_task: asyncio.Task[None] | None = None
        self._qr_wait_task: asyncio.Task[None] | None = None
        self._qr_generation = 0
        self._ui_slots: list[object] = []
        self._shutting_down = False
        self._settings_dialog: Any | None = None
        self._dialog_sync_task: asyncio.Task[Any] | None = None
        self._content_search_task: asyncio.Task[Any] | None = None
        self._thumbnail_tasks: dict[str, asyncio.Task[Any]] = {}
        self._progress_refresh_interval = progress_refresh_interval
        self._next_progress_refresh = 0.0
        self._progress_samples: dict[str, tuple[float, int]] = {}

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
            content_browser=dependencies.pop("content_browser", None),
            subscriptions=dependencies.pop("subscriptions", None),
            subscription_scheduler=dependencies.pop("subscription_scheduler", None),
            paths=dependencies.pop("paths", None),
            gateway_factory=dependencies.pop("gateway_factory", None),
            service_builder=dependencies.pop("service_builder", None),
            confirm_preview=dependencies.pop("confirm_preview", None),
            update_coordinator=dependencies.pop("update_coordinator", None),
            update_prompt=dependencies.pop("update_prompt", None),
            update_shutdown=dependencies.pop("update_shutdown", None),
            settings=dependencies.pop("settings", None),
            secrets=dependencies.pop("secrets", None),
            connection_recovery=dependencies.pop("connection_recovery", None),
            **dependencies,
        )

    async def ensure_telegram_online(self) -> bool:
        page = self._content_page()
        if self.gateway is None:
            page.set_logged_in(False)
            page.set_connection_state(
                "请先登录 Telegram；已保存的搜索历史仍可查看",
                retryable=False,
            )
            self.show_login()
            return False

        if self._gateway_is_connected(self.gateway):
            page.set_logged_in(True)
            page.set_connection_state("连接正常", retryable=False)
            return True

        def attempt(value: tuple[int, int]) -> None:
            number, total = value
            text = (
                "正在连接 Telegram…"
                if number == 1
                else f"正在重连（{number}/{total}）…"
            )
            page.set_connection_state(text, retryable=False)

        try:
            await self.connection_recovery.ensure_connected(
                self.gateway,
                attempt,
            )
        except SessionExpiredError as error:
            await self._handle_session_expired(error)
            return False
        except TransientNetworkError:
            page.set_logged_in(False)
            page.set_connection_state(
                "重连失败，请检查网络或代理后重试",
                retryable=True,
            )
            self._show_status("Telegram 重连失败，请检查网络或代理")
            return False
        except Exception as error:
            safe = self._safe_error(error)
            page.set_logged_in(False)
            page.set_connection_state(f"连接失败：{safe}", retryable=True)
            self._show_status(f"Telegram 连接失败：{safe}")
            return False

        page.set_logged_in(True)
        page.set_connection_state("连接已恢复", retryable=False)
        return True

    async def retry_telegram_connection(self) -> bool:
        return await self.ensure_telegram_online()

    @staticmethod
    def _gateway_is_connected(gateway: object) -> bool:
        method = getattr(gateway, "is_connected", None)
        return bool(method()) if callable(method) else False

    async def start(self) -> None:
        self.refresh_tasks()
        await self.activate_cached_content_account()
        if self.update_coordinator is not None and self.settings.check_updates_on_startup:
            self._spawn_background(self._run_update_check())
        if self.gateway is None:
            self.show_login()
            return
        self._ensure_connection_monitor()
        self._session_restore_task = self._spawn_background(
            self._restore_saved_session()
        )

    def _ensure_connection_monitor(self) -> None:
        task = self._connection_monitor_task
        if self._shutting_down or (task is not None and not task.done()):
            return
        self._connection_monitor_task = self._spawn_background(
            self._monitor_connection()
        )

    async def _restore_saved_session(self) -> None:
        if not await self.ensure_telegram_online():
            return
        try:
            name = await self._account_name()
            if name is None:
                self.show_login()
                return
            self.window.set_account(name)
            await self.activate_content_account()
            for task in self.repository.list_tasks():
                if task.status is TaskStatus.QUEUED:
                    self._start_task(task.id)
        except SessionExpiredError as error:
            await self._handle_session_expired(error)
        except Exception as error:
            self._show_status(f"Telegram 连接失败：{self._safe_error(error)}")

    async def _monitor_connection(self) -> None:
        while not self._shutting_down:
            await self._connection_sleeper(self._connection_monitor_interval)
            if self._shutting_down:
                return
            gateway = self.gateway
            if gateway is None or self._gateway_is_connected(gateway):
                continue
            if self.connection_recovery.active:
                continue
            await self.ensure_telegram_online()

    async def submit_credentials(
        self,
        api_id: int,
        api_hash: str,
        proxy: ProxySettings,
        proxy_password: str,
    ) -> None:
        try:
            await self._cancel_qr_wait()
            await self._cancel_content_operations()
            await self.connection_recovery.cancel()
            if self.content_browser is not None:
                go_offline = getattr(self.content_browser, "go_offline", None)
                if go_offline is not None:
                    go_offline()
            self.subscriptions.go_offline()
            self.subscription_scheduler.set_account(None)
            if self.gateway is not None:
                await self.gateway.disconnect()
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
                services = self.service_builder(
                    gateway,
                    updated_settings.concurrency,
                )
                if len(services) == 3:
                    self.planner, self.scheduler, self.content_browser = services
                else:
                    self.planner, self.scheduler = services
            await self.begin_qr_login()
        except Exception as error:
            self.login_dialog.show_error(self._safe_error(error))

    async def begin_qr_login(self) -> None:
        if self.gateway is None:
            self.login_dialog.show_error("请先填写 API 凭据")
            return
        try:
            await self._cancel_qr_wait()
            info = await self.gateway.begin_qr_login()
            self._show_qr_and_wait(info)
        except TransientNetworkError as error:
            from telegram_downloader.ui.login import LoginPage

            self._prefill_login()
            self.login_dialog.show_page(LoginPage.CREDENTIALS)
            self.login_dialog.show_error(self._safe_error(error))
        except Exception as error:
            self.login_dialog.show_error(self._safe_error(error))

    async def refresh_qr_login(self) -> None:
        if self.gateway is None:
            self.login_dialog.show_error("请先填写 API 凭据")
            return
        try:
            await self._cancel_qr_wait()
            info = await self.gateway.refresh_qr_login()
            self._show_qr_and_wait(info)
        except TransientNetworkError as error:
            from telegram_downloader.ui.login import LoginPage

            self._prefill_login()
            self.login_dialog.show_page(LoginPage.CREDENTIALS)
            self.login_dialog.show_error(self._safe_error(error))
        except Exception as error:
            self.login_dialog.show_error(self._safe_error(error))

    async def use_phone_fallback(self) -> None:
        await self._cancel_qr_wait()
        from telegram_downloader.ui.login import LoginPage

        self.login_dialog.show_page(LoginPage.PHONE)

    async def edit_credentials(self) -> None:
        await self._cancel_qr_wait()
        await self.connection_recovery.cancel()
        if self.gateway is not None:
            await self.gateway.disconnect()
        from telegram_downloader.ui.login import LoginPage

        self._prefill_login()
        self.login_dialog.show_page(LoginPage.CREDENTIALS)

    async def cancel_login(self) -> None:
        await self._cancel_qr_wait()
        if self.gateway is not None:
            await self.gateway.disconnect()

    def _show_qr_and_wait(self, info) -> None:
        self._display_qr(info)
        self._qr_generation += 1
        generation = self._qr_generation
        task = asyncio.create_task(self._wait_for_qr(generation))
        self._qr_wait_task = task

    def _display_qr(self, info) -> None:
        self.login_dialog.show_qr(info.url, info.expires_at)
        self.login_dialog.show_qr_status("等待手机扫码确认")

    async def _cancel_qr_wait(self) -> None:
        task = self._qr_wait_task
        self._qr_wait_task = None
        self._qr_generation += 1
        if task is None or task is asyncio.current_task():
            return
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _wait_for_qr(self, generation: int) -> None:
        if self.gateway is None:
            return
        try:
            while generation == self._qr_generation:
                try:
                    state = await self.gateway.wait_qr_login()
                except TimeoutError:
                    info = await self.gateway.refresh_qr_login()
                    if generation != self._qr_generation:
                        return
                    self._display_qr(info)
                    continue
                if generation != self._qr_generation:
                    return
                if state is AuthState.PASSWORD_REQUIRED:
                    from telegram_downloader.ui.login import LoginPage

                    self.login_dialog.show_page(LoginPage.PASSWORD)
                    return
                await self._finish_login()
                return
        except asyncio.CancelledError:
            raise
        except Exception as error:
            safe = self._safe_error(error)
            if isinstance(error, (GatewayError, ValueError)):
                _LOGGER.warning("QR login failed (%s): %s", type(error).__name__, safe)
            else:
                _LOGGER.error("QR login failed (%s)", type(error).__name__)
            self.login_dialog.show_error(safe)
        finally:
            if self._qr_wait_task is asyncio.current_task():
                self._qr_wait_task = None

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
        self.window.set_scan_busy(True)
        try:
            if not await self.ensure_telegram_online():
                return
            if self.planner is None:
                self._show_error("请先登录 Telegram 账号")
                return
            source = parse_telegram_link(link)
            preview = await self.planner.scan(source, filters)
            if not await self._confirm_download_preview(preview):
                self._show_status("已取消创建任务")
                return
            task = self.planner.commit(preview)
            self.refresh_tasks()
            if task is not None:
                self._start_task(task.id)
            self._show_status("任务已加入下载队列")
        except (InvalidTelegramLink, ValueError, GatewayError) as error:
            safe = self._safe_error(error)
            _LOGGER.warning("scan rejected (%s): %s", type(error).__name__, safe)
            self._show_error(safe)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            _LOGGER.error("scan failed (%s)", type(error).__name__)
            self._show_error(self._safe_error(error))
        finally:
            self.window.set_scan_busy(False)

    def route_content_link(self, link: str) -> None:
        try:
            source = parse_telegram_link(link)
        except (InvalidTelegramLink, ValueError) as error:
            self._content_page().show_error(str(error))
            return
        self.window.open_link_preview(source.normalized_url)

    async def activate_cached_content_account(self) -> None:
        page = self._content_page()
        page.set_logged_in(False)
        if self.content_browser is None:
            return
        try:
            profile, dialogs = await self.content_browser.activate_cached_account()
            page.set_dialogs(dialogs)
            self.subscriptions.set_account(profile)
            self.subscription_scheduler.set_account(
                profile.account_id if profile is not None else None
            )
            subscription_page = self._subscription_page()
            subscription_page.set_logged_in(False)
            subscription_page.set_dialogs(dialogs)
            self._reload_subscriptions()
            self._reload_content_history()
        except Exception as error:
            page.show_error(self._safe_error(error))

    async def activate_content_account(self) -> None:
        if self.content_browser is None:
            return
        try:
            profile, dialogs = await self.content_browser.activate_account()
            self.window.set_account(profile.display_name)
            page = self._content_page()
            page.set_logged_in(True)
            page.set_dialogs(dialogs)
            self.subscriptions.set_account(profile)
            subscription_page = self._subscription_page()
            subscription_page.set_logged_in(True)
            subscription_page.set_dialogs(dialogs)
            self._reload_subscriptions()
            self.subscription_scheduler.set_account(profile.account_id)
            self.subscription_scheduler.start()
            self.subscription_scheduler.wake()
            self._reload_content_history()
            self._schedule_content_dialog_sync_if_stale()
        except SessionExpiredError:
            raise
        except Exception as error:
            self._content_page().show_error(self._safe_error(error))

    async def activate_content_page(self) -> None:
        if self.content_browser is None:
            return
        if not await self.ensure_telegram_online():
            return
        account = getattr(self.content_browser, "account", None)
        if account is None:
            await self.activate_content_account()
            return
        page = self._content_page()
        page.set_logged_in(True)
        page.set_dialogs(self.content_browser.list_dialogs())
        self._reload_content_history()
        self._schedule_content_dialog_sync_if_stale()

    async def select_content_dialog(self, peer_ref: str) -> None:
        if self.content_browser is None:
            return
        latest = getattr(self.content_browser, "latest_session", None)
        session = latest(peer_ref) if latest is not None else None
        page = self._content_page()
        page.set_active_search(session)
        page.set_results(
            self.content_browser.list_results(session.id)
            if session is not None
            else []
        )
        if not await self.ensure_telegram_online():
            return
        self._schedule_content_dialog_sync_if_stale()

    def _schedule_content_dialog_sync_if_stale(self) -> None:
        if self.content_browser is None:
            return
        task = self._dialog_sync_task
        if task is not None and not task.done():
            return
        stale = getattr(self.content_browser, "dialog_cache_stale", None)
        if stale is not None and not stale(timedelta(seconds=60)):
            self._content_page().set_sync_state("同步完成", busy=False, count=0)
            return
        self._dialog_sync_task = self._spawn_background(
            self.refresh_content_dialogs()
        )

    async def refresh_content_dialogs(self) -> None:
        if self.content_browser is None:
            return
        page = self._content_page()
        current = asyncio.current_task()
        active = self._dialog_sync_task
        if active is not None and active is not current and not active.done():
            return
        if current is not None:
            self._dialog_sync_task = current
        discovered = 0
        page.set_sync_state("正在刷新群组…", busy=True, count=0)

        def progress(value: DialogSyncProgress) -> None:
            nonlocal discovered
            discovered = value.discovered
            page.set_sync_state(
                f"正在刷新，已发现 {discovered} 个群组/频道",
                busy=True,
                count=discovered,
            )

        try:
            if not await self.ensure_telegram_online():
                page.set_sync_state("刷新失败", busy=False, count=discovered)
                return
            dialogs = await self.content_browser.sync_dialogs(on_progress=progress)
            page.set_dialogs(dialogs)
            self._subscription_page().set_dialogs(dialogs)
            discovered = len(dialogs)
            page.set_sync_state(
                f"刚刚同步，共 {discovered} 个",
                busy=False,
                count=discovered,
            )
        except asyncio.CancelledError:
            page.set_sync_state("刷新已取消", busy=False, count=discovered)
            raise
        except SessionExpiredError as error:
            page.set_sync_state("登录已失效", busy=False, count=discovered)
            await self._handle_session_expired(error)
        except Exception as error:
            page.set_sync_state("刷新失败", busy=False, count=discovered)
            self._show_status(f"群组同步失败：{self._safe_error(error)}")
        finally:
            if self._dialog_sync_task is current:
                self._dialog_sync_task = None

    async def search_content(
        self,
        peer_ref: str,
        query: ContentSearchQuery,
    ) -> None:
        if self.content_browser is None:
            self._show_error("内容浏览服务不可用")
            return
        page = self._content_page()
        current = asyncio.current_task()
        await self._cancel_replaced_content_search(current)
        self._content_search_task = current
        page.set_search_busy(True)
        page.set_search_progress(SearchProgress(0, 0, "正在连接 Telegram"))
        search_id: str | None = None
        try:
            if not await self.ensure_telegram_online():
                return
            session, results = await self.content_browser.start_search(
                peer_ref,
                query,
                on_progress=page.set_search_progress,
            )
            search_id = session.id
            page.set_active_search(session)
            page.set_results(results)
            page.set_sessions(self.content_browser.list_sessions())
        except asyncio.CancelledError:
            raise
        except SessionExpiredError as error:
            await self._handle_session_expired(error)
        except Exception as error:
            page.show_error(self._safe_error(error))
        finally:
            if search_id is not None:
                self._reload_content_search(search_id)
            page.set_search_busy(False)
            page.set_search_progress(None)
            if self._content_search_task is current:
                self._content_search_task = None

    async def load_more_content(self, search_id: str) -> None:
        if self.content_browser is None:
            return
        page = self._content_page()
        current = asyncio.current_task()
        await self._cancel_replaced_content_search(current)
        self._content_search_task = current
        page.set_search_busy(True)
        page.set_search_progress(SearchProgress(0, 0, "正在连接 Telegram"))
        try:
            if not await self.ensure_telegram_online():
                return
            session, results = await self.content_browser.load_more(
                search_id,
                on_progress=page.set_search_progress,
            )
            page.set_active_search(session)
            page.set_results(results)
            page.set_sessions(self.content_browser.list_sessions())
        except asyncio.CancelledError:
            raise
        except SessionExpiredError as error:
            await self._handle_session_expired(error)
        except Exception as error:
            page.show_error(self._safe_error(error))
        finally:
            self._reload_content_search(search_id)
            page.set_search_busy(False)
            page.set_search_progress(None)
            if self._content_search_task is current:
                self._content_search_task = None

    def cancel_content_search(self) -> None:
        task = self._content_search_task
        if task is not None and not task.done():
            task.cancel()

    async def _cancel_replaced_content_search(
        self,
        current: asyncio.Task[Any] | None,
    ) -> None:
        task = self._content_search_task
        if task is None or task is current or task.done():
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    def set_content_selected(
        self,
        search_id: str,
        result_id: str,
        selected: bool,
    ) -> None:
        if self.content_browser is None:
            return
        try:
            results = self.content_browser.set_selected(
                search_id,
                result_id,
                selected,
            )
        except Exception as error:
            self._content_page().show_error(self._safe_error(error))
            results = self.content_browser.list_results(search_id)
        self._content_page().set_results(results)

    async def queue_content_selection(self, search_id: str) -> None:
        page = self._content_page()
        page.show_error("")
        page.set_queue_busy(True)
        try:
            if self.content_browser is None or self.planner is None:
                self._show_error("请先连接 Telegram 账号")
                return
            preparation = self.content_browser.prepare_download(search_id)
            if not await self._confirm_download_preview(preparation.preview):
                self._show_status("已取消创建任务")
                return
            committed = self.planner.commit_selected(preparation.preview)
            joined_count = len(committed.accepted_keys)
            report = self.content_browser.finalize_queue(
                search_id,
                joined_count,
            )
            self._reload_content_search(search_id)
            self.refresh_tasks()
            self._start_task(committed.task.id)
            self._show_status(
                f"选择 {report.selected_count} 项，加入 {report.joined_count} 项，"
                f"跳过重复 {report.duplicate_count} 项，"
                f"不可用 {report.unavailable_count} 项"
            )
        except NothingToQueueError as error:
            self._show_status(
                f"选择 {error.selected_count} 项，加入 0 项，"
                f"跳过重复 {error.duplicate_count} 项，"
                f"不可用 {error.unavailable_count} 项"
            )
        except Exception as error:
            page.show_error(self._safe_error(error))
        finally:
            page.set_queue_busy(False)

    async def _confirm_download_preview(self, preview: Any) -> bool:
        confirmation = self.confirm_preview(preview)
        if inspect.isawaitable(confirmation):
            confirmation = await confirmation
        return bool(confirmation)

    def request_thumbnail(self, result_id: str) -> None:
        if self.content_browser is None:
            return
        existing = self._thumbnail_tasks.get(result_id)
        if existing is not None and not existing.done():
            return

        task: asyncio.Task[Any] | None = None

        async def load() -> None:
            try:
                path = await self.content_browser.load_thumbnail(result_id)
                if path is not None:
                    self._content_page().set_thumbnail(result_id, path)
            except asyncio.CancelledError:
                raise
            finally:
                if task is not None:
                    self._forget_thumbnail_task(result_id, task)

        task = self._spawn_background(load())
        self._thumbnail_tasks[result_id] = task

    def _forget_thumbnail_task(self, result_id: str, task: object) -> None:
        if self._thumbnail_tasks.get(result_id) is task:
            self._thumbnail_tasks.pop(result_id, None)

    async def open_content_preview(self, result_id: str) -> None:
        if self.content_browser is None:
            return
        page = self._content_page()
        try:
            result = self.content_browser.get_result(result_id)
            page.show_preview(result, None)
            path = await self.content_browser.load_thumbnail(result_id)
            if path is not None:
                page.set_thumbnail(result_id, path)
                page.update_preview(result_id, path)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            page.show_error(self._safe_error(error))

    def delete_content_history(self, search_id: str) -> None:
        if self.content_browser is None:
            return
        try:
            warning = self.content_browser.delete_history(search_id)
            self._reload_content_history()
            if warning:
                self._show_status(warning)
        except Exception as error:
            self._content_page().show_error(self._safe_error(error))

    def clear_content_history(self) -> None:
        if self.content_browser is None:
            return
        try:
            warning = self.content_browser.clear_history()
            self._reload_content_history()
            if warning:
                self._show_status(warning)
        except Exception as error:
            self._content_page().show_error(self._safe_error(error))

    def clear_thumbnail_cache(self) -> None:
        if self.content_browser is None:
            return
        count, removed_bytes = self.content_browser.thumbnails.clear()
        dialog = self._settings_dialog
        if dialog is not None:
            dialog.set_thumbnail_cache_bytes(
                self.content_browser.thumbnails.total_bytes()
            )
        self._show_status(
            f"已清理 {count} 个缩略图，共 "
            f"{self._format_bytes(removed_bytes)}"
        )

    async def activate_subscriptions_page(self) -> None:
        page = self._subscription_page()
        account = getattr(self.subscriptions, "account", None)
        if account is None:
            cached = getattr(self.content_browser, "account", None)
            if cached is not None:
                self.subscriptions.set_account(cached)
                account = cached
        if self.content_browser is not None:
            page.set_dialogs(self.content_browser.list_dialogs())
        self._reload_subscriptions()
        online = await self.ensure_telegram_online()
        page.set_logged_in(online)
        if online and account is None:
            await self.activate_content_account()

    async def create_subscription(self, draft: SubscriptionDraft) -> None:
        page = self._subscription_page()
        page.set_rule_busy(None, True, "正在建立订阅基线…")
        try:
            saved = await self.subscriptions.create_rule(draft)
            self._reload_subscriptions()
            self.subscription_scheduler.wake()
            title = getattr(saved, "dialog_title", "")
            keyword = getattr(saved, "keyword", "")
            self._show_status(f"已创建自动订阅：{title} {keyword}".strip())
        except Exception as error:
            page.show_error(self._safe_error(error))
        finally:
            page.set_rule_busy(None, False)

    async def update_subscription(
        self,
        rule_id: str,
        draft: SubscriptionDraft,
    ) -> None:
        page = self._subscription_page()
        page.set_rule_busy(rule_id, True, "正在更新订阅…")
        try:
            await self.subscriptions.update_rule(rule_id, draft)
            self._reload_subscriptions()
            self.subscription_scheduler.wake()
            self._show_status("自动订阅已更新")
        except Exception as error:
            page.show_error(self._safe_error(error))
        finally:
            page.set_rule_busy(None, False)

    def set_subscription_enabled(self, rule_id: str, enabled: bool) -> None:
        page = self._subscription_page()
        try:
            self.subscriptions.set_enabled(rule_id, enabled)
            self._reload_subscriptions()
            if enabled:
                self.subscription_scheduler.wake(rule_id)
            self._show_status("自动订阅已继续" if enabled else "自动订阅已暂停")
        except Exception as error:
            page.show_error(self._safe_error(error))
        finally:
            page.set_rule_busy(None, False)

    def run_subscription_now(self, rule_id: str) -> None:
        page = self._subscription_page()
        try:
            self.subscriptions.get_rule(rule_id)
            self.subscription_scheduler.wake(rule_id)
            self._show_status("已安排立即检查")
        except Exception as error:
            page.show_error(self._safe_error(error))
        finally:
            page.set_rule_busy(None, False)

    def delete_subscription(self, rule_id: str) -> None:
        page = self._subscription_page()
        try:
            self.subscriptions.delete_rule(rule_id)
            self._reload_subscriptions()
            self._show_status("自动订阅已删除；已有任务和文件已保留")
        except Exception as error:
            page.show_error(self._safe_error(error))
        finally:
            page.set_rule_busy(None, False)

    def subscription_task_created(self, task_id: str) -> None:
        self.refresh_tasks()
        self._start_task(task_id)

    def foreground_telegram_busy(self) -> bool:
        if self._shutting_down or self.connection_recovery.active:
            return True
        return any(
            task is not None and not task.done()
            for task in (
                self._dialog_sync_task,
                self._content_search_task,
                self._qr_wait_task,
                self._session_restore_task,
            )
        )

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
        await self._cancel_connection_monitor()
        await self._cancel_qr_wait()
        await self._cancel_content_operations()
        await self.subscription_scheduler.shutdown()
        await self.connection_recovery.cancel()
        await self.scheduler.shutdown()
        pending = tuple(task for task in self._background if not task.done())
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if self.gateway is not None:
            await self.gateway.disconnect()
        if self.content_browser is not None:
            go_offline = getattr(self.content_browser, "go_offline", None)
            if go_offline is not None:
                go_offline()
        self.subscriptions.go_offline()
        self.subscription_scheduler.set_account(None)

    async def _cancel_connection_monitor(self) -> None:
        task = self._connection_monitor_task
        self._connection_monitor_task = None
        if task is None or task.done():
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    def refresh_tasks(self, *, now: float | None = None) -> None:
        sampled_at = monotonic_clock() if now is None else now
        summaries: list[TaskSummary] = []
        active_ids: set[str] = set()
        for task in self.repository.list_tasks():
            items = self.repository.list_items(task.id)
            completed = sum(item.status is ItemStatus.COMPLETED for item in items)
            downloaded = sum(item.downloaded_bytes for item in items)
            known_size = sum(item.expected_size or 0 for item in items)
            unknown = any(item.expected_size is None for item in items)
            total_bytes = None if unknown else known_size
            speed = self._sample_speed(task.id, task.status, downloaded, sampled_at)
            remaining_seconds = None
            if total_bytes is not None and speed > 0:
                remaining_seconds = max(
                    0,
                    round((total_bytes - downloaded) / speed),
                )
            error_text = task.last_error or next(
                (item.last_error for item in items if item.last_error),
                "—",
            )
            summaries.append(
                TaskSummary(
                    task.id,
                    getattr(task, "display_title", None) or task.source_title,
                    task.status,
                    f"{completed} / {len(items)}",
                    self._format_bytes(known_size) + (" + 未知" if unknown else ""),
                    self._format_rate(speed),
                    self._format_duration(remaining_seconds),
                    error_text,
                    completed,
                    len(items),
                    downloaded,
                    total_bytes,
                    speed,
                    remaining_seconds,
                )
            )
            if task.status is TaskStatus.DOWNLOADING:
                active_ids.add(task.id)
        for task_id in set(self._progress_samples) - active_ids:
            self._progress_samples.pop(task_id, None)
        self.window.set_task_summaries(summaries)

    def _sample_speed(
        self,
        task_id: str,
        status: TaskStatus,
        downloaded: int,
        now: float,
    ) -> float:
        if status is not TaskStatus.DOWNLOADING:
            self._progress_samples.pop(task_id, None)
            return 0.0
        previous = self._progress_samples.get(task_id)
        self._progress_samples[task_id] = (now, downloaded)
        if previous is None:
            return 0.0
        elapsed = now - previous[0]
        delta = downloaded - previous[1]
        return delta / elapsed if elapsed > 0 and delta > 0 else 0.0

    def _refresh_tasks_if_due(self, now: float | None = None) -> None:
        sampled_at = monotonic_clock() if now is None else now
        if sampled_at < self._next_progress_refresh:
            return
        self._next_progress_refresh = sampled_at + self._progress_refresh_interval
        self.refresh_tasks(now=sampled_at)

    def show_login(self) -> None:
        self._prefill_login()
        self.login_dialog.show()
        self.login_dialog.raise_()
        self.login_dialog.activateWindow()
        if (
            self.gateway is not None
            and self.settings.api_id > 0
            and self.secrets.get("api_hash", "")
        ):
            self._spawn_background(self.begin_qr_login())
            return
        from telegram_downloader.ui.login import LoginPage

        self.login_dialog.show_page(LoginPage.CREDENTIALS)

    def _prefill_login(self) -> None:
        set_saved = getattr(self.login_dialog, "set_saved_credentials", None)
        if set_saved is None:
            return
        set_saved(
            self.settings.api_id,
            self.secrets.get("api_hash", ""),
            self.settings.proxy,
            self.secrets.get("proxy_password", ""),
        )

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
        self._ensure_connection_monitor()
        await self.activate_content_account()

    async def _account_name(self) -> str | None:
        method = getattr(self.gateway, "account_name", None)
        if method is None:
            return "已登录"
        return await method()

    async def _handle_session_expired(self, error: SessionExpiredError) -> None:
        await self.connection_recovery.cancel()
        await self._cancel_content_operations()
        page = self._content_page()
        if self.content_browser is not None:
            go_offline = getattr(self.content_browser, "go_offline", None)
            if go_offline is not None:
                go_offline()
        self.subscriptions.go_offline()
        self.subscription_scheduler.set_account(None)

        self.secrets.pop("session", None)
        self.vault.save(self.secrets)
        self.window.set_account(None)
        page.set_logged_in(False)
        page.show_error(str(error))

        previous_scheduler = self.scheduler
        previous_gateway = self.gateway
        self.gateway = None
        self.planner = None
        self.scheduler = _NullScheduler()
        with suppress(Exception):
            await previous_scheduler.shutdown()
        if previous_gateway is not None:
            with suppress(Exception):
                await previous_gateway.disconnect()

        api_hash = self.secrets.get("api_hash", "")
        if self.gateway_factory is not None and self.settings.api_id > 0 and api_hash:
            fresh_gateway = self.gateway_factory(
                self.settings.api_id,
                api_hash,
                "",
                self.settings.proxy,
                self.secrets.get("proxy_password", ""),
            )
            self.gateway = fresh_gateway
            try:
                await fresh_gateway.connect()
            except Exception as reconnect_error:
                _LOGGER.warning(
                    "fresh Telegram connection failed (%s)",
                    type(reconnect_error).__name__,
                )
            if self.service_builder is not None:
                services = self.service_builder(
                    fresh_gateway,
                    self.settings.concurrency,
                )
                if len(services) == 3:
                    self.planner, self.scheduler, self.content_browser = services
                else:
                    self.planner, self.scheduler = services

        self.show_login()

    def _content_page(self):
        return getattr(self.window, "content_page", _NullContentPage())

    def _subscription_page(self):
        return getattr(
            self.window,
            "subscriptions_page",
            _NullSubscriptionPage(),
        )

    def _reload_subscriptions(self) -> None:
        page = self._subscription_page()
        try:
            page.set_rules(self.subscriptions.list_rules())
        except Exception as error:
            page.show_error(self._safe_error(error))

    def _reload_content_history(self) -> None:
        if self.content_browser is None:
            return
        page = self._content_page()
        sessions = self.content_browser.list_sessions()
        page.set_sessions(sessions)
        active_id = getattr(page, "active_search_id", None)
        active = next(
            (item for item in sessions if item.id == active_id),
            sessions[0] if sessions else None,
        )
        page.set_active_search(active)
        if active is not None:
            page.set_results(self.content_browser.list_results(active.id))
        elif hasattr(page, "set_results"):
            page.set_results([])

    def _reload_content_search(self, search_id: str) -> None:
        if self.content_browser is None:
            return
        try:
            sessions = self.content_browser.list_sessions()
            session = next(item for item in sessions if item.id == search_id)
            page = self._content_page()
            page.set_sessions(sessions)
            page.set_active_search(session)
            page.set_results(self.content_browser.list_results(search_id))
        except (KeyError, StopIteration):
            self._reload_content_history()

    async def _cancel_content_operations(self) -> None:
        current = asyncio.current_task()
        tracked = [
            self._dialog_sync_task,
            self._content_search_task,
            *self._thumbnail_tasks.values(),
        ]
        pending = [
            task
            for task in tracked
            if task is not None and task is not current and not task.done()
        ]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._dialog_sync_task = None
        self._content_search_task = None
        self._thumbnail_tasks.clear()

    def _start_task(self, task_id: str) -> None:
        self._spawn_background(self._run_and_refresh(task_id))

    def _spawn_background(self, operation) -> asyncio.Task[Any]:
        task = asyncio.create_task(operation)
        self._background.add(task)
        task.add_done_callback(self._background_finished)
        return task

    def _background_finished(self, task: asyncio.Task[Any]) -> None:
        self._background.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is None:
            return
        _LOGGER.error("background task failed (%s)", type(error).__name__)
        self._show_error(self._safe_error(error))

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
        operation = asyncio.create_task(self.scheduler.run_task(task_id))
        try:
            while not operation.done():
                self._refresh_tasks_if_due()
                try:
                    await asyncio.wait_for(
                        asyncio.shield(operation),
                        timeout=self._progress_refresh_interval,
                    )
                except TimeoutError:
                    continue
            await operation
        finally:
            self.refresh_tasks()

    def _show_status(self, message: str) -> None:
        self.window.statusBar().showMessage(message, 8000)

    def _show_error(self, message: str) -> None:
        self.window.statusBar().showMessage(message, 0)

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

    @classmethod
    def _format_rate(cls, value: float) -> str:
        return "—" if value <= 0 else f"{cls._format_bytes(round(value))}/s"

    @staticmethod
    def _format_duration(value: int | None) -> str:
        if value is None:
            return "—"
        if value < 60:
            return f"{value} 秒"
        minutes, seconds = divmod(value, 60)
        return f"{minutes} 分 {seconds} 秒"

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
