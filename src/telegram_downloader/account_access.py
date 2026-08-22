from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from telegram_downloader.content import AccountProfile


class AuthorizationState(StrEnum):
    MISSING = "missing"
    AUTHORIZED = "authorized"
    EXPIRED = "expired"
    UNKNOWN = "unknown"


class ConnectionState(StrEnum):
    OFFLINE = "offline"
    ONLINE = "online"
    DEGRADED = "degraded"


@dataclass(frozen=True, slots=True)
class AccountStatusSnapshot:
    account_id: str | None
    display_name: str
    authorization: AuthorizationState
    connection: ConnectionState
    session_encrypted: bool
    content_available: bool
    subscriptions_available: bool
    active_download_count: int

    @property
    def can_reconnect(self) -> bool:
        return (
            self.authorization is AuthorizationState.AUTHORIZED
            and self.connection is not ConnectionState.ONLINE
        )

    @property
    def can_reauthenticate(self) -> bool:
        return self.authorization is not AuthorizationState.MISSING


@dataclass(frozen=True, slots=True)
class OnlineServices:
    gateway: Any
    planner: Any
    scheduler: Any


@dataclass(slots=True)
class CandidateLoginSession:
    gateway: Any
    phone: str = ""
    phone_code_hash: str = ""
    profile: AccountProfile | None = None
    qr_wait_task: asyncio.Task[None] | None = None

    async def close(self) -> None:
        task = self.qr_wait_task
        self.qr_wait_task = None
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        await self.gateway.disconnect()
