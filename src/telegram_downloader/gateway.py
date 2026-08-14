from __future__ import annotations

import asyncio
import mimetypes
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from telegram_downloader import __version__
from telegram_downloader.content import (
    AccountProfile,
    ContentDialog,
    ContentSearchQuery,
    DialogKind,
    SearchCursor,
)
from telegram_downloader.domain import MediaKind, ParsedLink, ScanFilters, SourceKind
from telegram_downloader.files import classify_media, sanitize_component
from telegram_downloader.settings import ProxySettings


class AuthState(StrEnum):
    READY = "ready"
    CODE_SENT = "code_sent"
    PASSWORD_REQUIRED = "password_required"


@dataclass(frozen=True, slots=True)
class RemoteMedia:
    peer_ref: str
    source_title: str
    message_id: int
    grouped_id: int | None
    media_id: str
    kind: MediaKind
    original_name: str
    expected_size: int | None
    message_date_utc: datetime


@dataclass(frozen=True, slots=True)
class RemoteSearchHit:
    remote: RemoteMedia
    excerpt: str
    thumbnail_key: str


@dataclass(frozen=True, slots=True)
class RemoteSearchPage:
    items: tuple[RemoteSearchHit, ...]
    next_cursor: SearchCursor | None
    exhausted: bool


@dataclass(frozen=True, slots=True)
class QrLoginInfo:
    url: str
    expires_at: datetime


class GatewayError(RuntimeError):
    pass


class AccessDeniedError(GatewayError):
    pass


class EmptyMediaError(GatewayError):
    pass


class TransientNetworkError(GatewayError):
    pass


class MediaReferenceExpired(GatewayError):
    pass


class FloodWaitError(GatewayError):
    def __init__(self, seconds: int) -> None:
        super().__init__(f"Telegram 要求等待 {seconds} 秒")
        self.seconds = seconds


class _NoTelethonError(Exception):
    pass


class TelegramGateway(Protocol):
    async def connect(self) -> None: ...

    async def request_code(self, phone: str) -> str: ...

    async def sign_in(
        self,
        phone: str,
        code: str,
        phone_code_hash: str,
    ) -> AuthState: ...

    async def check_password(self, password: str) -> AuthState: ...

    async def begin_qr_login(self) -> QrLoginInfo: ...

    async def wait_qr_login(self) -> AuthState: ...

    async def refresh_qr_login(self) -> QrLoginInfo: ...

    def export_session(self) -> str: ...

    def scan(
        self,
        source: ParsedLink,
        filters: ScanFilters,
    ) -> AsyncIterator[RemoteMedia]: ...

    def stream_media(
        self,
        peer_ref: str,
        message_id: int,
        offset: int,
    ) -> AsyncIterator[bytes]: ...

    async def test_connection(self) -> None: ...

    async def account_name(self) -> str | None: ...

    async def account_profile(self) -> AccountProfile: ...

    def iter_content_dialogs(
        self,
        account_id: str,
    ) -> AsyncIterator[ContentDialog]: ...

    async def search_media_page(
        self,
        peer_ref: str,
        query: ContentSearchQuery,
        cursor: SearchCursor | None,
    ) -> RemoteSearchPage: ...

    async def expand_album(
        self,
        peer_ref: str,
        message_id: int,
        grouped_id: int,
    ) -> tuple[RemoteSearchHit, ...]: ...

    async def load_thumbnail(
        self,
        peer_ref: str,
        message_id: int,
        media_id: str,
    ) -> bytes | None: ...

    async def disconnect(self) -> None: ...


def proxy_dict(settings: ProxySettings, password: str) -> dict[str, object] | None:
    if settings.kind == "none":
        return None
    return {
        "proxy_type": settings.kind,
        "addr": settings.host,
        "port": settings.port,
        "username": settings.username or None,
        "password": password or None,
        "rdns": True,
    }


