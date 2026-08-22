from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from telegram_downloader.content import AccountProfile
from telegram_downloader.controller import AppController
from telegram_downloader.domain import MediaKind
from telegram_downloader.subscription_matching import SubscriptionCriteria
from telegram_downloader.subscriptions import SubscriptionDraft

NOW = datetime(2026, 8, 15, tzinfo=UTC)


class ContentPage:
    active_search_id = None

    def set_logged_in(self, _value):
        pass

    def set_dialogs(self, _value):
        pass

    def set_sessions(self, _value):
        pass

    def set_active_search(self, _value):
        pass

    def set_results(self, _value):
        pass

    def set_connection_state(self, _text, *, retryable=False):
        pass

    def set_sync_state(self, _text, *, busy=False, count=0):
        pass

    def show_error(self, _message):
        pass


class SubscriptionPage:
    def __init__(self) -> None:
        self.logged_in: list[bool] = []
        self.dialogs = []
        self.rules = []
        self.latest_runs = {}
        self.busy = []
        self.errors = []
        self.detail_rule = None
        self.detail_runs = []
        self.probe_busy = False
        self.probe_progress = []
        self.probe_reports = []
        self.cancelled_messages = 0
        self.editor_results = []

    def set_logged_in(self, value):
        self.logged_in.append(value)

    def set_dialogs(self, value):
        self.dialogs = value

    def set_rules(self, value, latest_runs=None):
        self.rules = value
        self.latest_runs = latest_runs or {}

    def set_rule_busy(self, rule_id, busy, text=""):
        self.busy.append((rule_id, busy, text))

    def set_selected_rule_details(self, rule, runs):
        self.detail_rule = rule
        self.detail_runs = list(runs)

    def set_probe_busy(self, _rule_id, busy):
        self.probe_busy = busy

    def set_probe_progress(self, progress):
        self.probe_progress.append(progress)

    def set_probe_result(self, report):
        self.probe_reports.append(report)
        self.probe_busy = False

    def show_probe_cancelled(self):
        self.cancelled_messages += 1
        self.probe_busy = False

    def show_error(self, message):
        self.errors.append(message)

    def finish_editor_save(self, success, error=""):
        self.editor_results.append((success, error))


class Window:
    def __init__(self) -> None:
        self.content_page = ContentPage()
        self.subscriptions_page = SubscriptionPage()
        self.account = None
        self.message = ""

    def set_account(self, value):
        self.account = value

    def set_task_summaries(self, _value):
        pass

    def statusBar(self):
        return self

    def showMessage(self, message, _timeout=0):
        self.message = message


class ContentService:
    account = None

    async def activate_account(self):
        self.account = AccountProfile("a1", "账号一")
        return self.account, ["dialog-1"]

    def list_sessions(self):
        return []

    def list_dialogs(self):
        return ["dialog-1"]

    def dialog_cache_stale(self, _max_age):
        return False


class Subscriptions:
    def __init__(self) -> None:
        self.account = None
        self.rules = []
        self.calls = []
        self.resume_count = 0
        self.runs = []
        self.list_run_limits = []

    def set_account(self, account):
        self.account = account
        self.calls.append(("account", account))

    def list_rules(self):
        return self.rules

    def latest_runs(self):
        return {"rule-1": "latest"} if self.rules else {}

    def resume_after_connection(self):
        self.resume_count += 1
        return 0

    def get_rule(self, rule_id):
        return next(item for item in self.rules if item.id == rule_id)

    def list_runs(self, rule_id, *, limit=20):
        self.get_rule(rule_id)
        self.list_run_limits.append(limit)
        return self.runs[:limit]

    async def create_rule(self, draft):
        self.calls.append(("create", draft))
        value = SimpleNamespace(id="rule-1")
        self.rules = [value]
        return value

    async def update_rule(self, rule_id, draft):
        self.calls.append(("update", rule_id, draft))
        return self.rules[0]

    def set_enabled(self, rule_id, enabled):
        self.calls.append(("enabled", rule_id, enabled))

    def delete_rule(self, rule_id):
        self.calls.append(("delete", rule_id))
        self.rules = []


