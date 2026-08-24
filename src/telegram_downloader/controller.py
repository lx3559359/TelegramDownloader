from __future__ import annotations

import asyncio
import inspect
import logging
import os
from collections import deque
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from functools import wraps
from math import isfinite
from pathlib import Path
from threading import Event
from time import monotonic as monotonic_clock
from typing import Any

from telegram_downloader.account_access import (
    AccountStatusSnapshot,
    AuthorizationState,
    CandidateLoginSession,
    ConnectionState,
    OnlineServices,
)
from telegram_downloader.connectivity import ConnectionRecovery
from telegram_downloader.content import (
    AccountProfile,
    ContentSearchQuery,
    SearchResult,
    SearchScope,
    SearchSelectionIntent,
)
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
    TaskStatus,
)
from telegram_downloader.download_paths import DownloadPathPolicy
from telegram_downloader.file_integrity import (
    IntegrityProgress,
    IntegritySummary,
    RepairPreparation,
)
from telegram_downloader.gateway import (
    AuthorizationFailureReason,
    AuthState,
    GatewayError,
    QrLoginInfo,
    SessionExpiredError,
    TelegramGateway,
    TransientNetworkError,
)
from telegram_downloader.links import InvalidTelegramLink, parse_telegram_link
from telegram_downloader.maintenance_activity import (
    ActivityKind,
    MaintenanceBusyError,
    OperationActivityRegistry,
)
from telegram_downloader.notifications import ApplicationEvent, auth_required_event
from telegram_downloader.paths import PortablePaths
from telegram_downloader.scheduler import SchedulerSnapshot
from telegram_downloader.settings import AppSettings, ProxySettings
from telegram_downloader.storage_maintenance import StorageMaintenanceError
from telegram_downloader.subscriptions import SubscriptionDraft
from telegram_downloader.ui.models import TaskItemSummary, TaskSummary
from telegram_downloader.update import UpdateStartupResult

_LOGGER = logging.getLogger("telegram_downloader.controller")
_MIN_QR_VALIDITY_SECONDS = 5.0
_QR_VALIDITY_ERROR = "二维码有效期异常，请检查系统时间或网络后重试"
_QR_LOGIN_ERROR = "二维码登录失败，请刷新后重试"


def _tracked_activity(kind: ActivityKind):
    def decorate(method):
        @wraps(method)
        async def tracked(self, *args, **kwargs):
            try:
                token = self.activity.track(kind)
            except MaintenanceBusyError as error:
                self._show_error(str(error))
                return None
            with token:
                return await method(self, *args, **kwargs)

        return tracked

    return decorate


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


class _SettingsStoreRuntimeEffects:
    def __init__(self, settings_store: Any) -> None:
        self.settings_store = settings_store

    async def apply(self, _previous: AppSettings, current: AppSettings) -> None:
        await asyncio.to_thread(self.settings_store.save, current)


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

    def set_batch_scan_progress(self, _progress: object) -> None:
        pass

    def finish_batch_preflight(self, _success: bool, _error: str = "") -> None:
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

    def show_qr(
        self,
        _url: str,
        _valid_for_seconds: float,
        _generation: int,
    ) -> None:
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


class _NullAccountStatusDialog:
    snapshot: AccountStatusSnapshot | None = None

    def set_snapshot(self, value: AccountStatusSnapshot) -> None:
        self.snapshot = value

    def show_error(self, _message: str) -> None:
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

    def finish_editor_save(self, _success: bool, _error: str = "") -> None:
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

    def get_tasks(self, _task_ids: list[str]) -> list[object]:
        return []

    def get_item(self, item_id: str):
        raise KeyError(item_id)

    def archive_tasks(self, _task_ids: list[str]) -> set[str]:
        return set()

    def restore_tasks(self, _task_ids: list[str]) -> set[str]:
        return set()


