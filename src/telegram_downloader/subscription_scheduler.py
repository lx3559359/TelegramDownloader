from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any

from telegram_downloader.gateway import (
    AccessDeniedError,
    FloodWaitError,
    SessionExpiredError,
    TransientNetworkError,
)
from telegram_downloader.subscription_service import SubscriptionUnavailableError
from telegram_downloader.subscriptions import (
    SubscriptionProgress,
    SubscriptionRule,
    SubscriptionState,
)


class SubscriptionScheduler:
    _BACKOFF_MINUTES = (1, 2, 5, 15)

    def __init__(
        self,
        service: Any,
        *,
        clock: Callable[[], datetime] | None = None,
        foreground_busy: Callable[[], bool] | None = None,
        on_rules_changed: Callable[[], None] | None = None,
        on_task_created: Callable[[str], None] | None = None,
        on_progress: Callable[[SubscriptionProgress | None], None] | None = None,
        idle_delay: float = 1.0,
    ) -> None:
        if idle_delay <= 0:
            raise ValueError("订阅调度检查间隔必须大于零")
        self.service = service
        self.clock = clock or (lambda: datetime.now(UTC))
        self.foreground_busy = foreground_busy or (lambda: False)
        self.on_rules_changed = on_rules_changed or (lambda: None)
        self.on_task_created = on_task_created or (lambda _task_id: None)
        self.on_progress = on_progress or (lambda _progress: None)
        self.idle_delay = idle_delay
        self.account_id: str | None = None
        self._wake_event = asyncio.Event()
        self._pending: deque[str] = deque()
        self._pending_set: set[str] = set()
        self._runner: asyncio.Task[None] | None = None
        self._active_rule_id: str | None = None
        self._closing = False

    @property
    def running(self) -> bool:
        return self._runner is not None and not self._runner.done()

    def start(self) -> None:
        if self.running:
            return
        self._closing = False
        self._runner = asyncio.create_task(
            self._run(),
            name="telegram-subscription-scheduler",
        )

    def set_account(self, account_id: str | None) -> None:
        if account_id != self.account_id:
            self._pending.clear()
            self._pending_set.clear()
        self.account_id = account_id
        self._wake_event.set()

    def wake(self, rule_id: str | None = None) -> None:
        if (
            rule_id is not None
            and rule_id != self._active_rule_id
            and rule_id not in self._pending_set
        ):
            self._pending.append(rule_id)
            self._pending_set.add(rule_id)
        self._wake_event.set()

    async def shutdown(self) -> None:
        self._closing = True
        self._wake_event.set()
        task = self._runner
        self._runner = None
        if task is None or task.done():
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _run(self) -> None:
        while not self._closing:
            self._wake_event.clear()
            selected = self._next_rule()
            if selected is not None:
                self._active_rule_id = selected.id
                try:
                    await self._execute(selected)
                finally:
                    self._active_rule_id = None
                continue
            try:
                await asyncio.wait_for(
                    self._wake_event.wait(),
                    timeout=self.idle_delay,
                )
            except TimeoutError:
                continue

    def _next_rule(self) -> SubscriptionRule | None:
        account_id = self.account_id
        if account_id is None or self.foreground_busy():
            return None
        while self._pending:
            rule_id = self._pending.popleft()
            self._pending_set.discard(rule_id)
            try:
                rule = self.service.get_rule(rule_id)
            except KeyError:
                continue
            if rule.account_id == account_id and rule.enabled:
                return rule
        due = self.service.list_due_rules(self.clock())
        return due[0] if due else None

    async def _execute(self, rule: SubscriptionRule) -> None:
        now = self.clock()
        try:
            report = await self.service.run_rule(
                rule.id,
                on_progress=self.on_progress,
            )
        except asyncio.CancelledError:
            self.service.update_runtime(
                rule.id,
                state=SubscriptionState.WAITING,
                next_run_at=now,
                last_run_at=now,
                last_error=None,
                failure_count=rule.failure_count,
            )
            self.on_progress(None)
            self._notify_rules_changed()
            raise
        except FloodWaitError as error:
            self._record_failure(
                rule,
                SubscriptionState.WAITING_NETWORK,
                now + timedelta(seconds=max(1, error.seconds)),
                f"Telegram 请求需等待 {error.seconds} 秒",
            )
        except TransientNetworkError:
            failure_count = rule.failure_count + 1
            self._record_failure(
                rule,
                SubscriptionState.WAITING_NETWORK,
                now + self._backoff(failure_count),
                "Telegram 网络连接失败",
                failure_count=failure_count,
            )
        except SessionExpiredError:
            self._record_failure(
                rule,
                SubscriptionState.AUTH_REQUIRED,
                None,
                "Telegram 登录已失效",
            )
        except AccessDeniedError as error:
            self.service.set_enabled(rule.id, False)
            self._record_failure(
                rule,
                SubscriptionState.PAUSED,
                None,
                str(error),
            )
        except SubscriptionUnavailableError as error:
            failure_count = rule.failure_count + 1
            self._record_failure(
                rule,
                SubscriptionState.FAILED,
                now + self._backoff(failure_count),
                str(error),
                failure_count=failure_count,
            )
        except Exception as error:
            failure_count = rule.failure_count + 1
            self._record_failure(
                rule,
                SubscriptionState.FAILED,
                now + self._backoff(failure_count),
                f"自动检查失败（{type(error).__name__}）",
                failure_count=failure_count,
            )
        else:
            for task_id in report.task_ids:
                self.on_task_created(task_id)
            self.on_progress(None)
            self._notify_rules_changed()

    def _record_failure(
        self,
        rule: SubscriptionRule,
        state: SubscriptionState,
        next_run_at: datetime | None,
        error: str,
        *,
        failure_count: int | None = None,
    ) -> None:
        count = rule.failure_count + 1 if failure_count is None else failure_count
        self.service.update_runtime(
            rule.id,
            state=state,
            next_run_at=next_run_at,
            last_run_at=self.clock(),
            last_error=error,
            failure_count=count,
        )
        self.on_progress(None)
        self._notify_rules_changed()

    @classmethod
    def _backoff(cls, failure_count: int) -> timedelta:
        index = min(max(1, failure_count) - 1, len(cls._BACKOFF_MINUTES) - 1)
        return timedelta(minutes=cls._BACKOFF_MINUTES[index])

    def _notify_rules_changed(self) -> None:
        self.on_rules_changed()
