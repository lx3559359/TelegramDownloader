from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from telegram_downloader.domain import MediaKind, ScanFilters
from telegram_downloader.gateway import (
    AuthState,
    EmptyMediaError,
    TelethonGateway,
    proxy_dict,
)
from telegram_downloader.links import parse_telegram_link
from telegram_downloader.settings import ProxySettings


def media_message(
    message_id: int,
    date: datetime,
    *,
    grouped_id: int | None = None,
    kind: str = "video",
) -> SimpleNamespace:
    flags = {
        "photo": None,
        "video": None,
        "audio": None,
        "voice": None,
        "document": object(),
    }
    flags[kind] = object()
    mime = "audio/ogg" if kind == "voice" else "video/mp4"
    extension = "ogg" if kind == "voice" else "mp4"
    return SimpleNamespace(
        id=message_id,
        date=date,
        grouped_id=grouped_id,
        media=object(),
        file=SimpleNamespace(
            name=f"file-{message_id}.{extension}",
            mime_type=mime,
            size=12,
            id=f"m{message_id}",
        ),
        **flags,
    )


def test_proxy_dict_supports_socks5_and_http() -> None:
    socks = proxy_dict(ProxySettings("socks5", "127.0.0.1", 1080, "u"), "p")

    assert socks == {
        "proxy_type": "socks5",
        "addr": "127.0.0.1",
        "port": 1080,
        "username": "u",
        "password": "p",
        "rdns": True,
    }
    assert proxy_dict(ProxySettings(), "") is None
    assert proxy_dict(ProxySettings("http", "proxy.local", 8080), "") == {
        "proxy_type": "http",
        "addr": "proxy.local",
        "port": 8080,
        "username": None,
        "password": None,
        "rdns": True,
    }


@pytest.mark.asyncio
async def test_login_reports_password_requirement() -> None:
    class PasswordNeeded(Exception):
        pass

    class Client:
        async def sign_in(self, **kwargs):
            raise PasswordNeeded

    gateway = TelethonGateway.from_client_for_test(
        Client(), password_needed_error=PasswordNeeded
    )

    assert (
        await gateway.sign_in("+8613800000000", "12345", "hash")
        is AuthState.PASSWORD_REQUIRED
    )


def test_message_metadata_classifies_voice_and_name() -> None:
    now = datetime(2026, 8, 13, tzinfo=UTC)
    message = media_message(7, now, kind="voice")

    media = TelethonGateway.remote_media_from_message("peer", message)

    assert media.kind is MediaKind.VOICE
    assert media.original_name == "file-7.ogg"
    assert media.expected_size == 12
    assert media.media_id == "m7"


@pytest.mark.asyncio
async def test_single_message_expands_album_in_message_order() -> None:
    now = datetime(2026, 8, 13, tzinfo=UTC)
    selected = media_message(10, now, grouped_id=55)
    album = [
        media_message(11, now, grouped_id=55),
        media_message(9, now, grouped_id=55),
        media_message(8, now, grouped_id=99),
    ]

    class Client:
        async def get_entity(self, entity):
            return SimpleNamespace(title="频道")

        async def get_messages(self, entity, ids):
            return selected

        def iter_messages(self, entity, **kwargs):
            async def messages():
                for message in album:
                    yield message

            return messages()

    gateway = TelethonGateway.from_client_for_test(Client())
    source = parse_telegram_link("https://t.me/example/10")
    filters = ScanFilters(now - timedelta(days=1), now, frozenset(MediaKind), 10)

    result = [media async for media in gateway.scan(source, filters)]

    assert [media.message_id for media in result] == [9, 10, 11]
    assert all(media.source_title == "频道" for media in result)


@pytest.mark.asyncio
async def test_batch_scan_applies_date_kind_and_limit_newest_first() -> None:
    now = datetime(2026, 8, 13, tzinfo=UTC)
    messages = [
        media_message(5, now + timedelta(hours=1)),
        media_message(4, now, kind="voice"),
        media_message(3, now - timedelta(hours=1)),
        media_message(2, now - timedelta(hours=2)),
        media_message(1, now - timedelta(days=2)),
    ]

    class Client:
        async def get_entity(self, entity):
            return SimpleNamespace(title="频道")

        def iter_messages(self, entity, **kwargs):
            async def stream():
                for message in messages:
                    yield message

            return stream()

    gateway = TelethonGateway.from_client_for_test(Client())
    source = parse_telegram_link("https://t.me/example")
    filters = ScanFilters(
        now - timedelta(days=1),
        now,
        frozenset({MediaKind.VIDEO}),
        2,
    )

    result = [media async for media in gateway.scan(source, filters)]

    assert [media.message_id for media in result] == [3, 2]


@pytest.mark.asyncio
async def test_stream_media_passes_existing_offset() -> None:
    message = media_message(7, datetime(2026, 8, 13, tzinfo=UTC))

    class Client:
        async def get_entity(self, entity):
            return "entity"

        async def get_messages(self, entity, ids):
            return message

        def iter_download(self, media, *, offset):
            assert offset == 4

            async def chunks():
                yield b"ab"
                yield b"cd"

            return chunks()

    gateway = TelethonGateway.from_client_for_test(Client())

    assert [chunk async for chunk in gateway.stream_media("peer", 7, 4)] == [
        b"ab",
        b"cd",
    ]


@pytest.mark.asyncio
async def test_single_message_without_media_is_reported() -> None:
    class Client:
        async def get_entity(self, entity):
            return SimpleNamespace(title="频道")

        async def get_messages(self, entity, ids):
            return SimpleNamespace(id=ids, media=None, grouped_id=None)

    gateway = TelethonGateway.from_client_for_test(Client())
    now = datetime(2026, 8, 13, tzinfo=UTC)

    with pytest.raises(EmptyMediaError):
        _ = [
            media
            async for media in gateway.scan(
                parse_telegram_link("https://t.me/example/7"),
                ScanFilters(now, now, frozenset(MediaKind), 10),
            )
        ]


@pytest.mark.asyncio
async def test_account_name_uses_display_name_or_username() -> None:
    class Client:
        async def get_me(self):
            return SimpleNamespace(first_name="Test", last_name="User", username="tester")

    gateway = TelethonGateway.from_client_for_test(Client())

    assert await gateway.account_name() == "Test User"
