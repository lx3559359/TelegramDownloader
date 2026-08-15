import asyncio
import logging
from types import SimpleNamespace

from telegram_downloader.controller import AppController
from telegram_downloader.logging import SecretRedactionFilter, configure_logging


def test_redaction_removes_registered_secrets_and_phone_numbers() -> None:
    record = logging.LogRecord(
        "test",
        logging.INFO,
        __file__,
        1,
        "hash=%s phone=+8613800000000",
        ("abc123",),
        None,
    )

    SecretRedactionFilter({"abc123"}).filter(record)

    assert record.getMessage() == "hash=*** phone=***"


def test_configured_logger_writes_only_redacted_project_log(tmp_path) -> None:
    path = tmp_path / "logs" / "app.log"
    logger = configure_logging(path, {"api-secret"})

    logger.info("credential=%s", "api-secret")
    for handler in logger.handlers:
        handler.flush()

    content = path.read_text(encoding="utf-8")
    assert "api-secret" not in content
    assert "credential=***" in content
    assert logger.propagate is False


def test_redaction_removes_complete_qr_login_url() -> None:
    record = logging.LogRecord(
        "test",
        logging.ERROR,
        __file__,
        1,
        "qr=tg://login?token=secret_token-123",
        (),
        None,
    )

    SecretRedactionFilter(set()).filter(record)

    assert record.getMessage() == "qr=***"


def test_subscription_probe_never_logs_or_displays_rule_content(caplog) -> None:
    sensitive = (
        "probe-secret-keyword",
        "private-dialog-title",
        "message-excerpt-secret",
        "private-file-name.mp4",
    )

    class Page:
        def __init__(self) -> None:
            self.errors = []

        def set_probe_busy(self, _rule_id, _busy):
            pass

        def set_probe_progress(self, _progress):
            pass

        def set_probe_result(self, _report):
            pass

        def show_probe_cancelled(self):
            pass

        def set_selected_rule_details(self, _rule, _runs):
            pass

        def show_error(self, message):
            self.errors.append(message)

    class Window:
        def __init__(self) -> None:
            self.subscriptions_page = Page()

        def statusBar(self):
            return SimpleNamespace(showMessage=lambda *_args: None)

    class Service:
        async def probe_rule(self, _rule_id, *, on_progress=None):
            raise RuntimeError(" ".join(sensitive))

        def get_rule(self, rule_id):
            return SimpleNamespace(id=rule_id)

        def list_runs(self, _rule_id, *, limit=20):
            return []

    window = Window()
    controller = AppController.for_test(subscriptions=Service(), window=window)

    with caplog.at_level(logging.INFO):
        asyncio.run(controller.probe_subscription("rule-1"))

    assert window.subscriptions_page.errors == ["操作失败（RuntimeError）"]
    assert all(value not in caplog.text for value in sensitive)
