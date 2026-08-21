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
from threading import Event
from time import monotonic as monotonic_clock
from typing import Any

from telegram_downloader.connectivity import ConnectionRecovery
from telegram_downloader.content import ContentSearchQuery, SearchResult, SearchScope
from telegram_downloader.content_browser import NothingToQueueError
from telegram_downloader.content_progress import (
    DialogSyncProgress,
    SearchProgress,
    SearchResultBatch,
)
from telegram_downloader.domain import (
    IntegrityStatus,
    ItemStatus,
    MediaKind,
    ScanFilters,
    SourceKind,
    TaskStatus,
)
from telegram_downloader.file_integrity import (
    IntegrityProgress,
    IntegritySummary,
    RepairPreparation,
)
from telegram_downloader.gateway import (
    AuthorizationFailureReason,
    AuthState,
    GatewayError,
    SessionExpiredError,
    TelegramGateway,
    TransientNetworkError,
)
from telegram_downloader.links import InvalidTelegramLink, parse_telegram_link
from telegram_downloader.paths import PortablePaths
from telegram_downloader.scheduler import SchedulerSnapshot
from telegram_downloader.settings import AppSettings, ProxySettings
from telegram_downloader.subscriptions import SubscriptionDraft
from telegram_downloader.ui.models import TaskItemSummary, TaskSummary

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
    def __init__(self) -> None:
        self.last_message = ""

    def showMessage(self, message: str, _timeout: int = 0) -> None:
        self.last_message = message


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

    def set_scheduler_summary(
        self,
        *,
        active: int,
        queued: int,
        concurrency: int,
        speed_limit_kib: int,
    ) -> None:
        pass

    def set_task_items(self, _task_id: str, _items: list[TaskItemSummary]) -> None:
        pass

    def set_scan_busy(self, _busy: bool) -> None:
        pass

    def set_integrity_busy(self, _busy: bool) -> None:
        pass

    def set_integrity_progress(self, _progress: IntegrityProgress | None) -> None:
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

    def apply_search_batch(self, _batch: SearchResultBatch) -> None:
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

    def set_rules(
        self,
        _value: list[object],
        _latest_runs: dict[str, object] | None = None,
    ) -> None:
        pass

    def set_rule_busy(
        self,
        _rule_id: str | None,
        _busy: bool,
        _text: str = "",
    ) -> None:
        pass

    def set_selected_rule_details(
        self,
        _rule: object | None,
        _runs: list[object],
    ) -> None:
        pass

    def set_probe_busy(self, _rule_id: str | None, _busy: bool) -> None:
        pass

    def set_probe_progress(self, _progress: object | None) -> None:
        pass

    def set_probe_result(self, _report: object | None) -> None:
        pass

    def show_probe_cancelled(self) -> None:
        pass

    def show_error(self, _message: str) -> None:
        pass


class _NullDiagnosticsPage:
    report = None

    def set_report(self, report: object | None, *, historical: bool) -> None:
        self.report = report

    def set_progress(self, _progress: object | None) -> None:
        pass

    def set_running(self, _running: bool) -> None:
        pass

    def show_error(self, _message: str) -> None:
        pass


class _NullRepository:
    def list_task_snapshots(self, *, include_archived: bool = False) -> list[object]:
        return []

    def list_items(self, _task_id: str, _statuses=None) -> list[object]:
        return []

    def get_task(self, task_id: str):
        raise KeyError(task_id)

    def get_item(self, item_id: str):
        raise KeyError(item_id)

    def archive_tasks(self, _task_ids: list[str]) -> set[str]:
        return set()

    def restore_tasks(self, _task_ids: list[str]) -> set[str]:
        return set()


class _NullScheduler:
    async def run_task(self, _task_id: str) -> None:
        pass

    async def resume_task(self, _task_id: str) -> None:
        pass

    async def run_items(self, _task_id: str, _item_ids: list[str]) -> None:
        pass

    def pause_task(self, _task_id: str) -> None:
        pass

    def snapshot(self) -> SchedulerSnapshot:
        return SchedulerSnapshot(None, (), 3, 0)

    def queue_positions(self) -> dict[str, int]:
        return {}

    def is_active(self, _task_id: str) -> bool:
        return False

    def prioritize_task(self, _task_id: str) -> bool:
        return False

    def configure_resources(self, _concurrency: int, _speed_limit_kib: int) -> None:
        pass

    async def shutdown(self) -> None:
        pass