class SubscriptionScheduler:
    def __init__(self, order=None) -> None:
        self.account_ids = []
        self.wakes = []
        self.started = 0
        self.order = order
        self.account_id = None

    def set_account(self, account_id):
        self.account_id = account_id
        self.account_ids.append(account_id)

    def start(self):
        self.started += 1

    def wake(self, rule_id=None):
        self.wakes.append(rule_id)

    async def shutdown(self):
        if self.order is not None:
            self.order.append("subscriptions")


@pytest.mark.asyncio
async def test_account_activation_binds_rules_and_starts_subscription_scheduler() -> None:
    subscriptions = Subscriptions()
    scheduler = SubscriptionScheduler()
    window = Window()
    controller = AppController.for_test(
        content_browser=ContentService(),
        subscriptions=subscriptions,
        subscription_scheduler=scheduler,
        window=window,
    )

    await controller.activate_content_account()

    assert subscriptions.account == AccountProfile("a1", "账号一")
    assert subscriptions.resume_count == 1
    assert scheduler.account_ids == ["a1"]
    assert scheduler.started == 1
    assert scheduler.wakes == [None]
    assert window.subscriptions_page.dialogs == ["dialog-1"]
    assert window.subscriptions_page.logged_in[-1] is True


@pytest.mark.asyncio
async def test_subscription_actions_restore_busy_state_and_refresh_rules() -> None:
    subscriptions = Subscriptions()
    subscriptions.set_account(AccountProfile("a1", "账号一"))
    scheduler = SubscriptionScheduler()
    window = Window()
    controller = AppController.for_test(
        subscriptions=subscriptions,
        subscription_scheduler=scheduler,
        window=window,
    )
    draft = SubscriptionDraft(
        "-1001",
        SubscriptionCriteria(("美女",)),
        frozenset({MediaKind.PHOTO}),
    )

    await controller.create_subscription(draft)
    await controller.update_subscription("rule-1", draft)
    await controller.set_subscription_enabled("rule-1", False)
    await controller.run_subscription_now("rule-1")
    await controller.delete_subscription("rule-1")

    assert [item[0] for item in subscriptions.calls if item[0] != "account"] == [
        "create",
        "update",
        "enabled",
        "delete",
    ]
    assert scheduler.wakes == [None, None, "rule-1"]
    assert window.subscriptions_page.rules == []
    assert window.subscriptions_page.busy[-1][1] is False
    assert window.subscriptions_page.editor_results == [(True, ""), (True, "")]


@pytest.mark.asyncio
async def test_subscription_save_failure_keeps_editor_open_with_safe_error() -> None:
    class FailingSubscriptions(Subscriptions):
        async def create_rule(self, draft):
            raise ValueError("无法读取历史边界")

    subscriptions = FailingSubscriptions()
    subscriptions.set_account(AccountProfile("a1", "账号一"))
    window = Window()
    controller = AppController.for_test(
        subscriptions=subscriptions,
        subscription_scheduler=SubscriptionScheduler(),
        window=window,
    )
    draft = SubscriptionDraft(
        "-1001",
        SubscriptionCriteria(("AI",)),
        frozenset({MediaKind.PHOTO}),
        history_days=7,
    )

    await controller.create_subscription(draft)

    assert window.subscriptions_page.editor_results == [(False, "无法读取历史边界")]
    assert window.subscriptions_page.rules == []
    assert window.subscriptions_page.busy[-1][1] is False


