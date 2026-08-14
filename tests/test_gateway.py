import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

import telegram_downloader.gateway as gateway_module
from telegram_downloader.content import (
    AccountProfile,
    ContentSearchQuery,
    DialogKind,
    SearchCursor,
)
from telegram_downloader.domain import MediaKind, ScanFilters
from telegram_downloader.gateway import (
    AccessDeniedError,
    AuthState,
    EmptyMediaError,
    GatewayError,
    SessionExpiredError,
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


@pytest.mark.asyncio
async def test_qr_login_begin_wait_and_refresh() -> None:
    expires = datetime(2026, 8, 14, 1, tzinfo=UTC)

    class FakeQr:
        def __init__(self):
            self.url = "tg://login?token=first"
            self.expires = expires
            self.waited = False

        async def recreate(self):
            self.url = "tg://login?token=refreshed"

        async def wait(self):
            self.waited = True

    qr = FakeQr()

    class Client:
        async def qr_login(self):
            return qr

    gateway = TelethonGateway.from_client_for_test(Client())

    info = await gateway.begin_qr_login()
    refreshed = await gateway.refresh_qr_login()
    state = await gateway.wait_qr_login()

    assert info == gateway_module.QrLoginInfo("tg://login?token=first", expires)
    assert state is AuthState.READY
    assert qr.waited is True
    assert refreshed == gateway_module.QrLoginInfo("tg://login?token=refreshed", expires)


@pytest.mark.asyncio
async def test_qr_begin_connects_disconnected_client_first() -> None:
    expires = datetime(2026, 8, 14, 1, tzinfo=UTC)

    class FakeQr:
        url = "tg://login?token=first"

        def __init__(self):
            self.expires = expires

    class Client:
        def __init__(self):
            self.connected = False
            self.connect_calls = 0

        async def connect(self):
            self.connect_calls += 1
            self.connected = True

        async def qr_login(self):
            assert self.connected is True
            return FakeQr()

    client = Client()
    gateway = TelethonGateway.from_client_for_test(client, connected=False)

    await gateway.begin_qr_login()

    assert client.connect_calls == 1


@pytest.mark.asyncio
async def test_qr_wait_reports_2fa_requirement() -> None:
    class PasswordNeeded(Exception):
        pass

    class FakeQr:
        url = "tg://login?token=first"
        expires = datetime(2026, 8, 14, 1, tzinfo=UTC)

        def __init__(self, error):
            self.error = error

        async def wait(self):
            raise self.error

    class Client:
        def __init__(self, qr):
            self.qr = qr

        async def qr_login(self):
            return self.qr

    password_gateway = TelethonGateway.from_client_for_test(
        Client(FakeQr(PasswordNeeded())),
        password_needed_error=PasswordNeeded,
    )
    await password_gateway.begin_qr_login()

    assert await password_gateway.wait_qr_login() is AuthState.PASSWORD_REQUIRED



@pytest.mark.asyncio
async def test_qr_wait_preserves_timeout_for_controller_refresh() -> None:
    class FakeQr:
        url = "tg://login?token=first"
        expires = datetime(2026, 8, 14, 1, tzinfo=UTC)

        async def wait(self):
            raise TimeoutError

    class Client:
        async def qr_login(self):
            return FakeQr()

    gateway = TelethonGateway.from_client_for_test(Client())
    await gateway.begin_qr_login()

    with pytest.raises(TimeoutError):
        await gateway.wait_qr_login()


@pytest.mark.asyncio
async def test_qr_wait_discards_used_token_and_requires_active_session() -> None:
    class FakeQr:
        url = "tg://login?token=first"
        expires = datetime(2026, 8, 14, 1, tzinfo=UTC)

        async def wait(self):
            return None

        async def recreate(self):
            return None

    class Client:
        async def qr_login(self):
            return FakeQr()

    gateway = TelethonGateway.from_client_for_test(Client())

    with pytest.raises(GatewayError, match="二维码登录会话尚未创建"):
        await gateway.wait_qr_login()

    await gateway.begin_qr_login()
    await gateway.wait_qr_login()

    with pytest.raises(GatewayError, match="二维码登录会话尚未创建"):
        await gateway.refresh_qr_login()


@pytest.mark.asyncio
async def test_qr_wait_propagates_cancellation_for_telethon_cleanup() -> None:
    started = asyncio.Event()
    cleaned = False

    class FakeQr:
        url = "tg://login?token=first"
        expires = datetime(2026, 8, 14, 1, tzinfo=UTC)

        async def wait(self):
            nonlocal cleaned
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned = True

    class Client:
        async def qr_login(self):
            return FakeQr()

    gateway = TelethonGateway.from_client_for_test(Client())
    await gateway.begin_qr_login()
    waiting = asyncio.create_task(gateway.wait_qr_login())
    await started.wait()

    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting

    assert cleaned is True
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
async def test_private_link_recovers_entity_from_dialogs_when_cache_is_empty() -> None:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    selected = media_message(7, now)

    class Client:
        async def get_entity(self, entity):
            assert entity == -100123456
            raise ValueError("entity cache is empty")

        def iter_dialogs(self):
            async def dialogs():
                yield SimpleNamespace(
                    entity=SimpleNamespace(peer_id=-100123456, title="私有频道")
                )

            return dialogs()

        async def get_messages(self, entity, ids):
            assert entity.peer_id == -100123456
            assert ids == 7
            return selected

    gateway = TelethonGateway.from_client_for_test(
        Client(), peer_id_getter=lambda entity: entity.peer_id
    )
    filters = ScanFilters(now - timedelta(days=1), now, frozenset(MediaKind), 10)

    result = [
        item
        async for item in gateway.scan(
            parse_telegram_link("https://t.me/c/123456/7"), filters
        )
    ]

    assert [item.message_id for item in result] == [7]
    assert result[0].source_title == "私有频道"


@pytest.mark.asyncio
async def test_private_link_reports_membership_error_when_dialog_is_absent() -> None:
    class Client:
        async def get_entity(self, entity):
            raise ValueError(entity)

        def iter_dialogs(self):
            async def dialogs():
                if False:
                    yield None

            return dialogs()

    gateway = TelethonGateway.from_client_for_test(
        Client(), peer_id_getter=lambda entity: entity.peer_id
    )
    now = datetime(2026, 8, 14, tzinfo=UTC)

    with pytest.raises(
        AccessDeniedError, match="当前账号未加入该私有频道或群组"
    ):
        _ = [
            item
            async for item in gateway.scan(
                parse_telegram_link("https://t.me/c/123456/7"),
                ScanFilters(now - timedelta(days=1), now, frozenset(MediaKind), 10),
            )
        ]


@pytest.mark.asyncio
async def test_public_link_does_not_enumerate_private_dialogs() -> None:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    selected = media_message(7, now)

    class Client:
        async def get_entity(self, entity):
            assert entity == "example"
            return SimpleNamespace(title="公开频道")

        def iter_dialogs(self):
            raise AssertionError("public links must not enumerate dialogs")

        async def get_messages(self, entity, ids):
            return selected

    gateway = TelethonGateway.from_client_for_test(Client())
    filters = ScanFilters(now - timedelta(days=1), now, frozenset(MediaKind), 10)

    result = [
        item
        async for item in gateway.scan(
            parse_telegram_link("https://t.me/example/7"), filters
        )
    ]

    assert result[0].source_title == "公开频道"


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
            return SimpleNamespace(
                id=42,
                first_name="Test",
                last_name="User",
                username="tester",
            )

    gateway = TelethonGateway.from_client_for_test(Client())

    assert await gateway.account_name() == "Test User"


@pytest.mark.asyncio
async def test_account_profile_uses_stable_id_and_display_name() -> None:
    class Client:
        async def get_me(self):
            return SimpleNamespace(
                id=42,
                first_name="张",
                last_name="三",
                username="zhangsan",
            )

    gateway = TelethonGateway.from_client_for_test(Client())

    assert await gateway.account_profile() == AccountProfile("42", "张 三")


@pytest.mark.asyncio
async def test_account_profile_requires_logged_in_account_id() -> None:
    class Client:
        async def get_me(self):
            return SimpleNamespace(first_name="无编号")

    gateway = TelethonGateway.from_client_for_test(Client())

    with pytest.raises(GatewayError, match="Telegram 账号尚未登录"):
        await gateway.account_profile()


@pytest.mark.asyncio
async def test_account_profile_maps_unregistered_auth_key_to_expired_login() -> None:
    class AuthKeyUnregisteredError(Exception):
        pass

    class Client:
        async def get_me(self):
            raise AuthKeyUnregisteredError("secret server detail")

    gateway = TelethonGateway.from_client_for_test(
        Client(),
        authorization_errors=(AuthKeyUnregisteredError,),
    )

    with pytest.raises(
        SessionExpiredError,
        match="Telegram 登录已失效，请重新扫码登录",
    ):
        await gateway.account_profile()


@pytest.mark.asyncio
async def test_content_dialogs_include_active_and_archived_but_not_users_or_bots() -> None:
    group = SimpleNamespace(id=101, title="普通群", username="group")
    channel = SimpleNamespace(id=102, title="资料频道", username="docs")
    user = SimpleNamespace(id=103, first_name="某人", bot=False)
    bot = SimpleNamespace(id=104, first_name="机器人", bot=True)

    class Client:
        def iter_dialogs(self, *, archived=False):
            values = (
                [
                    SimpleNamespace(
                        entity=group,
                        name="普通群",
                        is_group=True,
                        is_channel=False,
                    ),
                    SimpleNamespace(
                        entity=user,
                        name="某人",
                        is_group=False,
                        is_channel=False,
                    ),
                    SimpleNamespace(
                        entity=bot,
                        name="机器人",
                        is_group=False,
                        is_channel=False,
                    ),
                ]
                if not archived
                else [
                    SimpleNamespace(
                        entity=channel,
                        name="资料频道",
                        is_group=False,
                        is_channel=True,
                    )
                ]
            )

            async def generate():
                for value in values:
                    yield value

            return generate()

    gateway = TelethonGateway.from_client_for_test(
        Client(), peer_id_getter=lambda entity: -1_000_000_000_000 - entity.id
    )

    found = [item async for item in gateway.iter_content_dialogs("42")]

    assert [(item.title, item.kind, item.archived) for item in found] == [
        ("普通群", DialogKind.GROUP, False),
        ("资料频道", DialogKind.CHANNEL, True),
    ]
    assert all(item.account_id == "42" for item in found)
    assert [item.peer_ref for item in found] == ["-1000000000101", "-1000000000102"]


@pytest.mark.asyncio
async def test_content_dialogs_map_archived_enumeration_access_error() -> None:
    class DialogAccessError(Exception):
        pass

    class Client:
        def iter_dialogs(self, *, archived=False):
            if archived:
                raise DialogAccessError("secret server detail")

            async def generate():
                if False:
                    yield None

            return generate()

    gateway = TelethonGateway.from_client_for_test(
        Client(), access_errors=(DialogAccessError,)
    )

    with pytest.raises(AccessDeniedError) as caught:
        _ = [item async for item in gateway.iter_content_dialogs("42")]

    assert "secret server detail" not in str(caught.value)


@pytest.mark.asyncio
async def test_search_media_page_uses_server_search_and_raw_message_cursor() -> None:
    now = datetime(2026, 8, 14, 12, tzinfo=UTC)
    messages = []
    for index, message_id in enumerate(range(200, 99, -1)):
        message = media_message(
            message_id,
            now,
            kind="video" if index % 2 == 0 else "voice",
        )
        message.message = (
            "\x00安装\t教程\n" + "文" * 600 + "\x07"
            if message_id == 200
            else f"结果 {message_id}"
        )
        messages.append(message)

    class Client:
        def __init__(self):
            self.calls: list[dict[str, object]] = []
            self.last_scanned_id = 0

        async def get_entity(self, entity):
            assert entity == -1001
            return SimpleNamespace(title="资料群")

        def iter_messages(self, entity, **kwargs):
            self.calls.append(kwargs)

            async def generate():
                for message in messages[: int(kwargs["limit"])]:
                    self.last_scanned_id = message.id
                    yield message

            return generate()

    client = Client()
    gateway = TelethonGateway.from_client_for_test(client)
    query = ContentSearchQuery(
        "安装教程",
        ScanFilters(
            datetime(2026, 8, 1, tzinfo=UTC),
            datetime(2026, 8, 14, 23, 59, tzinfo=UTC),
            frozenset({MediaKind.VIDEO}),
            500,
        ),
    )

    page = await gateway.search_media_page("-1001", query, SearchCursor())

    assert client.calls[0] == {
        "search": "安装教程",
        "offset_id": 0,
        "limit": 100,
    }
    assert len(page.items) == 50
    assert all(item.remote.kind is MediaKind.VIDEO for item in page.items)
    assert all(len(item.excerpt) <= 500 for item in page.items)
    assert page.items[0].excerpt.startswith("安装 教程 ")
    assert not any(ord(char) < 32 for char in page.items[0].excerpt)
    assert page.next_cursor == SearchCursor(client.last_scanned_id)
    assert page.exhausted is False


@pytest.mark.asyncio
async def test_search_media_page_marks_short_page_exhausted_without_cursor() -> None:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    messages = [media_message(2, now), media_message(1, now)]
    for message in messages:
        message.message = "安装"

    class Client:
        async def get_entity(self, entity):
            return SimpleNamespace(title="资料群")

        def iter_messages(self, entity, **kwargs):
            async def generate():
                for message in messages:
                    yield message

            return generate()

    gateway = TelethonGateway.from_client_for_test(Client())
    query = ContentSearchQuery(
        "安装",
        ScanFilters(now - timedelta(days=1), now, frozenset(MediaKind), 500),
    )

    page = await gateway.search_media_page("-1001", query, None)

    assert [item.remote.message_id for item in page.items] == [2, 1]
    assert page.next_cursor is None
    assert page.exhausted is True


@pytest.mark.asyncio
async def test_expand_album_returns_matching_media_in_message_order() -> None:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    messages = [
        media_message(51, now, grouped_id=900),
        media_message(48, now, grouped_id=901),
        media_message(49, now, grouped_id=900),
        media_message(50, now, grouped_id=900),
    ]

    class Client:
        async def get_entity(self, entity):
            assert entity == -1001
            return SimpleNamespace(title="资料群")

        def iter_messages(self, entity, **kwargs):
            assert kwargs == {"min_id": 29, "max_id": 71}

            async def generate():
                for message in messages:
                    yield message

            return generate()

    gateway = TelethonGateway.from_client_for_test(Client())

    found = await gateway.expand_album("-1001", 50, 900)

    assert [item.remote.message_id for item in found] == [49, 50, 51]
    assert all(item.remote.grouped_id == 900 for item in found)


@pytest.mark.asyncio
async def test_load_thumbnail_validates_current_media_and_returns_only_bytes() -> None:
    now = datetime(2026, 8, 14, tzinfo=UTC)
    message = media_message(50, now)
    non_bytes_message = media_message(51, now)

    class Client:
        async def get_entity(self, entity):
            assert entity == -1001
            return SimpleNamespace(title="资料群")

        async def get_messages(self, entity, ids):
            return {50: message, 51: non_bytes_message}.get(ids)

        async def download_media(self, media, *, file, thumb):
            assert file is bytes
            assert thumb == -1
            return b"jpeg" if media is message.media else "thumbnail.jpg"

    gateway = TelethonGateway.from_client_for_test(Client())

    assert await gateway.load_thumbnail("-1001", 50, "m50") == b"jpeg"
    assert await gateway.load_thumbnail("-1001", 51, "m51") is None
    assert await gateway.load_thumbnail("-1001", 50, "changed") is None
    assert await gateway.load_thumbnail("-1001", 404, "missing") is None


@pytest.mark.asyncio
async def test_load_thumbnail_maps_access_errors() -> None:
    class ThumbnailAccessError(Exception):
        pass

    class Client:
        async def get_entity(self, entity):
            raise ThumbnailAccessError("private detail")

    gateway = TelethonGateway.from_client_for_test(
        Client(), access_errors=(ThumbnailAccessError,)
    )

    with pytest.raises(AccessDeniedError) as caught:
        await gateway.load_thumbnail("-1001", 50, "m50")

    assert "private detail" not in str(caught.value)
