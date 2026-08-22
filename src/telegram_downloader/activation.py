from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QObject
from PySide6.QtNetwork import QLocalServer, QLocalSocket

ACTIVATION_CHANNEL = "TelegramDownloader.Activation.v1"
ACTIVATE_COMMAND = b"activate"


class LocalActivationServer(QObject):
    def __init__(self, channel: str, activate: Callable[[], None]) -> None:
        super().__init__()
        self.channel = channel
        self._activate = activate
        self._server = QLocalServer(self)
        self._server.newConnection.connect(self._accept_connections)
        self._connections: set[QLocalSocket] = set()
        self._handled: set[QLocalSocket] = set()

    def start(self) -> bool:
        QLocalServer.removeServer(self.channel)
        return self._server.listen(self.channel)

    def close(self) -> None:
        for socket in tuple(self._connections):
            socket.abort()
        self._connections.clear()
        self._handled.clear()
        self._server.close()
        QLocalServer.removeServer(self.channel)

    def _accept_connections(self) -> None:
        while self._server.hasPendingConnections():
            socket = self._server.nextPendingConnection()
            if socket is None:
                return
            self._connections.add(socket)
            socket.readyRead.connect(lambda selected=socket: self._read(selected))
            socket.disconnected.connect(
                lambda selected=socket: self._discard(selected)
            )
            if socket.bytesAvailable():
                self._read(socket)

    def _read(self, socket: QLocalSocket) -> None:
        if socket in self._handled or not socket.bytesAvailable():
            return
        self._handled.add(socket)
        command = bytes(socket.readAll())
        accepted = command == ACTIVATE_COMMAND
        socket.write(b"ok" if accepted else b"rejected")
        socket.flush()
        if accepted:
            self._activate()

    def _discard(self, socket: QLocalSocket) -> None:
        self._connections.discard(socket)
        self._handled.discard(socket)
        socket.deleteLater()


def request_activation(
    channel: str = ACTIVATION_CHANNEL,
    *,
    command: bytes = ACTIVATE_COMMAND,
    timeout_ms: int = 1000,
) -> bool:
    socket = QLocalSocket()
    try:
        socket.connectToServer(channel)
        if not socket.waitForConnected(timeout_ms):
            return False
        if socket.write(command) != len(command):
            return False
        socket.flush()
        if (
            not socket.bytesAvailable()
            and not socket.waitForReadyRead(timeout_ms)
            and not socket.bytesAvailable()
        ):
            return False
        if not socket.bytesAvailable():
            return False
        return bytes(socket.readAll()) == b"ok"
    finally:
        socket.abort()