class TelethonGateway:
    _ALBUM_RADIUS = 20
    _SEARCH_PAGE_SIZE = 100

    def __init__(
        self,
        api_id: int,
        api_hash: str,
        session: str = "",
        proxy: ProxySettings | None = None,
        proxy_password: str = "",
    ) -> None:
        from telethon import TelegramClient, errors, functions, utils
        from telethon.sessions import StringSession

        self._client = TelegramClient(
            StringSession(session),
            api_id,
            api_hash,
            proxy=proxy_dict(proxy or ProxySettings(), proxy_password),
            flood_sleep_threshold=0,
            device_model="TelegramDownloader Windows",
            app_version=__version__,
            system_lang_code="zh-hans",
            lang_code="zh-hans",
        )
        self._password_needed_error: type[BaseException] = errors.SessionPasswordNeededError
        self._flood_wait_error: type[BaseException] = errors.FloodWaitError
        self._reference_expired_errors: tuple[type[BaseException], ...] = (
            errors.FileReferenceExpiredError,
        )
        self._access_errors: tuple[type[BaseException], ...] = (
            errors.ChannelPrivateError,
            errors.ChatAdminRequiredError,
            errors.InviteHashExpiredError,
            errors.InviteHashInvalidError,
            errors.MessageIdInvalidError,
            errors.UsernameInvalidError,
            errors.UsernameNotOccupiedError,
        )
        self._transient_errors: tuple[type[BaseException], ...] = (
            ConnectionError,
            OSError,
            TimeoutError,
        )
        self._check_invite_request = functions.messages.CheckChatInviteRequest
        self._peer_id_getter = utils.get_peer_id
        self._qr_login: object | None = None
        self._connected = False

    @classmethod
    def from_client_for_test(
        cls,
        client: object,
        *,
        password_needed_error: type[BaseException] = _NoTelethonError,
        flood_wait_error: type[BaseException] = _NoTelethonError,
        reference_expired_errors: tuple[type[BaseException], ...] = (),
        access_errors: tuple[type[BaseException], ...] = (),
        transient_errors: tuple[type[BaseException], ...] = (
            ConnectionError,
            OSError,
            TimeoutError,
        ),
        peer_id_getter=None,
        connected: bool = True,
    ) -> TelethonGateway:
        gateway = cls.__new__(cls)
        gateway._client = client
        gateway._password_needed_error = password_needed_error
        gateway._flood_wait_error = flood_wait_error
        gateway._reference_expired_errors = reference_expired_errors
        gateway._access_errors = access_errors
        gateway._transient_errors = transient_errors
        gateway._check_invite_request = None
        gateway._peer_id_getter = peer_id_getter or (lambda entity: entity)
        gateway._qr_login = None
        gateway._connected = connected
        return gateway

    async def connect(self) -> None:
        try:
            await self._client.connect()
        except Exception as exc:
            self._raise_mapped(exc)
        self._connected = True

    async def request_code(self, phone: str) -> str:
        try:
            sent = await self._client.send_code_request(phone)
        except Exception as exc:
            self._raise_mapped(exc)
        code_hash = getattr(sent, "phone_code_hash", "")
        if not code_hash:
            raise GatewayError("Telegram 未返回验证码会话")
        return str(code_hash)

    async def sign_in(
        self,
        phone: str,
        code: str,
        phone_code_hash: str,
    ) -> AuthState:
        try:
            await self._client.sign_in(
                phone=phone,
                code=code,
                phone_code_hash=phone_code_hash,
            )
        except self._password_needed_error:
            return AuthState.PASSWORD_REQUIRED
        except Exception as exc:
            self._raise_mapped(exc)
        return AuthState.READY

    async def check_password(self, password: str) -> AuthState:
        try:
            await self._client.sign_in(password=password)
        except Exception as exc:
            self._raise_mapped(exc)
        return AuthState.READY

    async def begin_qr_login(self) -> QrLoginInfo:
        if not self._connected:
            await self.connect()
        try:
            self._qr_login = await self._client.qr_login()
        except Exception as exc:
            self._raise_mapped(exc)
        return self._qr_info()

    async def wait_qr_login(self) -> AuthState:
        qr_login = self._require_qr_login()
        try:
            await qr_login.wait()
        except self._password_needed_error:
            self._qr_login = None
            return AuthState.PASSWORD_REQUIRED
        except (TimeoutError, asyncio.CancelledError):
            raise
        except Exception as exc:
            self._raise_mapped(exc)
        self._qr_login = None
        return AuthState.READY

    async def refresh_qr_login(self) -> QrLoginInfo:
        qr_login = self._require_qr_login()
        try:
            await qr_login.recreate()
        except Exception as exc:
            self._raise_mapped(exc)
        return self._qr_info()

    def _require_qr_login(self):
        if self._qr_login is None:
            raise GatewayError("二维码登录会话尚未创建")
        return self._qr_login

    def _qr_info(self) -> QrLoginInfo:
        qr_login = self._require_qr_login()
        url = getattr(qr_login, "url", "")
        expires_at = self._utc_datetime(getattr(qr_login, "expires", None))
        if not isinstance(url, str) or not url.startswith("tg://login?token="):
            raise GatewayError("Telegram 未返回有效二维码")
        if expires_at is None:
            raise GatewayError("Telegram 未返回二维码过期时间")
        return QrLoginInfo(url, expires_at)

    def export_session(self) -> str:
        session = getattr(self._client, "session", None)
        if session is None or not hasattr(session, "save"):
            raise GatewayError("当前 Telegram 会话无法导出")
        value = session.save()
        if not isinstance(value, str) or not value:
            raise GatewayError("Telegram 会话导出失败")
        return value

    async def scan(
        self,
        source: ParsedLink,
        filters: ScanFilters,
    ) -> AsyncIterator[RemoteMedia]:
        if filters.item_limit < 1 or filters.date_from_utc > filters.date_to_utc:
            raise ValueError("扫描条件无效")
        entity = await self._resolve_entity(source.entity_ref)
        title = self._entity_title(entity, source.entity_ref)

        if source.kind is SourceKind.SINGLE_MESSAGE:
            async for item in self._scan_single(entity, source, title):
                yield item
            return

        matched = 0
        try:
            messages = self._client.iter_messages(entity)
            async for message in messages:
                message_date = self._utc_datetime(getattr(message, "date", None))
                if message_date is None:
                    continue
                if message_date > filters.date_to_utc:
                    continue
                if message_date < filters.date_from_utc:
                    break
                remote = self.remote_media_from_message(source.entity_ref, message, title)
                if remote is None or remote.kind not in filters.media_kinds:
                    continue
                yield remote
                matched += 1
                if matched >= filters.item_limit:
                    break
        except Exception as exc:
            self._raise_mapped(exc)

    async def _scan_single(
        self,
        entity: object,
        source: ParsedLink,
        title: str,
    ) -> AsyncIterator[RemoteMedia]:
        try:
            selected = await self._client.get_messages(entity, ids=source.message_id)
            if selected is None or getattr(selected, "media", None) is None:
                raise EmptyMediaError("该消息不含可下载媒体")
            grouped_id = getattr(selected, "grouped_id", None)
            if grouped_id is None:
                remote = self.remote_media_from_message(source.entity_ref, selected, title)
                if remote is None:
                    raise EmptyMediaError("该消息不含可下载媒体")
                yield remote
                return

            selected_id = int(selected.id)
            grouped = {selected_id: selected}
            messages = self._client.iter_messages(
                entity,
                min_id=max(0, selected_id - self._ALBUM_RADIUS - 1),
                max_id=selected_id + self._ALBUM_RADIUS + 1,
            )
            async for candidate in messages:
                if getattr(candidate, "grouped_id", None) == grouped_id:
                    grouped[int(candidate.id)] = candidate
            for message_id in sorted(grouped):
                remote = self.remote_media_from_message(
                    source.entity_ref,
                    grouped[message_id],
                    title,
                )
                if remote is not None:
                    yield remote
        except EmptyMediaError:
            raise
        except Exception as exc:
            self._raise_mapped(exc)

    async def stream_media(
        self,
        peer_ref: str,
        message_id: int,
        offset: int,
    ) -> AsyncIterator[bytes]:
        if offset < 0:
            raise ValueError("下载偏移不能为负数")
        try:
            entity = await self._resolve_entity(peer_ref)
            message = await self._client.get_messages(entity, ids=message_id)
            media = getattr(message, "media", None) if message is not None else None
            if media is None:
                raise EmptyMediaError("来源消息已不存在或不含媒体")
            async for chunk in self._client.iter_download(media, offset=offset):
                if chunk:
                    yield bytes(chunk)
        except EmptyMediaError:
            raise
        except Exception as exc:
            self._raise_mapped(exc)

    async def test_connection(self) -> None:
        try:
            await self._client.connect()
            if await self._client.get_me() is None:
                raise GatewayError("Telegram 账号尚未登录")
        except GatewayError:
            raise
        except Exception as exc:
            self._raise_mapped(exc)

    async def account_profile(self) -> AccountProfile:
        try:
            account = await self._client.get_me()
        except Exception as exc:
            self._raise_mapped(exc)
        account_id = getattr(account, "id", None) if account is not None else None
        if not isinstance(account_id, int):
            raise GatewayError("Telegram 账号尚未登录")
        return AccountProfile(str(account_id), self._account_display_name(account))

    async def account_name(self) -> str | None:
        profile = await self.account_profile()
        return profile.display_name

    async def iter_content_dialogs(
        self,
        account_id: str,
    ) -> AsyncIterator[ContentDialog]:
        seen: set[str] = set()
        try:
            for archived in (False, True):
                async for dialog in self._client.iter_dialogs(archived=archived):
                    is_group = bool(getattr(dialog, "is_group", False))
                    is_channel = bool(getattr(dialog, "is_channel", False))
                    if not (is_group or is_channel):
                        continue
                    entity = getattr(dialog, "entity", None)
                    if entity is None:
                        continue
                    peer_ref = str(self._peer_id_getter(entity))
                    if peer_ref in seen:
                        continue
                    seen.add(peer_ref)
                    title = str(
                        getattr(dialog, "name", "")
                        or getattr(entity, "title", "")
                        or peer_ref
                    )
                    yield ContentDialog(
                        account_id=account_id,
                        peer_ref=peer_ref,
                        title=title,
                        username=str(getattr(entity, "username", "") or ""),
                        kind=(DialogKind.GROUP if is_group else DialogKind.CHANNEL),
                        archived=archived,
                        available=True,
                        last_synced_at=datetime.now(UTC),
                    )
        except Exception as exc:
            self._raise_mapped(exc)

    async def search_media_page(
        self,
        peer_ref: str,
        query: ContentSearchQuery,
        cursor: SearchCursor | None,
    ) -> RemoteSearchPage:
        items: list[RemoteSearchHit] = []
        inspected = 0
        last_inspected_id: int | None = None
        reached_start_date = False
        try:
            entity = await self._resolve_entity(peer_ref)
            title = self._entity_title(entity, peer_ref)
            messages = self._client.iter_messages(
                entity,
                search=query.keyword,
                offset_id=cursor.offset_id if cursor else 0,
                limit=self._SEARCH_PAGE_SIZE,
            )
            async for message in messages:
                inspected += 1
                last_inspected_id = int(message.id)
                message_date = self._utc_datetime(getattr(message, "date", None))
                if message_date is None:
                    continue
                if message_date > query.filters.date_to_utc:
                    continue
                if message_date < query.filters.date_from_utc:
                    reached_start_date = True
                    break
                remote = self.remote_media_from_message(peer_ref, message, title)
                if remote is None or remote.kind not in query.filters.media_kinds:
                    continue
                items.append(self._search_hit(remote, message))
        except Exception as exc:
            self._raise_mapped(exc)

        exhausted = reached_start_date or inspected < self._SEARCH_PAGE_SIZE
        next_cursor = (
            None
            if exhausted or last_inspected_id is None
            else SearchCursor(last_inspected_id)
        )
        return RemoteSearchPage(tuple(items), next_cursor, exhausted)

    async def expand_album(
        self,
        peer_ref: str,
        message_id: int,
        grouped_id: int,
    ) -> tuple[RemoteSearchHit, ...]:
        grouped: dict[int, RemoteSearchHit] = {}
        try:
            entity = await self._resolve_entity(peer_ref)
            title = self._entity_title(entity, peer_ref)
            messages = self._client.iter_messages(
                entity,
                min_id=max(0, message_id - self._ALBUM_RADIUS - 1),
                max_id=message_id + self._ALBUM_RADIUS + 1,
            )
            async for message in messages:
                if getattr(message, "grouped_id", None) != grouped_id:
                    continue
                remote = self.remote_media_from_message(peer_ref, message, title)
                if remote is not None:
                    grouped[remote.message_id] = self._search_hit(remote, message)
        except Exception as exc:
            self._raise_mapped(exc)
        return tuple(grouped[item_id] for item_id in sorted(grouped))

    async def load_thumbnail(
        self,
        peer_ref: str,
        message_id: int,
        media_id: str,
    ) -> bytes | None:
        try:
            entity = await self._resolve_entity(peer_ref)
            message = await self._client.get_messages(entity, ids=message_id)
            media = getattr(message, "media", None) if message is not None else None
            if media is None:
                return None
            title = self._entity_title(entity, peer_ref)
            remote = self.remote_media_from_message(peer_ref, message, title)
            if remote is None or remote.media_id != media_id:
                return None
            downloaded = await self._client.download_media(
                media,
                file=bytes,
                thumb=-1,
            )
            return downloaded if isinstance(downloaded, bytes) else None
        except Exception as exc:
            self._raise_mapped(exc)

    @classmethod
    def _search_hit(cls, remote: RemoteMedia, message: object) -> RemoteSearchHit:
        return RemoteSearchHit(
            remote=remote,
            excerpt=cls._message_excerpt(message),
            thumbnail_key=(
                f"{remote.peer_ref}:{remote.message_id}:{remote.media_id}"
            ),
        )

    @staticmethod
    def _message_excerpt(message: object) -> str:
        raw = str(getattr(message, "message", "") or "")
        visible = "".join(
            char for char in raw if char.isprintable() or char in "\n\t"
        )
        return " ".join(visible.split())[:500]

    @staticmethod
    def _account_display_name(account: object) -> str:
        first = getattr(account, "first_name", "") or ""
        last = getattr(account, "last_name", "") or ""
        display = " ".join(part for part in (first, last) if part).strip()
        username = getattr(account, "username", "") or ""
        return display or (f"@{username}" if username else "已登录")

    async def disconnect(self) -> None:
        try:
            await self._client.disconnect()
        except Exception as exc:
            self._raise_mapped(exc)
        finally:
            self._connected = False

    async def _resolve_entity(self, entity_ref: str) -> object:
        try:
            if entity_ref.startswith("+"):
                if self._check_invite_request is None:
                    return await self._client.get_entity(entity_ref)
                invitation = await self._client(self._check_invite_request(entity_ref[1:]))
                chat = getattr(invitation, "chat", None)
                if chat is None:
                    raise AccessDeniedError("请先使用 Telegram 加入该邀请链接")
                return chat
            reference: str | int = entity_ref
            if entity_ref.startswith("-100") and entity_ref[1:].isdigit():
                reference = int(entity_ref)
                return await self._resolve_private_entity(reference)
            return await self._client.get_entity(reference)
        except AccessDeniedError:
            raise
        except Exception as exc:
            self._raise_mapped(exc)

    async def _resolve_private_entity(self, reference: int) -> object:
        try:
            return await self._client.get_entity(reference)
        except ValueError:
            async for dialog in self._client.iter_dialogs():
                entity = getattr(dialog, "entity", None)
                if entity is not None and self._peer_id_getter(entity) == reference:
                    return entity
        raise AccessDeniedError("当前账号未加入该私有频道或群组")

    def _raise_mapped(self, error: Exception) -> None:
        if isinstance(error, self._flood_wait_error):
            raise FloodWaitError(max(1, int(getattr(error, "seconds", 1)))) from error
        if isinstance(error, self._reference_expired_errors):
            raise MediaReferenceExpired("媒体引用已过期，需要刷新来源消息") from error
        if isinstance(error, (ValueError, *self._access_errors)):
            raise AccessDeniedError("链接无效、已失效或当前账号无权访问") from error
        if isinstance(error, self._transient_errors):
            raise TransientNetworkError("Telegram 网络连接失败") from error
        raise error

    @staticmethod
    def _entity_title(entity: object, fallback: str) -> str:
        title = getattr(entity, "title", None)
        if not title:
            first = getattr(entity, "first_name", "") or ""
            last = getattr(entity, "last_name", "") or ""
            title = " ".join(part for part in (first, last) if part)
        return sanitize_component(str(title or fallback))

    @staticmethod
    def _utc_datetime(value: object) -> datetime | None:
        if not isinstance(value, datetime):
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    @staticmethod
    def remote_media_from_message(
        peer_ref: str,
        message: object,
        source_title: str | None = None,
    ) -> RemoteMedia | None:
        if getattr(message, "media", None) is None:
            return None
        message_id = int(message.id)
        date = TelethonGateway._utc_datetime(getattr(message, "date", None))
        if date is None:
            return None

        file = getattr(message, "file", None)
        mime = getattr(file, "mime_type", None)
        kind = classify_media(
            mime,
            str(getattr(file, "name", "") or ""),
            bool(getattr(message, "photo", None)),
            bool(getattr(message, "voice", None)),
            bool(getattr(message, "video", None)),
        )
        name = getattr(file, "name", None)
        if not name:
            suffix = getattr(file, "ext", None) or mimetypes.guess_extension(mime or "") or ""
            name = f"{kind.value}_{message_id}{suffix}"
        name = sanitize_component(str(name))

        media_object = getattr(message, "document", None) or getattr(message, "photo", None)
        media_id = getattr(file, "id", None) or getattr(media_object, "id", None)
        if media_id is None:
            media_id = f"{message_id}:{kind.value}"
        size = getattr(file, "size", None)
        expected_size = size if isinstance(size, int) and size >= 0 else None
        return RemoteMedia(
            peer_ref=peer_ref,
            source_title=source_title or peer_ref,
            message_id=message_id,
            grouped_id=getattr(message, "grouped_id", None),
            media_id=str(media_id),
            kind=kind,
            original_name=name,
            expected_size=expected_size,
            message_date_utc=date,
        )
