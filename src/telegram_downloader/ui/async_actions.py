from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

_LOGGER = logging.getLogger("telegram_downloader.ui.async_actions")

ActionFactory = Callable[[], Awaitable[Any]]
PayloadAction = Callable[[Any], Awaitable[Any]]
ArgsAction = Callable[..., Awaitable[Any]]
Callback = Callable[[], None]
FailureCallback = Callable[[Exception], None]


@dataclass(frozen=True, slots=True)
class ActionHooks:
    started: Callback | None = None
    succeeded: Callback | None = None
    failed: FailureCallback | None = None
    cancelled: Callback | None = None
    finished: Callback | None = None


_NO_HOOKS = ActionHooks()


class ActionPolicy(StrEnum):
    DEDUPLICATE = "deduplicate"
    REPLACE_LATEST = "replace_latest"


ACTION_POLICIES: dict[str, ActionPolicy] = {
    "account.access.open": ActionPolicy.DEDUPLICATE,
    "account.reauthenticate": ActionPolicy.DEDUPLICATE,
    "account.reconnect": ActionPolicy.DEDUPLICATE,
    "content.activate": ActionPolicy.REPLACE_LATEST,
    "content.history.open": ActionPolicy.REPLACE_LATEST,
    "content.search": ActionPolicy.REPLACE_LATEST,
    "content.load_more": ActionPolicy.REPLACE_LATEST,
    "content.queue": ActionPolicy.DEDUPLICATE,
    "content.history.delete": ActionPolicy.DEDUPLICATE,
    "content.history.clear": ActionPolicy.DEDUPLICATE,
    "dialogs.refresh": ActionPolicy.DEDUPLICATE,
    "telegram.retry": ActionPolicy.DEDUPLICATE,
    "diagnostics.activate": ActionPolicy.DEDUPLICATE,
    "diagnostics.run": ActionPolicy.DEDUPLICATE,
    "diagnostics.cancel": ActionPolicy.DEDUPLICATE,
    "diagnostics.export": ActionPolicy.DEDUPLICATE,
    "login.qr.refresh": ActionPolicy.DEDUPLICATE,
    "login.qr.expired": ActionPolicy.DEDUPLICATE,
    "login.phone": ActionPolicy.DEDUPLICATE,
    "login.credentials": ActionPolicy.DEDUPLICATE,
    "login.cancel": ActionPolicy.DEDUPLICATE,
    "settings.open": ActionPolicy.DEDUPLICATE,
    "settings.save": ActionPolicy.DEDUPLICATE,
    "settings.update.check": ActionPolicy.DEDUPLICATE,
    "settings.thumbnail_cache.clear": ActionPolicy.DEDUPLICATE,
    "subscriptions.activate": ActionPolicy.DEDUPLICATE,
    "subscriptions.probe": ActionPolicy.DEDUPLICATE,
    "subscriptions.run": ActionPolicy.DEDUPLICATE,
    "subscriptions.enabled": ActionPolicy.DEDUPLICATE,
    "subscriptions.delete": ActionPolicy.DEDUPLICATE,
    "tasks.pause": ActionPolicy.DEDUPLICATE,
    "tasks.prioritize": ActionPolicy.DEDUPLICATE,
    "tasks.archive": ActionPolicy.DEDUPLICATE,
    "tasks.restore": ActionPolicy.DEDUPLICATE,
    "tasks.resume": ActionPolicy.DEDUPLICATE,
    "tasks.retry": ActionPolicy.DEDUPLICATE,
    "task.details": ActionPolicy.REPLACE_LATEST,
    "task.page": ActionPolicy.DEDUPLICATE,
    "integrity.operation": ActionPolicy.DEDUPLICATE,
    "maintenance.storage.activate": ActionPolicy.DEDUPLICATE,
    "maintenance.storage.scan": ActionPolicy.DEDUPLICATE,
    "maintenance.storage.prepare-safe": ActionPolicy.DEDUPLICATE,
    "maintenance.storage.execute-safe": ActionPolicy.DEDUPLICATE,
    "maintenance.storage.scan-downloads": ActionPolicy.DEDUPLICATE,
    "maintenance.storage.prepare-manual": ActionPolicy.DEDUPLICATE,
    "maintenance.storage.execute-manual": ActionPolicy.DEDUPLICATE,
    "maintenance.storage.automatic": ActionPolicy.DEDUPLICATE,
    "maintenance.storage.cancel": ActionPolicy.DEDUPLICATE,
}


