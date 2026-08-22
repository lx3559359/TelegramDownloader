from PySide6.QtCore import Qt

from telegram_downloader.account_access import (
    AccountStatusSnapshot,
    AuthorizationState,
    ConnectionState,
)
from telegram_downloader.ui.account_status import AccountStatusDialog


def snapshot(
    *,
    active: int = 0,
    authorization: AuthorizationState = AuthorizationState.AUTHORIZED,
    connection: ConnectionState = ConnectionState.ONLINE,
) -> AccountStatusSnapshot:
    return AccountStatusSnapshot(
        "42",
        "测试账号",
        authorization,
        connection,
        True,
        True,
        True,
        active,
    )


def test_dialog_shows_account_without_starting_auth(qtbot) -> None:
    dialog = AccountStatusDialog()
    qtbot.addWidget(dialog)

    dialog.set_snapshot(snapshot())

    assert "测试账号" in dialog.account_name.text()
    assert "连接正常" in dialog.connection_label.text()
    assert dialog.reauthenticate_button.isEnabled()


def test_dialog_emits_only_explicit_reauthenticate_intent(qtbot) -> None:
    dialog = AccountStatusDialog()
    qtbot.addWidget(dialog)
    dialog.set_snapshot(snapshot())

    with qtbot.waitSignal(dialog.reauthenticate_requested, timeout=500):
        qtbot.mouseClick(
            dialog.reauthenticate_button,
            Qt.MouseButton.LeftButton,
        )


def test_dialog_renders_degraded_expired_and_active_download_guards(qtbot) -> None:
    dialog = AccountStatusDialog()
    qtbot.addWidget(dialog)
    emitted: list[str] = []
    dialog.reconnect_requested.connect(lambda: emitted.append("reconnect"))
    dialog.reauthenticate_requested.connect(lambda: emitted.append("reauth"))

    dialog.set_snapshot(snapshot(connection=ConnectionState.DEGRADED))
    assert dialog.reconnect_button.isEnabled()
    assert emitted == []

    dialog.set_snapshot(
        snapshot(
            authorization=AuthorizationState.EXPIRED,
            connection=ConnectionState.OFFLINE,
        )
    )
    assert dialog.reconnect_button.isVisible() is False
    assert dialog.reauthenticate_button.isEnabled()

    dialog.set_snapshot(snapshot(active=2))
    assert dialog.reauthenticate_button.isEnabled() is False
    assert "暂停或等待" in dialog.guard_label.text()
