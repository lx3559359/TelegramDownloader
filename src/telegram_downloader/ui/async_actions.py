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


class AsyncActionBridge:
    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._slots: list[Callable[[], None]] = []

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
        policy: ActionPolicy = ActionPolicy.DEDUPLICATE,
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
        policy: ActionPolicy = ActionPolicy.DEDUPLICATE,
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

    def start(
        self,
        key: str,
        action: ActionFactory,
        *,
        hooks: ActionHooks = _NO_HOOKS,
        policy: ActionPolicy = ActionPolicy.DEDUPLICATE,
    ) -> bool:
        existing = self._tasks.get(key)
        if existing is not None and not existing.done():
            if policy is ActionPolicy.DEDUPLICATE:
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
