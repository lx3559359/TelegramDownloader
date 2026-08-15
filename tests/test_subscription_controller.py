from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from telegram_downloader.content import AccountProfile
from telegram_downloader.controller import AppController
from telegram_downloader.domain import MediaKind
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

    def set_logged_in(self, value):
        self.logged_in.append(value)

    def set_dialogs(self, value):
        self.dialogs = value

    def set_rules(self, value, latest_runs=None):
        self.rules = value
        self.latest_runs = latest_runs or {}

    def set_rule_busy(self, rule_id, busy, text=""):
        self.busy.append((rule_id, busy, text))

    def show_error(self, message):
        self.errors.append(message)


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
        "美女",
        frozenset({MediaKind.PHOTO}),
    )

    await controller.create_subscription(draft)
    await controller.update_subscription("rule-1", draft)
    controller.set_subscription_enabled("rule-1", False)
    controller.run_subscription_now("rule-1")
    controller.delete_subscription("rule-1")

    assert [item[0] for item in subscriptions.calls if item[0] != "account"] == [
        "create",
        "update",
        "enabled",
        "delete",
    ]
    assert scheduler.wakes == [None, None, "rule-1"]
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
    draft = SubscriptionDraft("-1001", "美女", frozenset({MediaKind.PHOTO}))

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


def test_subscription_created_task_enters_existing_download_scheduler() -> None:
    controller = AppController.for_test(window=Window())
    started = []
    controller.refresh_tasks = lambda: started.append("refresh")
    controller._start_task = lambda task_id: started.append(task_id)

    controller.subscription_task_created("task-1")

    assert started == ["refresh", "task-1"]


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
