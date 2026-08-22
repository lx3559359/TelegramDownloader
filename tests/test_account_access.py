import asyncio

import pytest

from telegram_downloader.account_access import (
    AccountStatusSnapshot,
    AuthorizationState,
    CandidateLoginSession,
    ConnectionState,
    OnlineServices,
)


def test_account_status_snapshot_exposes_safe_actions() -> None:
    status = AccountStatusSnapshot(
        account_id="42",
        display_name="账号",
        authorization=AuthorizationState.AUTHORIZED,
        connection=ConnectionState.ONLINE,
        session_encrypted=True,
        content_available=True,
        subscriptions_available=True,
        active_download_count=0,
    )

    assert status.can_reconnect is False
    assert status.can_reauthenticate is True


def test_online_services_keeps_gateway_and_scheduler_together() -> None:
    services = OnlineServices("gateway", "planner", "scheduler")

    assert services.gateway == "gateway"
    assert services.planner == "planner"
    assert services.scheduler == "scheduler"


@pytest.mark.asyncio
async def test_candidate_session_close_cancels_wait_and_disconnects() -> None:
    class Gateway:
        def __init__(self) -> None:
            self.disconnect_calls = 0

        async def disconnect(self) -> None:
            self.disconnect_calls += 1

    gateway = Gateway()
    waiting = asyncio.create_task(asyncio.Event().wait())
    candidate = CandidateLoginSession(gateway, qr_wait_task=waiting)

    await candidate.close()

    assert waiting.cancelled()
    assert gateway.disconnect_calls == 1
    assert candidate.qr_wait_task is None