@pytest.mark.asyncio
async def test_subscription_baseline_action_has_foreground_priority() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    class BlockingSubscriptions(Subscriptions):
        async def create_rule(self, draft):
            entered.set()
            await release.wait()
            return await super().create_rule(draft)

    subscriptions = BlockingSubscriptions()
    subscriptions.set_account(AccountProfile("a1", "账号一"))
    controller = AppController.for_test(
        subscriptions=subscriptions,
        subscription_scheduler=SubscriptionScheduler(),
        window=Window(),
    )
    draft = SubscriptionDraft(
        "-1001",
        SubscriptionCriteria(("美女",)),
        frozenset({MediaKind.PHOTO}),
    )

    task = asyncio.create_task(controller.create_subscription(draft))
    await entered.wait()
    assert controller.foreground_telegram_busy() is True
    release.set()
    await task
    assert controller.foreground_telegram_busy() is False


@pytest.mark.asyncio
async def test_network_recovery_wakes_connection_blocked_subscriptions() -> None:
    class Gateway:
        connected = False

        def is_connected(self):
            return self.connected

        async def connect(self):
            self.connected = True

    subscriptions = Subscriptions()
    subscriptions.set_account(AccountProfile("a1", "账号一"))
    scheduler = SubscriptionScheduler()
    scheduler.set_account("a1")
    controller = AppController.for_test(
        gateway=Gateway(),
        content_browser=ContentService(),
        subscriptions=subscriptions,
        subscription_scheduler=scheduler,
        window=Window(),
    )

    assert await controller.retry_telegram_connection() is True

    assert subscriptions.resume_count == 1
    assert scheduler.wakes == [None]


@pytest.mark.asyncio
async def test_subscription_created_task_enters_existing_download_scheduler() -> None:
    controller = AppController.for_test(window=Window())
    started = []
    refreshed = asyncio.Event()

    async def refresh() -> None:
        started.append("refresh")
        refreshed.set()

    controller.refresh_tasks = Mock(side_effect=AssertionError("同步刷新不应被调用"))
    controller.refresh_tasks_async = refresh
    controller._start_task = lambda task_id: started.append(task_id)

    controller.subscription_task_created("task-1")
    await asyncio.wait_for(refreshed.wait(), timeout=1)

    assert started == ["task-1", "refresh"]


@pytest.mark.asyncio
async def test_shutdown_stops_subscription_scheduler_before_download_and_gateway() -> None:
    order = []

    class Downloads:
        async def shutdown(self):
            order.append("downloads")

    class Gateway:
        async def disconnect(self):
            order.append("gateway")

    controller = AppController.for_test(
        gateway=Gateway(),
        scheduler=Downloads(),
        subscription_scheduler=SubscriptionScheduler(order),
        window=Window(),
    )

    await controller.shutdown()

    assert order == ["subscriptions", "downloads", "gateway"]


@pytest.mark.asyncio
async def test_probe_is_foreground_and_repeated_request_is_deduplicated() -> None:
    class ProbeSubscriptions(Subscriptions):
        def __init__(self) -> None:
            super().__init__()
            self.rules = [SimpleNamespace(id="rule-1")]
            self.probe_started = asyncio.Event()
            self.probe_release = asyncio.Event()
            self.probe_calls = []

        async def probe_rule(self, rule_id, *, on_progress=None):
            self.probe_calls.append(rule_id)
            self.probe_started.set()
            await self.probe_release.wait()
            return SimpleNamespace(rule_id=rule_id)

    subscriptions = ProbeSubscriptions()
    page_window = Window()
    controller = AppController.for_test(
        subscriptions=subscriptions,
        subscription_scheduler=SubscriptionScheduler(),
        window=page_window,
    )

    first = asyncio.create_task(controller.probe_subscription("rule-1"))
    await subscriptions.probe_started.wait()
    assert controller.foreground_telegram_busy()

    await controller.probe_subscription("rule-1")
    assert subscriptions.probe_calls == ["rule-1"]

    subscriptions.probe_release.set()
    await first
    assert controller.foreground_telegram_busy() is False
    assert page_window.subscriptions_page.probe_reports[-1].rule_id == "rule-1"