class _NullIntegrityService:
    async def verify(
        self,
        _item_ids: list[str],
        *,
        progress=None,
        cancelled=None,
    ) -> IntegritySummary:
        return IntegritySummary()

    def prepare_repairs(self, _item_ids: list[str]) -> RepairPreparation:
        return RepairPreparation()


class _NullSubscriptionService:
    account = None

    def set_account(self, account: object | None) -> None:
        self.account = account

    def list_rules(self) -> list[object]:
        return []

    def latest_runs(self) -> dict[str, object]:
        return {}

    def get_rule(self, rule_id: str) -> object:
        raise KeyError(rule_id)

    def list_runs(self, _rule_id: str, *, limit: int = 20) -> list[object]:
        return []

    def resume_after_connection(self) -> int:
        return 0

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
        integrity_service: Any | None = None,
        diagnostics: Any | None = None,
        diagnostic_store: Any | None = None,
        paths: PortablePaths | None = None,
        gateway_factory: Callable[..., TelegramGateway] | None = None,
        service_builder: Callable[
            [TelegramGateway, AppSettings],
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
        self.subscription_scheduler = subscription_scheduler or _NullSubscriptionScheduler()
        self.integrity_service = integrity_service or _NullIntegrityService()
        self.diagnostics = diagnostics
        self.diagnostic_store = diagnostic_store
        self._diagnostic_report: Any | None = None
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
        self._subscription_probe_task: asyncio.Task[Any] | None = None
        self._subscription_actions_active = 0
        self._thumbnail_tasks: dict[str, asyncio.Task[Any]] = {}
        self._integrity_task: asyncio.Task[Any] | None = None
        self._integrity_cancel_event: Event | None = None
        self._integrity_repair_task_ids: set[str] = set()
        self._detail_task_id: str | None = None
        self._progress_refresh_interval = progress_refresh_interval
        self._next_progress_refresh = 0.0
        self._progress_samples: dict[str, tuple[float, int]] = {}
        self._session_expiry_lock = asyncio.Lock()
        self._session_expiry_handled = False
        self._last_authorization_failure_reason: (
            AuthorizationFailureReason | None
        ) = None

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
            integrity_service=dependencies.pop("integrity_service", None),
            diagnostics=dependencies.pop("diagnostics", None),
            diagnostic_store=dependencies.pop("diagnostic_store", None),
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

        recovered = False

        def attempt(value: tuple[int, int]) -> None:
            number, total = value
            text = "正在连接 Telegram…" if number == 1 else f"正在重连（{number}/{total}）…"
            page.set_connection_state(text, retryable=False)

        if not self._gateway_is_connected(self.gateway):
            try:
                await self.connection_recovery.ensure_connected(
                    self.gateway,
                    attempt,
                )
                recovered = True
            except SessionExpiredError as error:
                await self._handle_session_expired(error)
                return False
            except TransientNetworkError:
                self._show_connection_retryable(page)
                return False
            except Exception as error:
                safe = self._safe_error(error)
                page.set_logged_in(False)
                page.set_connection_state(f"连接失败：{safe}", retryable=True)
                self._show_status(f"Telegram 连接失败：{safe}")
                return False

        try:
            await self._verify_gateway_authorized(self.gateway)
        except SessionExpiredError as error:
            await self._handle_session_expired(error)
            return False
        except TransientNetworkError:
            self._show_connection_retryable(page)
            return False
        except Exception as error:
            safe = self._safe_error(error)
            page.set_logged_in(False)
            page.set_connection_state(f"连接失败：{safe}", retryable=True)
            self._show_status(f"Telegram 连接失败：{safe}")
            return False

        page.set_logged_in(True)
        page.set_connection_state(
            "连接已恢复" if recovered else "连接正常",
            retryable=False,
        )
        if recovered:
            self._resume_subscriptions_after_connection()
        return True

    @staticmethod
    async def _verify_gateway_authorized(gateway: object) -> None:
        method = getattr(gateway, "test_connection", None)
        if callable(method):
            await method()

    def _show_connection_retryable(self, page: object) -> None:
        page.set_logged_in(False)
        page.set_connection_state(
            "重连失败，请检查网络或代理后重试",
            retryable=True,
        )
        self._show_status("Telegram 重连失败，请检查网络或代理")

    def _resume_subscriptions_after_connection(self) -> None:
        account = getattr(self.subscriptions, "account", None)
        if account is None:
            return
        if getattr(self.subscription_scheduler, "account_id", None) != account.account_id:
            return
        try:
            self.subscriptions.resume_after_connection()
            self.subscription_scheduler.wake()
            self._reload_subscriptions()
        except Exception as error:
            self._subscription_page().show_error(self._safe_error(error))

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
        self._session_restore_task = self._spawn_background(self._restore_saved_session())

    def _ensure_connection_monitor(self) -> None:
        task = self._connection_monitor_task
        if self._shutting_down or (task is not None and not task.done()):
            return
        self._connection_monitor_task = self._spawn_background(self._monitor_connection())

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
            list_queued = getattr(self.repository, "list_queued_for_dispatch", None)
            if callable(list_queued):
                queued_tasks = list_queued()
            else:
                queued_tasks = [
                    task
                    for task in self.repository.list_tasks()
                    if task.status is TaskStatus.QUEUED
                ]
            for task in queued_tasks:
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
            await self._cancel_subscription_probe()
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
                    updated_settings,
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
        await self._cancel_subscription_probe()
        await self.connection_recovery.cancel()
        if self.gateway is not None:
            await self.gateway.disconnect()
        from telegram_downloader.ui.login import LoginPage

        self._prefill_login()
        self.login_dialog.show_page(LoginPage.CREDENTIALS)

    async def cancel_login(self) -> None:
        await self._cancel_qr_wait()
        await self._cancel_subscription_probe()
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
            committed = self.planner.commit(preview)
            self.refresh_tasks()
            self._start_task(committed.task.id)
            self._show_status(
                f"加入 {len(committed.accepted_keys)} 项，"
                f"跳过重复 {committed.skipped_count} 项；任务已开始下载"
            )
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
            await self._cancel_subscription_probe()
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
            await self._cancel_subscription_probe()
            profile, dialogs = await self.content_browser.activate_account()
            self.window.set_account(profile.display_name)
            page = self._content_page()
            page.set_logged_in(True)
            page.set_dialogs(dialogs)
            self.subscriptions.set_account(profile)
            subscription_page = self._subscription_page()
            subscription_page.set_logged_in(True)
            subscription_page.set_dialogs(dialogs)
            self.subscriptions.resume_after_connection()
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
            self.content_browser.list_results(session.id) if session is not None else []
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
        self._dialog_sync_task = self._spawn_background(self.refresh_content_dialogs())

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
        *,
        scope: SearchScope = SearchScope.SINGLE_DIALOG,
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
        search_generation: int | None = None
        succeeded = False

        def show_batch(batch: SearchResultBatch) -> None:
            nonlocal search_id, search_generation
            if (
                current is not None
                and self._content_search_task is current
                and not current.cancelled()
            ):
                search_id = batch.search_id
                search_generation = batch.generation
                page.apply_search_batch(batch)

        try:
            if not await self.ensure_telegram_online():
                return
            session, results = await self.content_browser.start_search(
                peer_ref,
                query,
                scope=scope,
                on_progress=page.set_search_progress,
                on_results=show_batch,
            )
            search_id = session.id
            page.set_active_search(session)
            generation = getattr(session, "generation", search_generation)
            if isinstance(generation, int) and generation > 0:
                show_batch(
                    SearchResultBatch(
                        session.id,
                        generation,
                        tuple(results),
                        stable=True,
                    )
                )
            else:
                page.set_results(results)
            page.set_sessions(self.content_browser.list_sessions())
            succeeded = True
        except asyncio.CancelledError:
            raise
        except SessionExpiredError as error:
            await self._handle_session_expired(error)
        except Exception as error:
            page.show_error(self._safe_error(error))
        finally:
            if not succeeded and search_id is not None:
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
        search_generation: int | None = None
        succeeded = False

        def show_batch(batch: SearchResultBatch) -> None:
            nonlocal search_generation
            if (
                current is not None
                and self._content_search_task is current
                and not current.cancelled()
            ):
                search_generation = batch.generation
                page.apply_search_batch(batch)

        try:
            if not await self.ensure_telegram_online():
                return
            session, results = await self.content_browser.load_more(
                search_id,
                on_progress=page.set_search_progress,
                on_results=show_batch,
            )
            page.set_active_search(session)
            generation = getattr(session, "generation", search_generation)
            if isinstance(generation, int) and generation > 0:
                show_batch(
                    SearchResultBatch(
                        session.id,
                        generation,
                        tuple(results),
                        stable=True,
                    )
                )
            else:
                page.set_results(results)
            page.set_sessions(self.content_browser.list_sessions())
            succeeded = True
        except asyncio.CancelledError:
            raise
        except SessionExpiredError as error:
            await self._handle_session_expired(error)
        except Exception as error:
            page.show_error(self._safe_error(error))
        finally:
            if not succeeded:
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
            dialog.set_thumbnail_cache_bytes(self.content_browser.thumbnails.total_bytes())
        self._show_status(f"已清理 {count} 个缩略图，共 {self._format_bytes(removed_bytes)}")

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

    def show_subscription_details(self, rule_id: str) -> None:
        page = self._subscription_page()
        try:
            rule = self.subscriptions.get_rule(rule_id)
            runs = self.subscriptions.list_runs(rule_id, limit=20)
            page.set_selected_rule_details(rule, runs)
        except Exception as error:
            page.set_selected_rule_details(None, [])
            page.show_error(self._safe_error(error))

    async def probe_subscription(self, rule_id: str) -> None:
        current = asyncio.current_task()
        if current is None:
            return
        existing = self._subscription_probe_task
        if existing is not None and existing is not current and not existing.done():
            return

        page = self._subscription_page()
        self._subscription_probe_task = current
        page.set_probe_busy(rule_id, True)
        page.set_probe_progress(None)
        try:
            report = await self.subscriptions.probe_rule(
                rule_id,
                on_progress=page.set_probe_progress,
            )
            page.set_probe_result(report)
            self._show_status("订阅规则只读测试完成")
        except asyncio.CancelledError:
            page.show_probe_cancelled()
        except SessionExpiredError as error:
            await self._handle_session_expired(error)
        except Exception as error:
            page.show_error(self._safe_error(error))
        finally:
            if self._subscription_probe_task is current:
                self._subscription_probe_task = None
            page.set_probe_progress(None)
            page.set_probe_busy(None, False)
            if not self._shutting_down:
                self.show_subscription_details(rule_id)

    def cancel_subscription_probe(self) -> None:
        task = self._subscription_probe_task
        if task is not None and not task.done():
            task.cancel()

    async def _cancel_subscription_probe(self) -> None:
        task = self._subscription_probe_task
        if task is None or task.done() or task is asyncio.current_task():
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def create_subscription(self, draft: SubscriptionDraft) -> None:
        page = self._subscription_page()
        page.set_rule_busy(None, True, "正在建立订阅基线…")
        self._subscription_actions_active += 1
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
            self._subscription_actions_active -= 1
            page.set_rule_busy(None, False)

    async def update_subscription(
        self,
        rule_id: str,
        draft: SubscriptionDraft,
    ) -> None:
        page = self._subscription_page()
        page.set_rule_busy(rule_id, True, "正在更新订阅…")
        self._subscription_actions_active += 1
        try:
            await self.subscriptions.update_rule(rule_id, draft)
            self._reload_subscriptions()
            self.subscription_scheduler.wake()
            self._show_status("自动订阅已更新")
        except Exception as error:
            page.show_error(self._safe_error(error))
        finally:
            self._subscription_actions_active -= 1
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
        if (
            self._shutting_down
            or self.connection_recovery.active
            or self._subscription_actions_active > 0
        ):
            return True
        return any(
            task is not None and not task.done()
            for task in (
                self._dialog_sync_task,
                self._content_search_task,
                self._subscription_probe_task,
                self._qr_wait_task,
                self._session_restore_task,
            )
        )

    def activate_diagnostics(self) -> None:
        page = self._diagnostics_page()
        if self.diagnostic_store is None:
            page.set_report(None, historical=True)
            return
        try:
            report = self.diagnostic_store.load_latest()
        except Exception:
            page.show_error("无法读取上次诊断报告")
            return
        self._diagnostic_report = report
        page.set_report(report, historical=True)

    async def run_diagnostics(self) -> None:
        page = self._diagnostics_page()
        if self.diagnostics is None or self.diagnostic_store is None:
            page.show_error("健康诊断服务不可用")
            return
        page.set_running(True)
        page.set_progress(None)
        try:
            report = await self.diagnostics.run(page.set_progress)
            register = getattr(self.diagnostic_store, "register_secrets", None)
            if callable(register):
                register(self.secrets.values())
            self.diagnostic_store.save(report)
            self._diagnostic_report = report
            page.set_report(report, historical=False)
            self._show_status("健康诊断已完成")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            page.show_error(f"健康诊断失败（{type(error).__name__}）")
        finally:
            page.set_running(False)

    async def cancel_diagnostics(self) -> None:
        if self.diagnostics is not None:
            await self.diagnostics.cancel()

    def export_diagnostics(self) -> None:
        page = self._diagnostics_page()
        if self.diagnostic_store is None:
            page.show_error("诊断导出服务不可用")
            return
        report = self._diagnostic_report or getattr(page, "report", None)
        if report is None:
            page.show_error("请先完成一次健康诊断")
            return
        try:
            register = getattr(self.diagnostic_store, "register_secrets", None)
            if callable(register):
                register(self.secrets.values())
            package = self.diagnostic_store.export(report)
        except Exception as error:
            page.show_error(f"诊断包导出失败（{type(error).__name__}）")
            return
        self._show_status(f"诊断包已导出：{package.name}")

    def open_diagnostics_directory(self) -> None:
        page = self._diagnostics_page()
        if self.paths is None:
            page.show_error("诊断目录不可用")
            return
        try:
            directory = self.paths.guard(self.paths.diagnostics)
            directory.mkdir(parents=True, exist_ok=True)
            startfile = getattr(os, "startfile", None)
            if startfile is not None:
                startfile(directory)
            self._show_status("诊断目录已打开")
        except (OSError, ValueError):
            page.show_error("Windows 无法打开诊断目录")

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
        connection_changed = (
            settings.api_id != self.settings.api_id
            or settings.proxy != self.settings.proxy
        )
        updated_secrets = dict(self.secrets)
        if proxy_password:
            updated_secrets["proxy_password"] = proxy_password
        else:
            updated_secrets.pop("proxy_password", None)
        self.settings_store.save(settings)
        self.vault.save(updated_secrets)
        self.settings = settings
        self.secrets = updated_secrets
        configure = getattr(self.scheduler, "configure_resources", None)
        if callable(configure):
            configure(settings.concurrency, settings.speed_limit_kib)
        message = "设置已保存；下载资源设置已即时应用"
        if connection_changed:
            message += "，API/代理变更将在下次连接时生效"
        self._show_status(message)

    def pause_task(self, task_id: str) -> None:
        self.pause_tasks([task_id])

    async def resume_task(self, task_id: str) -> None:
        await self.resume_tasks([task_id])

    async def retry_failed(self, task_id: str) -> None:
        await self.retry_failed_tasks([task_id])

    def pause_tasks(self, task_ids: list[str]) -> None:
        unique = self._unique_task_ids(task_ids)
        accepted = 0
        for task_id in unique:
            try:
                task = self.repository.get_task(task_id)
            except KeyError:
                continue
            if task.archived_at is not None or task.status not in {
                TaskStatus.QUEUED,
                TaskStatus.DOWNLOADING,
                TaskStatus.WAITING_RETRY,
            }:
                continue
            self.scheduler.pause_task(task_id)
            accepted += 1
        self.refresh_tasks()
        self._show_status(f"已暂停 {accepted} 个任务，跳过 {len(unique) - accepted} 个")

    def prioritize_task(self, task_id: str) -> None:
        try:
            task = self.repository.get_task(task_id)
        except KeyError:
            self._show_status("任务不存在或已被移除")
            return
        if task.archived_at is not None or task.status is not TaskStatus.QUEUED:
            self._show_status("任务已经开始下载或状态已变化")
            return

        prioritize = getattr(self.repository, "prioritize_task", None)
        persisted = bool(prioritize(task_id)) if callable(prioritize) else False
        reordered = self.scheduler.prioritize_task(task_id) if persisted else False
        if not reordered:
            clear_priority = getattr(self.repository, "clear_task_priority", None)
            if callable(clear_priority):
                clear_priority(task_id)
        self.refresh_tasks()
        if reordered:
            position = self.scheduler.queue_positions().get(task_id)
            if position is not None:
                self._show_status(f"已将任务移到等待队列第 {position} 位")
                return
            self._show_status("已将任务设为优先下载")
            return
        self._show_status("任务已经开始下载或状态已变化")

    async def resume_tasks(self, task_ids: list[str]) -> None:
        unique = self._unique_task_ids(task_ids)
        accepted: list[str] = []
        for task_id in unique:
            try:
                task = self.repository.get_task(task_id)
            except KeyError:
                continue
            if task.archived_at is None and task.status is TaskStatus.PAUSED:
                accepted.append(task_id)
        if accepted:
            await asyncio.gather(*(self.scheduler.resume_task(task_id) for task_id in accepted))
        self.refresh_tasks()
        self._show_status(f"已继续 {len(accepted)} 个任务，跳过 {len(unique) - len(accepted)} 个")

    async def retry_failed_tasks(self, task_ids: list[str]) -> None:
        unique = self._unique_task_ids(task_ids)
        accepted: list[str] = []
        for task_id in unique:
            try:
                task = self.repository.get_task(task_id)
            except KeyError:
                continue
            if task.archived_at is None and task.status is TaskStatus.PARTIAL_FAILURE:
                accepted.append(task_id)
        if accepted:
            await asyncio.gather(*(self.scheduler.resume_task(task_id) for task_id in accepted))
        self.refresh_tasks()
        self._show_status(f"已重试 {len(accepted)} 个任务，跳过 {len(unique) - len(accepted)} 个")

    def archive_tasks(self, task_ids: list[str]) -> None:
        unique = self._unique_task_ids(task_ids)
        accepted = self.repository.archive_tasks(unique)
        self.refresh_tasks()
        self._show_status(
            f"已归档 {len(accepted)} 个完成任务；下载文件已保留，"
            f"跳过 {len(unique) - len(accepted)} 个"
        )

    def restore_tasks(self, task_ids: list[str]) -> None:
        unique = self._unique_task_ids(task_ids)
        accepted = self.repository.restore_tasks(unique)
        self.refresh_tasks()
        self._show_status(
            f"已恢复 {len(accepted)} 个归档任务，跳过 {len(unique) - len(accepted)} 个"
        )

    def select_task_details(self, task_ids: list[str]) -> None:
        unique = self._unique_task_ids(task_ids)
        if len(unique) != 1:
            self._detail_task_id = None
            return
        task_id = unique[0]
        try:
            items = self.repository.list_items(task_id)
        except KeyError:
            return
        summaries = [
            TaskItemSummary(
                item.id,
                item.original_name,
                item.media_kind,
                item.status,
                item.downloaded_bytes,
                item.expected_size,
                item.retry_count,
                item.last_error or "—",
                getattr(
                    item,
                    "integrity_status",
                    IntegrityStatus.UNVERIFIED,
                ),
                getattr(item, "verified_at", None),
            )
            for item in items
        ]
        self._detail_task_id = task_id
        self.window.set_task_items(task_id, summaries)

    async def verify_media(self, item_ids: list[str]) -> None:
        unique = self._unique_task_ids(item_ids)
        if not unique:
            self._show_status("请先选择要校验的媒体")
            return
        await self._verify_integrity_items(unique)

    async def verify_tasks(self, task_ids: list[str]) -> None:
        item_ids: list[str] = []
        for task_id in self._unique_task_ids(task_ids):
            item_ids.extend(item.id for item in self.repository.list_items(task_id))
        unique = self._unique_task_ids(item_ids)
        if not unique:
            self._show_status("所选任务没有可校验的媒体")
            return
        await self._verify_integrity_items(unique)

    async def _verify_integrity_items(self, item_ids: list[str]) -> None:
        operation = self._begin_integrity_operation()
        if operation is None:
            return
        current, cancelled = operation
        try:
            summary = await self.integrity_service.verify(
                item_ids,
                progress=self.window.set_integrity_progress,
                cancelled=cancelled,
            )
            self._show_status(self._integrity_summary_text(summary))
        finally:
            self._finish_integrity_operation(current)
            self._refresh_integrity_views()

    async def repair_media(self, item_ids: list[str]) -> None:
        unique = self._unique_task_ids(item_ids)
        if not unique:
            self._show_status("请先选择要重新下载的异常媒体")
            return
        operation = self._begin_integrity_operation()
        if operation is None:
            return
        current, _cancelled = operation
        try:
            prepared = await asyncio.to_thread(
                self.integrity_service.prepare_repairs,
                unique,
            )
            grouped: dict[str, list[str]] = {}
            for item_id in prepared.accepted_ids:
                item = self.repository.get_item(item_id)
                grouped.setdefault(item.task_id, []).append(item_id)
            if grouped:
                self._integrity_repair_task_ids = set(grouped)
                await asyncio.gather(
                    *(
                        self.scheduler.run_items(task_id, selected)
                        for task_id, selected in grouped.items()
                    )
                )
            repaired = [
                self.repository.get_item(item_id)
                for item_id in prepared.accepted_ids
            ]
            succeeded = sum(item.status is ItemStatus.COMPLETED for item in repaired)
            failed = len(repaired) - succeeded
            self._show_status(
                "精准修复完成："
                f"成功 {succeeded}，失败 {failed}，跳过 {prepared.skipped}"
            )
        finally:
            self._finish_integrity_operation(current)
            self._refresh_integrity_views()

    def cancel_integrity(self) -> None:
        event = self._integrity_cancel_event
        if event is None:
            return
        event.set()
        for task_id in sorted(self._integrity_repair_task_ids):
            self.scheduler.pause_task(task_id)
        self._show_status("正在取消文件校验…")

    def _begin_integrity_operation(
        self,
    ) -> tuple[asyncio.Task[Any], Event] | None:
        current = asyncio.current_task()
        if current is None:
            raise RuntimeError("完整性操作必须在异步任务中运行")
        active = self._integrity_task
        if active is not None and active is not current and not active.done():
            self._show_status("已有文件完整性操作正在进行")
            return None
        cancelled = Event()
        self._integrity_task = current
        self._integrity_cancel_event = cancelled
        self._integrity_repair_task_ids.clear()
        self.window.set_integrity_busy(True)
        return current, cancelled

    def _finish_integrity_operation(self, current: asyncio.Task[Any]) -> None:
        if self._integrity_task is not current:
            return
        self._integrity_task = None
        self._integrity_cancel_event = None
        self._integrity_repair_task_ids.clear()
        self.window.set_integrity_progress(None)
        self.window.set_integrity_busy(False)

    def _refresh_integrity_views(self) -> None:
        self.refresh_tasks()
        if self._detail_task_id is not None:
            self.select_task_details([self._detail_task_id])

    @staticmethod
    def _integrity_summary_text(summary: IntegritySummary) -> str:
        prefix = "校验已取消" if summary.cancelled else "校验完成"
        return (
            f"{prefix}：通过 {summary.verified}，建立基线 {summary.baselined}，"
            f"缺失 {summary.missing}，大小异常 {summary.size_mismatch}，"
            f"哈希异常 {summary.hash_mismatch}，无法读取 {summary.read_error}，"
            f"跳过 {summary.skipped}，取消 {summary.cancelled}"
        )

    def open_media_file(self, item_id: str) -> None:
        if self.paths is None:
            return
        try:
            item = self.repository.get_item(item_id)
            if item.status is not ItemStatus.COMPLETED:
                self._show_status("媒体尚未下载完成，不能打开文件")
                return
            if getattr(item, "integrity_status", IntegrityStatus.UNVERIFIED) in {
                IntegrityStatus.MISSING,
                IntegrityStatus.SIZE_MISMATCH,
                IntegrityStatus.HASH_MISMATCH,
                IntegrityStatus.READ_ERROR,
            }:
                self._show_status("媒体完整性异常，请先校验或重新下载")
                return
            target = self.paths.guard(Path(item.target_path))
            if not target.is_file():
                self._show_status("本地文件不存在；当前操作不会修改任务记录")
                return
            expected_size = getattr(item, "expected_size", None)
            if expected_size is not None and target.stat().st_size != expected_size:
                self._show_status("本地文件大小异常，请先校验或重新下载")
                return
            startfile = getattr(os, "startfile", None)
            if startfile is not None:
                startfile(target)
        except ValueError:
            self._show_status("安全限制：文件路径不在应用目录内")
        except KeyError:
            self._show_status("媒体记录不存在，任务列表已刷新")
            self.refresh_tasks()
        except OSError:
            self._show_status("Windows 无法打开该文件")

    @staticmethod
    def _unique_task_ids(task_ids: list[str]) -> list[str]:
        return list(dict.fromkeys(str(value) for value in task_ids if value))

    def open_task_directory(self, task_id: str) -> None:
        if self.paths is None:
            return
        task = self.repository.get_task(task_id)
        directory = (
            self.paths.downloads
            if task.source_kind is SourceKind.ACCOUNT_SEARCH
            else self.paths.downloads / task.source_title
        )
        directory = self.paths.guard(directory)
        directory.mkdir(parents=True, exist_ok=True)
        startfile = getattr(os, "startfile", None)
        if startfile is not None:
            startfile(directory)

    async def shutdown(self) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        if self.diagnostics is not None:
            with suppress(Exception):
                await self.diagnostics.cancel()
        await self._cancel_integrity_for_shutdown()
        await self._cancel_connection_monitor()
        await self._cancel_qr_wait()
        await self._cancel_subscription_probe()
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

    async def _cancel_integrity_for_shutdown(self) -> None:
        task = self._integrity_task
        event = self._integrity_cancel_event
        if event is not None:
            event.set()
        if task is None or task.done() or task is asyncio.current_task():
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

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
        snapshot_method = getattr(self.scheduler, "snapshot", None)
        scheduler_state = (
            snapshot_method()
            if callable(snapshot_method)
            else SchedulerSnapshot(
                None,
                (),
                self.settings.concurrency,
                self.settings.speed_limit_kib,
            )
        )
        queue_positions_method = getattr(self.scheduler, "queue_positions", None)
        queue_positions = (
            queue_positions_method() if callable(queue_positions_method) else {}
        )
        snapshots = self.repository.list_task_snapshots(include_archived=True)
        for snapshot in snapshots:
            task = snapshot.task
            archived = task.archived_at is not None
            total_bytes = None if snapshot.unknown_size_count else snapshot.known_size
            speed = self._sample_speed(
                task.id,
                task.status if not archived else TaskStatus.COMPLETED,
                snapshot.downloaded_bytes,
                sampled_at,
            )
            remaining_seconds = None
            if total_bytes is not None and speed > 0:
                remaining_seconds = max(
                    0,
                    round((total_bytes - snapshot.downloaded_bytes) / speed),
                )
            error_text = task.last_error or snapshot.item_error or "—"
            summaries.append(
                TaskSummary(
                    task.id,
                    getattr(task, "display_title", None) or task.source_title,
                    task.status,
                    f"{snapshot.completed_items} / {snapshot.total_items}",
                    self._format_bytes(snapshot.known_size)
                    + (" + 未知" if snapshot.unknown_size_count else ""),
                    self._format_rate(speed),
                    self._format_duration(remaining_seconds),
                    error_text,
                    snapshot.completed_items,
                    snapshot.total_items,
                    snapshot.downloaded_bytes,
                    total_bytes,
                    speed,
                    remaining_seconds,
                    archived,
                    queue_positions.get(task.id)
                    if task.status is TaskStatus.QUEUED and not archived
                    else None,
                )
            )
            if not archived and task.status is TaskStatus.DOWNLOADING:
                active_ids.add(task.id)
        for task_id in set(self._progress_samples) - active_ids:
            self._progress_samples.pop(task_id, None)
        self.window.set_task_summaries(summaries)
        set_scheduler_summary = getattr(self.window, "set_scheduler_summary", None)
        if callable(set_scheduler_summary):
            set_scheduler_summary(
                active=1 if scheduler_state.active_task_id is not None else 0,
                queued=scheduler_state.queued_count,
                concurrency=scheduler_state.concurrency,
                speed_limit_kib=scheduler_state.speed_limit_kib,
            )

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
        self._session_expiry_handled = False
        self._last_authorization_failure_reason = None
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
        self._last_authorization_failure_reason = error.reason
        async with self._session_expiry_lock:
            if self._session_expiry_handled:
                return
            self._session_expiry_handled = True
            _LOGGER.warning(
                "Telegram authorization expired (reason=%s)",
                error.reason.value,
            )
            await self.connection_recovery.cancel()
            await self._cancel_subscription_probe()
            await self._cancel_content_operations()
            page = self._content_page()
            subscription_page = self._subscription_page()
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
            page.set_connection_state(
                "Telegram 登录已失效，请重新登录",
                retryable=False,
            )
            subscription_page.set_logged_in(False)
            page.show_error("Telegram 登录已失效，请重新扫码登录")

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
            if (
                self.gateway_factory is not None
                and self.settings.api_id > 0
                and api_hash
            ):
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
                        self.settings,
                    )
                    if len(services) == 3:
                        self.planner, self.scheduler, self.content_browser = services
                    else:
                        self.planner, self.scheduler = services

            self.show_login()

    @property
    def last_authorization_failure_reason(
        self,
    ) -> AuthorizationFailureReason | None:
        return self._last_authorization_failure_reason

    def _content_page(self):
        return getattr(self.window, "content_page", _NullContentPage())

    def _subscription_page(self):
        return getattr(
            self.window,
            "subscriptions_page",
            _NullSubscriptionPage(),
        )

    def _diagnostics_page(self):
        return getattr(
            self.window,
            "diagnostics_page",
            _NullDiagnosticsPage(),
        )

    def _reload_subscriptions(self) -> None:
        page = self._subscription_page()
        try:
            page.set_rules(
                self.subscriptions.list_rules(),
                self.subscriptions.latest_runs(),
            )
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
        list_results = getattr(self.content_browser, "list_results", None)
        if active is not None and callable(list_results):
            page.set_results(list_results(active.id))
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
            task for task in tracked if task is not None and task is not current and not task.done()
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
                is_active = getattr(self.scheduler, "is_active", None)
                if not callable(is_active) or is_active(task_id):
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
