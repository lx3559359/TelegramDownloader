import logging

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