class _NullScheduler:
    async def set_schedule_open(self, _opened: bool) -> set[str]:
        return set()

    async def run_task(self, _task_id: str) -> None:
        pass

    async def resume_task(self, _task_id: str) -> None:
        pass

    async def resume_tasks(self, _task_ids: list[str]) -> set[str]:
        return set()

    async def run_items(self, _task_id: str, _item_ids: list[str]) -> None:
        pass

    def pause_task(self, _task_id: str) -> None:
        pass

    def pause_tasks(self, _task_ids: list[str]) -> set[str]:
        return set()

    def snapshot(self) -> SchedulerSnapshot:
        return SchedulerSnapshot((), (), 3, 0)

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

    def snapshot(self) -> tuple[tuple[object, ...], tuple[tuple[str, object], ...]]:
        return (), ()

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
        account_status_dialog: Any | None = None,
        content_browser: Any | None = None,
        subscriptions: Any | None = None,
        subscription_scheduler: Any | None = None,
        integrity_service: Any | None = None,
        diagnostics: Any | None = None,
        diagnostic_store: Any | None = None,
        paths: PortablePaths | None = None,
        download_paths: DownloadPathPolicy | None = None,
        gateway_factory: Callable[..., TelegramGateway] | None = None,
        service_builder: Callable[
            [TelegramGateway, AppSettings],
            Any,
        ]
        | None = None,
        build_online_services: Callable[[Any, AppSettings], OnlineServices] | None = None,
        bind_online_services: Callable[[OnlineServices], None] | None = None,
        unbind_online_services: Callable[[], None] | None = None,
        confirm_preview: Callable[[Any], bool | Awaitable[bool]] | None = None,
        confirm_reauthentication: Callable[[], bool | Awaitable[bool]] | None = None,
        confirm_account_switch: (
            Callable[[AccountProfile, AccountProfile], bool | Awaitable[bool]] | None
        ) = None,
        update_coordinator: Any | None = None,
        update_prompt: Callable[[Any], bool] | None = None,
        update_shutdown: Callable[[], None] | None = None,
        publish: Callable[[ApplicationEvent], None] | None = None,
        runtime_settings_effects: Any | None = None,
        activity: OperationActivityRegistry | None = None,
        storage_service: Any | None = None,
        storage_scheduler: Any | None = None,
        update_protection: Any | None = None,
        storage_state: Any | None = None,
        settings: AppSettings | None = None,
        secrets: dict[str, str] | None = None,
        connection_recovery: ConnectionRecovery | None = None,
        connection_monitor_interval: float = 30.0,
        connection_sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
        progress_refresh_interval: float = 0.5,
        utc_now: Callable[[], datetime] | None = None,
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
        self.account_status_dialog = (
            account_status_dialog or _NullAccountStatusDialog()
        )
        self.content_browser = content_browser
        self.subscriptions = subscriptions or _NullSubscriptionService()
        self.subscription_scheduler = subscription_scheduler or _NullSubscriptionScheduler()
        self.integrity_service = integrity_service or _NullIntegrityService()
        self.diagnostics = diagnostics
        self.diagnostic_store = diagnostic_store
        self._diagnostic_report: Any | None = None
        self.paths = paths
        self.gateway_factory = gateway_factory
        self.build_online_services = build_online_services or service_builder
        self.service_builder = self.build_online_services
        self.bind_online_services = bind_online_services or (lambda _services: None)
        self.unbind_online_services = unbind_online_services or (lambda: None)
        self.confirm_preview = confirm_preview or (lambda _preview: True)
        self.confirm_reauthentication = confirm_reauthentication or (lambda: False)
        self.confirm_account_switch = confirm_account_switch or (
            lambda _old, _candidate: False
        )
        self.update_coordinator = update_coordinator
        self.update_prompt = update_prompt or (lambda _manifest: False)
        self.update_shutdown = update_shutdown or (lambda: None)
        self.publish = publish or (lambda _event: None)
        self.runtime_settings_effects = runtime_settings_effects or (
            _SettingsStoreRuntimeEffects(settings_store)
        )
        self.activity = activity or OperationActivityRegistry()
        self.storage_service = storage_service
        self.storage_scheduler = storage_scheduler
        self.update_protection = update_protection
        self.storage_state = storage_state
        self.settings = settings or settings_store.load()
        self.download_paths = download_paths or (
            DownloadPathPolicy(paths, self.settings.download_storage)
            if paths is not None
            else None
        )
        self.secrets = dict(secrets if secrets is not None else vault.load())
        self.connection_recovery = connection_recovery or ConnectionRecovery()
        self._connection_monitor_interval = connection_monitor_interval
        self._connection_sleeper = connection_sleeper
        self._utc_now = utc_now or (lambda: datetime.now(UTC))
        self.phone = ""
        self.phone_code_hash = ""
        self._background: set[asyncio.Task[Any]] = set()
        self._connection_monitor_task: asyncio.Task[None] | None = None
        self._update_check_task: asyncio.Task[Any] | None = None
        self._session_restore_task: asyncio.Task[None] | None = None
        self._qr_wait_task: asyncio.Task[None] | None = None
        self._qr_generation = 0
        self._qr_refresh_lock = asyncio.Lock()
        self._candidate_login: CandidateLoginSession | None = None
        self._ui_slots: list[object] = []
        self._shutting_down = False
        self._settings_dialog: Any | None = None
        self._dialog_sync_task: asyncio.Task[Any] | None = None
        self._content_search_task: asyncio.Task[Any] | None = None
        self._selection_intents: deque[SearchSelectionIntent] = deque()
        self._selection_persist_task: asyncio.Task[None] | None = None
        self._selection_committed_revisions: dict[tuple[str, int], int] = {}
        self._history_generation = 0
        self._subscription_probe_task: asyncio.Task[Any] | None = None
        self._subscription_actions_active = 0
        self._thumbnail_tasks: dict[str, asyncio.Task[Any]] = {}
        self._integrity_task: asyncio.Task[Any] | None = None
        self._integrity_cancel_event: Event | None = None
        self._integrity_repair_task_ids: set[str] = set()
        self._detail_task_id: str | None = None
        self._task_refresh_task: asyncio.Task[None] | None = None
        self._task_refresh_pending = False
        self._progress_refresh_interval = progress_refresh_interval
        self._next_progress_refresh = 0.0
        self._progress_samples: dict[str, tuple[float, int]] = {}
        self._session_expiry_lock = asyncio.Lock()
        self._session_expiry_handled = False
        self._background_launch = False
        self._last_authorization_failure_reason: AuthorizationFailureReason | None = None

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
            account_status_dialog=dependencies.pop("account_status_dialog", None),
            content_browser=dependencies.pop("content_browser", None),
            subscriptions=dependencies.pop("subscriptions", None),
            subscription_scheduler=dependencies.pop("subscription_scheduler", None),
            integrity_service=dependencies.pop("integrity_service", None),
            diagnostics=dependencies.pop("diagnostics", None),
            diagnostic_store=dependencies.pop("diagnostic_store", None),
            paths=dependencies.pop("paths", None),
            download_paths=dependencies.pop("download_paths", None),
            gateway_factory=dependencies.pop("gateway_factory", None),
            service_builder=dependencies.pop("service_builder", None),
            build_online_services=dependencies.pop("build_online_services", None),
            bind_online_services=dependencies.pop("bind_online_services", None),
            unbind_online_services=dependencies.pop("unbind_online_services", None),
            confirm_preview=dependencies.pop("confirm_preview", None),
            confirm_reauthentication=dependencies.pop(
                "confirm_reauthentication",
                None,
            ),
            confirm_account_switch=dependencies.pop("confirm_account_switch", None),
            update_coordinator=dependencies.pop("update_coordinator", None),
            update_prompt=dependencies.pop("update_prompt", None),
            update_shutdown=dependencies.pop("update_shutdown", None),
            activity=dependencies.pop("activity", None),
            storage_service=dependencies.pop("storage_service", None),
            storage_scheduler=dependencies.pop("storage_scheduler", None),
            update_protection=dependencies.pop("update_protection", None),
            storage_state=dependencies.pop("storage_state", None),
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
            await self._resume_subscriptions_after_connection()
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

    async def _resume_subscriptions_after_connection(self) -> None:
        account = getattr(self.subscriptions, "account", None)
        if account is None:
            return
        if getattr(self.subscription_scheduler, "account_id", None) != account.account_id:
            return
        try:
            await asyncio.to_thread(self.subscriptions.resume_after_connection)
            self.subscription_scheduler.wake()
            await self._reload_subscriptions()
        except Exception as error:
            self._subscription_page().show_error(self._safe_error(error))

    async def retry_telegram_connection(self) -> bool:
        return await self.ensure_telegram_online()

    @staticmethod
    def _gateway_is_connected(gateway: object) -> bool:
        method = getattr(gateway, "is_connected", None)
        return bool(method()) if callable(method) else False

    async def start(self, *, background: bool = False) -> None:
        self._background_launch = bool(background)
        self.refresh_tasks()
        await self.activate_cached_content_account()
        if self.gateway is None:
            if background:
                self._publish_event(auth_required_event())
            else:
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
                self._request_login()
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
            if self.build_online_services is not None:
                services = self.build_online_services(
                    gateway,
                    updated_settings,
                )
                if not isinstance(services, OnlineServices):
                    services = OnlineServices(gateway, services[0], services[1])
                self.bind_online_services(services)
                self.planner = services.planner
                self.scheduler = services.scheduler
            await self.begin_qr_login()
        except Exception as error:
            self.login_dialog.show_error(self._safe_error(error))

    async def start_candidate_login(self) -> None:
        if self._candidate_login is not None:
            self._show_login_dialog()
            return
        if self.scheduler.snapshot().active_count:
            self.account_status_dialog.show_error(
                "请先暂停或等待活动下载完成"
            )
            return
        confirmation = self.confirm_reauthentication()
        if inspect.isawaitable(confirmation):
            confirmation = await confirmation
        if not confirmation:
            return
        if self.gateway_factory is None:
            self.account_status_dialog.show_error("无法创建 Telegram 连接")
            return
        candidate_gateway = self.gateway_factory(
            self.settings.api_id,
            self.secrets.get("api_hash", ""),
            "",
            self.settings.proxy,
            self.secrets.get("proxy_password", ""),
        )
        try:
            await candidate_gateway.connect()
        except Exception as error:
            with suppress(Exception):
                await candidate_gateway.disconnect()
            self.account_status_dialog.show_error(self._safe_error(error))
            return
        self._candidate_login = CandidateLoginSession(candidate_gateway)
        reset = getattr(self.login_dialog, "reset_authentication", None)
        if callable(reset):
            reset()
        self._show_login_dialog()
        await self.begin_qr_login()

    def _login_gateway(self):
        candidate = self._candidate_login
        return candidate.gateway if candidate is not None else self.gateway

    async def begin_qr_login(self) -> None:
        gateway = self._login_gateway()
        if gateway is None:
            self.login_dialog.show_error("请先填写 API 凭据")
            return
        request_generation: int | None = None
        try:
            await self._cancel_qr_wait()
            request_generation = self._qr_generation
            info = await gateway.begin_qr_login()
            info = await self._displayable_qr_info(gateway, info)
            if request_generation != self._qr_generation:
                return
            await self._show_qr_and_wait(gateway, info)
        except TransientNetworkError as error:
            if (
                request_generation is not None
                and request_generation != self._qr_generation
            ):
                return
            if self._candidate_login is None:
                from telegram_downloader.ui.login import LoginPage

                self._prefill_login()
                self.login_dialog.show_page(LoginPage.CREDENTIALS)
            self.login_dialog.show_error(self._safe_qr_error(error))
        except Exception as error:
            if (
                request_generation is not None
                and request_generation != self._qr_generation
            ):
                return
            self.login_dialog.show_error(self._safe_qr_error(error))

    async def refresh_qr_login(self) -> None:
        requested_generation = self._qr_generation
        await self._refresh_qr(expected_generation=requested_generation)

    async def refresh_expired_qr(self, generation: int) -> None:
        await self._refresh_qr(expected_generation=generation)

    async def _refresh_qr(self, *, expected_generation: int) -> None:
        async with self._qr_refresh_lock:
            if expected_generation != self._qr_generation:
                _LOGGER.info(
                    "qr-refresh-deduplicated (generation=%s current_generation=%s)",
                    expected_generation,
                    self._qr_generation,
                )
                return
            gateway = self._login_gateway()
            if gateway is None:
                self.login_dialog.show_error("请先填写 API 凭据")
                return
            refresh_generation: int | None = None
            try:
                await self._cancel_qr_wait()
                refresh_generation = self._qr_generation
                _LOGGER.info(
                    "qr-refresh-started (generation=%s context=%s)",
                    self._qr_generation,
                    self._qr_login_context(),
                )
                info = await gateway.refresh_qr_login()
                info = await self._displayable_qr_info(gateway, info)
                if refresh_generation != self._qr_generation:
                    return
                await self._show_qr_and_wait(gateway, info)
            except TransientNetworkError as error:
                if (
                    refresh_generation is not None
                    and refresh_generation != self._qr_generation
                ):
                    return
                if self._candidate_login is None:
                    from telegram_downloader.ui.login import LoginPage

                    self._prefill_login()
                    self.login_dialog.show_page(LoginPage.CREDENTIALS)
                self.login_dialog.show_error(self._safe_qr_error(error))
            except Exception as error:
                if (
                    refresh_generation is not None
                    and refresh_generation != self._qr_generation
                ):
                    return
                self.login_dialog.show_error(self._safe_qr_error(error))

    async def use_phone_fallback(self) -> None:
        await self._cancel_qr_wait()
        from telegram_downloader.ui.login import LoginPage

        self.login_dialog.show_page(LoginPage.PHONE)

    async def edit_credentials(self) -> None:
        if self._candidate_login is not None:
            self.login_dialog.show_error(
                "重新登录期间不能修改 API/代理；请先取消，并在设置中保存后重试"
            )
            return
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
        if self._candidate_login is not None:
            await self._discard_candidate_login()
            reset = getattr(self.login_dialog, "reset_authentication", None)
            if callable(reset):
                reset()
            return
        await self._cancel_subscription_probe()
        if self.gateway is not None:
            await self.gateway.disconnect()

    @staticmethod
    def _qr_lifetime_is_usable(info: QrLoginInfo) -> bool:
        return (
            isfinite(info.valid_for_seconds)
            and info.valid_for_seconds >= _MIN_QR_VALIDITY_SECONDS
        )

    @staticmethod
    def _qr_ttl_metric(info: QrLoginInfo) -> int:
        if not isfinite(info.valid_for_seconds):
            return -1
        return max(0, int(info.valid_for_seconds))

    def _qr_login_context(self) -> str:
        return "candidate" if self._candidate_login is not None else "initial"

    @staticmethod
    def _safe_qr_error(error: Exception) -> str:
        safe = AppController._safe_error(error)
        if "tg://login?token=" in safe.casefold():
            return _QR_LOGIN_ERROR
        return safe

    async def _displayable_qr_info(
        self,
        gateway: TelegramGateway | Any,
        info: QrLoginInfo,
    ) -> QrLoginInfo:
        if self._qr_lifetime_is_usable(info):
            return info
        _LOGGER.info(
            "qr-rejected-short-ttl (ttl_seconds=%s context=%s)",
            self._qr_ttl_metric(info),
            self._qr_login_context(),
        )
        refreshed = await gateway.refresh_qr_login()
        if self._qr_lifetime_is_usable(refreshed):
            return refreshed
        _LOGGER.info(
            "qr-rejected-short-ttl (ttl_seconds=%s context=%s)",
            self._qr_ttl_metric(refreshed),
            self._qr_login_context(),
        )
        raise GatewayError(_QR_VALIDITY_ERROR)

    async def _show_qr_and_wait(
        self,
        gateway: TelegramGateway | Any,
        info: QrLoginInfo,
    ) -> None:
        self._qr_generation += 1
        generation = self._qr_generation
        task = asyncio.create_task(self._wait_for_qr(gateway, generation))
        self._qr_wait_task = task
        if self._candidate_login is not None:
            self._candidate_login.qr_wait_task = task
        await asyncio.sleep(0)
        if generation != self._qr_generation or task.done():
            if task.done():
                with suppress(asyncio.CancelledError):
                    await task
            return
        self._display_qr(info, generation)

    def _display_qr(self, info: QrLoginInfo, generation: int) -> None:
        self.login_dialog.show_qr(info.url, info.valid_for_seconds, generation)
        self.login_dialog.show_qr_status("等待手机扫码确认")
        _LOGGER.info(
            "qr-created (generation=%s ttl_seconds=%s context=%s)",
            generation,
            self._qr_ttl_metric(info),
            self._qr_login_context(),
        )

    async def _cancel_qr_wait(self) -> None:
        task = self._qr_wait_task
        self._qr_wait_task = None
        if self._candidate_login is not None:
            self._candidate_login.qr_wait_task = None
        self._qr_generation += 1
        if task is None or task is asyncio.current_task():
            return
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _wait_for_qr(
        self,
        gateway: TelegramGateway | Any,
        generation: int,
    ) -> None:
        try:
            while generation == self._qr_generation:
                try:
                    state = await gateway.wait_qr_login()
                except TimeoutError:
                    await self._refresh_qr(expected_generation=generation)
                    return
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
            safe = self._safe_qr_error(error)
            _LOGGER.warning(
                "qr-wait-failed (type=%s context=%s)",
                type(error).__name__,
                self._qr_login_context(),
            )
            self.login_dialog.show_error(safe)
        finally:
            if self._qr_wait_task is asyncio.current_task():
                self._qr_wait_task = None

    async def submit_phone(self, phone: str) -> None:
        gateway = self._login_gateway()
        if gateway is None:
            self.login_dialog.show_error("请先填写 API 凭据")
            return
        try:
            phone_code_hash = await gateway.request_code(phone)
            if self._candidate_login is not None:
                self._candidate_login.phone_code_hash = phone_code_hash
                self._candidate_login.phone = phone
            else:
                self.phone_code_hash = phone_code_hash
                self.phone = phone
            from telegram_downloader.ui.login import LoginPage

            self.login_dialog.show_page(LoginPage.CODE)
        except Exception as error:
            self.login_dialog.show_error(self._safe_error(error))

    async def submit_code(self, code: str) -> None:
        gateway = self._login_gateway()
        candidate = self._candidate_login
        phone = candidate.phone if candidate is not None else self.phone
        phone_code_hash = (
            candidate.phone_code_hash
            if candidate is not None
            else self.phone_code_hash
        )
        if gateway is None or not phone or not phone_code_hash:
            self.login_dialog.show_error("验证码会话已失效，请重新发送验证码")
            return
        try:
            state = await gateway.sign_in(phone, code, phone_code_hash)
            if state is AuthState.PASSWORD_REQUIRED:
                from telegram_downloader.ui.login import LoginPage

                self.login_dialog.show_page(LoginPage.PASSWORD)
                return
            await self._finish_login()
        except Exception as error:
            self.login_dialog.show_error(self._safe_error(error))

    async def submit_password(self, password: str) -> None:
        gateway = self._login_gateway()
        if gateway is None:
            self.login_dialog.show_error("Telegram 连接尚未创建")
            return
        try:
            state = await gateway.check_password(password)
            if state is AuthState.READY:
                await self._finish_login()
        except Exception as error:
            self.login_dialog.show_error(self._safe_error(error))

    async def _discard_candidate_login(self) -> None:
        candidate = self._candidate_login
        self._candidate_login = None
        if candidate is not None:
            candidate.qr_wait_task = None
            with suppress(Exception):
                await candidate.close()

    @_tracked_activity(ActivityKind.SCAN)
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
            await self.refresh_tasks_async()
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

    @_tracked_activity(ActivityKind.SCAN)
    async def scan_links(
        self,
        links: tuple[str, ...],
        filters: ScanFilters,
    ) -> None:
        self.window.set_scan_busy(True)
        preflight_finished = False
        try:
            if not await self.ensure_telegram_online():
                self.window.finish_batch_preflight(False, "请先登录 Telegram 账号")
                return
            if self.planner is None:
                self.window.finish_batch_preflight(False, "请先登录 Telegram 账号")
                return
            batch = await self.planner.scan_batch(
                links,
                filters,
                on_progress=self.window.set_batch_scan_progress,
            )
            self.window.finish_batch_preflight(True)
            preflight_finished = True
            if not await self._confirm_download_preview(batch):
                self._show_status("已取消批量创建任务")
                return
            committed = self.planner.commit(batch.preview)
            await self.refresh_tasks_async()
            self._start_task(committed.task.id)
            self._show_status(
                f"批量加入 {len(committed.accepted_keys)} 项，"
                f"确认时另跳过重复 {committed.skipped_count} 项；任务已开始下载"
            )
        except asyncio.CancelledError:
            if not preflight_finished:
                self.window.finish_batch_preflight(False, "批量预检已取消")
            raise
        except SessionExpiredError as error:
            if not preflight_finished:
                self.window.finish_batch_preflight(False, self._safe_error(error))
            await self._handle_session_expired(error)
        except (InvalidTelegramLink, ValueError, GatewayError) as error:
            safe = self._safe_error(error)
            _LOGGER.warning(
                "batch scan rejected (%s); input_count=%s",
                type(error).__name__,
                len(links),
            )
            if preflight_finished:
                self._show_error(safe)
            else:
                self.window.finish_batch_preflight(False, safe)
        except Exception as error:
            _LOGGER.error(
                "batch scan failed (%s); input_count=%s",
                type(error).__name__,
                len(links),
            )
            safe = self._safe_error(error)
            if preflight_finished:
                self._show_error(safe)
            else:
                self.window.finish_batch_preflight(False, safe)
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
            await self._reload_subscriptions()
            self._reload_content_history()
        except Exception as error:
            page.show_error(self._safe_error(error))

    async def activate_content_account(self, *, raise_errors: bool = False) -> None:
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
            await asyncio.to_thread(self.subscriptions.resume_after_connection)
            await self._reload_subscriptions()
            self.subscription_scheduler.set_account(profile.account_id)
            self.subscription_scheduler.start()
            self.subscription_scheduler.wake()
            self._reload_content_history()
            self._schedule_content_dialog_sync_if_stale()
        except SessionExpiredError:
            raise
        except Exception as error:
            self._content_page().show_error(self._safe_error(error))
            if raise_errors:
                raise

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

    @_tracked_activity(ActivityKind.SEARCH)
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

    @_tracked_activity(ActivityKind.SEARCH)
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
            page.set_sessions(await self._list_content_sessions_async())
            page.show_error(session.last_error or "")
            succeeded = True
        except asyncio.CancelledError:
            raise
        except SessionExpiredError as error:
            await self._handle_session_expired(error)
        except Exception as error:
            page.show_error(self._safe_error(error))
        finally:
            if not succeeded and search_id is not None:
                await self._reload_content_search_async(search_id)
            page.set_search_busy(False)
            page.set_search_progress(None)
            if self._content_search_task is current:
                self._content_search_task = None

    @_tracked_activity(ActivityKind.SEARCH)
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
            page.set_sessions(await self._list_content_sessions_async())
            page.show_error(session.last_error or "")
            succeeded = True
        except asyncio.CancelledError:
            raise
        except SessionExpiredError as error:
            await self._handle_session_expired(error)
        except Exception as error:
            page.show_error(self._safe_error(error))
        finally:
            if not succeeded:
                await self._reload_content_search_async(search_id)
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

    def submit_content_selection(self, intent: SearchSelectionIntent) -> None:
        self._selection_intents.append(intent)
        task = self._selection_persist_task
        if task is None or task.done():
            self._selection_persist_task = self._spawn_background(
                self._drain_content_selection()
            )

    async def _drain_content_selection(self) -> None:
        page = self._content_page()
        try:
            while self._selection_intents:
                intent = self._selection_intents.popleft()
                browser = self.content_browser
                if browser is None:
                    return
                account = getattr(browser, "account", None)
                try:
                    commit = await browser.persist_selection(intent)
                    if (
                        commit.search_id,
                        commit.generation,
                        commit.revision,
                    ) != (
                        intent.search_id,
                        intent.generation,
                        intent.revision,
                    ):
                        raise RuntimeError("选择写入返回了不匹配的操作代次")
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    _LOGGER.warning(
                        "content selection persistence failed revision=%d error=%s",
                        intent.revision,
                        type(error).__name__,
                    )
                    try:
                        snapshot = await browser.load_search_snapshot(
                            intent.search_id
                        )
                    except Exception as reload_error:
                        page.show_error(self._safe_error(reload_error))
                        continue
                    if (
                        getattr(browser, "account", None) == account
                        and getattr(page, "active_search_id", None)
                        == intent.search_id
                        and getattr(page, "batch_generation", None)
                        == intent.generation
                        and getattr(page, "selection_revision", None)
                        == intent.revision
                    ):
                        page.apply_search_batch(
                            SearchResultBatch(
                                snapshot.session.id,
                                snapshot.session.generation,
                                snapshot.results,
                                stable=True,
                            )
                        )
                        page.show_error(self._safe_error(error))
                    continue
                self._selection_committed_revisions[
                    (intent.search_id, intent.generation)
                ] = intent.revision
        finally:
            self._selection_persist_task = None

    async def open_content_history(self, search_id: str) -> None:
        if self.content_browser is None:
            return
        self._history_generation += 1
        generation = self._history_generation
        browser = self.content_browser
        account = getattr(browser, "account", None)
        page = self._content_page()
        set_history_busy = getattr(page, "set_history_busy", None)
        if callable(set_history_busy):
            set_history_busy(True)
        try:
            snapshot, sessions = await asyncio.gather(
                browser.load_search_snapshot(search_id),
                self._list_content_sessions_async(),
            )
            if (
                generation != self._history_generation
                or browser is not self.content_browser
                or getattr(browser, "account", None) != account
            ):
                return
            page.set_sessions(sessions)
            page.set_active_search(snapshot.session)
            page.apply_search_batch(
                SearchResultBatch(
                    snapshot.session.id,
                    snapshot.session.generation,
                    snapshot.results,
                    stable=True,
                )
            )
        finally:
            if generation == self._history_generation and callable(
                set_history_busy
            ):
                set_history_busy(False)

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
            await self.refresh_tasks_async()
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

    async def delete_content_history(self, search_id: str) -> None:
        if self.content_browser is None:
            return
        try:
            warning = await asyncio.to_thread(
                self.content_browser.delete_history,
                search_id,
            )
            self._reload_content_history()
            if warning:
                self._show_status(warning)
        except Exception as error:
            self._content_page().show_error(self._safe_error(error))

    async def clear_content_history(self) -> None:
        if self.content_browser is None:
            return
        try:
            warning = await asyncio.to_thread(self.content_browser.clear_history)
            self._reload_content_history()
            if warning:
                self._show_status(warning)
        except Exception as error:
            self._content_page().show_error(self._safe_error(error))

    async def clear_thumbnail_cache(self) -> None:
        if self.content_browser is None:
            return

        def clear_cache() -> tuple[int, int, int]:
            count, removed_bytes = self.content_browser.thumbnails.clear()
            remaining_bytes = self.content_browser.thumbnails.total_bytes()
            return count, removed_bytes, remaining_bytes

        count, removed_bytes, remaining_bytes = await asyncio.to_thread(clear_cache)
        dialog = self._settings_dialog
        if dialog is not None:
            dialog.set_thumbnail_cache_bytes(remaining_bytes)
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
        await self._reload_subscriptions()
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

    @_tracked_activity(ActivityKind.SUBSCRIPTION)
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
            await self._reload_subscriptions()
            self.subscription_scheduler.wake()
            title = getattr(saved, "dialog_title", "")
            keyword = getattr(saved, "keyword", "")
            self._show_status(f"已创建自动订阅：{title} {keyword}".strip())
            page.finish_editor_save(True)
        except Exception as error:
            page.finish_editor_save(False, self._safe_error(error))
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
            await self._reload_subscriptions()
            self.subscription_scheduler.wake()
            self._show_status("自动订阅已更新")
            page.finish_editor_save(True)
        except Exception as error:
            page.finish_editor_save(False, self._safe_error(error))
        finally:
            self._subscription_actions_active -= 1
            page.set_rule_busy(None, False)

    async def set_subscription_enabled(self, rule_id: str, enabled: bool) -> None:
        page = self._subscription_page()
        self._subscription_actions_active += 1
        try:
            await asyncio.to_thread(self.subscriptions.set_enabled, rule_id, enabled)
            await self._reload_subscriptions()
            if enabled:
                self.subscription_scheduler.wake(rule_id)
            self._show_status("自动订阅已继续" if enabled else "自动订阅已暂停")
        except Exception as error:
            page.show_error(self._safe_error(error))
        finally:
            self._subscription_actions_active -= 1
            page.set_rule_busy(None, False)

    async def run_subscription_now(self, rule_id: str) -> None:
        page = self._subscription_page()
        self._subscription_actions_active += 1
        try:
            await asyncio.to_thread(self.subscriptions.get_rule, rule_id)
            self.subscription_scheduler.wake(rule_id)
            self._show_status("已安排立即检查")
        except Exception as error:
            page.show_error(self._safe_error(error))
        finally:
            self._subscription_actions_active -= 1
            page.set_rule_busy(None, False)

    async def delete_subscription(self, rule_id: str) -> None:
        page = self._subscription_page()
        self._subscription_actions_active += 1
        try:
            await asyncio.to_thread(self.subscriptions.delete_rule, rule_id)
            await self._reload_subscriptions()
            self._show_status("自动订阅已删除；已有任务和文件已保留")
        except Exception as error:
            page.show_error(self._safe_error(error))
        finally:
            self._subscription_actions_active -= 1
            page.set_rule_busy(None, False)

    def subscription_task_created(self, task_id: str) -> None:
        self._start_task(task_id)
        self._schedule_task_refresh()

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

    async def activate_diagnostics(self) -> None:
        page = self._diagnostics_page()
        if self.diagnostic_store is None:
            page.set_report(None, historical=True)
            return
        try:
            report = await asyncio.to_thread(self.diagnostic_store.load_latest)
        except Exception:
            page.show_error("无法读取上次诊断报告")
            return
        self._diagnostic_report = report
        page.set_report(report, historical=True)

    @_tracked_activity(ActivityKind.DIAGNOSTICS)
    async def run_diagnostics(self) -> None:
        page = self._diagnostics_page()
        if self.diagnostics is None or self.diagnostic_store is None:
            page.show_error("健康诊断服务不可用")
            return
        page.set_running(True)
        page.set_progress(None)
        try:
            report = await self.diagnostics.run(page.set_progress)

            def persist_report() -> None:
                register = getattr(self.diagnostic_store, "register_secrets", None)
                if callable(register):
                    register(self.secrets.values())
                self.diagnostic_store.save(report)

            await asyncio.to_thread(persist_report)
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

    async def export_diagnostics(self) -> None:
        page = self._diagnostics_page()
        if self.diagnostic_store is None:
            page.show_error("诊断导出服务不可用")
            return
        report = self._diagnostic_report or getattr(page, "report", None)
        if report is None:
            page.show_error("请先完成一次健康诊断")
            return
        try:

            def export_report():
                register = getattr(self.diagnostic_store, "register_secrets", None)
                if callable(register):
                    register(self.secrets.values())
                return self.diagnostic_store.export(report)

            package = await asyncio.to_thread(export_report)
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

    async def apply_settings(self, settings: AppSettings, proxy_password: str) -> None:
        previous_settings = self.settings
        if self.download_paths is not None:
            prepared_storage = await asyncio.to_thread(
                self.download_paths.prepare,
                settings.download_storage,
            )
            settings = replace(settings, download_storage=prepared_storage)
        connection_changed = (
            settings.api_id != previous_settings.api_id or settings.proxy != previous_settings.proxy
        )
        updated_secrets = dict(self.secrets)
        if proxy_password:
            updated_secrets["proxy_password"] = proxy_password
        else:
            updated_secrets.pop("proxy_password", None)
        await self.runtime_settings_effects.apply(previous_settings, settings)
        try:
            await asyncio.to_thread(self.vault.save, updated_secrets)
        except Exception:
            with suppress(Exception):
                await self.runtime_settings_effects.apply(settings, previous_settings)
            raise
        if self.download_paths is not None:
            self.download_paths.apply(settings.download_storage)
        self.settings = settings
        self.secrets = updated_secrets
        configure = getattr(self.scheduler, "configure_resources", None)
        if callable(configure):
            configure(settings.concurrency, settings.speed_limit_kib)
        configure_downloads = getattr(self.planner, "configure_downloads", None)
        if callable(configure_downloads) and self.download_paths is not None:
            configure_downloads(
                self.download_paths.current_root,
                settings.download_naming,
            )
        else:
            configure_naming = getattr(self.planner, "configure_naming", None)
            if callable(configure_naming):
                configure_naming(settings.download_naming)
        message = "设置已保存；下载资源与路径模板已即时应用"
        if connection_changed:
            message += "，API/代理变更将在下次连接时生效"
        self._show_status(message)

    async def pause_task(self, task_id: str) -> None:
        await self.pause_tasks([task_id])

    async def resume_task(self, task_id: str) -> None:
        await self.resume_tasks([task_id])

    async def retry_failed(self, task_id: str) -> None:
        await self.retry_failed_tasks([task_id])

    async def pause_tasks(self, task_ids: list[str]) -> None:
        unique = self._unique_task_ids(task_ids)
        tasks = await asyncio.to_thread(self.repository.get_tasks, unique)
        eligible = [
            task.id
            for task in tasks
            if task.archived_at is None
            and task.status
            in {
                TaskStatus.QUEUED,
                TaskStatus.DOWNLOADING,
                TaskStatus.WAITING_RETRY,
            }
        ]
        accepted = self.scheduler.pause_tasks(eligible)
        await self.refresh_tasks_async()
        self._show_status(f"已暂停 {len(accepted)} 个任务，跳过 {len(unique) - len(accepted)} 个")

    async def prioritize_task(self, task_id: str) -> None:
        tasks = await asyncio.to_thread(self.repository.get_tasks, [task_id])
        if not tasks:
            await self.refresh_tasks_async()
            self._show_status("任务不存在或已被移除")
            return
        task = tasks[0]
        if task.archived_at is not None or task.status is not TaskStatus.QUEUED:
            await self.refresh_tasks_async()
            self._show_status("任务已经开始下载或状态已变化")
            return

        prioritize = getattr(self.repository, "prioritize_task", None)
        persisted = (
            bool(await asyncio.to_thread(prioritize, task_id)) if callable(prioritize) else False
        )
        reordered = self.scheduler.prioritize_task(task_id) if persisted else False
        if not reordered:
            clear_priority = getattr(self.repository, "clear_task_priority", None)
            if callable(clear_priority):
                await asyncio.to_thread(clear_priority, task_id)
        await self.refresh_tasks_async()
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
        tasks = await asyncio.to_thread(self.repository.get_tasks, unique)
        eligible = [
            task.id
            for task in tasks
            if task.archived_at is None and task.status is TaskStatus.PAUSED
        ]
        accepted = await self.scheduler.resume_tasks(eligible) if eligible else set()
        await self.refresh_tasks_async()
        self._show_status(f"已继续 {len(accepted)} 个任务，跳过 {len(unique) - len(accepted)} 个")

    async def retry_failed_tasks(self, task_ids: list[str]) -> None:
        unique = self._unique_task_ids(task_ids)
        tasks = await asyncio.to_thread(self.repository.get_tasks, unique)
        eligible = [
            task.id
            for task in tasks
            if task.archived_at is None and task.status is TaskStatus.PARTIAL_FAILURE
        ]
        accepted = await self.scheduler.resume_tasks(eligible) if eligible else set()
        await self.refresh_tasks_async()
        self._show_status(f"已重试 {len(accepted)} 个任务，跳过 {len(unique) - len(accepted)} 个")

    async def archive_tasks(self, task_ids: list[str]) -> None:
        unique = self._unique_task_ids(task_ids)
        accepted = await asyncio.to_thread(self.repository.archive_tasks, unique)
        await self.refresh_tasks_async()
        self._show_status(
            f"已归档 {len(accepted)} 个完成任务；下载文件已保留，"
            f"跳过 {len(unique) - len(accepted)} 个"
        )

    async def restore_tasks(self, task_ids: list[str]) -> None:
        unique = self._unique_task_ids(task_ids)
        accepted = await asyncio.to_thread(self.repository.restore_tasks, unique)
        await self.refresh_tasks_async()
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

    @_tracked_activity(ActivityKind.INTEGRITY)
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
            await self._refresh_integrity_views()

    @_tracked_activity(ActivityKind.INTEGRITY)
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
            repaired = [self.repository.get_item(item_id) for item_id in prepared.accepted_ids]
            succeeded = sum(item.status is ItemStatus.COMPLETED for item in repaired)
            failed = len(repaired) - succeeded
            self._show_status(
                f"精准修复完成：成功 {succeeded}，失败 {failed}，跳过 {prepared.skipped}"
            )
        finally:
            self._finish_integrity_operation(current)
            await self._refresh_integrity_views()

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

    async def _refresh_integrity_views(self) -> None:
        await self.refresh_tasks_async()
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
        if self.download_paths is None:
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
            target = self.download_paths.guard(Path(item.target_path))
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
            self._show_status("安全限制：文件路径不在受信下载目录内")
        except KeyError:
            self._show_status("媒体记录不存在，任务列表已刷新")
            self._schedule_task_refresh()
        except OSError:
            self._show_status("Windows 无法打开该文件")

    @staticmethod
    def _unique_task_ids(task_ids: list[str]) -> list[str]:
        return list(dict.fromkeys(str(value) for value in task_ids if value))

    def open_task_directory(self, task_id: str) -> None:
        if self.download_paths is None:
            return
        try:
            self.repository.get_task(task_id)
            items = self.repository.list_items(task_id)
            parents: list[Path] = []
            for item in items:
                parent = Path(item.target_path).resolve().parent
                parents.append(self.download_paths.guard(parent, allow_root=True))
            directory = (
                Path(os.path.commonpath([str(path) for path in parents]))
                if parents
                else self.download_paths.current_root
            )
            directory = self.download_paths.guard(directory, allow_root=True)
            directory.mkdir(parents=True, exist_ok=True)
            startfile = getattr(os, "startfile", None)
            if startfile is not None:
                startfile(directory)
        except ValueError:
            self._show_status("安全限制：下载目录不在受信下载目录内")
        except KeyError:
            self._show_status("任务不存在，任务列表已刷新")
            self._schedule_task_refresh()
        except OSError:
            self._show_status("Windows 无法打开下载目录")

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
        if self._candidate_login is not None:
            await self._discard_candidate_login()
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
        scheduler_state, queue_positions = self._task_scheduler_state()
        snapshots = self.repository.list_task_snapshots(include_archived=True)
        summaries = self._summaries_from_snapshots(
            snapshots,
            scheduler_state,
            queue_positions,
            sampled_at,
        )
        self._apply_task_summaries(summaries, scheduler_state)

    async def refresh_tasks_async(self, *, now: float | None = None) -> None:
        active = self._task_refresh_task
        if active is not None and not active.done():
            self._task_refresh_pending = True
            await asyncio.shield(active)
            return

        refresh = asyncio.create_task(self._refresh_task_views(now=now))
        self._task_refresh_task = refresh
        refresh.add_done_callback(self._task_refresh_finished)
        await asyncio.shield(refresh)

    async def _refresh_task_views(self, *, now: float | None) -> None:
        while True:
            self._task_refresh_pending = False
            sampled_at = monotonic_clock() if now is None else now
            snapshots = await asyncio.to_thread(
                self.repository.list_task_snapshots,
                include_archived=True,
            )
            scheduler_state, queue_positions = self._task_scheduler_state()
            summaries = self._summaries_from_snapshots(
                snapshots,
                scheduler_state,
                queue_positions,
                sampled_at,
            )
            self._apply_task_summaries(summaries, scheduler_state)
            if not self._task_refresh_pending:
                return

    def _task_refresh_finished(self, task: asyncio.Task[None]) -> None:
        if self._task_refresh_task is task:
            self._task_refresh_task = None
        if not task.cancelled():
            task.exception()

    def _task_scheduler_state(self) -> tuple[SchedulerSnapshot, dict[str, int]]:
        snapshot_method = getattr(self.scheduler, "snapshot", None)
        scheduler_state = (
            snapshot_method()
            if callable(snapshot_method)
            else SchedulerSnapshot(
                (),
                (),
                self.settings.concurrency,
                self.settings.speed_limit_kib,
            )
        )
        queue_positions_method = getattr(self.scheduler, "queue_positions", None)
        queue_positions = queue_positions_method() if callable(queue_positions_method) else {}
        return scheduler_state, queue_positions

    def _summaries_from_snapshots(
        self,
        snapshots: list[Any],
        scheduler_state: SchedulerSnapshot,
        queue_positions: dict[str, int],
        sampled_at: float,
    ) -> list[TaskSummary]:
        summaries: list[TaskSummary] = []
        active_ids: set[str] = set()
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
        return summaries

    def _apply_task_summaries(
        self,
        summaries: list[TaskSummary],
        scheduler_state: SchedulerSnapshot,
    ) -> None:
        self.window.set_task_summaries(summaries)
        set_scheduler_summary = getattr(self.window, "set_scheduler_summary", None)
        if callable(set_scheduler_summary):
            set_scheduler_summary(
                active=scheduler_state.active_count,
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

    async def _refresh_tasks_if_due(self, now: float | None = None) -> None:
        sampled_at = monotonic_clock() if now is None else now
        if sampled_at < self._next_progress_refresh:
            return
        self._next_progress_refresh = sampled_at + self._progress_refresh_interval
        await self.refresh_tasks_async(now=sampled_at)

    async def show_account_access(self) -> None:
        if (
            self.gateway is None
            or self.settings.api_id <= 0
            or not self.secrets.get("api_hash")
        ):
            self.show_login_credentials()
            return
        try:
            profile = await self.gateway.account_profile()
            authorization = AuthorizationState.AUTHORIZED
            connection = (
                ConnectionState.ONLINE
                if self._gateway_is_connected(self.gateway)
                else ConnectionState.DEGRADED
            )
        except SessionExpiredError:
            profile = None
            authorization = AuthorizationState.EXPIRED
            connection = ConnectionState.OFFLINE
        except Exception:
            profile = None
            authorization = AuthorizationState.UNKNOWN
            connection = ConnectionState.DEGRADED
        self.account_status_dialog.set_snapshot(
            self._account_status_snapshot(
                profile,
                authorization,
                connection,
            )
        )
        self.account_status_dialog.show()
        self.account_status_dialog.raise_()
        self.account_status_dialog.activateWindow()

    def _account_status_snapshot(
        self,
        profile: AccountProfile | None,
        authorization: AuthorizationState,
        connection: ConnectionState,
    ) -> AccountStatusSnapshot:
        scheduler_snapshot = self.scheduler.snapshot()
        fallback_name = getattr(self.window, "account", None) or "账号信息不可用"
        return AccountStatusSnapshot(
            profile.account_id if profile is not None else None,
            profile.display_name if profile is not None else fallback_name,
            authorization,
            connection,
            bool(self.secrets.get("session")),
            self.content_browser is not None,
            not isinstance(self.subscriptions, _NullSubscriptionService),
            scheduler_snapshot.active_count,
        )

    def show_login_credentials(self) -> None:
        self._prefill_login()
        from telegram_downloader.ui.login import LoginPage

        self.login_dialog.show_page(LoginPage.CREDENTIALS)
        self._show_login_dialog()

    def show_login(self) -> None:
        self.show_login_credentials()

    def _show_login_dialog(self) -> None:
        self.login_dialog.show()
        self.login_dialog.raise_()
        self.login_dialog.activateWindow()

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
        if self._candidate_login is not None:
            await self._finish_candidate_login()
            return
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

    async def _finish_candidate_login(self) -> None:
        candidate = self._candidate_login
        if candidate is None:
            return
        try:
            candidate.profile = await candidate.gateway.account_profile()
            old_profile = await self._current_account_profile()
            if candidate.profile.account_id != old_profile.account_id:
                confirmation = self.confirm_account_switch(
                    old_profile,
                    candidate.profile,
                )
                if inspect.isawaitable(confirmation):
                    confirmation = await confirmation
                if not confirmation:
                    await self._discard_candidate_login()
                    return
            if self.scheduler.snapshot().active_count:
                self.account_status_dialog.show_error(
                    "检测到活动下载，请暂停或等待完成后重试"
                )
                await self._discard_candidate_login()
                return
            await self._commit_candidate_services(candidate)
        except Exception as error:
            self.login_dialog.show_error(self._safe_error(error))
            await self._discard_candidate_login()

    async def _current_account_profile(self) -> AccountProfile:
        gateway = self.gateway
        if gateway is not None:
            try:
                return await gateway.account_profile()
            except Exception:
                pass
        account = getattr(self.subscriptions, "account", None)
        if isinstance(account, AccountProfile):
            return account
        display_name = getattr(self.window, "account", None) or "当前账号"
        return AccountProfile("", display_name)

    async def _commit_candidate_services(
        self,
        candidate: CandidateLoginSession,
    ) -> None:
        if self.build_online_services is None:
            raise GatewayError("无法创建账号在线服务")
        built = self.build_online_services(candidate.gateway, self.settings)
        services = (
            built
            if isinstance(built, OnlineServices)
            else OnlineServices(candidate.gateway, built[0], built[1])
        )
        old_services = OnlineServices(self.gateway, self.planner, self.scheduler)
        old_secrets = dict(self.secrets)
        committed = False
        vault_changed = False
        self.scheduler.set_admission_open(False)
        try:
            await self._cancel_subscription_probe()
            await self._cancel_content_operations()
            self.bind_online_services(services)
            new_secrets = {
                **old_secrets,
                "session": candidate.gateway.export_session(),
            }
            self.vault.save(new_secrets)
            vault_changed = True
            self.gateway = services.gateway
            self.planner = services.planner
            self.scheduler = services.scheduler
            self.secrets = new_secrets
            await self.activate_content_account(raise_errors=True)
            if candidate.profile is not None and self.content_browser is None:
                self.window.set_account(candidate.profile.display_name)
            committed = True
        finally:
            if not committed:
                self.gateway = old_services.gateway
                self.planner = old_services.planner
                self.scheduler = old_services.scheduler
                self.secrets = old_secrets
                with suppress(Exception):
                    self.bind_online_services(old_services)
                if vault_changed:
                    self.vault.save(old_secrets)
                set_admission = getattr(self.scheduler, "set_admission_open", None)
                if callable(set_admission):
                    set_admission(True)
                shutdown = getattr(services.scheduler, "shutdown", None)
                if callable(shutdown):
                    with suppress(Exception):
                        await shutdown()
                await self._discard_candidate_login()
        if not committed:
            return

        self._candidate_login = None
        candidate.qr_wait_task = None
        self._session_expiry_handled = False
        self._last_authorization_failure_reason = None
        profile = candidate.profile or AccountProfile("", "已登录")
        self.login_dialog.show_ready(profile.display_name)
        self.login_dialog.accept()
        self._ensure_connection_monitor()
        old_shutdown = getattr(old_services.scheduler, "shutdown", None)
        if callable(old_shutdown):
            try:
                await old_shutdown()
            except Exception:
                _LOGGER.warning("old scheduler cleanup failed after account commit")
        if old_services.gateway is not None:
            try:
                await old_services.gateway.disconnect()
            except Exception:
                _LOGGER.warning("old gateway cleanup failed after account commit")

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
            self._publish_event(auth_required_event())
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
                if self.build_online_services is not None:
                    services = self.build_online_services(
                        fresh_gateway,
                        self.settings,
                    )
                    if not isinstance(services, OnlineServices):
                        services = OnlineServices(
                            fresh_gateway,
                            services[0],
                            services[1],
                        )
                    self.bind_online_services(services)
                    self.planner = services.planner
                    self.scheduler = services.scheduler

            self._request_login(publish=False)

    def _publish_event(self, event: ApplicationEvent) -> None:
        try:
            self.publish(event)
        except Exception:
            _LOGGER.error("notification event callback failed")

    def _request_login(self, *, publish: bool = True) -> None:
        if publish:
            self._publish_event(auth_required_event())
        visible = getattr(self.window, "isVisible", None)
        if callable(visible):
            if not visible():
                return
        elif self._background_launch:
            return
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

    async def _reload_subscriptions(self) -> None:
        page = self._subscription_page()
        try:

            def load_snapshot():
                snapshot = getattr(self.subscriptions, "snapshot", None)
                if callable(snapshot):
                    rules, latest_runs = snapshot()
                else:
                    rules = self.subscriptions.list_rules()
                    latest_runs = self.subscriptions.latest_runs()
                latest_items = (
                    tuple(latest_runs.items())
                    if isinstance(latest_runs, dict)
                    else tuple(latest_runs)
                )
                return tuple(rules), latest_items

            rules, latest_items = await asyncio.to_thread(load_snapshot)
            page.set_rules(list(rules), dict(latest_items))
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

    async def _list_content_sessions_async(self) -> list[Any]:
        if self.content_browser is None:
            return []
        async_list = getattr(self.content_browser, "list_sessions_async", None)
        if callable(async_list):
            return list(await async_list())
        return list(await asyncio.to_thread(self.content_browser.list_sessions))

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

    async def _reload_content_search_async(self, search_id: str) -> None:
        if self.content_browser is None:
            return
        load_snapshot = getattr(
            self.content_browser,
            "load_search_snapshot",
            None,
        )
        if callable(load_snapshot):
            await self.open_content_history(search_id)
            return
        self._reload_content_search(search_id)

    async def _cancel_content_operations(self) -> None:
        current = asyncio.current_task()
        self._selection_intents.clear()
        self._selection_committed_revisions.clear()
        self._history_generation += 1
        tracked = [
            self._dialog_sync_task,
            self._content_search_task,
            self._selection_persist_task,
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
        self._selection_persist_task = None
        self._thumbnail_tasks.clear()

    def _start_task(self, task_id: str) -> None:
        self._spawn_background(self._run_and_refresh(task_id))

    def _schedule_task_refresh(self) -> None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            self.refresh_tasks()
            return
        self._spawn_background(self.refresh_tasks_async())

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

    async def _run_update_check(self) -> UpdateStartupResult:
        try:
            result = await self.update_coordinator.startup(
                self.update_prompt,
                self.update_shutdown,
            )
            if result in {
                UpdateStartupResult.NO_UPDATE,
                UpdateStartupResult.DECLINED,
                UpdateStartupResult.LAUNCHED,
            }:
                self._record_successful_update_check()
            if result is UpdateStartupResult.NO_UPDATE:
                self._show_status("当前已是最新正式版")
            elif result is UpdateStartupResult.BLOCKED:
                self._show_status("更新检查暂不可用，已继续使用当前版本")
            return result
        except MaintenanceBusyError as error:
            self._show_status(str(error))
            return UpdateStartupResult.BLOCKED
        except Exception as error:
            self._show_status(f"更新检查失败（{type(error).__name__}）")
            raise

    def _record_successful_update_check(self) -> str:
        stamp = (
            self._utc_now()
            .astimezone(UTC)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )
        updated = replace(
            self.settings,
            last_successful_update_check_utc=stamp,
        )
        self.settings_store.save(updated)
        self.settings = updated
        return stamp

    def check_for_updates(self) -> asyncio.Task[Any] | None:
        if self.update_coordinator is None or self._shutting_down:
            return None
        current = self._update_check_task
        if current is not None and not current.done():
            return current
        self._update_check_task = self._spawn_background(self._run_update_check())
        return self._update_check_task

    async def _run_and_refresh(self, task_id: str) -> None:
        operation = asyncio.create_task(self.scheduler.run_task(task_id))
        try:
            while not operation.done():
                is_active = getattr(self.scheduler, "is_active", None)
                if not callable(is_active) or is_active(task_id):
                    await self._refresh_tasks_if_due()
                try:
                    await asyncio.wait_for(
                        asyncio.shield(operation),
                        timeout=self._progress_refresh_interval,
                    )
                except TimeoutError:
                    continue
            await operation
        finally:
            await self.refresh_tasks_async()

    def _show_status(self, message: str) -> None:
        self.window.statusBar().showMessage(message, 8000)

    def _show_error(self, message: str) -> None:
        self.window.statusBar().showMessage(message, 0)

    @staticmethod
    def _safe_error(error: Exception) -> str:
        if isinstance(
            error,
            (
                GatewayError,
                InvalidTelegramLink,
                MaintenanceBusyError,
                StorageMaintenanceError,
                ValueError,
            ),
        ):
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