class AsyncActionBridge:
    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._slots: list[Callable[..., None]] = []

    @property
    def active_keys(self) -> frozenset[str]:
        return frozenset(self._tasks)

    def connect(
        self,
        signal: Any,
        key: str,
        action: ActionFactory,
        *,
        hooks: ActionHooks = _NO_HOOKS,
        policy: ActionPolicy | None = None,
    ) -> Callable[[], None]:
        def trigger() -> None:
            self.start(key, action, hooks=hooks, policy=policy)

        signal.connect(trigger)
        self._slots.append(trigger)
        return trigger

    def connect_payload(
        self,
        signal: Any,
        key: str,
        action: PayloadAction,
        *,
        hooks: ActionHooks = _NO_HOOKS,
        policy: ActionPolicy | None = None,
    ) -> Callable[[Any], None]:
        def trigger(value: Any) -> None:
            self.start(
                key,
                lambda: action(value),
                hooks=hooks,
                policy=policy,
            )

        signal.connect(trigger)
        self._slots.append(trigger)
        return trigger

    def connect_args(
        self,
        signal: Any,
        key: str,
        action: ArgsAction,
        *,
        hooks: ActionHooks = _NO_HOOKS,
        policy: ActionPolicy | None = None,
    ) -> Callable[..., None]:
        def trigger(*values: Any) -> None:
            self.start(
                key,
                lambda: action(*values),
                hooks=hooks,
                policy=policy,
            )

        signal.connect(trigger)
        self._slots.append(trigger)
        return trigger

    def start(
        self,
        key: str,
        action: ActionFactory,
        *,
        hooks: ActionHooks = _NO_HOOKS,
        policy: ActionPolicy | None = None,
    ) -> bool:
        resolved_policy = policy or ACTION_POLICIES.get(
            key,
            ActionPolicy.DEDUPLICATE,
        )
        existing = self._tasks.get(key)
        if existing is not None and not existing.done():
            if resolved_policy is ActionPolicy.DEDUPLICATE:
                return False
            existing.cancel()
        self._invoke(hooks.started, key)
        task = asyncio.create_task(self._run(key, action, hooks), name=f"ui:{key}")
        self._tasks[key] = task
        return True

    async def _run(
        self,
        key: str,
        action: ActionFactory,
        hooks: ActionHooks,
    ) -> None:
        try:
            await action()
        except asyncio.CancelledError:
            self._invoke(hooks.cancelled, key)
            raise
        except Exception as error:
            _LOGGER.warning("async UI action %s failed (%s)", key, type(error).__name__)
            self._invoke(hooks.failed, key, error)
        else:
            self._invoke(hooks.succeeded, key)
        finally:
            if self._tasks.get(key) is asyncio.current_task():
                self._invoke(hooks.finished, key)
                self._tasks.pop(key, None)

    async def wait_idle(self) -> None:
        pending = tuple(self._tasks.values())
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def shutdown(self) -> None:
        pending = tuple(task for task in self._tasks.values() if not task.done())
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    @staticmethod
    def _invoke(callback: Callable[..., None] | None, key: str, *args: object) -> None:
        if callback is None:
            return
        try:
            callback(*args)
        except Exception as error:
            _LOGGER.warning(
                "async UI action callback %s failed (%s)",
                key,
                type(error).__name__,
            )
