from __future__ import annotations

import re
from urllib.parse import urlparse

from telegram_downloader.domain import ParsedLink, SourceKind


class InvalidTelegramLink(ValueError):
    """Raised when a value is not a supported Telegram source URL."""


_PUBLIC = re.compile(
    r"^/(?P<slug>[A-Za-z0-9_]{4,})"
    r"(?:/(?:(?P<topic>\d+)/)?(?P<message>\d+))?/?$"
)
_PRIVATE = re.compile(
    r"^/c/(?P<slug>\d+)"
    r"(?:/(?:(?P<topic>\d+)/)?(?P<message>\d+))?/?$"
)
_PREVIEW = re.compile(
    r"^/s/(?P<slug>[A-Za-z0-9_]{4,})"
    r"(?:/(?:(?P<topic>\d+)/)?(?P<message>\d+))?/?$"
)
_INVITE = re.compile(r"^/\+(?P<slug>[A-Za-z0-9_-]+)/?$")


def is_telegram_link_candidate(value: str) -> bool:
    parsed = urlparse(value.strip())
    return (
        parsed.scheme.lower() in {"http", "https"}
        and (parsed.hostname or "").lower() in {"t.me", "www.t.me"}
    )


def parse_telegram_link(value: str) -> ParsedLink:
    parsed = urlparse(value.strip())
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or (parsed.hostname or "").lower() not in {"t.me", "www.t.me"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or parsed.fragment
        or parsed.query not in {"", "single"}
    ):
        raise InvalidTelegramLink("请输入有效的 t.me 链接")

    match = _PRIVATE.fullmatch(parsed.path)
    link_form = "private"
    if match is None:
        match = _PREVIEW.fullmatch(parsed.path)
        link_form = "preview"
    if match is None:
        match = _PUBLIC.fullmatch(parsed.path)
        link_form = "public"
    if match is None:
        match = _INVITE.fullmatch(parsed.path)
        link_form = "invite"
    if match is None:
        raise InvalidTelegramLink("不支持此 Telegram 链接格式")

    parts = match.groupdict()
    message_text = parts.get("message")
    topic_text = parts.get("topic")
    if message_text == "0" or topic_text == "0":
        raise InvalidTelegramLink("消息编号必须大于零")

    message_id = int(message_text) if message_text else None
    kind = (
        SourceKind.SINGLE_MESSAGE
        if message_id is not None
        else SourceKind.CHANNEL_OR_GROUP
    )
    slug = parts["slug"]
    if link_form == "private":
        entity_ref = f"-100{slug}"
        normalized_parts = ["c", slug]
    elif link_form == "invite":
        entity_ref = f"+{slug}"
        normalized_parts = [f"+{slug}"]
    else:
        entity_ref = slug
        normalized_parts = [slug]
    if topic_text:
        normalized_parts.append(topic_text)
    if message_text:
        normalized_parts.append(message_text)

    normalized = "https://t.me/" + "/".join(normalized_parts)
    return ParsedLink(normalized, entity_ref, kind, message_id)
