import pytest

from telegram_downloader.domain import SourceKind
from telegram_downloader.links import InvalidTelegramLink, parse_telegram_link


@pytest.mark.parametrize(
    ("url", "kind", "message_id", "entity_ref"),
    [
        ("https://t.me/example/42", SourceKind.SINGLE_MESSAGE, 42, "example"),
        ("https://t.me/c/123456/99", SourceKind.SINGLE_MESSAGE, 99, "-100123456"),
        ("https://t.me/example", SourceKind.CHANNEL_OR_GROUP, None, "example"),
        ("https://t.me/+AbCdEf123", SourceKind.CHANNEL_OR_GROUP, None, "+AbCdEf123"),
        ("https://t.me/s/example/42", SourceKind.SINGLE_MESSAGE, 42, "example"),
        ("https://t.me/example/7/42", SourceKind.SINGLE_MESSAGE, 42, "example"),
        ("https://t.me/c/123456/7/99", SourceKind.SINGLE_MESSAGE, 99, "-100123456"),
    ],
)
def test_parse_supported_links(url, kind, message_id, entity_ref) -> None:
    parsed = parse_telegram_link(url)

    assert parsed.kind is kind
    assert parsed.message_id == message_id
    assert parsed.entity_ref == entity_ref


def test_normalizes_host_trailing_slash_and_ignores_single_view_hint() -> None:
    parsed = parse_telegram_link(" HTTPS://WWW.T.ME/example/42/?single ")

    assert parsed.normalized_url == "https://t.me/example/42"


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/channel/1",
        "ftp://t.me/example/1",
        "https://t.me/ab/1",
        "https://t.me/example/not-a-message",
        "https://t.me/example/0",
        "https://user:password@t.me/example/1",
        "https://t.me/example/1#fragment",
    ],
)
def test_rejects_unsupported_or_ambiguous_links(url) -> None:
    with pytest.raises(InvalidTelegramLink):
        parse_telegram_link(url)
