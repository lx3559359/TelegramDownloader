from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

_PHONE_NUMBER = re.compile(r"(?<![\w+])\+\d{7,15}\b")
_QR_LOGIN_URL = re.compile(r"tg://login\?token=[A-Za-z0-9_-]+")


class SecretRedactionFilter(logging.Filter):
    def __init__(self, secrets: set[str]) -> None:
        super().__init__()
        self.secrets = tuple(sorted((value for value in secrets if value), key=len, reverse=True))

    def redact(self, message: str) -> str:
        for secret in self.secrets:
            message = message.replace(secret, "***")
        message = _QR_LOGIN_URL.sub("***", message)
        return _PHONE_NUMBER.sub("***", message)

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self.redact(record.getMessage())
        record.args = ()
        return True


class _RedactingFormatter(logging.Formatter):
    def __init__(self, redactor: SecretRedactionFilter) -> None:
        super().__init__("%(asctime)s %(levelname)s %(name)s %(message)s")
        self._redactor = redactor

    def format(self, record: logging.LogRecord) -> str:
        return self._redactor.redact(super().format(record))


def configure_logging(path: Path, secrets: set[str]) -> logging.Logger:
    path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("telegram_downloader")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for existing in logger.handlers:
        existing.close()
    logger.handlers.clear()

    redactor = SecretRedactionFilter(secrets)
    handler = RotatingFileHandler(
        path,
        maxBytes=2_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(_RedactingFormatter(redactor))
    handler.addFilter(redactor)
    logger.addHandler(handler)
    return logger
