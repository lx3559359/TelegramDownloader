import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from telegram_downloader.activation import LocalActivationServer, request_activation


def request_while_qt_runs(qtbot, channel: str, command: bytes) -> bool:
    script = (
        "import sys; "
        "from telegram_downloader.activation import request_activation; "
        "accepted=request_activation(sys.argv[1], command=bytes.fromhex(sys.argv[2])); "
        "raise SystemExit(0 if accepted else 1)"
    )
    environment = dict(os.environ)
    source = str(Path.cwd() / "src")
    environment["PYTHONPATH"] = os.pathsep.join(
        value for value in (source, environment.get("PYTHONPATH", "")) if value
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script, channel, command.hex()],
        env=environment,
    )
    qtbot.waitUntil(lambda: process.poll() is not None, timeout=10_000)
    return process.returncode == 0


def test_activation_server_accepts_only_fixed_command(qtbot) -> None:
    activated: list[bool] = []
    server = LocalActivationServer(
        f"TelegramDownloader.Test.{uuid4().hex}",
        lambda: activated.append(True),
    )
    assert server.start() is True

    try:
        accepted = request_while_qt_runs(qtbot, server.channel, b"activate")
        assert accepted is True, activated
        qtbot.waitUntil(lambda: activated == [True])
        assert request_while_qt_runs(qtbot, server.channel, b"private-data") is False
        assert activated == [True]
    finally:
        server.close()


def test_missing_activation_server_returns_false_without_raising() -> None:
    assert (
        request_activation(
            f"TelegramDownloader.Missing.{uuid4().hex}",
            timeout_ms=50,
        )
        is False
    )