@pytest.mark.asyncio
async def test_probe_cancel_restores_page_without_recording_failure() -> None:
    class ProbeSubscriptions(Subscriptions):
        def __init__(self) -> None:
            super().__init__()
            self.rules = [SimpleNamespace(id="rule-1")]
            self.probe_started = asyncio.Event()

        async def probe_rule(self, rule_id, *, on_progress=None):
            self.probe_started.set()
            await asyncio.Event().wait()

    subscriptions = ProbeSubscriptions()
    page_window = Window()
    controller = AppController.for_test(
        subscriptions=subscriptions,
        subscription_scheduler=SubscriptionScheduler(),
        window=page_window,
    )
    running = asyncio.create_task(controller.probe_subscription("rule-1"))
    await subscriptions.probe_started.wait()

    controller.cancel_subscription_probe()
    await running

    page = page_window.subscriptions_page
    assert page.cancelled_messages == 1
    assert page.probe_busy is False
    assert controller._subscription_probe_task is None


@pytest.mark.asyncio
async def test_account_switch_cancels_probe_before_rebinding_service() -> None:
    class ProbeSubscriptions(Subscriptions):
        def __init__(self) -> None:
            super().__init__()
            self.rules = [SimpleNamespace(id="rule-1")]
            self.probe_started = asyncio.Event()
            self.events = []

        async def probe_rule(self, rule_id, *, on_progress=None):
            self.probe_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.events.append("probe-cancelled")
                raise

        def set_account(self, account):
            self.events.append(f"account:{getattr(account, 'account_id', None)}")
            super().set_account(account)

    subscriptions = ProbeSubscriptions()
    controller = AppController.for_test(
        subscriptions=subscriptions,
        subscription_scheduler=SubscriptionScheduler(),
        window=Window(),
    )
    running = asyncio.create_task(controller.probe_subscription("rule-1"))
    await subscriptions.probe_started.wait()

    await controller._cancel_subscription_probe()
    subscriptions.set_account(AccountProfile("a2", "账号二"))
    await running

    assert subscriptions.events[:2] == ["probe-cancelled", "account:a2"]


@pytest.mark.asyncio
async def test_shutdown_awaits_probe_before_gateway_disconnect() -> None:
    events = []

    class ProbeSubscriptions(Subscriptions):
        def __init__(self) -> None:
            super().__init__()
            self.rules = [SimpleNamespace(id="rule-1")]
            self.probe_started = asyncio.Event()

        async def probe_rule(self, rule_id, *, on_progress=None):
            self.probe_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                events.append("probe-stopped")

        def go_offline(self):
            events.append("subscriptions-offline")

    class Gateway:
        async def disconnect(self):
            events.append("gateway-disconnected")

    subscriptions = ProbeSubscriptions()
    controller = AppController.for_test(
        gateway=Gateway(),
        subscriptions=subscriptions,
        subscription_scheduler=SubscriptionScheduler(),
        window=Window(),
    )
    running = asyncio.create_task(controller.probe_subscription("rule-1"))
    await subscriptions.probe_started.wait()

    await controller.shutdown()
    await running

    assert events.index("probe-stopped") < events.index("gateway-disconnected")


def test_rule_selection_loads_only_latest_twenty_runs() -> None:
    subscriptions = Subscriptions()
    selected = SimpleNamespace(id="rule-1")
    subscriptions.rules = [selected]
    subscriptions.runs = [SimpleNamespace(id=f"run-{index}") for index in range(25)]
    page_window = Window()
    controller = AppController.for_test(
        subscriptions=subscriptions,
        subscription_scheduler=SubscriptionScheduler(),
        window=page_window,
    )

    controller.show_subscription_details("rule-1")

    assert subscriptions.list_run_limits == [20]
    assert page_window.subscriptions_page.detail_rule == selected
    assert len(page_window.subscriptions_page.detail_runs) == 20
